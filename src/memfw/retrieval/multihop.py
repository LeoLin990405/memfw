"""MCP multihop search. HTTP stays on single-hop FTS.

One expansion round by default, configurable up to two. An id, a path, or a
quoted phrase is a direct lookup. A query that says 现在, 最新, or 上次 stays
on the current-time filter and does not expand. Expansion uses the same rare
unigrams as the R0.5 experiment. No judge and no request endpoint.
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import datetime
from typing import Any

from memfw.core.repository import Repository
from memfw.stats.bm25 import BM25Scorer
from memfw.stats.keyness import tokenize

PASSAGE_K = 5
RRF_K = 60
MAX_DOCUMENT_FREQUENCY = 3
TERM_LIMIT = 3
MAX_ROUNDS = 2
_TEMPORAL_CUES = ("现在", "最新", "上次")
_QUOTED = re.compile(r'"([^"]+)"')


def expansion_rounds(value: int | None = None) -> int:
    """Server-side hop budget. The request may narrow it. The cap is 2."""

    if value is None:
        raw = os.environ.get("MEMFW_MULTIHOP_ROUNDS", "1")
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError("MEMFW_MULTIHOP_ROUNDS must be 1 or 2") from exc
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > MAX_ROUNDS:
        raise ValueError("rounds must be 1 or 2")
    return value


def classify_query(query: str) -> str:
    """Return ``direct``, ``temporal``, or ``expand``."""

    text = query.strip()
    if _QUOTED.search(text) or _looks_like_id_or_path(text):
        return "direct"
    if any(cue in text for cue in _TEMPORAL_CUES):
        return "temporal"
    return "expand"


def expansion_terms(
    passages: list[str],
    query: str,
    frequencies: Counter[str],
    *,
    limit: int = TERM_LIMIT,
    max_document_frequency: int = MAX_DOCUMENT_FREQUENCY,
) -> list[str]:
    """Rare unigrams. An earlier passage outranks a later one, then a rarer term."""

    query_terms = set(tokenize(query))
    best: dict[str, tuple[int, int, int, str]] = {}
    for rank, text in enumerate(passages):
        for term in set(tokenize(text)):
            if term in query_terms or len(term) < 4:
                continue
            frequency = frequencies.get(term, 0)
            if frequency <= 0 or frequency > max_document_frequency:
                continue
            candidate = (rank, frequency, -len(term), term)
            current = best.get(term)
            if current is None or candidate < current:
                best[term] = candidate
    ranked = sorted(best.values())
    return [term for _rank, _frequency, _length, term in ranked[:limit]]


def search_multihop(
    repository: Repository,
    query: str,
    *,
    limit: int = 10,
    tenant: str = "",
    source: str = "",
    allowed_sources: tuple[str, ...] = (),
    include_history: bool = False,
    as_of: datetime | None = None,
    rounds: int | None = None,
) -> dict[str, Any]:
    """Run one routed search. ``degraded`` is true when expansion adds no hit.

    Ranking reads the disposable index once. Tenant equality is in that read,
    before candidates, ranking, and the limit cut. An empty tenant matches
    only an empty tenant.
    """

    budget = expansion_rounds(rounds)
    bounded = max(1, min(int(limit), 50))
    rows = _index_rows(repository, tenant=tenant)
    by_id = {row["id"]: row for row in rows}
    route = classify_query(query)
    if route == "direct" and not _explicit_locator(query):
        identifier = _identifier(query.strip())
        if identifier not in by_id:
            route = "lexical"
    visible = [
        row
        for row in rows
        if _row_visible(row, include_history=include_history, as_of=as_of)
        and _source_ok(row, source, allowed_sources)
    ]
    corpus = {
        row["id"]: " ".join((row["title"], row["search_body"], row["summary"], row["tags"]))
        for row in visible
    }
    scorer = BM25Scorer(corpus) if corpus else None
    frequencies = _document_frequencies(rows, scorer)
    tenants: dict[str, str] = {}
    first = _first_hop(
        route,
        query,
        scorer,
        visible,
        by_id,
        repository=repository,
        tenants=tenants,
        tenant=tenant,
        limit=bounded,
    )
    hops = [first]
    new_ids: list[str] = []
    if route == "expand" and scorer is not None:
        current = list(first)
        for _round in range(budget):
            passages = [
                f"{by_id[item]['title']}\n{by_id[item]['body']}"
                for item in current[:PASSAGE_K]
                if item in by_id
            ]
            terms = expansion_terms(passages, query, frequencies)
            if not terms:
                break
            follow = _rank(
                scorer,
                " ".join(terms),
                visible,
                by_id,
                repository=repository,
                tenants=tenants,
                tenant=tenant,
                limit=bounded,
            )
            added = [item for item in follow if item not in set(current)]
            if not added:
                break
            new_ids.extend(added)
            hops.append(follow)
            current = _rrf(hops, limit=bounded)
    degraded = route == "expand" and not new_ids
    ordered = first if degraded else _rrf(hops, limit=bounded)
    first_ids = set(first)
    results = []
    for identifier in ordered:
        row = by_id.get(identifier)
        if row is None:
            continue
        item = {
            "id": row["id"],
            "title": row["title"],
            "score": 0.0,
            "source": row["source"],
            "why": "lexical" if identifier in first_ids else "expansion",
            "index_version": row["version"],
            "sot_version": row["version"],
            "layer": row["layer"],
            "tier": row["tier"],
            "updated_at": row["updated_at"],
            "valid_from": row["valid_from"] or None,
            "valid_to": row["valid_to"] or None,
            "superseded_by": row["superseded_by"] or None,
            "provenance": row["provenance"],
        }
        if row.get("tenant_known"):
            item["memory_type"] = row["memory_type"]
        results.append(item)
    return {
        "mode": "multihop",
        "degraded": degraded,
        "errors": [],
        "model_versions": {},
        "route": route,
        "rounds": budget if route == "expand" else 0,
        "results": results,
    }


def _index_rows(repository: Repository, *, tenant: str) -> list[dict[str, Any]]:
    """One read of the disposable index. Missing index means there is nothing to rank.

    Rows are limited to ``tenant`` in SQL. Without the access table the read
    fails closed instead of treating an empty tenant as every tenant.
    """

    path = repository.index_path
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        has_access = (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'memory_access'"
            ).fetchone()
            is not None
        )
        if not has_access:
            return []
        fetched = connection.execute(
            "SELECT r.id, r.title, r.body, r.search_body, r.summary, r.tags, "
            "r.source, r.provenance, r.version, r.layer, r.tier, r.updated_at, "
            "r.created_at AS created_at, "
            "COALESCE(t.valid_from, '') AS valid_from, "
            "COALESCE(t.valid_to, '') AS valid_to, "
            "COALESCE(t.superseded_by, '') AS superseded_by, "
            "a.tenant AS tenant, a.memory_type AS memory_type "
            "FROM memory_rows r LEFT JOIN memory_temporal t ON t.id = r.id "
            "JOIN memory_access a ON a.id = r.id AND a.tenant = ? "
            "ORDER BY r.id",
            (tenant,),
        ).fetchall()
    rows = []
    for item in fetched:
        tenant = item["tenant"]
        row = {
            "id": str(item["id"]),
            "title": str(item["title"]),
            "body": str(item["body"]),
            "search_body": str(item["search_body"]),
            "summary": str(item["summary"]),
            "tags": str(item["tags"]),
            "source": str(item["source"]),
            "provenance": str(item["provenance"]),
            "version": int(item["version"]),
            "layer": str(item["layer"]),
            "tier": str(item["tier"]),
            "updated_at": str(item["updated_at"]),
            "valid_from": str(item["valid_from"]),
            "valid_to": str(item["valid_to"]),
            "superseded_by": str(item["superseded_by"]),
            "created_at": str(item["created_at"]),
            "memory_type": item["memory_type"],
            "tenant_known": True,
            "tenant": str(tenant),
        }
        rows.append(row)
    return rows


def _document_frequencies(rows: list[dict[str, Any]], scorer: BM25Scorer | None) -> Counter[str]:
    """Document frequency of title and body. Reuse the scorer tokens when they match."""

    frequencies: Counter[str] = Counter()
    scored = scorer._documents if scorer is not None else {}
    for row in rows:
        counts = scored.get(row["id"])
        same_text = (
            counts is not None
            and row["search_body"] == row["body"]
            and row["summary"] == ""
            and row["tags"] == ""
        )
        if same_text:
            frequencies.update(counts.keys())
            continue
        frequencies.update(set(tokenize(f"{row['title']}\n{row['body']}")))
    return frequencies


def _explicit_locator(text: str) -> bool:
    """A path or a quoted phrase is an id lookup. A bare word is not."""

    stripped = text.strip()
    if _QUOTED.search(stripped):
        return True
    return stripped.startswith("memories/") or stripped.endswith(".md") or "/" in stripped


def _looks_like_id_or_path(text: str) -> bool:
    if text.startswith("memories/") or text.endswith(".md"):
        return True
    if "/" in text or " " in text:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", text))


def _row_visible(row: dict[str, Any], *, include_history: bool, as_of: datetime | None) -> bool:
    if include_history:
        return True
    if as_of is not None:
        start = _parse_instant(row["valid_from"]) or _parse_instant(row.get("created_at") or "")
        end = _parse_instant(row["valid_to"])
        if start is None or start > as_of:
            return False
        return end is None or end > as_of
    return row["valid_to"] == ""


def _parse_instant(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _source_ok(row: dict[str, Any], source: str, allowed_sources: tuple[str, ...]) -> bool:
    if source and row["source"] != source:
        return False
    return not allowed_sources or row["source"] in allowed_sources


def _tenant_of(repository: Repository, memory_id: str, cache: dict[str, str]) -> str:
    """Read the tenant line. The index does not store tenant."""

    cached = cache.get(memory_id)
    if cached is not None:
        return cached
    path = repository.root / "memories" / f"{memory_id}.md"
    try:
        head = path.read_text(encoding="utf-8")[:4000]
    except OSError:
        cache[memory_id] = ""
        return ""
    match = re.search(r"(?m)^tenant:\s*'?\"?([^'\"\n]*)", head)
    value = match.group(1).strip() if match else ""
    cache[memory_id] = value
    return value


def _row_tenant(row: dict[str, Any], repository: Repository, cache: dict[str, str]) -> str:
    if row.get("tenant_known"):
        value = str(row.get("tenant") or "")
        cache[row["id"]] = value
        return value
    return _tenant_of(repository, row["id"], cache)


def _rank(
    scorer: BM25Scorer,
    query: str,
    visible: list[dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
    *,
    repository: Repository,
    tenants: dict[str, str],
    tenant: str,
    limit: int,
) -> list[str]:
    allowed = {row["id"] for row in visible}
    chosen: list[str] = []
    for identifier in scorer.rank(query):
        if identifier not in allowed:
            continue
        if _row_tenant(by_id[identifier], repository, tenants) != tenant:
            continue
        chosen.append(identifier)
        if len(chosen) >= limit:
            break
    return chosen


def _first_hop(
    route: str,
    query: str,
    scorer: BM25Scorer | None,
    visible: list[dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
    *,
    repository: Repository,
    tenants: dict[str, str],
    tenant: str,
    limit: int,
) -> list[str]:
    text = query.strip()
    if route == "direct":
        quoted = _QUOTED.findall(text)
        if quoted and scorer is not None:
            return _rank(
                scorer,
                " ".join(quoted),
                visible,
                by_id,
                repository=repository,
                tenants=tenants,
                tenant=tenant,
                limit=limit,
            )
        identifier = _identifier(text)
        row = by_id.get(identifier)
        if row is None or row["id"] not in {item["id"] for item in visible}:
            return []
        if _row_tenant(row, repository, tenants) != tenant:
            return []
        return [row["id"]]
    if scorer is None:
        return []
    return _rank(
        scorer,
        text,
        visible,
        by_id,
        repository=repository,
        tenants=tenants,
        tenant=tenant,
        limit=limit,
    )


def _identifier(text: str) -> str:
    name = text[len("memories/") :] if text.startswith("memories/") else text
    if name.endswith(".md"):
        name = name[: -len(".md")]
    return name


def _rrf(hops: list[list[str]], *, limit: int, rrf_k: int = RRF_K) -> list[str]:
    scores: dict[str, float] = {}
    for identifiers in hops:
        for rank, identifier in enumerate(identifiers, start=1):
            scores[identifier] = scores.get(identifier, 0.0) + 1.0 / (rrf_k + rank)
    ordered = sorted(scores, key=lambda identifier: (-scores[identifier], identifier))
    return ordered[:limit]

"""MCP multihop search. HTTP stays on single-hop FTS.

One expansion round by default, configurable up to two. An id, a path, or a
quoted phrase is a direct lookup. A query that says 现在, 最新, or 上次 stays
on the current-time filter and does not expand. Expansion uses the same rare
unigrams as the R0.5 experiment. No judge and no request endpoint.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from datetime import datetime
from typing import Any

from memfw.core.models import MemoryDocument
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
    """Run one routed search. ``degraded`` is true when expansion adds no hit."""

    budget = expansion_rounds(rounds)
    bounded = max(1, min(int(limit), 50))
    documents = list(repository.list())
    visible = [
        document
        for document in documents
        if _visible(document, include_history=include_history, as_of=as_of)
        and _source_ok(document, source, allowed_sources)
    ]
    by_id = {document.id: document for document in documents}
    frequencies: Counter[str] = Counter()
    for document in documents:
        frequencies.update(set(tokenize(f"{document.title}\n{document.body}")))
    corpus = {
        document.id: " ".join(
            (document.title, document.body, document.summary, " ".join(document.tags))
        )
        for document in visible
    }
    scorer = BM25Scorer(corpus) if corpus else None
    route = classify_query(query)
    first = _first_hop(
        route,
        query,
        scorer,
        visible,
        by_id,
        tenant=tenant,
        limit=bounded,
    )
    hops = [first]
    new_ids: list[str] = []
    if route == "expand" and scorer is not None:
        current = list(first)
        for _round in range(budget):
            passages = [_passage(by_id[item]) for item in current[:PASSAGE_K] if item in by_id]
            terms = expansion_terms(passages, query, frequencies)
            if not terms:
                break
            follow = _rank(scorer, " ".join(terms), visible, tenant=tenant, limit=bounded)
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
        document = by_id.get(identifier)
        if document is None:
            continue
        results.append(
            {
                "id": document.id,
                "title": document.title,
                "score": 0.0,
                "source": document.source,
                "why": "lexical" if identifier in first_ids else "expansion",
                "index_version": document.version,
                "sot_version": document.version,
            }
        )
    return {
        "mode": "multihop",
        "degraded": degraded,
        "errors": [],
        "model_versions": {},
        "route": route,
        "rounds": budget if route == "expand" else 0,
        "results": results,
    }


def _looks_like_id_or_path(text: str) -> bool:
    if text.startswith("memories/") or text.endswith(".md"):
        return True
    if "/" in text or " " in text:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", text))


def _passage(document: MemoryDocument) -> str:
    return f"{document.title}\n{document.body}"


def _visible(document: MemoryDocument, *, include_history: bool, as_of: datetime | None) -> bool:
    if include_history:
        return True
    if as_of is not None:
        start = document.valid_from or document.created_at
        end = document.valid_to
        return start <= as_of and (end is None or end > as_of)
    return document.valid_to is None


def _source_ok(document: MemoryDocument, source: str, allowed_sources: tuple[str, ...]) -> bool:
    if source and document.source != source:
        return False
    return not allowed_sources or document.source in allowed_sources


def _rank(
    scorer: BM25Scorer,
    query: str,
    visible: list[MemoryDocument],
    *,
    tenant: str,
    limit: int,
) -> list[str]:
    allowed = {document.id for document in visible if not tenant or document.tenant == tenant}
    chosen: list[str] = []
    for identifier in scorer.rank(query):
        if identifier not in allowed:
            continue
        chosen.append(identifier)
        if len(chosen) >= limit:
            break
    return chosen


def _first_hop(
    route: str,
    query: str,
    scorer: BM25Scorer | None,
    visible: list[MemoryDocument],
    by_id: dict[str, MemoryDocument],
    *,
    tenant: str,
    limit: int,
) -> list[str]:
    text = query.strip()
    if route == "direct":
        quoted = _QUOTED.findall(text)
        if quoted and scorer is not None:
            return _rank(scorer, " ".join(quoted), visible, tenant=tenant, limit=limit)
        identifier = _identifier(text)
        document = by_id.get(identifier)
        if document is None:
            return []
        if tenant and document.tenant != tenant:
            return []
        if document.id not in {item.id for item in visible}:
            return []
        return [document.id]
    if scorer is None:
        return []
    return _rank(scorer, text, visible, tenant=tenant, limit=limit)


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

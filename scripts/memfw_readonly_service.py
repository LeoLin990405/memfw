#!/usr/bin/env python3
"""Read-only HTTP facade over a MemFW vault, implementing contract ``memfw-http/2``.

The service exposes ``GET /health`` and authenticated ``POST /search``,
``POST /related`` and ``POST /stage``. It reads Markdown and the disposable
SQLite index/related projections only; the one write path, ``/stage``, writes a
candidate file under ``<vault>/_candidates/remember/`` and never the vault.

Contract summary:

* every response body carries ``"contract": "memfw-http/2"``;
* ``/search`` and ``/related`` accept the read token or the write token,
  ``/stage`` accepts the write token only; with no write token configured,
  ``/stage`` always answers 403;
* ``score`` is normalised to ``[0, 1]`` and grows with relevance. With a bm25
  raw score, ``raw_score`` keeps the SQLite FTS5 value (smaller is better) and
  ``score = x / (1 + x)`` with ``x = max(0, -raw_score)``; with no bm25 (the
  two-character substring branch) ``raw_score`` is ``null`` and
  ``score = 1 / (1 + rank)`` for the returned rank starting at 0. Both formulas
  are monotonic;
* ``degraded`` is true only when a request did not take its preferred path:
  ``/search`` always runs FTS (the only implemented mode) and reports false,
  ``/related`` reports false on the edges projection and true when it falls back
  to lexical search;
* temporal filtering happens in SQL. tenant has no column in the index, so it is
  applied by reading frontmatter in batches (``LIMIT``/``OFFSET``) until
  ``limit`` matches are collected or the matches are exhausted - the deviation
  from "tenant filtered in SQL" that ``docs/v1-integration.md`` records.

Configuration: ``MEMFW_READONLY_ROOT`` (or ``--root``), ``MEMFW_RELATED_DB``
(or ``--related-db``), ``MEMFW_READONLY_TOKEN`` / ``MEMFW_READONLY_TOKEN_FILE``
and ``MEMFW_WRITE_TOKEN`` / ``MEMFW_WRITE_TOKEN_FILE`` (environment first, then
file; file content is stripped).
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
import traceback
import uuid
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

CONTRACT = "memfw-http/2"
TOKEN_HEADER = "X-Memfw-Token"
MAX_QUERY_CHARS = 512
MAX_LIMIT = 50
MAX_SUMMARY_CHARS = 500
MAX_TITLE_CHARS = 200
MAX_CONTENT_BYTES = 64 * 1024
SEARCH_BODY_BYTES = 8 * 1024
STAGE_BODY_BYTES = 128 * 1024
PROPOSAL_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")
CONTROL_CHARACTERS = (
    frozenset(chr(code) for code in range(0x20))
    | frozenset(chr(code) for code in range(0x7F, 0xA0))
    | {"\u2028", "\u2029"}
)
CANDIDATE_DIRECTORY = ("_candidates", "remember")
INTERNAL_MESSAGE = "internal error"
ERROR_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    413: "too_large",
    500: "internal",
}
READ_TOKEN_ENV = "MEMFW_READONLY_TOKEN"
READ_TOKEN_FILE_ENV = "MEMFW_READONLY_TOKEN_FILE"
WRITE_TOKEN_ENV = "MEMFW_WRITE_TOKEN"
WRITE_TOKEN_FILE_ENV = "MEMFW_WRITE_TOKEN_FILE"
ROOT_ENV = "MEMFW_READONLY_ROOT"
RELATED_DB_ENV = "MEMFW_RELATED_DB"


class ServiceError(Exception):
    """An error with an HTTP status and a stable contract error code."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = ERROR_CODES.get(status, ERROR_CODES[500])
        self.message = message


def _bad_request(message: str) -> ServiceError:
    return ServiceError(400, message)


def _internal() -> ServiceError:
    return ServiceError(500, INTERNAL_MESSAGE)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _env_secret(name: str, file_name: str) -> str:
    """Read a secret from the environment, then from a file (stripped)."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    path = os.environ.get(file_name, "").strip()
    if not path:
        return ""
    try:
        return Path(path).expanduser().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def read_token() -> str:
    return _env_secret(READ_TOKEN_ENV, READ_TOKEN_FILE_ENV)


def write_token() -> str:
    return _env_secret(WRITE_TOKEN_ENV, WRITE_TOKEN_FILE_ENV)


def _root() -> Path:
    value = os.environ.get(ROOT_ENV, "").strip()
    if not value:
        raise _internal()
    return Path(value).expanduser()


def _related_db() -> Path | None:
    value = os.environ.get(RELATED_DB_ENV, "").strip()
    if not value:
        return None
    return Path(value).expanduser()


def _root_or_none() -> Path | None:
    value = os.environ.get(ROOT_ENV, "").strip()
    return Path(value).expanduser() if value else None


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------


def _connect_index(root: Path) -> sqlite3.Connection:
    path = root / "index.sqlite3"
    uri = f"file:{quote(str(path), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _connect_related(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _has_related_edges(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        connection = _connect_related(path)
    except sqlite3.Error:
        return False
    try:
        return _has_table(connection, "related_edges") and _has_table(connection, "related_items")
    except sqlite3.Error:
        return False
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Vault document helpers
# ---------------------------------------------------------------------------


def _metadata_from_text(raw: str) -> dict[str, Any]:
    if not raw.startswith("---\n"):
        return {}
    try:
        _, header, _ = raw.split("---\n", 2)
        parsed = yaml.safe_load(header)
    except (ValueError, yaml.YAMLError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _metadata(path: Path) -> dict[str, Any]:
    try:
        return _metadata_from_text(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return {}


def _iso_string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _optional(value: Any) -> Any:
    text = _iso_string(value)
    return text or None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _collapsed(text: str, limit: int) -> str:
    return " ".join(text.split())[:limit]


def _document_payload(root: Path, identifier: str) -> dict[str, Any] | None:
    """Read one memory document's authoritative Markdown for the result payload."""
    if not identifier or "/" in identifier or ".." in identifier or "\\" in identifier:
        return None
    path = root / "memories" / f"{identifier}.md"
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    text = raw.decode("utf-8", errors="replace")
    metadata = _metadata_from_text(text)
    return {
        "tenant": str(metadata.get("tenant") or ""),
        "related": _string_list(metadata.get("related")),
        "valid_from": _iso_string(metadata.get("valid_from")),
        "valid_to": _iso_string(metadata.get("valid_to")),
        "superseded_by": _iso_string(metadata.get("superseded_by")),
        "content_hash": "sha256:" + hashlib.sha256(raw).hexdigest(),
    }


# ---------------------------------------------------------------------------
# /search
# ---------------------------------------------------------------------------


def _fts_query(query: str) -> str:
    return '"' + query.replace('"', '""') + '"'


def _normalise_as_of(value: Any) -> str:
    if not isinstance(value, str):
        raise _bad_request("as_of must be an ISO-8601 timestamp")
    raw = value.strip()
    if not raw:
        raise _bad_request("as_of must be an ISO-8601 timestamp")
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise _bad_request("as_of must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _temporal_clause(
    connection: sqlite3.Connection, include_history: bool, as_of: str | None
) -> tuple[str, list[Any]]:
    if include_history or not _has_table(connection, "memory_temporal"):
        return "", []
    if as_of is not None:
        return (
            "EXISTS (SELECT 1 FROM memory_temporal t WHERE t.id = r.id "
            "AND (t.valid_from = '' OR t.valid_from <= ?) "
            "AND (t.valid_to = '' OR t.valid_to > ?))",
            [as_of, as_of],
        )
    return (
        "NOT EXISTS (SELECT 1 FROM memory_temporal t WHERE t.id = r.id "
        "AND (t.valid_to != '' OR t.superseded_by != ''))",
        [],
    )


def _search_batch(
    connection: sqlite3.Connection,
    *,
    needle: str,
    source: str,
    temporal_sql: str,
    temporal_params: list[Any],
    limit: int,
    offset: int,
) -> list[sqlite3.Row]:
    if len(needle) == 2:
        select = (
            "SELECT r.id AS id, r.title AS title, r.summary AS summary, "
            "r.search_body AS search_body, r.source AS source, r.tier AS tier, "
            "r.layer AS layer, NULL AS raw_score FROM memory_rows r"
        )
        where = [
            "(instr(r.title, ?) > 0 OR instr(r.search_body, ?) > 0 "
            "OR instr(r.summary, ?) > 0 OR instr(r.tags, ?) > 0)"
        ]
        params: list[Any] = [needle, needle, needle, needle]
        order = " ORDER BY r.id"
    else:
        select = (
            "SELECT r.id AS id, r.title AS title, r.summary AS summary, "
            "r.search_body AS search_body, r.source AS source, r.tier AS tier, "
            "r.layer AS layer, bm25(memory_fts) AS raw_score "
            "FROM memory_fts JOIN memory_rows r ON r.id = memory_fts.id"
        )
        where = ["memory_fts MATCH ?"]
        params = [_fts_query(needle)]
        order = " ORDER BY raw_score, r.id"
    if source:
        where.append("r.source = ?")
        params.append(source)
    if temporal_sql:
        where.append(temporal_sql)
        params.extend(temporal_params)
    sql = f"{select} WHERE " + " AND ".join(where) + order + " LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    return list(connection.execute(sql, params).fetchall())


def _score_from_raw(raw_score: Any, rank: int) -> tuple[float, float | None, bool]:
    """Return (score, raw_score, used_bm25) for one result."""
    if raw_score is None:
        return 1.0 / (1.0 + rank), None, False
    raw = float(raw_score)
    x = max(0.0, -raw)
    return x / (1.0 + x), raw, True


def search_vault(
    root: Path | str,
    query: str,
    *,
    limit: int = 10,
    source: str = "",
    tenant: str = "",
    include_history: bool = False,
    as_of: str | None = None,
) -> dict[str, Any]:
    base = Path(root).expanduser().resolve()
    if not (base / "vault.yaml").is_file() or not (base / "index.sqlite3").is_file():
        raise _internal()
    if not isinstance(query, str):
        raise _bad_request("query must be 1..512 characters")
    needle = query.strip()
    if not needle or len(needle) > MAX_QUERY_CHARS:
        raise _bad_request("query must be 1..512 characters")
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise _bad_request("limit must be an integer")
    limit = max(1, min(limit, MAX_LIMIT))
    if not isinstance(source, str):
        raise _bad_request("source must be a string")
    if not isinstance(tenant, str):
        raise _bad_request("tenant must be a string")
    as_of_normalised = _normalise_as_of(as_of) if as_of is not None else None
    if not isinstance(include_history, bool):
        raise _bad_request("include_history must be a boolean")

    connection = _connect_index(base)
    try:
        temporal_sql, temporal_params = _temporal_clause(
            connection, include_history, as_of_normalised
        )
        batch = limit if not tenant else max(limit * 4, 100)
        offset = 0
        matched: list[sqlite3.Row] = []
        payloads: list[dict[str, Any]] = []
        while True:
            rows = _search_batch(
                connection,
                needle=needle,
                source=source,
                temporal_sql=temporal_sql,
                temporal_params=temporal_params,
                limit=batch,
                offset=offset,
            )
            if not rows:
                break
            offset += len(rows)
            for row in rows:
                payload = _document_payload(base, str(row["id"]))
                if payload is None:
                    continue
                if tenant and payload["tenant"] != tenant:
                    continue
                matched.append(row)
                payloads.append(payload)
                if len(matched) >= limit:
                    break
            if len(matched) >= limit or len(rows) < batch:
                break
            if not tenant:
                break
    finally:
        connection.close()

    results = []
    for rank, (row, payload) in enumerate(zip(matched, payloads, strict=True)):
        score, raw_score, _used_bm25 = _score_from_raw(row["raw_score"], rank)
        summary = _collapsed(
            str(row["summary"] or "") or str(row["search_body"] or ""), MAX_SUMMARY_CHARS
        )
        results.append(
            {
                "id": str(row["id"]),
                "title": str(row["title"]),
                "summary": summary,
                "source": str(row["source"]),
                "tier": str(row["tier"]),
                "layer": str(row["layer"]),
                "tenant": payload["tenant"],
                "related": payload["related"],
                "valid_from": _optional(payload["valid_from"]),
                "valid_to": _optional(payload["valid_to"]),
                "superseded_by": _optional(payload["superseded_by"]),
                "score": score,
                "raw_score": raw_score,
                "content_hash": payload["content_hash"],
            }
        )
    return {
        "contract": CONTRACT,
        "mode": "fts",
        "degraded": False,
        "errors": [],
        "results": results,
    }


# ---------------------------------------------------------------------------
# /related
# ---------------------------------------------------------------------------


def _memory_metadata(root: Path, memory_id: str, tenant: str) -> tuple[Path, dict[str, Any]]:
    if not isinstance(memory_id, str) or not memory_id.strip():
        raise _bad_request("memory_id is invalid")
    identifier = memory_id.strip()
    if "/" in identifier or ".." in identifier or "\\" in identifier:
        raise _bad_request("memory_id is invalid")
    path = root / "memories" / f"{identifier}.md"
    if not path.is_file():
        raise ServiceError(404, "memory not found")
    metadata = _metadata(path)
    if tenant and str(metadata.get("tenant") or "") != tenant:
        raise ServiceError(404, "memory not found")
    return path, metadata


# Contract section 7 fixes this vocabulary; the projection's edge methods map
# onto it and an unknown method is reported as the lexical bucket.
ALLOWED_REASONS = frozenset(
    {
        "link",
        "related_field",
        "shared_tag",
        "shared_course",
        "shared_entity",
        "lexical",
        "semantic",
        "section",
    }
)
_REASON_NAMES = {
    "explicit": "link",
    "related": "related_field",
    "tag": "shared_tag",
    "course": "shared_course",
    "entity": "shared_entity",
    "lexical": "lexical",
    "semantic": "semantic",
    "section": "section",
}


def _reason_name(method: str) -> str:
    return _REASON_NAMES.get(method, "lexical")


def _related_from_edges(
    related_db: Path, memory_id: str, *, limit: int, tenant: str
) -> list[dict[str, Any]]:
    connection = _connect_related(related_db)
    try:
        filters = ""
        params: list[Any] = []
        if tenant:
            filters = " AND i.tenant=?"
            params.append(tenant)
        outgoing = connection.execute(
            "SELECT e.*, i.title AS title, i.path AS path FROM related_edges e "
            "LEFT JOIN related_items i ON i.source=e.dst_source AND i.id=e.dst_id "
            "WHERE e.src_source='memory' AND e.src_id=?" + filters,
            [memory_id, *params],
        ).fetchall()
        incoming = connection.execute(
            "SELECT e.*, i.title AS title, i.path AS path FROM related_edges e "
            "LEFT JOIN related_items i ON i.source=e.src_source AND i.id=e.src_id "
            "WHERE e.dst_source='memory' AND e.dst_id=?" + filters,
            [memory_id, *params],
        ).fetchall()
    finally:
        connection.close()
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for row, outgoing_edge in [(row, True) for row in outgoing] + [
        (row, False) for row in incoming
    ]:
        other_source = row["dst_source"] if outgoing_edge else row["src_source"]
        other_id = row["dst_id"] if outgoing_edge else row["src_id"]
        if str(other_source) == "memory" and str(other_id) == memory_id:
            continue
        key = (str(other_source), str(other_id))
        item = merged.get(key)
        if item is None:
            item = {
                "id": str(other_id),
                "title": str(row["title"] or ""),
                "score": 0.0,
                "reasons": set(),
            }
            merged[key] = item
        item["score"] = max(float(item["score"]), float(row["score"]))
        item["reasons"].add(_reason_name(str(row["method"])))
    ordered = sorted(merged.values(), key=lambda item: (-item["score"], item["id"]))[:limit]
    return [
        {
            "id": item["id"],
            "title": item["title"],
            "score": float(item["score"]),
            "reasons": sorted(item["reasons"]),
        }
        for item in ordered
    ]


def _related_from_search(
    root: Path, memory_id: str, metadata: dict[str, Any], *, limit: int, tenant: str
) -> list[dict[str, Any]]:
    title = str(metadata.get("title") or "")
    path = root / "memories" / f"{memory_id}.md"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    if text.startswith("---\n"):
        try:
            _, _, body = text.split("---\n", 2)
        except ValueError:
            body = ""
    else:
        body = text
    candidates = [title, " ".join(body.split()[:8])]
    for candidate in candidates:
        query = candidate.strip()[:MAX_QUERY_CHARS]
        if not query:
            continue
        payload = search_vault(root, query, limit=MAX_LIMIT, tenant=tenant)
        results = []
        for item in payload["results"]:
            if item["id"] == memory_id:
                continue
            results.append(
                {
                    "id": item["id"],
                    "title": item["title"],
                    "score": float(item["score"]),
                    "reasons": ["lexical"],
                }
            )
            if len(results) >= limit:
                break
        if results:
            return results
    return []


def related_vault(
    root: Path | str,
    memory_id: str,
    *,
    limit: int = 10,
    tenant: str = "",
    related_db: str | Path | None = None,
) -> dict[str, Any]:
    base = Path(root).expanduser().resolve()
    if not (base / "vault.yaml").is_file() or not (base / "index.sqlite3").is_file():
        raise _internal()
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise _bad_request("limit must be an integer")
    limit = max(1, min(limit, MAX_LIMIT))
    if not isinstance(tenant, str):
        raise _bad_request("tenant must be a string")
    candidate = Path(related_db).expanduser() if related_db is not None else _related_db()
    identifier = memory_id.strip() if isinstance(memory_id, str) else ""
    _path, metadata = _memory_metadata(base, memory_id, tenant)
    if candidate is not None and candidate.is_file() and _has_related_edges(candidate):
        try:
            results = _related_from_edges(candidate, identifier, limit=limit, tenant=tenant)
        except sqlite3.Error:
            results = _related_from_search(base, identifier, metadata, limit=limit, tenant=tenant)
            return {
                "contract": CONTRACT,
                "mode": "fts",
                "degraded": True,
                "memory_id": identifier,
                "results": results,
            }
        return {
            "contract": CONTRACT,
            "mode": "edges",
            "degraded": False,
            "memory_id": identifier,
            "results": results,
        }
    results = _related_from_search(base, identifier, metadata, limit=limit, tenant=tenant)
    return {
        "contract": CONTRACT,
        "mode": "fts",
        "degraded": True,
        "memory_id": identifier,
        "results": results,
    }


# ---------------------------------------------------------------------------
# /stage
# ---------------------------------------------------------------------------


def _header_text(
    value: Any,
    field: str,
    *,
    required: bool = False,
    max_length: int | None = None,
    single_line: bool = False,
) -> str:
    """Validate a scalar rendered into the candidate's YAML frontmatter.

    Multi-line values are allowed because the frontmatter escapes every string
    onto one double-quoted line, so a value cannot carry the ``---`` delimiter
    into the header. ``source`` is the exception: it names where the candidate
    came from, so control characters and line breaks are refused.
    """
    if not isinstance(value, str):
        raise _bad_request(f"{field} must be a string")
    text = value.strip() if required else value
    if required and not text:
        raise _bad_request(f"{field} is required")
    if single_line and any(character in CONTROL_CHARACTERS for character in text):
        raise _bad_request(f"{field} must not contain control characters or line breaks")
    if max_length is not None and len(text) > max_length:
        raise _bad_request(f"{field} must be at most {max_length} characters")
    return text


def _validate_stage_payload(payload: dict[str, Any]) -> dict[str, Any]:
    proposal_id = payload.get("proposal_id")
    if not isinstance(proposal_id, str) or PROPOSAL_ID_PATTERN.fullmatch(proposal_id) is None:
        raise _bad_request("proposal_id must match [A-Za-z0-9_-]{1,128}")
    title = _header_text(payload.get("title"), "title", required=True, max_length=MAX_TITLE_CHARS)
    content = payload.get("content")
    if not isinstance(content, str) or not content:
        raise _bad_request("content is required")
    if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise ServiceError(413, "content too large")
    tags = payload.get("tags", [])
    if not isinstance(tags, list) or any(not isinstance(item, str) for item in tags):
        raise _bad_request("tags must be an array of strings")
    related = payload.get("related", [])
    if not isinstance(related, list) or any(not isinstance(item, str) for item in related):
        raise _bad_request("related must be an array of strings")
    tenant = _header_text(payload.get("tenant", ""), "tenant")
    source = _header_text(payload.get("source", "conductor"), "source", single_line=True)
    return {
        "proposal_id": proposal_id,
        "title": title,
        "content": content,
        "tags": [_header_text(item, "tags entries") for item in tags],
        "related": [_header_text(item, "related entries") for item in related],
        "tenant": tenant,
        "source": source or "conductor",
    }


class _SingleLine(str):
    """A string rendered as one double-quoted YAML scalar."""


class _CandidateDumper(yaml.SafeDumper):
    """Dumper that renders ``_SingleLine`` values on one escaped line."""


def _represent_single_line(dumper: yaml.SafeDumper, data: _SingleLine) -> yaml.Node:
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style='"')


_CandidateDumper.add_representer(_SingleLine, _represent_single_line)


def _single_line(value: Any) -> Any:
    """Recursively escape every string so the frontmatter stays one line per value."""
    if isinstance(value, dict):
        return {key: _single_line(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_single_line(item) for item in value]
    if isinstance(value, str):
        return _SingleLine(value)
    return value


def _candidate_markdown(metadata: dict[str, Any], content: str) -> str:
    title = str(metadata["title"])
    header: dict[str, Any] = {
        "name": title,
        "title": title,
        "type": "candidate",
        "lifecycle": "active",
        "proposal_id": metadata["proposal_id"],
        "source": metadata["source"],
        "tenant": metadata["tenant"],
        "tags": metadata["tags"],
        "related": metadata["related"],
        "created_at": metadata["created_at"],
        "content_hash": metadata["content_hash"],
        "authority": False,
        "metadata": copy.deepcopy(
            {
                "proposal_id": metadata["proposal_id"],
                "source": metadata["source"],
                "tenant": metadata["tenant"],
                "tags": metadata["tags"],
                "related": metadata["related"],
                "title": title,
                "created_at": metadata["created_at"],
                "content_hash": metadata["content_hash"],
                "authority": False,
            }
        ),
    }
    rendered = yaml.dump(
        _single_line(header),
        Dumper=_CandidateDumper,
        allow_unicode=True,
        sort_keys=False,
        width=10**9,
    ).rstrip()
    if "---\n" in rendered + "\n":
        raise _internal()
    return f"---\n{rendered}\n---\n\n{content}\n"


def _relative_candidate_path(proposal_id: str) -> str:
    return "/".join((*CANDIDATE_DIRECTORY, f"conductor-{proposal_id}.md"))


def _assert_safe_candidate_directory(base: Path, directory: Path) -> None:
    """Refuse candidate directories that leave the vault through a symlink.

    The service is only allowed to write below ``<root>/_candidates/remember``;
    a symlinked component would let a request create a file in ``memories/`` or
    outside the vault.
    """
    current = base
    for part in CANDIDATE_DIRECTORY:
        current = current / part
        if current.is_symlink():
            raise _internal()
    try:
        resolved = directory.resolve(strict=False)
        root = base.resolve()
    except OSError as exc:
        raise _internal() from exc
    if resolved != root and root not in resolved.parents:
        raise _internal()


def _published_candidate_hash(base: Path, relative: str) -> str:
    path = base / relative
    if path.is_symlink() or not path.is_file():
        raise _internal()
    metadata = _metadata(path)
    return str(metadata.get("content_hash") or "")


class _DirectoryHandle:
    """A directory fd for <root>/_candidates/remember that followed no symlink."""

    def __init__(self, descriptor: int) -> None:
        self.descriptor = descriptor

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1

    def __enter__(self) -> _DirectoryHandle:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _open_candidate_directory(base: Path) -> _DirectoryHandle:
    """Create and open ``<root>/_candidates/remember`` one component at a time.

    Each component is opened relative to its parent with ``O_NOFOLLOW``, so a
    symlink - including one swapped in after an earlier check - cannot redirect
    the write outside the vault.
    """
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(base, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as exc:
        raise _internal() from exc
    current = root_fd
    try:
        for part in CANDIDATE_DIRECTORY:
            try:
                os.mkdir(part, 0o777, dir_fd=current)
            except FileExistsError:
                pass
            except OSError as exc:
                raise _internal() from exc
            try:
                next_fd = os.open(part, flags, dir_fd=current)
            except OSError as exc:
                raise _internal() from exc
            if current != root_fd:
                os.close(current)
            current = next_fd
        return _DirectoryHandle(current)
    except BaseException:
        if current != root_fd:
            os.close(current)
        raise
    finally:
        os.close(root_fd)


def _publish_candidate(directory_fd: int, candidate_id: str, markdown_text: str) -> bool:
    """Write ``candidate_id`` atomically; return False when another writer won.

    The candidate is written to a unique temporary file inside the directory
    and published with ``os.link``: link is atomic and fails when the target
    exists, so two processes staging the same proposal cannot both publish and
    neither can replace the winner. Concurrent writers converge on one file.
    """
    final_name = f"{candidate_id}.md"
    temporary_name = f".{candidate_id}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        temporary_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory_fd,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(markdown_text)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.link(
            temporary_name,
            final_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        return True
    except FileExistsError:
        return False
    finally:
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def stage_candidate(root: Path | str, payload: dict[str, Any]) -> dict[str, Any]:
    """Stage one candidate under ``<vault>/_candidates/remember/`` (never the vault)."""
    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        raise _internal()
    values = _validate_stage_payload(payload)
    relative = _relative_candidate_path(values["proposal_id"])
    directory = base.joinpath(*CANDIDATE_DIRECTORY)
    _assert_safe_candidate_directory(base, directory)
    candidate_id = f"conductor-{values['proposal_id']}"
    content_hash = "sha256:" + hashlib.sha256(values["content"].encode("utf-8")).hexdigest()
    duplicate_response = {
        "contract": CONTRACT,
        "staged": True,
        "duplicate": True,
        "candidate_id": candidate_id,
        "path": relative,
        "authority": False,
    }
    if (base / relative).exists() or (base / relative).is_symlink():
        if _published_candidate_hash(base, relative) == content_hash:
            return duplicate_response
        raise ServiceError(409, "candidate exists with different content")
    created_at = datetime.now(UTC).isoformat()
    header = {
        "proposal_id": values["proposal_id"],
        "source": values["source"],
        "tenant": values["tenant"],
        "tags": values["tags"],
        "related": values["related"],
        "title": values["title"],
        "created_at": created_at,
        "content_hash": content_hash,
        "authority": False,
    }
    markdown_text = _candidate_markdown(header, values["content"])
    with _open_candidate_directory(base) as directory_handle:
        published = _publish_candidate(directory_handle.descriptor, candidate_id, markdown_text)
    if not published:
        if _published_candidate_hash(base, relative) == content_hash:
            return duplicate_response
        raise ServiceError(409, "candidate exists with different content")
    return {
        "contract": CONTRACT,
        "staged": True,
        "duplicate": False,
        "candidate_id": candidate_id,
        "path": relative,
        "authority": False,
    }


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


def health_payload() -> dict[str, Any]:
    root = _root_or_none()
    index_present = bool(root and (root / "index.sqlite3").is_file())
    related_db = _related_db()
    related_present = bool(related_db and _has_related_edges(related_db))
    return {
        "ok": True,
        "service": "memfw",
        "contract": CONTRACT,
        "index_present": index_present,
        "related_index_present": related_present,
    }


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


def _matches(provided: str, expected: str) -> bool:
    if not provided or not expected:
        return False
    return hmac.compare_digest(
        provided.encode("utf-8", "surrogateescape"),
        expected.encode("utf-8", "surrogateescape"),
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "memfw"
    sys_version = ""

    # -- plumbing ----------------------------------------------------------
    def _send(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if status != 200:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(data)

    def _fail(self, error: ServiceError) -> None:
        self._send(
            error.status,
            {"contract": CONTRACT, "error": {"code": error.code, "message": error.message}},
        )

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        del message, explain
        status = 400 if int(code) == 400 else 404 if int(code) in (404, 405, 501) else 500
        error = (
            _bad_request("bad request")
            if status == 400
            else (ServiceError(404, "not found") if status == 404 else _internal())
        )
        self.close_connection = True
        try:
            self._fail(error)
        except OSError:
            pass

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write(f"{self.log_date_time_string()} - {format % args}\n")

    # -- authentication and body handling ----------------------------------
    def _token_matches(self, candidate: str) -> bool:
        return _matches(candidate, read_token()) or _matches(candidate, write_token())

    def _authorise(self) -> None:
        provided = self.headers.get(TOKEN_HEADER, "")
        if self.path == "/stage":
            expected = write_token()
            if not expected:
                raise ServiceError(403, "write access is not configured")
            if not provided:
                raise ServiceError(401, "missing or invalid credential")
            if _matches(provided, expected):
                return
            if _matches(provided, read_token()):
                raise ServiceError(403, "write credential required")
            raise ServiceError(401, "missing or invalid credential")
        if not self._token_matches(provided):
            raise ServiceError(401, "missing or invalid credential")

    def _read_json(self, body_limit: int) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise _bad_request("missing Content-Length")
        try:
            length = int(raw_length)
        except (TypeError, ValueError) as exc:
            raise _bad_request("invalid Content-Length") from exc
        if length <= 0:
            raise _bad_request("empty request body")
        if length > body_limit:
            raise ServiceError(413, "request body too large")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise _bad_request("incomplete request body")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _bad_request("invalid JSON") from exc
        if not isinstance(payload, dict):
            raise _bad_request("request must be a JSON object")
        return payload

    # -- routing -----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        try:
            if self.path == "/health":
                self._send(200, health_payload())
                return
            raise ServiceError(404, "not found")
        except ServiceError as error:
            self._fail(error)
        except Exception:
            traceback.print_exc()
            self._fail(_internal())

    def do_POST(self) -> None:  # noqa: N802
        try:
            if self.path not in ("/search", "/related", "/stage"):
                raise ServiceError(404, "not found")
            self._authorise()
            body_limit = STAGE_BODY_BYTES if self.path == "/stage" else SEARCH_BODY_BYTES
            payload = self._read_json(body_limit)
            root = _root()
            if self.path == "/search":
                result = search_vault(
                    root,
                    payload.get("query", ""),
                    limit=payload.get("limit", 10),
                    source=payload.get("source", ""),
                    tenant=payload.get("tenant", ""),
                    include_history=payload.get("include_history", False),
                    as_of=payload.get("as_of"),
                )
            elif self.path == "/related":
                result = related_vault(
                    root,
                    payload.get("memory_id", ""),
                    limit=payload.get("limit", 10),
                    tenant=payload.get("tenant", ""),
                )
            else:
                result = stage_candidate(root, payload)
            self._send(200, result)
        except ServiceError as error:
            self._fail(error)
        except Exception:
            traceback.print_exc()
            self._fail(_internal())

    def do_HEAD(self) -> None:  # noqa: N802
        self._not_found()

    def do_PUT(self) -> None:  # noqa: N802
        self._not_found()

    def do_PATCH(self) -> None:  # noqa: N802
        self._not_found()

    def do_DELETE(self) -> None:  # noqa: N802
        self._not_found()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._not_found()

    def _not_found(self) -> None:
        try:
            self._fail(ServiceError(404, "not found"))
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MemFW read-only HTTP service (memfw-http/2)")
    parser.add_argument("--root", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--related-db", default=None)
    args = parser.parse_args(argv)
    if not read_token() and not write_token():
        raise SystemExit(
            f"{READ_TOKEN_ENV}, {READ_TOKEN_FILE_ENV}, {WRITE_TOKEN_ENV} or "
            f"{WRITE_TOKEN_FILE_ENV} is required"
        )
    os.environ[ROOT_ENV] = str(Path(args.root).expanduser().resolve())
    if args.related_db:
        os.environ[RELATED_DB_ENV] = str(Path(args.related_db).expanduser().resolve())
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    sys.stderr.write(f"memfw read-only service on {args.host}:{args.port} (contract {CONTRACT})\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

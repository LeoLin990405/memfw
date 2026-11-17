#!/usr/bin/env python3
"""Read-only HTTP facade over a MemFW vault: contracts ``memfw-http/2`` and ``memfw-http/3``.

The service exposes ``GET /health`` and authenticated ``POST /search``,
``POST /related`` and ``POST /stage``; under ``memfw-http/3`` also
``POST /context`` and ``POST /forget``. It reads Markdown and the disposable
SQLite index/related projections only. The write paths never touch the vault:
``/stage`` writes a candidate file under ``<vault>/_candidates/remember/`` and
``/forget`` a deletion-request candidate under ``<vault>/_candidates/forget/``;
nothing is ever deleted.

Contract selection: the ``MemFW-Contract`` request header. Absent, or
``memfw-http/2``, selects v2, whose responses are byte-identical to the
service before v3 existed (``/context`` and ``/forget`` answer 404 there, as
they always did). ``memfw-http/3`` selects v3; any other value is 400
``contract is not available`` (tagged v2). Every ``/health`` response also
carries the ``MemFW-Contracts`` header listing both.

Contract summary (v2; v3 differs only where ``docs/v1-integration.md`` says):

* every response body carries ``"contract": "memfw-http/2"``;
* ``/search`` and ``/related`` accept the read token or the write token,
  ``/stage`` accepts the write token only; with no write token configured,
  ``/stage`` always answers 403;
* ``score`` is normalised to ``[0, 1]`` and grows with relevance. With a bm25
  raw score, ``raw_score`` keeps the SQLite FTS5 value (smaller is better) and
  ``score = x / (1 + x)`` with ``x = max(0, -raw_score)``; with no bm25 (a
  query of one or two characters uses a case-insensitive substring branch)
  ``raw_score`` is ``null`` and
  ``score = 1 / (1 + rank)`` for the returned rank starting at 0. On a
  trigram index, terms under three characters in a longer query are matched
  the same substring way and ranked after the FTS hits, never above them.
  ``/related``
  edge scores use the same ``x / (1 + x)`` shape with ``x`` the positive raw
  edge value. Both formulas are monotonic;
* ``degraded`` is true only when a request did not take its preferred path:
  ``/search`` always runs FTS (the only implemented mode) and reports false,
  except on an index of an older schema generation, which is never read or
  rebuilt here: it answers no results, ``degraded`` true and ``errors``
  ``["index_stale"]``,
  ``/related`` reports false on the edges projection and true when it falls back
  to lexical search;
* temporal filtering and tenant filtering both happen before a hit is ranked
  into the returned page. Alias hits use that same temporal rule, then
  reciprocal-rank fusion with the lexical candidates, then the limit cut.
  Tenant is exact equality on ``memory_access.tenant``.
  An empty tenant matches only memories whose tenant is empty.

Configuration: ``MEMFW_READONLY_ROOT`` (or ``--root``; ``--root`` falls back
to the environment variable), ``MEMFW_RELATED_DB`` (or ``--related-db``),
``MEMFW_READONLY_TOKEN`` / ``MEMFW_READONLY_TOKEN_FILE`` and
``MEMFW_WRITE_TOKEN`` / ``MEMFW_WRITE_TOKEN_FILE`` (environment first, then
file; file content is stripped), ``MEMFW_READONLY_CLEARANCE`` (or
``--clearance``): the privacy levels ``/search`` and ``/related`` may return,
comma separated from ``public``, ``shared``, ``local-private`` (MM0). Unset
means no privacy filtering, the behaviour before MM0, and v3 ``/context`` is
not served (404) at all; the design's remote
default is ``public,shared``. A memory with no ``privacy`` field has the
vault's ``legacy_privacy_default`` (``local-private`` when unset). A token
file must be mode ``0600``. The read and write tokens must differ. A
``--host`` that resolves to a wildcard address (``0.0.0.0``, ``::``, ``0``,
an empty host) is refused. Each request socket times out after 5 seconds.

The read side is ``memfw.index.readonly``. This script loads its own copy of
that file as module ``memfw_readonly_library`` (beside the script, the
checkout's ``src/``, or the installed package), so ``ServiceError`` raised
here is a different class from ``memfw.index.readonly.ServiceError``.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import hmac
import ipaddress
import json
import os
import re
import socket
import sqlite3
import sys
import traceback
import uuid
from datetime import UTC, datetime
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import yaml

UPGRADE_DOC = "docs/READONLY-SERVICE-UPGRADE.md"


def _installed_readonly() -> Path | None:
    """``memfw/index/readonly.py`` of an installed memfw, found without importing it."""

    import importlib.util

    try:
        spec = importlib.util.find_spec("memfw")
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.submodule_search_locations:
        return None
    for location in spec.submodule_search_locations:
        path = Path(location) / "index" / "readonly.py"
        if path.is_file():
            return path
    return None


def _library_candidates(script: Path | None = None) -> list[Path]:
    """Files to try for the read side, in order; the first that exists wins.

    1. ``readonly.py`` beside ``script`` (default: this file), a deployed copy;
    2. ``src/memfw/index/readonly.py`` of the checkout, only when the script
       sits in that checkout's ``scripts/`` directory.

    ``_load_library`` falls back to the installed ``memfw`` package after these.
    """

    here = (script if script is not None else Path(__file__)).resolve()
    candidates = [here.with_name("readonly.py")]
    if len(here.parents) > 1 and here.parent.name == "scripts":
        candidates.append(here.parents[1] / "src" / "memfw" / "index" / "readonly.py")
    return candidates


def _load_library() -> Any:
    """Load ``memfw.index.readonly`` by file, without importing ``memfw.index``.

    The module needs only the standard library and PyYAML, and loads the
    ``read_path.py`` beside it; ``memfw`` itself is imported lazily, only for
    a unicode61 index or ontology alias expansion.
    """

    import importlib.util

    candidates = _library_candidates()
    chosen = next((path for path in candidates if path.is_file()), None)
    if chosen is None:
        chosen = _installed_readonly()
    if chosen is None:
        expected = ", ".join(str(path) for path in candidates)
        raise ImportError(
            "memfw read-only service: readonly.py not found. Expected readonly.py and "
            f"read_path.py beside this script, or one of: {expected}, or an installed "
            f"memfw. See {UPGRADE_DOC} (Deploy)."
        )
    spec = importlib.util.spec_from_file_location("memfw_readonly_library", chosen)
    if spec is None or spec.loader is None:
        raise ImportError(f"memfw read-only service: cannot load {chosen}; see {UPGRADE_DOC}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_library = _load_library()

#: The ``read_path.READ_PATH_VERSION`` this script needs. Checked here too, so
#: an older ``readonly.py`` (which does not check) beside an older
#: ``read_path.py`` cannot start either.
REQUIRED_READ_PATH_VERSION = 2


def _check_read_path(library: Any) -> Any:
    module = getattr(library, "read_path", None)
    version = getattr(module, "READ_PATH_VERSION", 1)
    if not isinstance(version, int) or version < REQUIRED_READ_PATH_VERSION:
        raise ImportError(
            f"memfw read-only service: read_path.py loaded by {library.__file__} is version "
            f"{version}, older than the {REQUIRED_READ_PATH_VERSION} this script needs; "
            f"deploy the three files together. See {UPGRADE_DOC} (Deploy)."
        )
    return module


# The read side lives in ``memfw.index.readonly``. These names keep the ones
# this script exported before (tests, eval and deploy checks import them).
read_path = _check_read_path(_library)
CONTRACT = _library.CONTRACT
MAX_QUERY_CHARS = _library.MAX_QUERY_CHARS
MAX_LIMIT = _library.MAX_LIMIT
MAX_TERMS = _library.MAX_TERMS
_TERM = _library._TERM
MAX_SUMMARY_CHARS = _library.MAX_SUMMARY_CHARS
INTERNAL_MESSAGE = _library.INTERNAL_MESSAGE
ERROR_CODES = _library.ERROR_CODES
ServiceError = _library.ServiceError
_bad_request = _library.bad_request
_internal = _library.internal_error
_connect_index = _library._connect_index
INDEX_SCHEMA_VERSION = _library.INDEX_SCHEMA_VERSION
INDEX_TABLES = _library.INDEX_TABLES
INDEX_STALE = _library.INDEX_STALE
_index_is_current = _library._index_is_current
_has_table = _library.has_table
_metadata_from_text = _library._metadata_from_text
_metadata = _library.read_metadata
_iso_string = _library.iso_string
_optional = _library.optional_text
_recorded_at = _library.recorded_at_text
_string_list = _library._string_list
_collapsed = _library._collapsed
_same_instant = _library._same_instant
fold_temporal_aliases = _library.fold_temporal_aliases
_document_payload = _library.document_payload
_fts_terms = _library._fts_terms
_shared_query_terms = _library._shared_query_terms
_query_terms = _library.fts_query_terms
_fts_query = _library.fts_query
_quoted_or = _library.quoted_or
_split_short_terms = _library._split_short_terms
_SHORT_STOPWORDS = _library._SHORT_STOPWORDS
_useful_short_term = _library._useful_short_term
_short_terms_clause = _library._short_terms_clause
_index_tokenizer = _library._index_tokenizer
_unsupported_query = _library.unsupported_query
_normalise_as_of = _library.normalise_as_of
_temporal_clause = _library._temporal_clause
_ROW_COLUMNS = _library._ROW_COLUMNS
_SUBSTRING_COLUMNS = _library._SUBSTRING_COLUMNS
_row_filters = _library._row_filters
_search_batch = _library._search_batch
_score_from_raw = _library._score_from_raw
_alias_expansion = _library._alias_expansion
search_vault = _library.search_vault

TOKEN_HEADER = "X-Memfw-Token"
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
#: ``/forget`` (v3) writes here: a separate directory, so ``memfw candidates``
#: and ``candidate-apply`` can never turn a deletion request into a memory.
FORGET_DIRECTORY = ("_candidates", "forget")
#: Pending ``/forget`` request files after which a new request is refused (429).
MAX_PENDING_FORGET_REQUESTS = 1000
MEMORY_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
MAX_REASON_CHARS = 500
READ_TOKEN_ENV = "MEMFW_READONLY_TOKEN"
READ_TOKEN_FILE_ENV = "MEMFW_READONLY_TOKEN_FILE"
WRITE_TOKEN_ENV = "MEMFW_WRITE_TOKEN"
WRITE_TOKEN_FILE_ENV = "MEMFW_WRITE_TOKEN_FILE"
ROOT_ENV = "MEMFW_READONLY_ROOT"
RELATED_DB_ENV = "MEMFW_RELATED_DB"
TENANT_ENV = "MEMFW_TENANT"
CLEARANCE_ENV = "MEMFW_READONLY_CLEARANCE"
TOKEN_FILE_MODE = 0o600
# Codes BaseHTTPRequestHandler raises before the route runs. 414/431 are size
# failures; 505 is an unsupported version and must still carry a status line.
PROTOCOL_ERROR_STATUS = {
    400: 400,
    404: 404,
    405: 404,
    414: 413,
    431: 413,
    501: 404,
    505: 400,
}


_ENDPOINT_KEYS = frozenset(
    {
        "endpoint",
        "base_url",
        "embedding_endpoint",
        "embedding_url",
        "rerank_endpoint",
        "rerank_url",
        "judge_endpoint",
        "judge_url",
        "rewrite_endpoint",
        "rewrite_url",
        "model_endpoint",
        "openai_base_url",
    }
)


# ---------------------------------------------------------------------------
# Contract selection (memfw-http/2 and memfw-http/3)
# ---------------------------------------------------------------------------

CONTRACT_V2: str = CONTRACT
CONTRACT_V3 = "memfw-http/3"
#: Every contract this service serves, oldest first (``/health`` lists them).
CONTRACTS = (CONTRACT_V2, CONTRACT_V3)
#: Request header that selects the contract; absent means ``memfw-http/2``.
CONTRACT_HEADER = "MemFW-Contract"
#: Response header on every ``/health`` answer, whatever the contract.
CONTRACTS_HEADER = "MemFW-Contracts"
#: ``/search`` modes v3 serves. ``mode`` is part of v3; only FTS runs over
#: HTTP until the multihop pilot opens here (roadmap R2: MCP only so far).
V3_SEARCH_MODES = ("fts",)
V3_PATHS = ("/search", "/related", "/stage", "/context", "/forget")
V2_PATHS = ("/search", "/related", "/stage")
WRITE_PATHS = frozenset({"/stage", "/forget"})


def select_contract(values: list[str] | None) -> str:
    """The contract the ``MemFW-Contract`` header values select.

    No header (or an empty one) and ``memfw-http/2`` select v2;
    ``memfw-http/3`` selects v3. A repeated header or any other value is 400
    ``contract is not available``: a client asking for a contract this
    service does not speak must not silently get another.
    """

    if not values:
        return CONTRACT_V2
    if len(values) > 1:
        raise _bad_request("contract is not available")
    value = values[0].strip()
    if value in ("", CONTRACT_V2):
        return CONTRACT_V2
    if value == CONTRACT_V3:
        return CONTRACT_V3
    raise _bad_request("contract is not available")


def _tagged(payload: dict[str, Any], contract: str) -> dict[str, Any]:
    """``payload`` carrying ``contract``; the v2 object is returned unchanged."""

    if contract == CONTRACT_V2:
        return payload
    return {**payload, "contract": contract}


def reject_unavailable_search_mode(payload: dict[str, Any]) -> None:
    """HTTP search stays on FTS. Other modes are refused until the v3 contract."""

    if "mode" not in payload:
        return
    if payload.get("mode") != "fts":
        raise _bad_request("mode is not available")


def reject_client_endpoints(payload: dict[str, Any]) -> None:
    """A request body cannot name a judge, rerank, embedding, or rewrite endpoint."""

    found: list[str] = []
    for key in payload:
        normalized = str(key).strip().lower().replace("-", "_")
        if (
            normalized in _ENDPOINT_KEYS
            or normalized.endswith("_endpoint")
            or normalized.endswith("_url")
        ):
            found.append(str(key))
    if found:
        raise _bad_request("endpoint fields are not accepted")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _env_secret(name: str, file_name: str) -> str:
    """Read a secret from the environment, then from a mode-0600 file.

    An unreadable file or a mode other than ``0600`` is refused. It must not
    look like "no token configured".
    """
    value = os.environ.get(name, "").strip()
    if value:
        return value
    path_text = os.environ.get(file_name, "").strip()
    if not path_text:
        return ""
    path = Path(path_text).expanduser()
    try:
        mode = path.stat().st_mode & 0o777
        if mode != TOKEN_FILE_MODE:
            raise _internal()
        return path.read_text(encoding="utf-8").strip()
    except ServiceError:
        raise
    except OSError as exc:
        raise _internal() from exc


def _same_secret(left: str, right: str) -> bool:
    if not left or not right or len(left) != len(right):
        return False
    return hmac.compare_digest(
        left.encode("utf-8", "surrogateescape"),
        right.encode("utf-8", "surrogateescape"),
    )


def _configured_tokens() -> tuple[str, str]:
    """Return ``(read, write)`` or refuse when the two configured tokens match."""
    read_value = read_token()
    write_value = write_token()
    if read_value and write_value and _same_secret(read_value, write_value):
        raise ServiceError(403, "read and write credentials must differ")
    return read_value, write_value


def read_token() -> str:
    return _env_secret(READ_TOKEN_ENV, READ_TOKEN_FILE_ENV)


def write_token() -> str:
    return _env_secret(WRITE_TOKEN_ENV, WRITE_TOKEN_FILE_ENV)


def _root() -> Path:
    value = os.environ.get(ROOT_ENV, "").strip()
    if not value:
        raise _internal()
    return Path(value).expanduser()


def scope_tenant(requested: Any) -> str:
    """Bind a read to the service tenant. A payload may only repeat that tenant.

    ``MEMFW_TENANT`` is the tenant bound to this process's token. An omitted
    tenant uses that binding. A different tenant is refused. With no binding,
    the payload tenant is the scope, including an empty one.
    """

    bound = os.environ.get(TENANT_ENV, "")
    if not isinstance(bound, str):
        bound = ""
    if requested is None:
        requested = ""
    if not isinstance(requested, str):
        raise _bad_request("tenant must be a string")
    if not bound:
        return requested
    if requested in ("", bound):
        return bound
    raise ServiceError(403, "tenant is outside the bound tenant")


def configured_clearance() -> frozenset[str] | None:
    """``MEMFW_READONLY_CLEARANCE`` parsed; unset → ``None`` (no privacy filtering).

    A malformed value narrows to ``{"public"}`` here; ``main`` refuses to start
    with one.
    """

    parsed: frozenset[str] | None = read_path.parse_clearance(os.environ.get(CLEARANCE_ENV))
    return parsed


def _related_db() -> Path | None:
    value = os.environ.get(RELATED_DB_ENV, "").strip()
    if not value:
        return None
    return Path(value).expanduser()


def _root_or_none() -> Path | None:
    value = os.environ.get(ROOT_ENV, "").strip()
    return Path(value).expanduser() if value else None


def _connect_related(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


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


def _unit_interval(raw: float) -> float:
    """Map a non-negative relevance sum into ``[0, 1]`` via ``x / (1 + x)``."""
    magnitude = max(0.0, raw)
    return magnitude / (1.0 + magnitude)


# ---------------------------------------------------------------------------
# /related
# ---------------------------------------------------------------------------


def _memory_metadata(
    root: Path, memory_id: str, tenant: str, *, clearance: frozenset[str] | None = None
) -> tuple[Path, dict[str, Any]]:
    if not isinstance(memory_id, str) or not memory_id.strip():
        raise _bad_request("memory_id is invalid")
    identifier = memory_id.strip()
    if "/" in identifier or ".." in identifier or "\\" in identifier:
        raise _bad_request("memory_id is invalid")
    path = root / "memories" / f"{identifier}.md"
    if not path.is_file():
        raise ServiceError(404, "memory not found")
    metadata = _metadata(path)
    # Exact equality, empty included: an untenanted request must not learn
    # that another tenant's memory exists.
    if str(metadata.get("tenant") or "") != tenant:
        raise ServiceError(404, "memory not found")
    # Same answer for a memory the clearance withholds: its existence is not
    # revealed either.
    if clearance is not None:
        try:
            # Bytes, not ``read_text``: universal newlines would hide CRLF.
            raw = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            raw = ""
        level = _library.metadata_privacy(raw, metadata, _library.vault_legacy_privacy(root))
        if not read_path.admits(clearance, level):
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


def _projection_target_is_current(
    root: Path,
    source: str,
    identifier: str,
    *,
    tenant: str = "",
    clearance: frozenset[str] | None = None,
    legacy_privacy: str | None = None,
) -> bool:
    """Drop superseded memories and memories whose live tenant has moved.

    The edge table still carries the tenant from the last rebuild. Markdown is
    authoritative, so a target that now belongs to another tenant must not
    contribute its id or title. Non-memory targets have no vault temporal
    fields. Search results keep emitting ``valid_to`` and ``superseded_by``;
    this only decides membership.

    With a ``clearance`` a memory target must be admitted by it, and a
    non-memory target (a note) is dropped: its privacy cannot be read here,
    so it fails closed.
    """
    if source != "memory":
        return clearance is None
    payload = _document_payload(root, identifier, legacy_privacy=legacy_privacy)
    if payload is None:
        return False
    if payload["valid_to"] or payload["superseded_by"]:
        return False
    if str(payload["tenant"] or "") != tenant:
        return False
    return read_path.admits(clearance, payload["privacy"])


def _target_provenance_type(root: Path, source: str, identifier: str) -> str:
    """``provenance_type`` of a ``/related`` target (v3). Called only on admitted targets.

    A memory reads its own ``provenance``/``source``; a note (only listed
    without a clearance) is ``imported``.
    """

    if source != "memory":
        return str(read_path.provenance_type("", "notes"))
    try:
        metadata = _metadata(root / "memories" / f"{identifier}.md")
    except Exception:
        metadata = {}
    return str(
        read_path.provenance_type(
            str(metadata.get("provenance") or ""), str(metadata.get("source") or "")
        )
    )


def _related_from_edges(
    related_db: Path,
    memory_id: str,
    *,
    root: Path,
    limit: int,
    tenant: str,
    clearance: frozenset[str] | None = None,
    evidence: bool = False,
) -> list[dict[str, Any]]:
    connection = _connect_related(related_db)
    try:
        # Always scoped, empty tenant included: a neighbour of another tenant
        # contributes neither its id nor its title.
        filters = " AND i.tenant=?"
        params: list[Any] = [tenant]
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
                "source": str(other_source),
                "id": str(other_id),
                "title": str(row["title"] or ""),
                "score": 0.0,
                "reasons": set(),
            }
            merged[key] = item
        item["score"] = max(float(item["score"]), float(row["score"]))
        item["reasons"].add(_reason_name(str(row["method"])))
    legacy_privacy = _library.vault_legacy_privacy(root)
    current = [
        item
        for item in merged.values()
        if _projection_target_is_current(
            root,
            str(item["source"]),
            str(item["id"]),
            tenant=tenant,
            clearance=clearance,
            legacy_privacy=legacy_privacy,
        )
    ]
    ordered = sorted(current, key=lambda item: (-float(item["score"]), item["id"]))[:limit]
    results = [
        {
            "id": item["id"],
            "title": item["title"],
            "score": _unit_interval(float(item["score"])),
            "reasons": sorted(item["reasons"]),
        }
        for item in ordered
    ]
    if evidence:
        for result, item in zip(results, ordered, strict=True):
            result["why"] = "related"
            result["provenance_type"] = _target_provenance_type(
                root, str(item["source"]), str(item["id"])
            )
    return results


def _related_from_search(
    root: Path,
    memory_id: str,
    metadata: dict[str, Any],
    *,
    limit: int,
    tenant: str,
    clearance: frozenset[str] | None = None,
    evidence: bool = False,
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
        payload = search_vault(root, query, limit=MAX_LIMIT, tenant=tenant, clearance=clearance)
        results = []
        for item in payload["results"]:
            if item["id"] == memory_id:
                continue
            entry = {
                "id": item["id"],
                "title": item["title"],
                "score": float(item["score"]),
                "reasons": ["lexical"],
            }
            if evidence:
                entry["why"] = "lexical"
                entry["provenance_type"] = item["provenance_type"]
            results.append(entry)
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
    clearance: frozenset[str] | str | None = None,
    evidence: bool = False,
) -> dict[str, Any]:
    """``/related``. ``clearance`` as in ``search_vault`` (``None`` filters nothing).

    ``evidence`` (memfw-http/3) adds ``why`` and ``provenance_type`` to each
    result; without it the v2 result keys are unchanged.
    """

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
    cleared = _library.checked_clearance(clearance)
    _path, metadata = _memory_metadata(base, memory_id, tenant, clearance=cleared)
    if candidate is not None and candidate.is_file() and _has_related_edges(candidate):
        try:
            results = _related_from_edges(
                candidate,
                identifier,
                root=base,
                limit=limit,
                tenant=tenant,
                clearance=cleared,
                evidence=evidence,
            )
        except sqlite3.Error:
            results = _related_from_search(
                base,
                identifier,
                metadata,
                limit=limit,
                tenant=tenant,
                clearance=cleared,
                evidence=evidence,
            )
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
    results = _related_from_search(
        base,
        identifier,
        metadata,
        limit=limit,
        tenant=tenant,
        clearance=cleared,
        evidence=evidence,
    )
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
    if not isinstance(content, str) or not content.strip():
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
    # Optional privacy level (MM0), carried to the memory by candidate-apply.
    privacy = payload.get("privacy")
    if privacy is not None:
        if read_path.field_level(privacy) == "unknown":
            raise _bad_request("privacy must be one of: public, shared, local-private")
        privacy = str(privacy).strip().lower()
    extra = {"privacy": privacy} if privacy is not None else {}
    return {
        **extra,
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
        "recorded_at": metadata["recorded_at"],
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
                "recorded_at": metadata["recorded_at"],
                "content_hash": metadata["content_hash"],
                "authority": False,
            }
        ),
    }
    if metadata.get("privacy") is not None:
        # MM0: written only when the request set it, so other candidates keep
        # their bytes.
        header["privacy"] = metadata["privacy"]
        header["metadata"]["privacy"] = metadata["privacy"]
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


def _assert_safe_candidate_directory(
    base: Path, directory: Path, parts: tuple[str, ...] = CANDIDATE_DIRECTORY
) -> None:
    """Refuse candidate directories that leave the vault through a symlink.

    The service is only allowed to write below ``<root>/_candidates/remember``;
    a symlinked component would let a request create a file in ``memories/`` or
    outside the vault.
    """
    current = base
    for part in parts:
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


def _published_candidate_key(base: Path, relative: str) -> dict[str, Any]:
    """The fields that make a resubmission identical to the published candidate."""
    path = base / relative
    if path.is_symlink() or not path.is_file():
        raise _internal()
    return _candidate_key(_metadata(path))


def _candidate_key(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "content_hash": str(metadata.get("content_hash") or ""),
        "title": str(metadata.get("title") or ""),
        "tenant": str(metadata.get("tenant") or ""),
        "source": str(metadata.get("source") or ""),
        "tags": [str(item) for item in metadata.get("tags") or []],
        "related": [str(item) for item in metadata.get("related") or []],
        "privacy": str(metadata.get("privacy") or ""),
    }


def _existing_candidate(base: Path, relative: str, expected: dict[str, Any]) -> None:
    """Return when the published candidate is this same submission, else 409.

    The same ``proposal_id`` with a different title, tenant, source, tags or
    related list is a different proposal, not a duplicate.
    """
    published = _published_candidate_key(base, relative)
    if published["content_hash"] != expected["content_hash"]:
        raise ServiceError(409, "candidate exists with different content")
    if published != expected:
        raise ServiceError(409, "candidate exists with different metadata")


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


def _open_candidate_directory(
    base: Path, parts: tuple[str, ...] = CANDIDATE_DIRECTORY
) -> _DirectoryHandle:
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
        for part in parts:
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
    try:
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
        except FileExistsError:
            return False
        return True
    finally:
        # Covers write/fsync failures (a full disk) as well as a lost link race.
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
    expected = _candidate_key({**values, "content_hash": content_hash})
    if (base / relative).exists() or (base / relative).is_symlink():
        _existing_candidate(base, relative, expected)
        return duplicate_response
    created_at = datetime.now(UTC).isoformat()
    header = {
        "proposal_id": values["proposal_id"],
        "source": values["source"],
        "tenant": values["tenant"],
        "tags": values["tags"],
        "related": values["related"],
        "title": values["title"],
        "created_at": created_at,
        # Server clock at write; the 30-day candidate TTL counts from here.
        "recorded_at": created_at,
        "content_hash": content_hash,
        "authority": False,
    }
    if values.get("privacy") is not None:
        header["privacy"] = values["privacy"]
    markdown_text = _candidate_markdown(header, values["content"])
    with _open_candidate_directory(base) as directory_handle:
        published = _publish_candidate(directory_handle.descriptor, candidate_id, markdown_text)
    if not published:
        _existing_candidate(base, relative, expected)
        return duplicate_response
    return {
        "contract": CONTRACT,
        "staged": True,
        "duplicate": False,
        "candidate_id": candidate_id,
        "path": relative,
        "authority": False,
    }


# ---------------------------------------------------------------------------
# /forget (memfw-http/3): a deletion-request candidate, never a deletion
# ---------------------------------------------------------------------------


def _validate_forget_payload(payload: dict[str, Any]) -> dict[str, str]:
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or PROPOSAL_ID_PATTERN.fullmatch(request_id) is None:
        raise _bad_request("request_id must match [A-Za-z0-9_-]{1,128}")
    memory_id = payload.get("memory_id")
    if not isinstance(memory_id, str) or MEMORY_ID_PATTERN.fullmatch(memory_id) is None:
        raise _bad_request("memory_id is invalid")
    reason = _header_text(payload.get("reason", ""), "reason", max_length=MAX_REASON_CHARS)
    source = _header_text(payload.get("source", "conductor"), "source", single_line=True)
    return {
        "request_id": request_id,
        "memory_id": memory_id,
        "reason": reason,
        "source": source or "conductor",
    }


def _forget_key(metadata: dict[str, Any]) -> dict[str, str]:
    """The fields that make a replay identical to the recorded request."""

    return {
        "type": str(metadata.get("type") or ""),
        "request_id": str(metadata.get("request_id") or ""),
        "memory_id": str(metadata.get("memory_id") or ""),
        "tenant": str(metadata.get("tenant") or ""),
        "source": str(metadata.get("source") or ""),
        "reason": str(metadata.get("reason") or ""),
    }


def _existing_forget_request(base: Path, relative: str, expected: dict[str, str]) -> None:
    """Return when the recorded request is this same request, else 409."""

    path = base / relative
    if path.is_symlink() or not path.is_file():
        raise _internal()
    if _forget_key(_metadata(path)) != expected:
        raise ServiceError(409, "request exists with different content")


def forget_candidate_id(tenant: str, request_id: str) -> str:
    """``forget-<tenant key>-<request_id>``: request ids are scoped per tenant.

    The tenant key is the first 16 hex digits of ``sha256(tenant)`` (the
    empty tenant included), so two tenants never share a file name, a
    ``request_id`` one tenant used is no oracle or pre-claim for another,
    and the file name carries no tenant name.
    """

    tenant_key = hashlib.sha256(tenant.encode("utf-8")).hexdigest()[:16]
    return f"forget-{tenant_key}-{request_id}"


def _pending_forget_requests(directory: Path) -> int:
    try:
        with os.scandir(directory) as entries:
            return sum(
                1
                for entry in entries
                if entry.name.startswith("forget-")
                and entry.name.endswith(".md")
                and entry.is_file(follow_symlinks=False)
            )
    except FileNotFoundError:
        return 0
    except OSError as exc:
        raise _internal() from exc


def _forget_queue_full() -> ServiceError:
    error = ServiceError(429, "too many pending forget requests")
    # ``readonly.py`` maps 429 to nothing (it stays unchanged for the
    # three-file deploy), so the v3 code is set here.
    error.code = "too_many_requests"
    return error


def _forget_markdown(header: dict[str, Any]) -> str:
    rendered = yaml.dump(
        _single_line(header),
        Dumper=_CandidateDumper,
        allow_unicode=True,
        sort_keys=False,
        width=10**9,
    ).rstrip()
    if "---\n" in rendered + "\n":
        raise _internal()
    body = (
        f"Deletion request for memory {header['memory_id']}. Nothing was deleted: "
        "a human reviews this request and decides.\n"
    )
    return f"---\n{rendered}\n---\n\n{body}"


def forget_request(
    root: Path | str,
    payload: dict[str, Any],
    *,
    tenant: str = "",
    clearance: frozenset[str] | str | None = None,
) -> dict[str, Any]:
    """Record one deletion request under ``<vault>/_candidates/forget/`` (never delete).

    The request names a memory the caller may see: in ``tenant`` (exact
    equality, empty included) and admitted by ``clearance`` (MM0). A missing
    memory, another tenant's and one the clearance withholds all answer the
    same 404, so the existence of a hidden memory is never revealed.
    ``request_id`` makes the call idempotent within a tenant (the file name is
    ``forget_candidate_id(tenant, request_id)``): the same request again is
    ``duplicate: true`` without a rewrite, the same ``request_id`` with any
    other memory, source or reason is 409. While the memory exists, the
    visibility check runs before either answer, so a hidden target is 404
    even when a request for it was recorded; once it is gone, a replay of
    the tenant's own request is still ``duplicate: true``. The memory file, the event
    log and every projection are left alone; ``deleted`` is always false.
    """

    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        raise _internal()
    if not isinstance(tenant, str):
        raise _bad_request("tenant must be a string")
    values = _validate_forget_payload(payload)
    cleared = _library.checked_clearance(clearance)
    candidate_id = forget_candidate_id(tenant, values["request_id"])
    relative = "/".join((*FORGET_DIRECTORY, f"{candidate_id}.md"))
    directory = base.joinpath(*FORGET_DIRECTORY)
    _assert_safe_candidate_directory(base, directory, FORGET_DIRECTORY)
    expected = {
        "type": "forget-request",
        "request_id": values["request_id"],
        "memory_id": values["memory_id"],
        "tenant": tenant,
        "source": values["source"],
        "reason": values["reason"],
    }

    def response(duplicate: bool) -> dict[str, Any]:
        return {
            "contract": CONTRACT_V3,
            "requested": True,
            "duplicate": duplicate,
            "request_id": values["request_id"],
            "memory_id": values["memory_id"],
            "candidate_id": candidate_id,
            "path": relative,
            "deleted": False,
            "authority": False,
        }

    recorded = (base / relative).exists() or (base / relative).is_symlink()
    target_exists = (base / "memories" / f"{values['memory_id']}.md").is_file()
    if recorded and not target_exists:
        # The memory is gone (deleted after the request, as intended): only a
        # request this tenant recorded answers ``duplicate`` or 409, and the
        # file name is tenant-scoped, so another tenant's never matches.
        _existing_forget_request(base, relative, expected)
        return response(True)
    # Visibility first: a memory this caller may not see answers the same 404
    # whether or not a request for it was recorded, so neither a replay nor
    # a 409 reveals that it exists.
    memory_path, _memory = _memory_metadata(base, values["memory_id"], tenant, clearance=cleared)
    if recorded:
        _existing_forget_request(base, relative, expected)
        return response(True)
    # A bound on what a write token can pile up for review. Replays above are
    # still answered; a request beyond the cap writes nothing. Concurrent
    # requests may pass the count together, so the cap is approximate.
    if _pending_forget_requests(directory) >= MAX_PENDING_FORGET_REQUESTS:
        raise _forget_queue_full()
    try:
        target_hash = "sha256:" + hashlib.sha256(memory_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ServiceError(404, "memory not found") from exc
    created_at = datetime.now(UTC).isoformat()
    header: dict[str, Any] = {
        "name": f"Deletion request for {values['memory_id']}",
        "title": f"Deletion request for {values['memory_id']}",
        "type": "forget-request",
        "lifecycle": "active",
        "status": "pending",
        "request_id": values["request_id"],
        "memory_id": values["memory_id"],
        "tenant": tenant,
        "source": values["source"],
        "reason": values["reason"],
        "target_content_hash": target_hash,
        "created_at": created_at,
        "recorded_at": created_at,
        "deleted": False,
        "authority": False,
    }
    markdown_text = _forget_markdown(header)
    with _open_candidate_directory(base, FORGET_DIRECTORY) as directory_handle:
        published = _publish_candidate(directory_handle.descriptor, candidate_id, markdown_text)
    if not published:
        _existing_forget_request(base, relative, expected)
        return response(True)
    return response(False)


# ---------------------------------------------------------------------------
# memfw-http/3 read endpoints
# ---------------------------------------------------------------------------


def search_vault_v3(root: Path | str, query: str, **kwargs: Any) -> dict[str, Any]:
    """v3 ``/search``: the v2 result plus ``recorded_at`` on every hit.

    ``recorded_at`` is read from the hit's own Markdown, which the hit was
    already admitted from (tenant, clearance), so it adds a timestamp and
    nothing else.
    """

    result = search_vault(root, query, **kwargs)
    base = Path(root).expanduser().resolve()
    legacy = _library.vault_legacy_privacy(base)
    for item in result["results"]:
        payload = _document_payload(base, str(item["id"]), legacy_privacy=legacy)
        item["recorded_at"] = payload["recorded_at"] if payload is not None else None
    return _tagged(result, CONTRACT_V3)


_PACK_VERSION = re.compile(r"[0-9a-f]{64}")
_CONTEXT_POLICIES = ("compat_fts", "strict")
#: Same ceiling ``memfw.context`` clamps to.
MAX_CONTEXT_TOKENS = 100_000


def _context_module() -> Any | None:
    """``memfw.runtime.http``, or ``None`` when ``memfw`` is not installed."""

    try:
        import importlib

        return importlib.import_module("memfw.runtime.http")
    except ImportError:
        return None


def context_available() -> bool:
    """Whether ``/context`` can be served (it needs the ``memfw`` package)."""

    import importlib.util

    try:
        return importlib.util.find_spec("memfw.runtime.http") is not None
    except (ImportError, ValueError):
        return False


def context_v3(
    root: Path | str,
    payload: dict[str, Any],
    *,
    tenant: str,
    clearance: frozenset[str] | str | None,
) -> dict[str, Any]:
    """v3 ``/context``: the ``memory_context`` v2 pack (``core`` slot), read-only."""

    base = Path(root).expanduser().resolve()
    if not (base / "vault.yaml").is_file():
        raise _internal()
    version = payload.get("v", 2)
    if type(version) is not int or version != 2:
        raise _bad_request("context version is not available")
    limit_tokens = payload.get("limit_tokens", 2000)
    if isinstance(limit_tokens, bool) or not isinstance(limit_tokens, int):
        raise _bad_request("limit_tokens must be an integer")
    limit_tokens = max(1, min(limit_tokens, MAX_CONTEXT_TOKENS))
    policy = payload.get("policy", "strict")
    if policy is None or policy == "":
        policy = "strict"
    if not isinstance(policy, str) or policy not in _CONTEXT_POLICIES:
        raise _bad_request("policy must be compat_fts or strict")
    slots = payload.get("slots")
    if slots is not None and slots != ["core"]:
        raise _bad_request("slot is not available")
    if_version = payload.get("if_version")
    if if_version == "":
        if_version = None
    if if_version is not None and (
        not isinstance(if_version, str) or _PACK_VERSION.fullmatch(if_version) is None
    ):
        raise _bad_request("if_version must be a pack_version")
    if not isinstance(tenant, str):
        raise _bad_request("tenant must be a string")
    module = _context_module()
    if module is None:
        raise ServiceError(404, "context is not available")
    try:
        pack = module.http_context_pack(
            base,
            tenant=tenant,
            clearance=_library.checked_clearance(clearance),
            policy=policy,
            limit_tokens=limit_tokens,
            if_version=if_version,
        )
    except ValueError as exc:
        # Inputs were checked above; anything left is the vault, not the caller.
        raise _internal() from exc
    return {"contract": CONTRACT_V3, **pack}


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


def context_enabled() -> bool:
    """Whether ``/context`` is routed: only with a clearance configured.

    ``/context`` packs memory text, so it is never served unfiltered: with
    ``MEMFW_READONLY_CLEARANCE`` (``--clearance``) unset it answers 404
    ``not found`` like an unknown path, and ``/health`` leaves it out.
    """

    return configured_clearance() is not None


def v3_paths() -> tuple[str, ...]:
    """The ``memfw-http/3`` POST paths this deployment routes right now."""

    if context_enabled():
        return V3_PATHS
    return tuple(path for path in V3_PATHS if path != "/context")


def health_payload(contract: str = CONTRACT_V2) -> dict[str, Any]:
    """``/health``. v2 is unchanged; v3 adds ``contracts``, ``modes`` and ``endpoints``.

    Only non-sensitive fields: no path, host name or token. ``endpoints``
    lists ``/context`` only when this deployment can serve it: the ``memfw``
    package is installed and a clearance is configured.
    """

    root = _root_or_none()
    index_present = bool(root and (root / "index.sqlite3").is_file())
    related_db = _related_db()
    related_present = bool(related_db and _has_related_edges(related_db))
    payload: dict[str, Any] = {
        "ok": True,
        "service": "memfw",
        "contract": CONTRACT_V2,
        "index_present": index_present,
        "related_index_present": related_present,
    }
    if contract == CONTRACT_V3:
        endpoints = ["/health", *v3_paths()]
        if "/context" in endpoints and not context_available():
            endpoints.remove("/context")
        payload = {
            **_tagged(payload, CONTRACT_V3),
            "contracts": list(CONTRACTS),
            "modes": list(V3_SEARCH_MODES),
            "endpoints": endpoints,
        }
    return payload


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
    # A slow client must not hold a worker thread past this many seconds.
    timeout = 5
    #: The contract of the request being answered; reset by ``_select_contract``
    #: at the start of every request (a keep-alive connection reuses the handler).
    contract = CONTRACT_V2

    # -- plumbing ----------------------------------------------------------
    def _ensure_status_line(self) -> None:
        """HTTP/0.9 omits the status line; protocol errors still need one."""
        version = getattr(self, "request_version", "") or ""
        if version in ("", "HTTP/0.9"):
            self.request_version = "HTTP/1.0"

    def _send(
        self, status: int, payload: dict[str, Any], *, headers: dict[str, str] | None = None
    ) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        include_body = self.command != "HEAD"
        self._ensure_status_line()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data) if include_body else 0))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        if status != 200:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        if include_body:
            self.wfile.write(data)

    def _fail(self, error: ServiceError) -> None:
        self._send(
            error.status,
            {"contract": self.contract, "error": {"code": error.code, "message": error.message}},
        )

    def _select_contract(self) -> None:
        """Pick the request's contract from ``MemFW-Contract`` (400 when unknown).

        On an unknown value the error is tagged v2, the contract a client
        that sent no header would get.
        """

        self.contract = CONTRACT_V2
        headers = getattr(self, "headers", None)
        values = headers.get_all(CONTRACT_HEADER) if headers is not None else None
        self.contract = select_contract(list(values) if values else None)

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        del message, explain
        # A protocol error can come before the headers were parsed: v2 tag.
        self.contract = CONTRACT_V2
        status = PROTOCOL_ERROR_STATUS.get(int(code), 500)
        if status == 400:
            error = _bad_request("bad request")
        elif status == 413:
            error = ServiceError(413, "request too large")
        elif status == 404:
            error = ServiceError(404, "not found")
        else:
            error = _internal()
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
        _configured_tokens()
        if self.path in WRITE_PATHS:
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
            self._select_contract()
            if self.path == "/health":
                self._send(
                    200,
                    health_payload(self.contract),
                    headers={CONTRACTS_HEADER: ", ".join(CONTRACTS)},
                )
                return
            raise ServiceError(404, "not found")
        except ServiceError as error:
            self._fail(error)
        except Exception:
            traceback.print_exc()
            self._fail(_internal())

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._select_contract()
            v3 = self.contract == CONTRACT_V3
            if self.path not in (v3_paths() if v3 else V2_PATHS):
                raise ServiceError(404, "not found")
            self._authorise()
            body_limit = STAGE_BODY_BYTES if self.path == "/stage" else SEARCH_BODY_BYTES
            payload = self._read_json(body_limit)
            reject_client_endpoints(payload)
            root = _root()
            if self.path == "/search":
                reject_unavailable_search_mode(payload)
                search = search_vault_v3 if v3 else search_vault
                result = search(
                    root,
                    payload.get("query", ""),
                    limit=payload.get("limit", 10),
                    source=payload.get("source", ""),
                    tenant=scope_tenant(payload.get("tenant", "")),
                    include_history=payload.get("include_history", False),
                    as_of=payload.get("as_of"),
                    scope=payload.get("scope", ""),
                    clearance=configured_clearance(),
                )
            elif self.path == "/related":
                result = related_vault(
                    root,
                    payload.get("memory_id", ""),
                    limit=payload.get("limit", 10),
                    tenant=scope_tenant(payload.get("tenant", "")),
                    clearance=configured_clearance(),
                    evidence=v3,
                )
            elif self.path == "/context":
                result = context_v3(
                    root,
                    payload,
                    tenant=scope_tenant(payload.get("tenant", "")),
                    clearance=configured_clearance(),
                )
            elif self.path == "/forget":
                result = forget_request(
                    root,
                    payload,
                    tenant=scope_tenant(payload.get("tenant", "")),
                    clearance=configured_clearance(),
                )
            else:
                if os.environ.get(TENANT_ENV, ""):
                    # A bound token stages only for its own tenant.
                    payload = {**payload, "tenant": scope_tenant(payload.get("tenant", ""))}
                result = stage_candidate(root, payload)
            self._send(200, _tagged(result, self.contract))
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
            try:
                self._select_contract()
            except ServiceError as error:
                self._fail(error)
                return
            self._fail(ServiceError(404, "not found"))
        except OSError:
            pass


def check_readonly_contract(url: str, *, timeout: float = 1.0) -> dict[str, Any]:
    """GET ``/health`` for ``url`` and report whether it advertises memfw-http/2.

    ``url`` is a service base or an explicit ``/health`` URL. Redirects are not
    followed. There is no default host; the caller chooses the URL.
    """
    parsed = urlsplit(url)
    result: dict[str, Any] = {
        "ok": False,
        "reachable": False,
        "contract": None,
        "status": None,
        "detail": "not checked",
    }
    if parsed.scheme != "http" or not parsed.hostname:
        result["detail"] = "only absolute http URLs can be checked"
        return result
    path = parsed.path or "/"
    if not path.rstrip("/").endswith("/health"):
        path = path.rstrip("/") + "/health"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    connection = HTTPConnection(parsed.hostname, parsed.port or 80, timeout=timeout)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        raw = response.read(65537)
        status = int(response.status)
    except OSError:
        result["detail"] = "unreachable"
        return result
    finally:
        connection.close()
    result["reachable"] = True
    result["status"] = status
    if len(raw) > 65536:
        result["detail"] = "health body too large"
        return result
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        result["detail"] = "health body is not JSON"
        return result
    contract = payload.get("contract") if isinstance(payload, dict) else None
    if not isinstance(contract, str):
        contract = None
    result["contract"] = contract
    result["ok"] = status == 200 and contract == CONTRACT
    result["detail"] = CONTRACT if result["ok"] else "contract missing or not memfw-http/2"
    return result


def _binds_wildcard(host: str, port: int) -> bool:
    """True when ``host`` would bind an unspecified address, or cannot be resolved.

    ``""``, ``"0"``, ``"0.0"`` and ``"000.0.0.0"`` all bind INADDR_ANY, so the
    host is resolved the way ``bind`` would and every address is checked.
    """

    try:
        infos = socket.getaddrinfo(
            host or None, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
        )
    except (OSError, UnicodeError):
        return True
    if not infos:
        return True
    for info in infos:
        address = str(info[4][0]).split("%", 1)[0]
        try:
            if ipaddress.ip_address(address).is_unspecified:
                return True
        except ValueError:
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="MemFW read-only HTTP service (memfw-http/2 and memfw-http/3)"
    )
    parser.add_argument("--root", default=os.environ.get(ROOT_ENV))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--related-db", default=None)
    parser.add_argument(
        "--clearance",
        default=None,
        help=(
            "privacy levels /search and /related may return, comma separated "
            f"(public, shared, local-private); falls back to {CLEARANCE_ENV}; "
            "unset means no privacy filtering"
        ),
    )
    parser.add_argument(
        "--check-url",
        default=None,
        help="GET /health at this URL, print the contract check as JSON, and exit",
    )
    args = parser.parse_args(argv)
    if args.check_url:
        checked = check_readonly_contract(args.check_url)
        sys.stdout.write(json.dumps(checked, sort_keys=True) + "\n")
        return 0 if checked["ok"] else 1
    clearance = args.clearance if args.clearance is not None else os.environ.get(CLEARANCE_ENV)
    if not read_path.clearance_is_valid(clearance):
        raise SystemExit(
            f"--clearance / {CLEARANCE_ENV} must list only public, shared, local-private"
        )
    host = str(args.host).strip()
    if _binds_wildcard(host, args.port):
        raise SystemExit(
            "--host must resolve to a specific address; wildcard binds are not supported"
        )
    root_value = str(args.root).strip() if args.root else ""
    if not root_value:
        raise SystemExit(f"--root or {ROOT_ENV} is required")
    try:
        read_value = read_token()
        write_value = write_token()
    except ServiceError as exc:
        raise SystemExit("credential file is unusable") from exc
    if read_value and write_value and _same_secret(read_value, write_value):
        raise SystemExit("read and write tokens must differ")
    if not read_value and not write_value:
        raise SystemExit(
            f"{READ_TOKEN_ENV}, {READ_TOKEN_FILE_ENV}, {WRITE_TOKEN_ENV} or "
            f"{WRITE_TOKEN_FILE_ENV} is required"
        )
    if args.clearance is not None:
        os.environ[CLEARANCE_ENV] = str(args.clearance)
    os.environ[ROOT_ENV] = str(Path(root_value).expanduser().resolve())
    if args.related_db:
        os.environ[RELATED_DB_ENV] = str(Path(args.related_db).expanduser().resolve())
    server = ThreadingHTTPServer((host, args.port), Handler)
    server.daemon_threads = True
    sys.stderr.write(
        f"memfw read-only service on {host}:{args.port} (contracts {', '.join(CONTRACTS)})\n"
    )
    sys.stderr.write(f"memfw read-only service: read path from {_library.__file__}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

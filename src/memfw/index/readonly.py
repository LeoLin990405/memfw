"""Read side of the vault: query rules, ``as_of`` and the FTS search path.

``search_vault`` is the one FTS read path behind HTTP ``/search``
(``scripts/memfw_readonly_service.py``), MCP ``memory_search`` ``mode=fts``
and the recall ``FtsProvider``. The input rules other entry points share
(``check_query_text``, ``normalise_as_of``, ``MAX_QUERY_CHARS``) and the
document helpers MCP uses (``read_metadata``, ``iso_string``,
``optional_text``, ``recorded_at_text``) live here too.

The module needs only the standard library and PyYAML at import time, so
the HTTP service can still run it from a copy beside the script, without
``memfw`` installed. ``memfw`` is imported lazily, only for a unicode61
index (shared query terms) and for ontology alias expansion; both degrade
when it is missing. Errors are ``ServiceError`` with an HTTP status and a
stable contract code.
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import quote

import yaml

#: The ``read_path.READ_PATH_VERSION`` this file needs.
REQUIRED_READ_PATH_VERSION = 2


def _check_read_path(module: Any, origin: str) -> Any:
    """Refuse a ``read_path`` older than this file (fail closed on a partial deploy)."""

    version = getattr(module, "READ_PATH_VERSION", 1)
    if not isinstance(version, int) or version < REQUIRED_READ_PATH_VERSION:
        raise ImportError(
            f"read_path.py at {origin} is version {version}, older than the "
            f"{REQUIRED_READ_PATH_VERSION} readonly.py needs (deploy the three files "
            "together); see docs/READONLY-SERVICE-UPGRADE.md"
        )
    return module


def _load_read_path() -> Any:
    """``memfw.index.read_path``, or the ``read_path.py`` beside this file.

    Imported as ``memfw.index.readonly`` the package module is used. Loaded by
    file (the HTTP script does this) the sibling file is loaded the same way,
    so neither ``memfw`` nor ``memfw.index`` is imported.
    """

    if __name__ == "memfw.index.readonly":
        from memfw.index import read_path as module

        return _check_read_path(module, str(module.__file__))
    path = Path(__file__).with_name("read_path.py")
    if not path.is_file():
        raise ImportError(
            f"read_path.py is missing: expected {path} beside {Path(__file__).name}; "
            "see docs/READONLY-SERVICE-UPGRADE.md"
        )
    spec = importlib.util.spec_from_file_location("memfw_read_path", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"read_path.py cannot be loaded from {path}")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return _check_read_path(loaded, str(path))


read_path = _load_read_path()


CONTRACT = "memfw-http/2"
MAX_QUERY_CHARS = 512
MAX_LIMIT = 50
MAX_TERMS = 16
_TERM = re.compile(r"\w+", re.UNICODE)
MAX_SUMMARY_CHARS = 500
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


class ServiceError(Exception):
    """An error with an HTTP status and a stable contract error code."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = ERROR_CODES.get(status, ERROR_CODES[500])
        self.message = message


def bad_request(message: str) -> ServiceError:
    return ServiceError(400, message)


def internal_error() -> ServiceError:
    return ServiceError(500, INTERNAL_MESSAGE)


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


#: Stdlib mirror of ``memfw.index._INDEX_SCHEMA_VERSION`` and ``_INDEX_TABLES``.
#: ``tests/test_fix_service_index_stale.py`` checks the two stay equal.
INDEX_SCHEMA_VERSION = 2
INDEX_TABLES = frozenset({"memory_rows", "memory_temporal", "memory_access", "memory_fts"})
INDEX_STALE = "index_stale"


def _index_is_current(connection: sqlite3.Connection) -> bool:
    """Read-only twin of ``MemoryIndex.schema_is_current``.

    An index from an earlier schema generation lacks the temporal or tenant
    projection; reading it would silently drop filtering or return nothing.
    The service never rebuilds it. Any SQLite error reads as stale.
    """

    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        }
    except (sqlite3.Error, TypeError, ValueError):
        return False
    return version == INDEX_SCHEMA_VERSION and INDEX_TABLES <= names


def has_table(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


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


class _DuplicateKey(ValueError):
    pass


class _UniqueKeyLoader(yaml.SafeLoader):
    """``SafeLoader`` that refuses a duplicate key at any depth, as core does."""


def _unique_mapping(
    loader: yaml.SafeLoader, node: yaml.nodes.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise _DuplicateKey(f"duplicate frontmatter key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def header_is_mapping(raw: str) -> bool:
    """True when ``raw`` opens with a frontmatter block that parses to a mapping.

    Anything else (no frontmatter, CRLF line ends, a YAML error, a duplicate
    key, a list or a scalar) means the privacy of the file cannot be read: an
    exit with a clearance treats it as ``unknown`` and never returns it (MM0).
    A duplicate ``privacy:`` or ``sensitivity:`` would otherwise be read
    last-wins by plain ``safe_load``; core refuses such a file outright.
    """

    if not raw.startswith("---\n"):
        return False
    try:
        _, header, _ = raw.split("---\n", 2)
        parsed = yaml.load(header, Loader=_UniqueKeyLoader)  # noqa: S506 - SafeLoader subclass
    except (ValueError, yaml.YAMLError):
        return False
    return isinstance(parsed, dict)


def metadata_privacy(raw: str, metadata: dict[str, Any], legacy: str) -> str:
    """The level of one memory file's text, ``unknown`` when its header is unreadable."""

    if not header_is_mapping(raw):
        return "unknown"
    return str(read_path.metadata_level(metadata, legacy))


def read_metadata(path: Path) -> dict[str, Any]:
    try:
        return _metadata_from_text(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return {}


def iso_string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def optional_text(value: Any) -> str | None:
    text = iso_string(value)
    return text or None


def recorded_at_text(metadata: dict[str, Any]) -> str | None:
    """When memfw wrote the record (server clock), for the result payload.

    Files from before ``recorded_at`` existed report ``created_at``: memfw
    stamped that from the same clock at write time. Neither present is None.
    An aware instant is reported in UTC as ``isoformat`` writes it, the same
    text MCP gives for the parsed document.
    """
    text = optional_text(metadata.get("recorded_at")) or optional_text(metadata.get("created_at"))
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return text
    return parsed.astimezone(UTC).isoformat()


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _collapsed(text: str, limit: int) -> str:
    return " ".join(text.split())[:limit]


def _same_instant(left: Any, right: Any) -> bool:
    """Reader-side instant compare. Supersession ids are not parsed as times."""
    if left == right:
        return True
    if left in (None, "") or right in (None, ""):
        return False
    try:
        left_stamp = (
            left
            if isinstance(left, datetime)
            else datetime.fromisoformat(str(left).replace("Z", "+00:00"))
        )
        right_stamp = (
            right
            if isinstance(right, datetime)
            else datetime.fromisoformat(str(right).replace("Z", "+00:00"))
        )
    except (TypeError, ValueError):
        return False
    return left_stamp == right_stamp


def fold_temporal_aliases(data: dict[str, Any], *, strict: bool = True) -> dict[str, Any]:
    """Stdlib copy of the alias fold. This module does not import memfw for it."""
    folded = dict(data)
    specs = (
        ("valid_until", "valid_to", "instant"),
        ("superseded", "superseded_by", "exact"),
    )
    for alias, canonical, kind in specs:
        if alias not in folded:
            continue
        alias_value = folded.pop(alias)
        if canonical not in folded:
            folded[canonical] = alias_value
            continue
        current = folded[canonical]
        same = _same_instant(current, alias_value) if kind == "instant" else current == alias_value
        if same:
            continue
        if strict:
            raise ValueError(f"{alias} conflicts with {canonical}")
    return folded


def vault_legacy_privacy(root: Path) -> str:
    """``legacy_privacy_default`` from ``<root>/vault.yaml``; unreadable → ``local-private``."""

    try:
        config = yaml.safe_load((root / "vault.yaml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        config = None
    return str(read_path.legacy_default_from_config(config))


def checked_clearance(clearance: Any) -> frozenset[str] | None:
    """A clearance argument: ``None`` (no filtering), a parsed set, or text to parse."""

    if clearance is None or isinstance(clearance, frozenset):
        return clearance
    parsed: frozenset[str] | None = read_path.parse_clearance(clearance)
    return parsed


def document_payload(
    root: Path, identifier: str, *, legacy_privacy: str | None = None
) -> dict[str, Any] | None:
    """Read one memory document's authoritative Markdown for the result payload.

    ``privacy`` is the level an exit filters on (MM0): the strictest of the
    ``privacy`` field (missing → ``legacy_privacy``, else the vault's
    ``legacy_privacy_default``) and the ``privacy:``/``sensitivity:`` tags.
    It is for filtering only; no result carries it.
    """
    if not identifier or "/" in identifier or ".." in identifier or "\\" in identifier:
        return None
    path = root / "memories" / f"{identifier}.md"
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    text = raw.decode("utf-8", errors="replace")
    metadata = fold_temporal_aliases(_metadata_from_text(text), strict=False)
    return {
        "tenant": str(metadata.get("tenant") or ""),
        "related": _string_list(metadata.get("related")),
        "valid_from": iso_string(metadata.get("valid_from")),
        "valid_to": iso_string(metadata.get("valid_to")),
        "superseded_by": iso_string(metadata.get("superseded_by")),
        # Read by MCP. HTTP ``/search`` gains it with memfw-http/3 (roadmap R6);
        # ``memfw-http/2`` result keys are fixed by the contract fixtures.
        "recorded_at": recorded_at_text(metadata),
        "content_hash": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "provenance": str(metadata.get("provenance") or ""),
        "version": metadata.get("version"),
        "privacy": metadata_privacy(
            text,
            metadata,
            legacy_privacy if legacy_privacy is not None else vault_legacy_privacy(root),
        ),
    }


def visible_related(
    root: Path,
    related: list[str],
    *,
    tenant: str,
    clearance: frozenset[str] | None,
    legacy_privacy: str,
    cache: dict[str, bool] | None = None,
) -> list[str]:
    """``related`` ids a caller with ``clearance`` may learn about (MM0).

    No clearance returns the list unchanged. Otherwise an id is kept only
    when it names a memory of ``tenant`` that the clearance admits; a missing
    or unreadable target is dropped too, so a withheld id is never listed.
    """

    if clearance is None:
        return related
    seen = cache if cache is not None else {}
    kept: list[str] = []
    for identifier in related:
        if identifier not in seen:
            payload = document_payload(root, identifier, legacy_privacy=legacy_privacy)
            seen[identifier] = (
                payload is not None
                and payload["tenant"] == tenant
                and bool(read_path.admits(clearance, payload["privacy"]))
            )
        if seen[identifier]:
            kept.append(identifier)
    return kept


# ---------------------------------------------------------------------------
# /search
# ---------------------------------------------------------------------------


def _fts_terms(query: str) -> list[str]:
    """Stdlib copy of the trigram rules in ``memfw.index.fts_terms.query_terms``."""

    seen: set[str] = set()
    out: list[str] = []
    for token in _TERM.findall(query):
        key = token.casefold()
        if len(token) < 2 or key in seen:
            continue
        seen.add(key)
        out.append(token)
        if len(out) == MAX_TERMS:
            break
    return out


def _shared_query_terms() -> tuple[Any, str | None]:
    """The shared unicode61 term builder, or ``None`` and why it is unavailable."""

    try:
        from memfw.index.fts_terms import jieba_segmenter, query_terms
    except ImportError:
        return None, "unicode61 query terms unavailable: memfw is not importable; regex terms used"
    try:
        jieba_segmenter()
    except RuntimeError:
        return None, "unicode61 query terms unavailable: jieba is not installed; regex terms used"
    return query_terms, None


def fts_query_terms(query: str, tokenizer: str = "trigram") -> list[str]:
    """Terms for an index built with ``tokenizer``.

    A trigram index keeps the stdlib path and needs no ``memfw`` import. A
    unicode61 index stores jieba segments, so the terms come from the shared
    ``memfw.index.fts_terms.query_terms``, the same function the library
    search uses. When that is unavailable the regex terms are used and
    ``search_vault`` reports it in ``errors`` with ``degraded`` true.
    """

    if tokenizer == "unicode61":
        builder, _note = _shared_query_terms()
        if builder is not None:
            terms: list[str] = builder(query, tokenizer)
            return terms
    return _fts_terms(query)


def fts_query(query: str, tokenizer: str = "trigram") -> str | None:
    """Quote each term and join them with OR. ``None`` means there is no term."""

    return quoted_or(fts_query_terms(query, tokenizer))


def quoted_or(terms: list[str]) -> str | None:
    if not terms:
        return None
    return " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)


def _split_short_terms(terms: list[str], tokenizer: str = "trigram") -> tuple[list[str], list[str]]:
    """Stdlib copy of ``memfw.index.fts_terms.split_short_terms``.

    A trigram index cannot match a term shorter than three code points, so
    such terms go to the short list. A unicode61 index keeps every term.
    """

    if tokenizer != "trigram":
        return list(terms), []
    return [term for term in terms if len(term) >= 3], [
        term for term in terms if len(term) < 3 and _useful_short_term(term)
    ]


#: Stdlib copy of ``memfw.index.fts_terms.SHORT_STOPWORDS``: common English
#: two-letter words, skipped as short terms when lowercase.
_SHORT_STOPWORDS = frozenset(
    {
        "am",
        "an",
        "as",
        "at",
        "be",
        "by",
        "do",
        "he",
        "if",
        "in",
        "is",
        "it",
        "me",
        "my",
        "no",
        "of",
        "on",
        "or",
        "so",
        "to",
        "up",
        "us",
        "we",
    }
)


def _useful_short_term(term: str) -> bool:
    """Keep CJK and other non-ASCII terms, acronyms (``AI``, ``Q3``) and any
    ASCII term that is not a lowercase English stopword."""

    if not term.isascii():
        return True
    if term.isupper() or any(character.isdigit() for character in term):
        return True
    return term.casefold() not in _SHORT_STOPWORDS


def _short_terms_clause(columns: list[str], terms: list[str]) -> tuple[str, list[str]]:
    """Stdlib copy of ``memfw.index.fts_terms.short_terms_clause``.

    ``columns`` are trusted identifiers chosen here, never user input.
    """

    parts: list[str] = []
    params: list[str] = []
    for term in terms:
        for column in columns:
            parts.append(f"instr(lower({column}), lower(?)) > 0")
            params.append(term)
    if not parts:
        return "0", []
    return "(" + " OR ".join(parts) + ")", params


def _index_tokenizer(connection: sqlite3.Connection) -> str:
    """``trigram`` or ``unicode61``, read from the ``memory_fts`` schema."""

    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_fts'"
    ).fetchone()
    if row is None or row[0] is None:
        return "trigram"
    return "trigram" if "trigram" in str(row[0]).lower() else "unicode61"


def unsupported_query(text: str) -> bool:
    """True for NUL or a lone surrogate, both of which used to become 500."""
    if "\x00" in text:
        return True
    return any(0xD800 <= ord(character) <= 0xDFFF for character in text)


def normalise_as_of(value: Any) -> str:
    if not isinstance(value, str):
        raise bad_request("as_of must be an ISO-8601 timestamp")
    raw = value.strip()
    if not raw:
        raise bad_request("as_of must be an ISO-8601 timestamp")
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise bad_request("as_of must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _temporal_clause(
    connection: sqlite3.Connection, include_history: bool, as_of: str | None
) -> tuple[str, list[Any]]:
    if include_history or not has_table(connection, "memory_temporal"):
        return "", []
    if as_of is not None:
        return (
            "EXISTS (SELECT 1 FROM memory_temporal t WHERE t.id = r.id "
            "AND (CASE WHEN t.valid_from != '' THEN t.valid_from ELSE r.created_at END) <= ? "
            "AND (t.valid_to = '' OR t.valid_to > ?))",
            [as_of, as_of],
        )
    return (
        "NOT EXISTS (SELECT 1 FROM memory_temporal t WHERE t.id = r.id "
        "AND (t.valid_to != '' OR t.superseded_by != ''))",
        [],
    )


_ROW_COLUMNS = (
    "r.id AS id, r.title AS title, r.summary AS summary, "
    "r.search_body AS search_body, r.source AS source, r.tier AS tier, "
    "r.layer AS layer, r.provenance AS provenance, r.version AS version"
)
_SUBSTRING_COLUMNS = ["r.title", "r.search_body", "r.summary", "r.tags"]


def _row_filters(
    *, source: str, tenant: str, temporal_sql: str, temporal_params: list[Any]
) -> tuple[list[str], list[Any]]:
    where = ["a.tenant = ?"]
    params: list[Any] = [tenant]
    if source:
        where.append("r.source = ?")
        params.append(source)
    if temporal_sql:
        where.append(temporal_sql)
        params.extend(temporal_params)
    return where, params


def _search_batch(
    connection: sqlite3.Connection,
    *,
    needle: str,
    source: str,
    tenant: str,
    temporal_sql: str,
    temporal_params: list[Any],
    limit: int,
    offset: int,
    tokenizer: str = "trigram",
) -> list[sqlite3.Row]:
    filters, filter_params = _row_filters(
        source=source, tenant=tenant, temporal_sql=temporal_sql, temporal_params=temporal_params
    )
    if len(needle) <= 2:
        # FTS drops tokens this short. Match every field, and fold ASCII case
        # so "AL" hits the same rows as "al". SQLite lower() covers that range.
        folded = needle.lower()
        select = (
            f"SELECT {_ROW_COLUMNS}, NULL AS raw_score FROM memory_rows r "
            "JOIN memory_access a ON a.id = r.id"
        )
        where = [
            "(instr(lower(r.title), ?) > 0 OR instr(lower(r.search_body), ?) > 0 "
            "OR instr(lower(r.summary), ?) > 0 OR instr(lower(r.tags), ?) > 0)",
            *filters,
        ]
        params: list[Any] = [folded, folded, folded, folded, *filter_params]
        sql = f"{select} WHERE " + " AND ".join(where) + " ORDER BY r.id LIMIT ? OFFSET ?"
        return list(connection.execute(sql, [*params, limit, offset]).fetchall())
    terms = fts_query_terms(needle, tokenizer)
    long_terms, short_terms = _split_short_terms(terms, tokenizer)
    match = quoted_or(long_terms)
    fts_select = (
        f"SELECT {_ROW_COLUMNS}, bm25(memory_fts) AS raw_score, 0 AS branch "
        "FROM memory_fts JOIN memory_rows r ON r.id = memory_fts.id "
        "JOIN memory_access a ON a.id = r.id WHERE "
        + " AND ".join(["memory_fts MATCH ?", *filters])
    )
    if not short_terms:
        if match is None:
            return []
        sql = fts_select + " ORDER BY raw_score, r.id LIMIT ? OFFSET ?"
        return list(connection.execute(sql, [match, *filter_params, limit, offset]).fetchall())
    # A trigram index cannot match a term under three characters, so those
    # terms are a substring OR over the same fields. FTS hits come first in
    # rank order, then the substring-only hits by id; one row appears once.
    clause, clause_params = _short_terms_clause(_SUBSTRING_COLUMNS, short_terms)
    substring_where = [clause, *filters]
    substring_params: list[Any] = [*clause_params, *filter_params]
    if match is not None:
        substring_where.append(
            "r.id NOT IN (SELECT memory_fts.id FROM memory_fts WHERE memory_fts MATCH ?)"
        )
        substring_params.append(match)
    substring_select = (
        f"SELECT {_ROW_COLUMNS}, NULL AS raw_score, 1 AS branch FROM memory_rows r "
        "JOIN memory_access a ON a.id = r.id WHERE " + " AND ".join(substring_where)
    )
    if match is None:
        sql = substring_select + " ORDER BY id LIMIT ? OFFSET ?"
        params = [*substring_params, limit, offset]
    else:
        sql = (
            f"{fts_select} UNION ALL {substring_select} "
            "ORDER BY branch, raw_score, id LIMIT ? OFFSET ?"
        )
        params = [match, *filter_params, *substring_params, limit, offset]
    return list(connection.execute(sql, params).fetchall())


def _score_from_raw(raw_score: Any, rank: int) -> tuple[float, float | None, bool]:
    """Return (score, raw_score, used_bm25) for one result."""
    if raw_score is None:
        return 1.0 / (1.0 + rank), None, False
    raw = float(raw_score)
    x = max(0.0, -raw)
    return x / (1.0 + x), raw, True


def _alias_expansion(
    base: Path,
    query: str,
    tenant: str,
    source: str,
    results: list[dict[str, Any]],
    limit: int,
    *,
    include_history: bool,
    as_of: str | None,
    scope: str = "",
    clearance: frozenset[str] | None = None,
    legacy_privacy: str | None = None,
    related_cache: dict[str, bool] | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Fuse alias hits into ``results``. No ``concepts/`` directory costs one stat.

    ``scope`` only selects scoped alias labels; it does not filter lexical hits.
    ``clearance`` drops alias hits the same way as lexical ones.
    """

    directory = base / "concepts"
    if directory.is_symlink() or not directory.is_dir():
        return [], None
    try:
        from memfw.ontology.expand import fuse_rows, matching_memories
        from memfw.ontology.model import OntologyError
    except ImportError:
        del results[limit:]
        return [], "alias expansion unavailable"
    try:
        hits, ambiguities = matching_memories(
            base,
            query,
            tenant=tenant,
            source=source,
            include_history=include_history,
            as_of=as_of,
            scope=scope,
        )
    except OntologyError as exc:
        # The detail can name a vault file; it goes to the operator log only.
        sys.stderr.write(f"memfw alias expansion failed: {exc}\n")
        del results[limit:]
        return [], "alias expansion unavailable: a concept file is invalid"
    alias_rows: list[dict[str, Any]] = []
    for memory_id, why in hits:
        payload = document_payload(base, memory_id, legacy_privacy=legacy_privacy)
        if payload is None or payload["tenant"] != tenant:
            continue
        if not read_path.admits(clearance, payload["privacy"]):
            continue
        meta = read_metadata(base / "memories" / f"{memory_id}.md")
        if source and str(meta.get("source") or "") != source:
            continue
        item = {
            "id": memory_id,
            "title": str(meta.get("title") or ""),
            "summary": _collapsed(str(meta.get("summary") or ""), MAX_SUMMARY_CHARS),
            "source": str(meta.get("source") or ""),
            "tier": str(meta.get("tier") or ""),
            "layer": str(meta.get("layer") or ""),
            "tenant": payload["tenant"],
            "related": visible_related(
                base,
                payload["related"],
                tenant=tenant,
                clearance=clearance,
                legacy_privacy=legacy_privacy or vault_legacy_privacy(base),
                cache=related_cache,
            ),
            "valid_from": optional_text(payload["valid_from"]),
            "valid_to": optional_text(payload["valid_to"]),
            "superseded_by": optional_text(payload["superseded_by"]),
            "score": 1.0,
            "raw_score": None,
            "content_hash": payload["content_hash"],
            # Present so MCP fts keeps this hit without parsing extra frontmatter
            # through MemoryDocument, which forbids unknown keys.
            "updated_at": iso_string(meta.get("updated_at")),
            "memory_type": meta.get("type"),
            "provenance": str(meta.get("provenance") or ""),
            "why": why,
        }
        read_path.annotate(
            item,
            provenance=str(payload.get("provenance") or ""),
            source=str(meta.get("source") or ""),
            index_version=payload.get("version"),
            sot_version=payload.get("version"),
            why=why,
        )
        alias_rows.append(item)
    fused = fuse_rows(results, alias_rows, limit=limit)
    results[:] = fused
    return ambiguities, None


def search_vault(
    root: Path | str,
    query: str,
    *,
    limit: int = 10,
    source: str = "",
    tenant: str = "",
    include_history: bool = False,
    as_of: str | None = None,
    scope: str = "",
    clearance: frozenset[str] | str | None = None,
) -> dict[str, Any]:
    """FTS search over the vault index; Markdown decides tenant and privacy.

    ``clearance`` (MM0) is the set of privacy levels the caller may see.
    ``None`` (the default) filters nothing, as before MM0. Otherwise a hit
    whose level (see ``document_payload``) is not in it is skipped before it
    is ranked into the page, like a hit of another tenant; a malformed text
    clearance becomes ``{"public"}``.
    """

    base = Path(root).expanduser().resolve()
    if not (base / "vault.yaml").is_file() or not (base / "index.sqlite3").is_file():
        raise internal_error()
    if not isinstance(query, str):
        raise bad_request("query must be 1..512 characters")
    needle = query.strip()
    if not needle or len(needle) > MAX_QUERY_CHARS:
        raise bad_request("query must be 1..512 characters")
    if unsupported_query(needle):
        raise bad_request("query contains an unsupported character")
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise bad_request("limit must be an integer")
    limit = max(1, min(limit, MAX_LIMIT))
    if not isinstance(source, str):
        raise bad_request("source must be a string")
    if not isinstance(tenant, str):
        raise bad_request("tenant must be a string")
    if scope is None:
        scope = ""
    if not isinstance(scope, str):
        raise bad_request("scope must be a string")
    if len(scope) > MAX_QUERY_CHARS:
        raise bad_request("scope too long")
    as_of_normalised = normalise_as_of(as_of) if as_of is not None else None
    if not isinstance(include_history, bool):
        raise bad_request("include_history must be a boolean")
    cleared = checked_clearance(clearance)
    legacy_privacy = vault_legacy_privacy(base)

    connection = _connect_index(base)
    try:
        if not _index_is_current(connection):
            # Never an empty page that looks like "no match": the caller must
            # rebuild the disposable index (``memfw index rebuild``).
            return {
                "contract": CONTRACT,
                "mode": "fts",
                "degraded": True,
                "errors": [INDEX_STALE],
                "results": [],
            }
        temporal_sql, temporal_params = _temporal_clause(
            connection, include_history, as_of_normalised
        )
        tokenizer = _index_tokenizer(connection)
        term_note = None
        if tokenizer == "unicode61" and len(needle) > 2:
            term_note = _shared_query_terms()[1]
        concepts = base / "concepts"
        pool = MAX_LIMIT if concepts.is_dir() and not concepts.is_symlink() else limit
        batch = pool
        offset = 0
        matched: list[sqlite3.Row] = []
        payloads: list[dict[str, Any]] = []
        while True:
            rows = _search_batch(
                connection,
                needle=needle,
                source=source,
                tenant=tenant,
                temporal_sql=temporal_sql,
                temporal_params=temporal_params,
                limit=batch,
                offset=offset,
                tokenizer=tokenizer,
            )
            if not rows:
                break
            offset += len(rows)
            for row in rows:
                payload = document_payload(base, str(row["id"]), legacy_privacy=legacy_privacy)
                if payload is None:
                    continue
                if payload["tenant"] != tenant:
                    continue
                if not read_path.admits(cleared, payload["privacy"]):
                    continue
                matched.append(row)
                payloads.append(payload)
                if len(matched) >= pool:
                    break
            if len(matched) >= pool or len(rows) < batch:
                break
    finally:
        connection.close()

    results = []
    related_cache: dict[str, bool] = {}
    previous_score: float | None = None
    for rank, (row, payload) in enumerate(zip(matched, payloads, strict=True)):
        score, raw_score, _used_bm25 = _score_from_raw(row["raw_score"], rank)
        if raw_score is None and previous_score is not None:
            # A substring hit ranked after bm25 hits never outscores them.
            score = min(score, previous_score)
        previous_score = score
        summary = _collapsed(
            str(row["summary"] or "") or str(row["search_body"] or ""), MAX_SUMMARY_CHARS
        )
        item = {
            "id": str(row["id"]),
            "title": str(row["title"]),
            "summary": summary,
            "source": str(row["source"]),
            "tier": str(row["tier"]),
            "layer": str(row["layer"]),
            "tenant": payload["tenant"],
            "related": visible_related(
                base,
                payload["related"],
                tenant=tenant,
                clearance=cleared,
                legacy_privacy=legacy_privacy,
                cache=related_cache,
            ),
            "valid_from": optional_text(payload["valid_from"]),
            "valid_to": optional_text(payload["valid_to"]),
            "superseded_by": optional_text(payload["superseded_by"]),
            "score": score,
            "raw_score": raw_score,
            "content_hash": payload["content_hash"],
        }
        read_path.annotate(
            item,
            provenance=str(payload.get("provenance") or row["provenance"] or ""),
            source=str(row["source"]),
            index_version=row["version"],
            sot_version=payload.get("version"),
            why="lexical",
        )
        results.append(item)
    ambiguities, alias_error = _alias_expansion(
        base,
        needle,
        tenant,
        source,
        results,
        limit,
        include_history=include_history,
        as_of=as_of_normalised,
        scope=scope.strip(),
        clearance=cleared,
        legacy_privacy=legacy_privacy,
        related_cache=related_cache,
    )
    errors: list[str] = []
    if term_note:
        errors.append(term_note)
    if alias_error:
        errors.append(alias_error)
    response: dict[str, Any] = {
        "contract": CONTRACT,
        "mode": "fts",
        "degraded": term_note is not None,
        "errors": errors,
        "results": results,
    }
    if ambiguities:
        response["ambiguity"] = ambiguities
    return response


def check_query_text(needle: str) -> None:
    """Raise the ``search_vault`` errors for a too-long or unsupported query.

    ``needle`` is the stripped query. Callers that do not go through
    ``search_vault`` (MCP ``mode=two_stage``) use this so both paths reject
    the same input with the same ``ServiceError``.
    """

    if len(needle) > MAX_QUERY_CHARS:
        raise bad_request(f"query must be 1..{MAX_QUERY_CHARS} characters")
    if unsupported_query(needle):
        raise bad_request("query contains an unsupported character")


def shared_readonly() -> ModuleType:
    """This module. Kept for callers of ``memfw.memory_hybrid.shared_readonly``."""

    return sys.modules[__name__]


# Deprecated: the names this code had in ``scripts/memfw_readonly_service.py``
# (``shared_readonly()`` returned that module). Kept for one release so
# callers of the old private names keep working; use the public names.
_bad_request = bad_request
_internal = internal_error
_unsupported_query = unsupported_query
_normalise_as_of = normalise_as_of
_metadata = read_metadata
_iso_string = iso_string
_optional = optional_text
_recorded_at = recorded_at_text
_document_payload = document_payload
_has_table = has_table
_query_terms = fts_query_terms
_quoted_or = quoted_or
_fts_query = fts_query

"""``memory_search`` below the interface (A3b): every mode, row building, freshness.

Moved from ``memfw.mcp.tools_search``: the MCP server parses and checks the
arguments, opens the vault (``Runtime.open_repository``) and calls
``Runtime.search``, which lands in ``memory_search`` here. Error texts that
may name vault paths go through the interface's ``client_message`` (the MCP
server passes its ``_client_message``), so the payload is byte-identical to
what the server built before.
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from memfw.core.errors import MemfwError
from memfw.core.models import MemoryDocument
from memfw.core.repository import Repository
from memfw.index import readonly
from memfw.memory_hybrid import MemoryHybridRetriever
from memfw.retrieval.multihop import search_multihop

if TYPE_CHECKING:
    from .runtime import Runtime

#: ``client_message(text, root)``: a message safe to return to the caller.
ClientMessage = Callable[[str, Path], str]


_FRONTMATTER_SCAN_BYTES = 64 * 1024


def _frontmatter_scalar(raw: bytes, key: str) -> str:
    """One top-level scalar from the frontmatter, by a prefix line scan.

    Like ``frontmatter_version`` this never calls the YAML parser, so the
    search path stays free of it. Quotes are stripped; null is empty.
    """

    if not raw.startswith(b"---\n"):
        return ""
    end = raw.find(b"\n---\n", 4)
    block = raw[4:] if end < 0 else raw[4:end]
    prefix = key.encode("ascii") + b":"
    for line in block.splitlines():
        if not line.startswith(prefix):
            continue
        value = line[len(prefix) :].strip()
        if len(value) >= 2 and value[0] in b"'\"" and value[-1:] == value[:1]:
            value = value[1:-1].strip()
        if value in {b"null", b"Null", b"NULL", b"~"}:
            return ""
        return value.decode("utf-8", "replace")
    return ""


def _recorded_at_of(path: Path) -> str | None:
    """``recorded_at`` of one memory file, as the shared HTTP reader reports it."""

    try:
        with path.open("rb") as handle:
            raw = handle.read(_FRONTMATTER_SCAN_BYTES)
    except OSError:
        return None
    stamps = {key: _frontmatter_scalar(raw, key) for key in ("recorded_at", "created_at")}
    result: str | None = readonly.recorded_at_text(stamps)
    return result


def _epistemic_labels_of(path: Path) -> tuple[str | None, str]:
    """``(epistemic, causal)`` from one memory file's frontmatter.

    The search fast path does not parse every hit through ``MemoryDocument``,
    so the two labels are read from the same frontmatter prefix scan. A missing
    ``epistemic`` stays ``None`` (old files had no such field); a missing
    ``causal`` means ``none`` because that is the field's default.
    """

    try:
        with path.open("rb") as handle:
            raw = handle.read(_FRONTMATTER_SCAN_BYTES)
    except OSError:
        return None, "none"
    return _frontmatter_scalar(raw, "epistemic") or None, (
        _frontmatter_scalar(raw, "causal") or "none"
    )


def _content_hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


_FRESHNESS_LIMIT = 10
_FRESHNESS_CACHE_CAP = 2048
FRESHNESS_READ_ATTEMPTS = 3
_StatKey = tuple[str, int, int, int, int]


def frontmatter_version(raw: bytes) -> str:
    """Top-level ``version:`` line from bytes already read for the hash.

    This is a prefix scan of the frontmatter block. It does not call the YAML
    parser. A missing or unreadable version is empty and must not be replaced
    with the index version. ``yaml.safe_dump`` writes ``version: 2``.
    """

    if not raw.startswith(b"---\n"):
        return ""
    end = raw.find(b"\n---\n", 4)
    if end < 0:
        return ""
    for line in raw[4:end].splitlines():
        if not line.startswith(b"version:"):
            continue
        value = line.split(b":", 1)[1].strip()
        if len(value) >= 2 and value[0] in b"'\"" and value[-1:] == value[:1]:
            value = value[1:-1].strip()
        if not value or value in {b"null", b"Null", b"NULL", b"~"}:
            return ""
        return value.decode("utf-8", "replace")
    return ""


def _stat_key(path: Path, stat: os.stat_result) -> _StatKey:
    return (str(path), stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


class RawHashCache:
    """LRU of raw-byte hashes keyed by path, inode, size, mtime, and ctime.

    A user edit cannot preserve ctime, so a file whose mtime was restored
    still misses. The cached value is the hash plus the version line from
    those same bytes. The stat key is taken before and after the read, and
    the pair is stored only when those keys match.
    """

    def __init__(self, cap: int) -> None:
        self._cap = cap
        self._items: dict[_StatKey, tuple[str, str]] = {}
        self._lock = threading.Lock()

    def lookup(self, path: Path) -> tuple[str, str, bool]:
        """Return ``(sha256-hash, version, consistent)``.

        A missing file is ``("", "", True)``. When the stat key still
        differs after every attempt, nothing is stored and the third value
        is false so the caller can fail closed.
        """

        for _attempt in range(FRESHNESS_READ_ATTEMPTS):
            try:
                before = path.stat()
            except OSError:
                return "", "", True
            key = _stat_key(path, before)
            with self._lock:
                found = self._items.get(key)
                if found is not None:
                    self._items.pop(key)
                    self._items[key] = found
                    return (*found, True)
            try:
                raw = path.read_bytes()
                after = path.stat()
            except OSError:
                return "", "", True
            # The key has to name the bytes just read. A mid-read replace
            # must not publish the old digest under the new stat key.
            if _stat_key(path, after) != key:
                continue
            stored = (
                "sha256:" + hashlib.sha256(raw).hexdigest(),
                frontmatter_version(raw),
            )
            with self._lock:
                self._items.pop(key, None)
                self._items[key] = stored
                while len(self._items) > self._cap:
                    self._items.pop(next(iter(self._items)))
            return (*stored, True)
        return "", "", False


RAW_HASH_CACHE = RawHashCache(_FRESHNESS_CACHE_CAP)


def _hash_matches(live: str, indexed: str) -> bool:
    if not live or not indexed:
        return True
    return live.removeprefix("sha256:") == indexed.removeprefix("sha256:")


def _annotate_hit(row: dict[str, Any], document: Any, item: dict[str, Any], *, why: str) -> None:
    """Stamp the shared read-path fields.

    The projection version is whatever the index stored. A missing projection
    version stays empty, so an unknown index cannot look like the live file.
    """

    if "index_version" in item and item["index_version"] is not None:
        index_version = item["index_version"]
    else:
        index_version = ""
    readonly.read_path.annotate(
        row,
        provenance=str(document.provenance or ""),
        source=str(document.source or ""),
        index_version=index_version,
        sot_version=document.version,
        why=why,
    )
    # Retrieval labels: an answer must be able to say "this is a hypothesis".
    row["epistemic"] = document.epistemic
    row["causal"] = document.causal
    projection_hash = str(item.get("projection_hash") or "")
    if projection_hash:
        row["projection_hash"] = projection_hash
        live = str(row.get("content_hash") or "").removeprefix("sha256:")
        if live != projection_hash:
            row["stale"] = True


def _index_row(
    repository: Repository, item: dict[str, Any], offset: int, *, why: str
) -> dict[str, Any]:
    """A ``memory_search`` row from an index item (one that has ``memory_type``).

    The first ``_FRESHNESS_LIMIT`` rows (by ``offset``) are checked against the
    live file and marked ``stale`` when it changed since it was indexed.
    """

    memory_path = repository.root / "memories" / f"{item['id']}.md"
    checked = offset < _FRESHNESS_LIMIT
    if checked:
        live_hash, live_version, consistent = RAW_HASH_CACHE.lookup(memory_path)
    else:
        live_hash, live_version, consistent = "", "", True
    index_version = item["index_version"] if item.get("index_version") is not None else ""
    indexed_hash = str(item.get("content_hash") or "")
    row = {
        "id": item["id"],
        "title": item["title"],
        "layer": item["layer"],
        "tier": item["tier"],
        "updated_at": item["updated_at"],
        "valid_from": item["valid_from"],
        "valid_to": item["valid_to"],
        "superseded_by": item["superseded_by"],
        # Alias and multihop items do not carry it: read the file.
        "recorded_at": item.get("recorded_at") or _recorded_at_of(memory_path),
        "type": item["memory_type"],
        "score": item["score"],
        "source": item["source"],
        # An inconsistent read has no digest. Do not fill in the
        # indexed hash, which would look like a verified live one.
        "content_hash": ("" if checked and not consistent else (live_hash or indexed_hash)),
    }
    # The two labels an answering agent must see. An old file has no
    # epistemic type; ``causal`` defaults to ``none``.
    epistemic_label, causal_label = _epistemic_labels_of(memory_path)
    row["epistemic"] = epistemic_label
    row["causal"] = causal_label
    readonly.read_path.annotate(
        row,
        provenance=str(item.get("provenance") or ""),
        source=str(item.get("source") or ""),
        index_version=index_version,
        # Past the first 10, the file is not read. Keep the index
        # version so a skipped check is not reported as stale.
        sot_version=live_version if checked else index_version,
        why=str(item.get("why") or why),
    )
    if checked:
        if not consistent:
            row["stale"] = True
        if indexed_hash and not _hash_matches(live_hash, indexed_hash):
            row["stale"] = True
        projection_hash = str(item.get("projection_hash") or "")
        if projection_hash:
            row["projection_hash"] = projection_hash
            if live_hash.removeprefix("sha256:") != projection_hash:
                row["stale"] = True
    return row


def _document_row(
    repository: Repository, document: MemoryDocument, item: dict[str, Any], *, why: str
) -> dict[str, Any]:
    """A ``memory_search`` row built from the vault document itself.

    ``item`` carries the score and, optionally, ``why``, ``content_hash``,
    ``index_version`` and ``projection_hash`` from the path that found it.
    """

    row = {
        "id": document.id,
        "title": document.title,
        "layer": document.layer.value,
        "tier": document.tier.value,
        "updated_at": document.updated_at.isoformat(),
        "valid_from": document.valid_from.isoformat() if document.valid_from else None,
        "valid_to": document.valid_to.isoformat() if document.valid_to else None,
        "superseded_by": document.superseded_by,
        "recorded_at": (document.recorded_at or document.created_at).isoformat(),
        "type": document.type,
        "score": item["score"],
        "source": document.source,
        "content_hash": item.get("content_hash")
        or _content_hash(repository.root / "memories" / f"{document.id}.md"),
    }
    _annotate_hit(row, document, item, why=str(item.get("why") or why))
    return row


_MAX_SCOPE_CHARS = 512


def checked_scope(scope: object) -> str:
    """Same rule as HTTP ``/search``: optional string of at most 512 characters."""
    if scope is None:
        return ""
    if not isinstance(scope, str):
        raise ValueError("scope must be a string")
    if len(scope) > _MAX_SCOPE_CHARS:
        raise ValueError("scope too long")
    return scope.strip()


def _alias_candidates(
    runtime: Runtime,
    repository: Repository,
    query: str,
    *,
    source: str,
    include_history: bool,
    as_of: str | None,
    scope: str = "",
    client_message: ClientMessage,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    """Alias rows for modes that do not go through ``search_vault``.

    The caller fuses these with the lexical page. A missing ``concepts/``
    directory costs one stat and adds nothing.
    """

    directory = repository.root / "concepts"
    if directory.is_symlink() or not directory.is_dir():
        return [], [], None
    try:
        from memfw.ontology.expand import matching_memories
        from memfw.ontology.model import OntologyError
    except ImportError:
        return [], [], "alias expansion unavailable"
    try:
        hits, ambiguities = matching_memories(
            repository.root,
            query,
            tenant=runtime.tenant,
            source=source,
            include_history=include_history,
            as_of=as_of,
            scope=scope,
        )
    except OntologyError as exc:
        return [], [], client_message(str(exc), repository.root)
    rows: list[dict[str, Any]] = []
    for memory_id, why in hits:
        path = repository.root / "memories" / f"{memory_id}.md"
        meta = readonly.read_metadata(path)
        if not meta:
            continue
        if str(meta.get("tenant") or "") != runtime.tenant:
            continue
        if source and str(meta.get("source") or "") != source:
            continue
        if runtime.allowed_sources and str(meta.get("source") or "") not in runtime.allowed_sources:
            continue
        version = meta.get("version")
        row = {
            "id": memory_id,
            "title": str(meta.get("title") or ""),
            "layer": str(meta.get("layer") or ""),
            "tier": str(meta.get("tier") or ""),
            "updated_at": readonly.iso_string(meta.get("updated_at")),
            "valid_from": readonly.optional_text(readonly.iso_string(meta.get("valid_from"))),
            "valid_to": readonly.optional_text(readonly.iso_string(meta.get("valid_to"))),
            "superseded_by": readonly.optional_text(readonly.iso_string(meta.get("superseded_by"))),
            "recorded_at": readonly.recorded_at_text(meta),
            "type": meta.get("type"),
            "score": 1.0,
            "source": str(meta.get("source") or ""),
            "content_hash": _content_hash(path),
            "why": why,
        }
        readonly.read_path.annotate(
            row,
            provenance=str(meta.get("provenance") or ""),
            source=str(meta.get("source") or ""),
            index_version=version,
            sot_version=version,
            why=why,
        )
        rows.append(row)
    return rows, ambiguities, None


def _retriever(runtime: Runtime, repository: Repository) -> MemoryHybridRetriever:
    # ``record_access`` off (session-safe, C6): no memory_access event, no warming.
    record = runtime.record_access()
    return MemoryHybridRetriever(
        repository,
        embedder=runtime.embedder,
        tenant=runtime.tenant,
        entity_enabled=runtime.entity_enabled,
        allowed_sources=runtime.allowed_sources,
        record_access=record,
        autowarm=record,
        # MM0: a hit the clearance withholds (``_cleared_rows`` drops its row)
        # is neither warmed nor access-logged, as in ``two_stage``. No
        # clearance: none, exactly as before.
        admit=None if runtime.clearance is None else runtime.privacy_admits,
        embedding_allow_remote=runtime.embedding_allow_remote(repository),
    )


def _corroborate(repository: Repository, rows: list[dict[str, Any]], *, app: str | None) -> None:
    """``labels=true``: the U5 ``corroboration`` field on every row (imported on first use)."""

    from .corroboration import rows_corroboration

    rows_corroboration(repository, rows, app=app)


def memory_search(
    runtime: Runtime,
    repository: Repository,
    query: str,
    limit: int,
    *,
    mode: str,
    source: str,
    include_history: bool,
    as_of: str | None,
    rounds: int | None,
    scope: str,
    policy: str,
    labels: bool,
    client_message: ClientMessage,
    app: str | None = None,
) -> dict[str, Any]:
    """One ``memory_search`` call on the caller's open vault.

    ``Runtime.search`` has checked the arguments (non-empty query, known
    ``policy``, only with ``two_stage``; ``scope`` cleaned; ``source``
    allowed) and opened ``repository``. ``labels`` adds the U5
    ``corroboration`` field to every row, in every mode, as ``app`` sees it.
    """

    if mode == "two_stage":
        payload = _search_two_stage(
            runtime,
            repository,
            query,
            limit,
            source=source,
            include_history=include_history,
            as_of=as_of,
            scope=scope,
            policy=policy,
            client_message=client_message,
        )
        if labels:
            _corroborate(repository, payload["results"], app=app)
        return payload
    concepts = repository.root / "concepts"
    expand = concepts.is_dir() and not concepts.is_symlink()
    if mode == "multihop":
        page = max(1, min(limit, 50))
        runtime.ensure_index(repository)
        parsed_as_of = None
        if as_of:
            parsed_as_of = datetime.fromisoformat(as_of)
            if parsed_as_of.tzinfo is None or parsed_as_of.utcoffset() is None:
                raise ValueError("as_of must include a timezone")
        result = search_multihop(
            repository,
            query,
            limit=50 if expand else page,
            tenant=runtime.tenant,
            source=source,
            allowed_sources=runtime.allowed_sources,
            include_history=include_history,
            as_of=parsed_as_of,
            rounds=rounds,
        )
    elif mode == "fts":
        page = max(1, min(int(limit), 50))
        runtime.ensure_index(repository)
        # With an allow-list and no explicit source, ask the shared reader
        # once per allowed source so the SQL filters by source before the
        # page cap: filtering one capped page afterwards could return
        # fewer than ``limit`` hits, or none, while allowed hits exist.
        sources = [source] if source or not runtime.allowed_sources else runtime.allowed_sources
        merged: list[dict[str, Any]] = []
        fts_errors: list[str] = []
        fts_ambiguity: list[Any] = []
        for each in dict.fromkeys(sources):
            payload = readonly.search_vault(
                repository.root,
                query,
                limit=page,
                source=each,
                tenant=runtime.tenant,
                include_history=include_history,
                as_of=as_of,
                scope=scope,
                clearance=runtime.clearance,
            )
            merged.extend(
                item
                for item in payload["results"]
                if not runtime.allowed_sources or item["source"] in runtime.allowed_sources
            )
            for message in payload.get("errors") or []:
                text = client_message(str(message), repository.root)
                if text not in fts_errors:
                    fts_errors.append(text)
            for entry in payload.get("ambiguity") or []:
                if entry not in fts_ambiguity:
                    fts_ambiguity.append(entry)
        if len(sources) > 1:
            merged.sort(key=lambda item: -float(item.get("score") or 0.0))
        result = {
            "mode": "fts",
            "degraded": False,
            "errors": fts_errors,
            "model_versions": {},
            "results": merged[:page],
        }
        if fts_ambiguity:
            result["ambiguity"] = fts_ambiguity
    else:
        page = max(0, min(limit, 50))
        retriever = _retriever(runtime, repository)
        result = retriever.search(
            query,
            limit=50 if expand and page else page,
            mode=mode,
            source=source,
            include_history=include_history,
            as_of=as_of,
            tenant=runtime.tenant,
        )
    why = "lexical" if result["mode"] == "fts" else "semantic"
    rows = []
    for offset, item in enumerate(result["results"]):
        if "memory_type" in item:
            rows.append(_index_row(repository, item, offset, why=why))
            continue
        document = repository.get(item["id"])
        rows.append(_document_row(repository, document, item, why=why))
    if mode == "fts":
        ambiguities = list(result.get("ambiguity") or [])
        alias_note = None
    else:
        alias_rows: list[dict[str, Any]] = []
        ambiguities = []
        alias_note = None
        if expand and page:
            alias_rows, ambiguities, alias_note = _alias_candidates(
                runtime,
                repository,
                query,
                source=source,
                include_history=include_history,
                as_of=as_of,
                scope=scope,
                client_message=client_message,
            )
        if alias_rows:
            from memfw.ontology.expand import fuse_rows

            rows = fuse_rows(rows, alias_rows, limit=page)
        elif len(rows) > page:
            rows = rows[:page]
    rows = _cleared_rows(runtime, repository, rows)
    if labels and rows:
        # Opt-in (U2): the Runtime computes them with the compat_fts policy
        # these modes filter by; without the flag rows are unchanged.
        found = runtime.open_question_labels(
            repository,
            [str(row["id"]) for row in rows],
            policy_name=policy,
            source=source,
            include_history=include_history,
            as_of=(
                datetime.fromisoformat(readonly.normalise_as_of(as_of))
                if as_of is not None
                else None
            ),
            scope=scope,
        )
        for row in rows:
            row["recall_labels"] = list(found.get(str(row["id"]), ()))
        _corroborate(repository, rows, app=app)
    errors = [client_message(str(item), repository.root) for item in result["errors"]]
    if alias_note:
        errors.append(alias_note)
    payload = {
        "mode": result["mode"],
        "degraded": result["degraded"],
        "errors": errors,
        "model_versions": result["model_versions"],
        "results": rows,
    }
    if ambiguities:
        payload["ambiguity"] = ambiguities
    if "route" in result:
        payload["route"] = result["route"]
    return payload


def _cleared_rows(
    runtime: Runtime, repository: Repository, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Drop rows the Runtime's privacy clearance does not admit (MM0).

    No clearance (the default) returns ``rows`` unchanged. Otherwise every row
    is checked against its Markdown file, whatever mode produced it, so a
    stale index or a provider cannot widen access; an unreadable memory is
    dropped. A page may then hold fewer than ``limit`` rows.
    """

    if runtime.clearance is None:
        return rows
    kept: list[dict[str, Any]] = []
    for row in rows:
        try:
            document = repository.get(str(row.get("id") or ""))
        except (MemfwError, OSError, ValueError):
            continue
        if runtime.privacy_admits(document):
            kept.append(row)
    return kept


def _search_two_stage(
    runtime: Runtime,
    repository: Repository,
    query: str,
    limit: int,
    *,
    source: str,
    include_history: bool,
    as_of: str | None,
    scope: str,
    policy: str,
    client_message: ClientMessage,
) -> dict[str, Any]:
    """``mode=two_stage``: recall through the coordinator, then build rows.

    Providers: FTS always, plus hybrid when an embedder is configured, plus
    the operator's ``runtime.recall.adapters`` (``Runtime.recall_adapters``;
    none by default). An adapter hit has no index row, so its row is built
    from the vault document. The coordinator resolves every candidate against the vault and filters
    it with ``policy`` before fusing, so a row is built only for a memory
    that passed the filter. Rows are then built by the same code as the
    other modes, from the provider's own index row when it returned one,
    so freshness (``stale``, ``index_version``, ``sot_version``,
    ``content_hash``) matches ``mode=fts``.

    Ontology alias expansion is not applied in this mode (yet):
    ``concepts/`` aliases do not add rows here. Provider exception
    messages never reach the payload; the coordinator keeps only fixed
    codes.
    """

    # The same input rules as ``search_vault`` (``mode=fts``), and the same
    # errors, before any provider runs.
    needle = query.strip()
    readonly.check_query_text(needle)
    page = max(1, min(int(limit), 50))
    # Like ``mode=fts``: a timestamp without a timezone is read as UTC.
    parsed_as_of = (
        datetime.fromisoformat(readonly.normalise_as_of(as_of)) if as_of is not None else None
    )
    # Before the coordinator starts its deadline: a rebuild may take a while.
    runtime.ensure_index(repository)
    outcome = runtime.recall(
        repository,
        query,
        limit=page,
        policy_name=policy,
        source=source,
        include_history=include_history,
        as_of=parsed_as_of,
        scope=scope,
    )
    result = outcome.result
    seen = outcome.observed
    rows: list[dict[str, Any]] = []
    for hit in result.hits:
        item: dict[str, Any] | None = None
        for name in hit.providers:
            found = seen.get(name, ({}, []))[0].get(hit.memory_id)
            if found is not None:
                item = found
                break
        why = "semantic" if item is None or "memory_type" not in item else "lexical"
        if item is not None and "memory_type" in item:
            # The policy already checked both; never widen on a race.
            if str(item.get("tenant") or "") != runtime.tenant:
                continue
            if runtime.allowed_sources and item.get("source") not in runtime.allowed_sources:
                continue
            row = _index_row(repository, item, len(rows), why=why)
        else:
            try:
                document = repository.get(hit.memory_id)
            except (MemfwError, OSError, ValueError):
                continue  # removed or unreadable since it was resolved
            if not runtime.visible(document):
                continue  # the policy already checked; never widen on a race
            base = dict(item or {})
            extra_why = hit.extra.get("why")
            if not base.get("why") and isinstance(extra_why, str) and extra_why:
                base["why"] = extra_why
            base["score"] = hit.score
            row = _document_row(repository, document, base, why=why)
        row["score"] = hit.score
        row["providers"] = list(hit.providers)
        row["recall_labels"] = list(hit.labels)
        rows.append(row)
    rows = _cleared_rows(runtime, repository, rows)
    errors: list[str] = []
    ambiguities: list[Any] = []
    payload_degraded = False
    for name in result.labels.providers:
        for raw in seen.get(name, ({}, []))[1]:
            payload_degraded = payload_degraded or bool(raw.get("degraded"))
            for message in raw.get("errors") or []:
                text = client_message(str(message), repository.root)
                if text not in errors:
                    errors.append(text)
            for entry in raw.get("ambiguity") or []:
                if entry not in ambiguities:
                    ambiguities.append(entry)
    for code in outcome.errors:
        # A post-filter warm or access event failed; the rows stand.
        payload_degraded = True
        if code not in errors:
            errors.append(code)
    labels = result.labels
    payload: dict[str, Any] = {
        "mode": "two_stage",
        "policy": policy,
        # The one flag to read: true when any provider or resolver
        # degraded, the call failed closed, or a provider reported a
        # degraded page (``recall`` says which).
        "degraded": bool(
            labels.degraded or labels.resolvers_degraded or labels.fail_closed or payload_degraded
        ),
        "errors": errors,
        "model_versions": {},
        "results": rows,
        "recall": labels.as_dict(),
        "recall_metrics": result.metrics.as_dict(),
    }
    if ambiguities:
        payload["ambiguity"] = ambiguities
    notes = note_rows(result.segments, clearance=runtime.clearance)
    if notes:
        # Only when a notes resolver answered: memory-only output is unchanged.
        payload["notes"] = notes
    return payload


def note_rows(
    segments: Sequence[Any], *, clearance: frozenset[str] | None = None
) -> list[dict[str, Any]]:
    """``two_stage`` rows for the note sections the coordinator returned.

    Kept apart from ``results`` (memories) and labelled ``source: notes``.
    ``title``, ``heading`` and ``content`` are what ``notes_search`` returns
    for the section; nothing else of the note is added. A Runtime privacy
    clearance (MM0) applies to them as to memory rows.
    """

    return [
        {
            "source": "notes",
            "ref": segment.ref,
            "path": segment.path,
            "title": segment.title,
            "heading": segment.anchor,
            "content": segment.snippet,
            "score": segment.score,
            "providers": list(segment.providers),
            "recall_labels": list(segment.labels),
        }
        for segment in segments
        if segment.source == "notes" and (clearance is None or segment.privacy in clearance)
    ]


__all__ = [
    "FRESHNESS_READ_ATTEMPTS",
    "RAW_HASH_CACHE",
    "ClientMessage",
    "RawHashCache",
    "checked_scope",
    "frontmatter_version",
    "memory_search",
    "note_rows",
]

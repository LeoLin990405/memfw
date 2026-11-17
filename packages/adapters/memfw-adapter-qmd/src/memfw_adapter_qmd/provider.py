"""QMD recall providers: memfw memory projections and Knowledge-Hub notes.

Two ``RecallProvider`` adapters, both default off, both talking to QMD only
through its MCP ``query`` tool (R6 design §5.1–5.2: one ``tools/call`` with
``searches`` = lex + vec, one collection, ``rerank`` off):

``qmd`` (:class:`QmdMemoryProvider`)
    Over a collection that indexes a ``memfw projection export`` tree
    (``<tree>/files``). Each result is mapped back to a memory id through the
    projection manifest (``documents[].path`` → id; see :mod:`.paths` for why
    the result path is never parsed directly). It returns ``memory_id``
    candidates; the coordinator resolves and filters them against the vault.
    Before that, fail closed: a result whose path is not in the manifest, or
    that two manifest paths map to, is dropped; so is every document the
    manifest does not mark ``privacy_class == "none"`` (``local-private`` and
    ``unknown`` are never returned, whatever the caller's clearance), whose
    ``tenant`` differs from the call's tenant, or that the manifest says is
    not current: ``lifecycle`` set and not ``active``, ``superseded_by`` or
    ``valid_to`` set, or ``valid_until`` at or before now (or unparsable).
    The vault resolver cannot see the meta-index's lifecycle fields, so this
    check is the only one for them; QMD therefore never contributes history
    (``include_retired`` / ``include_history`` results come from FTS).

``qmd-notes`` (:class:`QmdNotesProvider`)
    Over a collection that indexes a Knowledge-Hub tree that is also readable
    here (``notes_root``). Each result is mapped to the file it came from
    (handelized-name table of the tree), then to the section heading of its
    best-matching line, and returned as a ``path`` candidate
    ``kh:<path>#<heading>`` (``kh:<path>`` when the line has no heading
    above it). A note is returned only when its privacy level, read from the
    note file itself like memfw's notes reader (:func:`note_level`:
    ``yaml.safe_load`` + ``memfw.ports.privacy.metadata_level``), is in
    ``clearance`` (``public``/``shared`` only;
    ``local-private`` is refused in the settings). A note without a
    ``privacy`` field takes ``legacy_privacy`` (default ``local-private``, so
    nothing is returned until the operator says what unlabelled notes are).

Neither provider returns body text, titles or snippets from QMD: only the
reference, QMD's score and the fixed source label ``extra={"kind": ...}``.
``available()`` never touches the network. When QMD is down, slow or answers
off-protocol, ``search`` raises (``ProviderUnavailable`` or
:class:`~.mcp.QmdResponseError`), the coordinator marks the provider
degraded and the other providers (FTS) answer (compat framework T7).
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from ._ports import (
    MAX_REF_CHARS,
    MEMORY_ID_RE,
    PATH_REF_RE,
    PRIVACY_LEVELS,
    Availability,
    Candidate,
    ProviderUnavailable,
    RecallBudget,
    admits,
    metadata_level,
    strictest,
)
from .mcp import McpHttpClient, QmdResponseError, check_endpoint
from .paths import handelize, heading_at, note_ref, strip_collection

__all__ = [
    "DEFAULT_ENDPOINT",
    "MEMORY_NAME",
    "NOTES_NAME",
    "QMD_VERSION",
    "MemorySettings",
    "NotesSettings",
    "QmdMemoryProvider",
    "QmdNotesProvider",
    "memory_factory",
    "notes_factory",
]

#: The QMD release the contract fixtures were checked against.
QMD_VERSION = "2.5.3"
MEMORY_NAME = "qmd"
NOTES_NAME = "qmd-notes"
#: ``qmd mcp --http`` listens on ``localhost:8181`` unless ``--port`` is given.
DEFAULT_ENDPOINT = "http://localhost:8181/mcp"
SEARCH_TYPES = ("lex", "vec", "hyde")
MAX_QMD_LIMIT = 100
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_NOTE_BYTES = 4 * 1024 * 1024
MAX_NOTES = 200_000
#: Grantable to the notes provider; ``local-private`` never is.
NOTES_GRANTABLE = frozenset({"public", "shared"})
_COLLECTION_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- settings


def _bool(raw: Mapping[str, Any], key: str, default: bool) -> bool:
    value = raw.get(key, default)
    if type(value) is not bool:
        raise ValueError(f"{key} must be a boolean")
    return value


def _number(raw: Mapping[str, Any], key: str, default: float, *, low: float, high: float) -> float:
    value = raw.get(key, default)
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{key} must be a number in [{low}, {high}]")
    return float(value)


def _optional_int(raw: Mapping[str, Any], key: str, *, low: int, high: int) -> int | None:
    value = raw.get(key)
    if value is None:
        return None
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{key} must be an integer in [{low}, {high}] or null")
    return value


def _collection(raw: Mapping[str, Any], default: str) -> str:
    value = raw.get("collection", default)
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 64
        or not set(value) <= _COLLECTION_CHARS
        or value.startswith(".")
    ):
        raise ValueError("collection must be 1-64 of [A-Za-z0-9._-], not starting with '.'")
    return value


def _searches(raw: Mapping[str, Any]) -> tuple[str, ...]:
    value = raw.get("searches", ["lex", "vec"])
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= len(SEARCH_TYPES)
        or any(item not in SEARCH_TYPES for item in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError("searches must be a non-empty list of distinct lex/vec/hyde")
    return tuple(value)


def _path(raw: Mapping[str, Any], key: str) -> Path:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required (a path)")
    return Path(value).expanduser()


@dataclass(frozen=True)
class _Common:
    endpoint: str
    allow_remote: bool
    collection: str
    searches: tuple[str, ...]
    rerank: bool
    min_score: float
    fetch_factor: int

    @staticmethod
    def parse(raw: Mapping[str, Any], *, collection: str) -> dict[str, Any]:
        endpoint = raw.get("endpoint", DEFAULT_ENDPOINT)
        if not isinstance(endpoint, str):
            raise ValueError("endpoint must be a string")
        fetch = raw.get("fetch_factor", 2)
        if type(fetch) is not int or not 1 <= fetch <= 10:
            raise ValueError("fetch_factor must be an integer in [1, 10]")
        return {
            "endpoint": endpoint,
            "allow_remote": _bool(raw, "allow_remote", False),
            "collection": _collection(raw, collection),
            "searches": _searches(raw),
            "rerank": _bool(raw, "rerank", False),
            "min_score": _number(raw, "min_score", 0.0, low=0.0, high=1.0),
            "fetch_factor": fetch,
        }


_COMMON_KEYS = frozenset(
    {"endpoint", "allow_remote", "collection", "searches", "rerank", "min_score", "fetch_factor"}
)


#: Keys memfw's runtime recall loader (``runtime.recall.adapters`` in
#: ``vault.yaml``) adds to every factory call. QMD searches its own index and
#: embeds on its own, so both are accepted and ignored.
RUNTIME_INJECTED_KEYS = frozenset({"vault", "embedder"})


def _reject_unknown(raw: Mapping[str, Any], allowed: frozenset[str]) -> None:
    if not isinstance(raw, Mapping):
        raise ValueError("settings must be a mapping")
    unknown = sorted(
        str(key) for key in raw if key not in allowed and key not in RUNTIME_INJECTED_KEYS
    )
    if unknown:
        raise ValueError("unknown settings: " + ", ".join(unknown))


@dataclass(frozen=True)
class MemorySettings(_Common):
    """Settings of the ``qmd`` adapter (``AdaptersConfig`` ``"qmd": {...}``).

    ``manifest`` (required): a projection ``manifest.json`` or the generation
    directory holding it (a ``current`` symlink is fine; it is re-read when the
    file changes). ``max_manifest_age_seconds``: refuse a manifest whose
    ``generated_at_utc`` is older (default: no age limit).
    ``require_lifecycle_ok`` (default true): refuse a manifest whose
    ``lifecycle_source_status`` is not ``ok``.
    """

    manifest: Path = field(default=Path())
    max_manifest_age_seconds: int | None = None
    require_lifecycle_ok: bool = True

    KEYS = _COMMON_KEYS | {"manifest", "max_manifest_age_seconds", "require_lifecycle_ok"}

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> MemorySettings:
        _reject_unknown(raw, cls.KEYS)
        return cls(
            **_Common.parse(raw, collection="memfw-memory"),
            manifest=_path(raw, "manifest"),
            max_manifest_age_seconds=_optional_int(
                raw, "max_manifest_age_seconds", low=1, high=10 * 365 * 86400
            ),
            require_lifecycle_ok=_bool(raw, "require_lifecycle_ok", True),
        )


@dataclass(frozen=True)
class NotesSettings(_Common):
    """Settings of the ``qmd-notes`` adapter (``AdaptersConfig`` ``"qmd-notes": {...}``).

    ``notes_root`` (required): the Knowledge-Hub tree QMD indexes, readable
    here. ``clearance``: comma list of ``public``/``shared`` (default both).
    ``legacy_privacy``: level of a note without a ``privacy`` field
    (``public``, ``shared`` or ``local-private``; default ``local-private``).
    ``rescan_seconds``: how long the file-name table is reused (default 300).
    """

    notes_root: Path = field(default=Path())
    clearance: frozenset[str] = NOTES_GRANTABLE
    legacy_privacy: str = "local-private"
    rescan_seconds: float = 300.0

    KEYS = _COMMON_KEYS | {"notes_root", "clearance", "legacy_privacy", "rescan_seconds"}

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> NotesSettings:
        _reject_unknown(raw, cls.KEYS)
        clearance_raw = raw.get("clearance", "public,shared")
        if not isinstance(clearance_raw, str):
            raise ValueError("clearance must be a comma-separated string")
        clearance = frozenset(
            token.strip().lower() for token in clearance_raw.split(",") if token.strip()
        )
        if not clearance or not clearance <= NOTES_GRANTABLE:
            raise ValueError("clearance may list only public and shared")
        legacy = raw.get("legacy_privacy", "local-private")
        if not isinstance(legacy, str) or legacy.strip().lower() not in PRIVACY_LEVELS:
            raise ValueError("legacy_privacy must be public, shared or local-private")
        return cls(
            **_Common.parse(raw, collection="knowledge-hub"),
            notes_root=_path(raw, "notes_root"),
            clearance=clearance,
            legacy_privacy=legacy.strip().lower(),
            rescan_seconds=_number(raw, "rescan_seconds", 300.0, low=0.0, high=86400.0),
        )


# --------------------------------------------------------------------------- query


def _query_arguments(settings: _Common, query: str, limit: int) -> dict[str, Any]:
    """The ``query`` tool arguments (R6 §5.1)."""

    return {
        "searches": [{"type": kind, "query": query} for kind in settings.searches],
        "limit": limit,
        "minScore": settings.min_score,
        "candidateLimit": max(20, 4 * limit),
        "collections": [settings.collection],
        "rerank": settings.rerank,
    }


@dataclass(frozen=True)
class _Row:
    file: str
    score: float
    line: int | None


def _rows(result: Mapping[str, Any]) -> list[_Row]:
    """``structuredContent.results`` → ``(file, score, line)``; nothing else is read."""

    content = result.get("structuredContent")
    rows = content.get("results") if isinstance(content, dict) else None
    if not isinstance(rows, list):
        raise QmdResponseError("query result without structuredContent.results")
    out: list[_Row] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        file = row.get("file")
        score = row.get("score")
        line = row.get("line")
        if not isinstance(file, str) or type(score) not in (int, float):
            continue  # bool is not a score
        value = float(score)  # type: ignore[arg-type]
        if not math.isfinite(value):
            continue
        out.append(_Row(file, value, line if type(line) is int else None))
    return out


class _QmdProvider:
    """Shared transport and search loop; subclasses map rows to candidates."""

    name: str
    lexical = False
    kind = ""

    def __init__(self, settings: _Common, *, client: McpHttpClient | None = None) -> None:
        self.settings = settings
        self._endpoint_reason = check_endpoint(
            settings.endpoint, allow_remote=settings.allow_remote
        )
        self._client = client
        if self._client is None and not self._endpoint_reason:
            from . import __version__

            self._client = McpHttpClient(
                settings.endpoint,
                allow_remote=settings.allow_remote,
                client_version=__version__,
            )
        self.projection_version = ""

    def _available(self) -> Availability:
        if self._endpoint_reason:
            return Availability.missing(self._endpoint_reason)
        return Availability.ok()

    def search(
        self,
        query: str,
        *,
        limit: int,
        budget: RecallBudget,
        tenant: str,
        as_of: datetime | None,
    ) -> Sequence[Candidate]:
        del as_of  # QMD has no validity window; the coordinator filters
        if not query.strip() or limit < 1:
            return []
        availability = self.available()
        if not availability.available:
            raise ProviderUnavailable(availability.reason or "unavailable")
        assert self._client is not None
        deadline = time.monotonic() + budget.timeout_seconds
        asked = min(MAX_QMD_LIMIT, max(limit, limit * self.settings.fetch_factor))
        result = self._client.call_tool(
            "query", _query_arguments(self.settings, query, asked), deadline=deadline
        )
        out: list[Candidate] = []
        seen: set[str] = set()
        for row in _rows(result):
            mapped = self._map(row, tenant=tenant)
            if mapped is None or mapped[0] in seen:
                continue
            ref, ref_kind = mapped
            seen.add(ref)
            out.append(
                Candidate(
                    ref=ref,
                    ref_kind=ref_kind,  # type: ignore[arg-type]
                    score=row.score,
                    provider=self.name,
                    rank=len(out) + 1,
                    extra={"kind": self.kind},
                )
            )
            if len(out) >= limit:
                break
        return out

    def available(self) -> Availability:  # pragma: no cover - overridden
        return self._available()

    def _map(self, row: _Row, *, tenant: str) -> tuple[str, str] | None:  # pragma: no cover
        raise NotImplementedError


# --------------------------------------------------------------------------- memory


#: ``valid_until`` that is set but not a date/time: never current.
_UNPARSABLE = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True)
class _Entry:
    """What the adapter keeps of one manifest document (``documents[].meta``)."""

    memory_id: str
    privacy_class: str
    tenant: str
    #: False when ``lifecycle`` is set and not ``active``, ``superseded_by`` is
    #: set, or ``valid_to`` is set (the index's own rule for ``valid_to``).
    current: bool
    #: Parsed ``valid_until``; ``_UNPARSABLE`` when set but unreadable.
    valid_until: datetime | None


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _entry(memory_id: str, meta: Mapping[str, Any]) -> _Entry:
    privacy = meta.get("privacy_class")
    tenant = meta.get("tenant")
    lifecycle = meta.get("lifecycle")
    lifecycle_ok = _blank(lifecycle) or (
        isinstance(lifecycle, str) and lifecycle.strip().lower() == "active"
    )
    until_raw = meta.get("valid_until")
    until: datetime | None = None
    if not _blank(until_raw):
        until = _parse_time(until_raw) if isinstance(until_raw, str) else None
        until = until or _UNPARSABLE
    return _Entry(
        memory_id=memory_id,
        privacy_class=privacy if isinstance(privacy, str) else "unknown",
        tenant=tenant if isinstance(tenant, str) else "\0",  # a non-string tenant matches nothing
        current=lifecycle_ok and _blank(meta.get("superseded_by")) and _blank(meta.get("valid_to")),
        valid_until=until,
    )


@dataclass(frozen=True)
class _ManifestView:
    tree: str
    by_handle: Mapping[str, _Entry]
    ambiguous: frozenset[str]
    generated_at: datetime | None
    lifecycle_status: str


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _manifest_view(data: Any) -> _ManifestView:
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("bad_manifest")
    documents = data.get("documents")
    if not isinstance(documents, list):
        raise ValueError("bad_manifest")
    by_handle: dict[str, _Entry] = {}
    ambiguous: set[str] = set()
    for document in documents:
        if not isinstance(document, dict):
            continue
        memory_id = document.get("id")
        path = document.get("path")
        meta = document.get("meta")
        if (
            not isinstance(memory_id, str)
            or not MEMORY_ID_RE.fullmatch(memory_id)
            or path != f"memories/{memory_id}.md"
            or not isinstance(meta, dict)
        ):
            continue
        handle = handelize(path)
        if handle is None:
            continue
        if handle in by_handle:  # two documents, one stored name: neither is trusted
            ambiguous.add(handle)
        by_handle[handle] = _entry(memory_id, meta)
    tree = data.get("tree")
    status = data.get("lifecycle_source_status")
    return _ManifestView(
        tree=tree if isinstance(tree, str) else "",
        by_handle=by_handle,
        ambiguous=frozenset(ambiguous),
        generated_at=_parse_time(data.get("generated_at_utc")),
        lifecycle_status=status if isinstance(status, str) else "",
    )


class _ManifestCache:
    """The manifest, re-read when the file behind the configured path changes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._key: tuple[str, int, int, int] | None = None
        self._view: _ManifestView | None = None
        self._error = "no_manifest"

    def _file(self) -> Path:
        return self.path / "manifest.json" if self.path.is_dir() else self.path

    def get(self) -> tuple[_ManifestView | None, str]:
        with self._lock:
            try:
                file = self._file().resolve(strict=True)
                info = file.stat()
            except (OSError, RuntimeError):
                self._key, self._view, self._error = None, None, "no_manifest"
                return None, self._error
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MANIFEST_BYTES:
                self._key, self._view, self._error = None, None, "bad_manifest"
                return None, self._error
            key = (str(file), info.st_mtime_ns, info.st_size, info.st_ino)
            if key != self._key:
                try:
                    data = json.loads(file.read_bytes())
                    self._view, self._error = _manifest_view(data), ""
                except (OSError, ValueError, RecursionError):
                    self._view, self._error = None, "bad_manifest"
                self._key = key
            return self._view, self._error


class QmdMemoryProvider(_QmdProvider):
    """``qmd``: memfw memory projections in QMD → ``memory_id`` candidates."""

    kind = "qmd-memory"

    def __init__(
        self,
        settings: MemorySettings,
        *,
        client: McpHttpClient | None = None,
        clock: Clock = _utc_now,
    ) -> None:
        super().__init__(settings, client=client)
        self.name = MEMORY_NAME
        self.settings: MemorySettings = settings
        self._manifest = _ManifestCache(settings.manifest)
        self._clock = clock
        self._view: _ManifestView | None = None

    def _current(self) -> tuple[_ManifestView | None, str]:
        view, error = self._manifest.get()
        if view is None:
            return None, error
        if self.settings.require_lifecycle_ok and view.lifecycle_status != "ok":
            return None, "manifest_lifecycle_not_ok"
        limit = self.settings.max_manifest_age_seconds
        if limit is not None:
            if view.generated_at is None:
                return None, "manifest_stale"
            if (self._clock() - view.generated_at).total_seconds() > limit:
                return None, "manifest_stale"
        return view, ""

    def available(self) -> Availability:
        base = self._available()
        if not base.available:
            return base
        view, error = self._current()
        if view is None:
            return Availability.missing(error)
        self._view = view
        self.projection_version = view.tree if _version_ok(view.tree) else ""
        return Availability.ok()

    def _map(self, row: _Row, *, tenant: str) -> tuple[str, str] | None:
        view = self._view
        if view is None:
            return None
        relative = strip_collection(row.file, self.settings.collection)
        if relative is None or relative in view.ambiguous:
            return None
        entry = view.by_handle.get(relative)
        if entry is None:
            return None
        if entry.privacy_class != "none" or entry.tenant != tenant or not entry.current:
            return None
        if entry.valid_until is not None and entry.valid_until <= self._clock():
            return None  # expired, or an unparsable valid_until
        return entry.memory_id, "memory_id"


def _version_ok(value: str) -> bool:
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:+-")
    return 1 <= len(value) <= 64 and set(value) <= allowed


# --------------------------------------------------------------------------- notes


def _within(root: Path, path: Path) -> bool:
    """``path`` is a regular file under ``root`` reached without any symlink."""

    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    current = root
    for part in relative.parts:
        current = current / part
        try:
            info = os.lstat(current)
        except OSError:
            return False
        if stat.S_ISLNK(info.st_mode):
            return False
    return stat.S_ISREG(info.st_mode)


def _scan_notes(root: Path) -> tuple[dict[str, str], frozenset[str]]:
    """``handelize(rel) -> rel`` for every visible ``*.md`` under ``root``.

    Mirrors QMD's default collection walk: ``**/*.md``, no hidden files or
    directories, symlinks not followed. Names two files share go to the
    ambiguous set.
    """

    table: dict[str, str] = {}
    ambiguous: set[str] = set()
    count = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(
            name
            for name in dirs
            if not name.startswith(".") and not os.path.islink(os.path.join(directory, name))
        )
        for name in sorted(files):
            if name.startswith(".") or not name.endswith(".md"):
                continue
            full = os.path.join(directory, name)
            if os.path.islink(full):
                continue
            rel = Path(full).relative_to(root).as_posix()
            handle = handelize(rel)
            if handle is None:
                continue
            if handle in table and table[handle] != rel:
                ambiguous.add(handle)
            table[handle] = rel
            count += 1
            if count >= MAX_NOTES:
                return table, frozenset(ambiguous)
    return table, frozenset(ambiguous)


#: Lines of a note without frontmatter searched for a stray privacy key.
_HEAD_LINES = 20
_PRIVACY_HINT = re.compile(r"^\s*[-*>#]*\s*[\"']?(privacy|sensitivity)[\"']?\s*:", re.IGNORECASE)
#: Keys whose values decide a note's privacy level.
_TAG_KEYS = ("tags", "tag")


def _tags_readable(value: Any) -> bool:
    """A ``tags``/``tag`` value ``metadata_level`` can read: absent, or a list of scalars."""

    if value is None:
        return True
    if not isinstance(value, list):
        return False
    return all(not isinstance(item, (list, dict, set)) for item in value)


def note_level(text: str, legacy: str) -> tuple[str, int]:
    """A note's privacy level and the 0-based index of its first body line.

    The frontmatter is parsed with ``yaml.safe_load``, the same YAML reading as
    memfw's notes index (``memfw.notes.index._split_frontmatter``), and the
    level is ``memfw.ports.privacy.metadata_level`` of that mapping, so every
    YAML form (explicit ``?`` keys, merge keys, anchors, escaped or tagged
    keys) means here what it means to the kernel. On top of that, whatever the
    kernel would read some other way is ``unknown`` (no clearance admits it):

    - a YAML error, a frontmatter that is not a mapping, or ``tags``/``tag``
      that are not a list of scalars;
    - a ``---`` block that never closes, or that does not start on line 1;
    - a ``privacy:``/``sensitivity:`` line among the first ``_HEAD_LINES``
      lines of a note without frontmatter.

    ``tag`` adds its levels to ``tags`` (it can only make a note stricter). A
    note starting with a byte-order mark gets the stricter of its frontmatter's
    level and the level without frontmatter (the kernel's notes reader does not
    strip the mark).
    """

    bom = text.startswith("\ufeff")
    lines = text.removeprefix("\ufeff").splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        head = lines[:_HEAD_LINES]
        first = next((line.strip() for line in head if line.strip()), "")
        if first == "---" or any(_PRIVACY_HINT.match(line) for line in head):
            return "unknown", 0
        return metadata_level({}, legacy), 0
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return "unknown", 0  # an unterminated block: unreadable
    try:
        loaded = yaml.safe_load("".join(lines[1:end]))
    except (yaml.YAMLError, RecursionError, ValueError, TypeError):
        return "unknown", end + 1
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        return "unknown", end + 1
    if not all(_tags_readable(loaded.get(key)) for key in _TAG_KEYS):
        return "unknown", end + 1
    merged = dict(loaded)
    merged["tags"] = [*(loaded.get("tags") or []), *(loaded.get("tag") or [])]
    try:
        level = metadata_level(merged, legacy)
    except Exception:  # noqa: BLE001 - an odd value must not widen access
        return "unknown", end + 1
    if bom:
        level = strictest([level, metadata_level({}, legacy)])
    return level, end + 1


class QmdNotesProvider(_QmdProvider):
    """``qmd-notes``: Knowledge-Hub notes in QMD → ``kh:<path>#<heading>`` candidates."""

    kind = "qmd-note"

    def __init__(
        self,
        settings: NotesSettings,
        *,
        client: McpHttpClient | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(settings, client=client)
        self.name = NOTES_NAME
        self.settings: NotesSettings = settings
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._table: tuple[float, dict[str, str], frozenset[str]] | None = None

    def available(self) -> Availability:
        base = self._available()
        if not base.available:
            return base
        root = self.settings.notes_root
        try:
            info = os.lstat(root)
        except OSError:
            return Availability.missing("no_notes_root")
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return Availability.missing("no_notes_root")
        return Availability.ok()

    def _names(self) -> tuple[dict[str, str], frozenset[str]]:
        with self._lock:
            now = self._monotonic()
            if self._table is None or now - self._table[0] > self.settings.rescan_seconds:
                table, ambiguous = _scan_notes(self.settings.notes_root)
                self._table = (now, table, ambiguous)
            return self._table[1], self._table[2]

    def _map(self, row: _Row, *, tenant: str) -> tuple[str, str] | None:
        if tenant:  # Knowledge-Hub notes carry no tenant; only the shared tenant sees them
            return None
        relative = strip_collection(row.file, self.settings.collection)
        if relative is None:
            return None
        table, ambiguous = self._names()
        if relative in ambiguous or relative not in table:
            return None
        rel = table[relative]
        root = self.settings.notes_root
        path = root / rel
        if not _within(root, path):
            return None
        try:
            if path.stat().st_size > MAX_NOTE_BYTES:
                return None
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        level, body_start = note_level(text, self.settings.legacy_privacy)
        lines = text.removeprefix("\ufeff").splitlines(keepends=True)
        if not admits(self.settings.clearance, level):
            return None
        heading = heading_at(lines, row.line, body_start=body_start) if row.line else ""
        try:
            ref = note_ref(rel, heading)
        except ValueError:
            return None
        if len(ref) > MAX_REF_CHARS or not PATH_REF_RE.fullmatch(ref):
            ref = note_ref(rel)  # an over-long heading: the note alone
            if len(ref) > MAX_REF_CHARS or not PATH_REF_RE.fullmatch(ref):
                return None
        return ref, "path"


# --------------------------------------------------------------------------- factories


def memory_factory(settings: Mapping[str, Any]) -> QmdMemoryProvider:
    """Factory for ``qmd``: ``AdapterSpec`` / ``AdaptersConfig``, or the runtime's
    ``module:factory`` loader (``memfw_adapter_qmd:memory_factory``; its injected
    ``vault``/``embedder`` keys are ignored). Settings never come from a request."""

    return QmdMemoryProvider(MemorySettings.from_mapping(settings))


def notes_factory(settings: Mapping[str, Any]) -> QmdNotesProvider:
    """Factory for ``qmd-notes`` (same two entry routes as :func:`memory_factory`).

    Its ``kh:`` path refs resolve to note sections through memfw's
    ``NotesSegmentResolver`` when the coordinator registers it (not by default);
    otherwise they are dropped as unresolved."""

    return QmdNotesProvider(NotesSettings.from_mapping(settings))

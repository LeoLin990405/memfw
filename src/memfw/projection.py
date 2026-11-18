"""Recall projection: memfw memories as plain files for an external recall backend.

memfw couples to a recall backend (the first one is QMD) only through two
backend-neutral contracts, both implemented here and documented in
``docs/qmd-projection.md``:

* **files out** — :func:`export_projection` writes one Markdown file per
  projectable memory plus a ``manifest.json`` into a new generation directory.
  The backend indexes those files with its own tooling; nothing here imports,
  vendors or calls backend code.
* **ids back** — :func:`resolve` takes the hit paths (or ids) a backend
  returned and answers with the authoritative memfw metadata for each one:
  lifecycle, validity, supersession, privacy and tenant. :func:`filter_resolved`
  applies the fail-closed filter matrix to that answer.

:func:`join_meta` is the single id ↔ path join shared by both directions: a
vault document carries a ``fleet_path:<rel>`` tag, and the lifecycle source
(``status/memory-meta-index.json``) is keyed by that ``rel``.

Extractability: this module depends only on the memfw-core document model
(:mod:`memfw.core.models`, :mod:`memfw.core.errors`) and the narrow parser
:func:`memfw.core.markdown_parse.parse_memory_markdown` — never on the
repository, storage, index or any backend — so it can move into its own
package later.

Every function here is read-only on the vault. The export writes only under its
``out`` directory, which may not be inside the vault.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat as stat_mode
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

import yaml

from .core.errors import IntegrityError, MemfwError
from .core.markdown_parse import parse_memory_markdown
from .core.models import MemoryDocument

MANIFEST_SCHEMA_VERSION = 1
POLICY_SCHEMA_VERSION = 1
RESOLVE_SCHEMA = "memfw-resolve-v1"
EXPORT_SCHEMA = "memfw-projection-export-v1"
AUTHORITY_CONTENT_DOMAIN = "memfw-authority-content-v1"
MAX_RESOLVE_BATCH = 50
DEFAULT_MAX_LINES = 200
MAX_STDIN_BYTES = 1024 * 1024
MAX_HIT_LINE_CHARS = 4096
MAX_ECHO_CHARS = 200
RESOLVE_DOCUMENT_KEYS = (
    "id",
    "path",
    "fleet_path",
    "join_status",
    "join_reason",
    "title",
    "lifecycle",
    "superseded_by",
    "valid_to",
    "valid_until",
    "privacy_class",
    "privacy_raw",
    "privacy_source_mismatch",
    "tenant",
    "scope",
    "status",
    "layer",
    "tier",
    "is_stub",
    "type",
    "epistemic",
    "causal",
    "updated_at",
    "tags",
    "content_sha256",
    "body",
    "body_truncated",
    "hit",
)

JoinStatus = Literal["ok", "unjoined", "id_mismatch"]
PrivacyClass = Literal["none", "local-private", "unknown"]
LifecycleStatus = Literal["ok", "stale", "unavailable"]

_IMPORTED_ID_RE = re.compile(r"^f[0-9a-f]{12}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_CLEARANCE_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

FLEET_PATH_PREFIX = "fleet_path:"
SENSITIVITY_PREFIX = "sensitivity:"
PRIVATE_TAG = "sensitivity:private"
_PRIVACY_NONE_VALUES = frozenset({"", "none", "public", "fleet", "shared"})
_PRIVACY_STRICTNESS: dict[str, int] = {"none": 0, "local-private": 1, "unknown": 2}

DEFAULT_TENANTS: frozenset[str] = frozenset({"", "fleet"})
DEFAULT_CLEARANCE: frozenset[str] = frozenset({"none"})
#: Ranking hints the filter attaches; provisional, tuned by evaluation (§5.4).
ANCHOR_RANK_BONUS = 0.05
DISPUTED_RANK_PENALTY = -0.2

_EPISTEMIC_ZH = {"experience": "经验", "fact": "事实", "judgment": "判断", "inference": "推断"}
_CAUSAL_ZH = {"none": "none", "association": "相关", "hypothesis": "假设", "verified": "已验证"}


class ProjectionError(MemfwError):
    """A projection input is unreadable or invalid; callers must fail closed."""


# --------------------------------------------------------------------------- policy


@dataclass(frozen=True)
class ChangeEventMarker:
    tag_prefix: str = "fleet_path:feedback-change-event-"
    type: str = "change-event"


@dataclass(frozen=True)
class ProjectionPolicy:
    """The projection policy file (§3.7). Version 1 is the default."""

    projection_policy_version: int = 1
    include_change_events: bool = False
    change_event_marker: ChangeEventMarker = field(default_factory=ChangeEventMarker)
    exclude_tiers: tuple[str, ...] = ("cold",)
    exclude_stubbed: bool = True
    exclude_lifecycle: tuple[str, ...] = ("retired",)
    lifecycle_source: str = "status/memory-meta-index.json"
    lifecycle_max_lag_seconds: int = 600
    join_key: str = "fleet_path"
    tenant_map: str = "identity"
    label_line_version: int = 1
    authority: str = "files"
    #: Privacy classes not projected at all (opt-in; default projects every class
    #: and relies on the backend being reachable only through memfw).
    skip_privacy: tuple[str, ...] = ()

    @property
    def includes_epistemic(self) -> bool:
        return self.projection_policy_version >= 2

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": POLICY_SCHEMA_VERSION,
            "projection_policy_version": self.projection_policy_version,
            "include_change_events": self.include_change_events,
            "change_event_marker": {
                "tag_prefix": self.change_event_marker.tag_prefix,
                "type": self.change_event_marker.type,
            },
            "exclude_tiers": list(self.exclude_tiers),
            "exclude_stubbed": self.exclude_stubbed,
            "exclude_lifecycle": list(self.exclude_lifecycle),
            "lifecycle_source": self.lifecycle_source,
            "lifecycle_max_lag_seconds": self.lifecycle_max_lag_seconds,
            "join_key": self.join_key,
            "tenant_map": self.tenant_map,
            "label_line_version": self.label_line_version,
            "authority": self.authority,
            "skip_privacy": list(self.skip_privacy),
        }

    @property
    def sha256(self) -> str:
        return _sha256(_canonical(self.to_json()))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> ProjectionPolicy:
        known = set(cls().to_json())
        unknown = sorted(set(raw) - known)
        if unknown:
            raise ProjectionError(f"unknown projection policy keys: {', '.join(unknown)}")
        if raw.get("schema_version", POLICY_SCHEMA_VERSION) != POLICY_SCHEMA_VERSION:
            raise ProjectionError("projection policy schema_version must be 1")
        version = raw.get("projection_policy_version", 1)
        if version not in (1, 2):
            raise ProjectionError("projection_policy_version must be 1 or 2")
        if raw.get("join_key", "fleet_path") != "fleet_path":
            raise ProjectionError("join_key must be fleet_path")
        if raw.get("tenant_map", "identity") != "identity":
            raise ProjectionError("tenant_map must be identity")
        if raw.get("label_line_version", 1) != 1:
            raise ProjectionError("label_line_version must be 1")
        marker_raw = raw.get("change_event_marker", {})
        if not isinstance(marker_raw, Mapping) or set(marker_raw) - {"tag_prefix", "type"}:
            raise ProjectionError("change_event_marker must be {tag_prefix, type}")
        marker = ChangeEventMarker(
            tag_prefix=str(marker_raw.get("tag_prefix", ChangeEventMarker.tag_prefix)),
            type=str(marker_raw.get("type", ChangeEventMarker.type)),
        )
        lag = raw.get("lifecycle_max_lag_seconds", 600)
        if not isinstance(lag, int) or isinstance(lag, bool) or lag < 0:
            raise ProjectionError("lifecycle_max_lag_seconds must be a non-negative integer")
        for key in ("include_change_events", "exclude_stubbed"):
            if key in raw and not isinstance(raw[key], bool):
                raise ProjectionError(f"{key} must be a boolean")
        return cls(
            projection_policy_version=int(version),
            include_change_events=bool(raw.get("include_change_events", False)),
            change_event_marker=marker,
            exclude_tiers=_str_tuple(raw.get("exclude_tiers", ["cold"]), "exclude_tiers"),
            exclude_stubbed=bool(raw.get("exclude_stubbed", True)),
            exclude_lifecycle=_str_tuple(
                raw.get("exclude_lifecycle", ["retired"]), "exclude_lifecycle"
            ),
            lifecycle_source=str(raw.get("lifecycle_source", "status/memory-meta-index.json")),
            lifecycle_max_lag_seconds=lag,
            authority=str(raw.get("authority", "files")),
            skip_privacy=_privacy_classes(raw.get("skip_privacy", [])),
        )


def _privacy_classes(value: Any) -> tuple[str, ...]:
    classes = _str_tuple(value, "skip_privacy")
    bad = sorted(set(classes) - set(_PRIVACY_STRICTNESS))
    if bad:
        raise ProjectionError(f"skip_privacy: unknown privacy class: {', '.join(bad)}")
    return tuple(sorted(set(classes)))


def _str_tuple(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ProjectionError(f"{name} must be a list of strings")
    return tuple(value)


def load_policy(
    path: Path | None,
    *,
    version: int | None = None,
    skip_privacy: Sequence[str] | None = None,
) -> ProjectionPolicy:
    """Read a policy file, or return the built-in v1 policy.

    ``version`` overrides ``projection_policy_version`` (the CLI's
    ``--policy-version``); v2 adds epistemic/causal to the projection.
    """

    if path is None:
        policy = ProjectionPolicy()
    else:
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ProjectionError(f"projection policy unreadable: {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ProjectionError("projection policy must be a JSON object")
        policy = ProjectionPolicy.from_mapping(raw)
    if skip_privacy is not None:
        data = policy.to_json()
        data["skip_privacy"] = list(skip_privacy)
        policy = ProjectionPolicy.from_mapping(data)
    if version is not None:
        if version not in (1, 2):
            raise ProjectionError("projection_policy_version must be 1 or 2")
        data = policy.to_json()
        data["projection_policy_version"] = version
        policy = ProjectionPolicy.from_mapping(data)
    return policy


# ----------------------------------------------------------------------- meta-index


@dataclass(frozen=True)
class MetaIndex:
    """The lifecycle source: ``{generated_at, root, stats, items[]}`` keyed by path."""

    generated_at: str | None
    items: Mapping[str, Mapping[str, Any]]
    duplicate_paths: frozenset[str]
    items_sha256: str

    def lookup(self, rel: str) -> Mapping[str, Any] | None:
        if rel in self.duplicate_paths:
            return None
        return self.items.get(rel)


def parse_meta_index(raw: Any) -> MetaIndex:
    if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
        raise ProjectionError("lifecycle source must be an object with an items list")
    items: dict[str, Mapping[str, Any]] = {}
    duplicates: set[str] = set()
    for item in raw["items"]:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ProjectionError("every lifecycle source item needs a string path")
        path = item["path"]
        if path in items:
            duplicates.add(path)
        items[path] = item
    ordered = sorted(raw["items"], key=lambda item: (item["path"], _canonical(item)))
    generated = raw.get("generated_at")
    return MetaIndex(
        generated_at=generated if isinstance(generated, str) else None,
        items=items,
        duplicate_paths=frozenset(duplicates),
        items_sha256=_sha256(_canonical(ordered)),
    )


def load_meta_index(path: Path | None) -> MetaIndex | None:
    """Read the lifecycle source. ``None`` path → ``None`` (status ``unavailable``).

    An unreadable or malformed file given explicitly raises :class:`ProjectionError`.
    """

    if path is None:
        return None
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProjectionError(f"lifecycle source unreadable: {path}: {exc}") from exc
    return parse_meta_index(raw)


#: The lifecycle meta-index read when none is named (relative to the vault),
#: shared by recall (``memfw.recall.builtin``) and ``memfw dream``.
DEFAULT_LIFECYCLE_SOURCE = Path("status") / "memory-meta-index.json"


@dataclass(frozen=True)
class Lifecycle:
    """The lifecycle meta-index for one reader (``meta=None``: none present).

    ``unreadable`` means a meta-index should be there but cannot be used;
    every document with a ``fleet_path`` tag then fails closed.
    """

    meta: MetaIndex | None = None
    unreadable: bool = False


#: Parsed meta-indexes by path, each with the ``(device, inode, mtime_ns,
#: ctime_ns, size)`` it was read at (``ctime`` catches an in-place rewrite
#: whose mtime was restored, as ``cp -p``, ``rsync -t`` or ``tar x`` do).
#: A small bound: one Runtime reads one or two files.
_LIFECYCLE_CACHE: dict[str, tuple[tuple[int, int, int, int, int], MetaIndex]] = {}
_LIFECYCLE_CACHE_MAX = 8
_LIFECYCLE_LOCK = threading.Lock()


def _cached_meta_index(path: Path, info: os.stat_result) -> MetaIndex:
    """``load_meta_index(path)``, re-read only when the file's stat key changes."""

    key = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size)
    name = str(path)
    with _LIFECYCLE_LOCK:
        cached = _LIFECYCLE_CACHE.get(name)
    if cached is not None and cached[0] == key:
        return cached[1]
    loaded = load_meta_index(path)
    if loaded is None:  # pragma: no cover - a path was given
        raise ProjectionError("lifecycle source unavailable")
    # Shared by every caller until the file changes: read-only views, so no
    # caller can change what another one reads.
    meta = replace(
        loaded,
        items=MappingProxyType(
            {rel: MappingProxyType(dict(item)) for rel, item in loaded.items.items()}
        ),
    )
    with _LIFECYCLE_LOCK:
        if name not in _LIFECYCLE_CACHE and len(_LIFECYCLE_CACHE) >= _LIFECYCLE_CACHE_MAX:
            _LIFECYCLE_CACHE.pop(next(iter(_LIFECYCLE_CACHE)))
        _LIFECYCLE_CACHE[name] = (key, meta)
    return meta


def load_lifecycle(root: Path | str, source: Path | str | None = None) -> Lifecycle:
    """The lifecycle meta-index: ``source``, else ``<root>/status/memory-meta-index.json``.

    The one reader for every caller (recall's ``VaultIdResolver``, the
    Runtime's context and open-question filters, ``memfw dream``), so they
    all treat the same file the same way:

    - no ``source`` and no default file: ``Lifecycle()`` (the vault alone
      decides, as without a meta-index);
    - a default file that is a symlink, a directory or anything but a regular
      file: ``unreadable``;
    - a named ``source`` (``~`` expanded; symlinks followed, the operator
      named it) that is missing or not a regular file: ``unreadable``;
    - a file that cannot be read or parsed: ``unreadable``.

    A parsed file is kept and reused while its device, inode, modification
    and change times and size stay the same, so a long-running server reads
    it once per change rather than once per call. The cached items are
    read-only mappings.
    """

    try:
        if source is None:
            path = Path(root) / DEFAULT_LIFECYCLE_SOURCE
            try:
                info = path.lstat()
            except FileNotFoundError:
                return Lifecycle()
            if stat_mode.S_ISLNK(info.st_mode) or not stat_mode.S_ISREG(info.st_mode):
                return Lifecycle(unreadable=True)
        else:
            path = Path(source).expanduser()
            info = path.stat()
            if not stat_mode.S_ISREG(info.st_mode):
                return Lifecycle(unreadable=True)
        return Lifecycle(meta=_cached_meta_index(path, info))
    except (OSError, ProjectionError):
        return Lifecycle(unreadable=True)


# ----------------------------------------------------------------------------- join


def stable_id(rel: str) -> str:
    """The id an imported document gets from its ``files/`` relative path."""

    return "f" + hashlib.sha256(rel.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class JoinResult:
    status: JoinStatus
    fleet_path: str | None
    item: Mapping[str, Any] | None
    reason: str


def fleet_paths(tags: Iterable[str]) -> list[str]:
    return [tag[len(FLEET_PATH_PREFIX) :] for tag in tags if tag.startswith(FLEET_PATH_PREFIX)]


def join_meta(memory_id: str, tags: Sequence[str], meta: MetaIndex | None) -> JoinResult:
    """Join one vault document to its lifecycle-source item (§3.8).

    1. exactly one ``fleet_path:<rel>`` tag, else ``unjoined``;
    2. an ``f`` + 12 hex id must equal :func:`stable_id` of ``rel``, else
       ``id_mismatch`` (other id shapes are not checked);
    3. ``rel`` must be an exact, unique ``items[].path``, else ``unjoined``.
    """

    paths = fleet_paths(tags)
    if not paths:
        return JoinResult("unjoined", None, None, "no fleet_path tag")
    if len(paths) > 1:
        return JoinResult("unjoined", None, None, "multiple fleet_path tags")
    rel = paths[0]
    if _IMPORTED_ID_RE.fullmatch(memory_id) and memory_id != stable_id(rel):
        return JoinResult("id_mismatch", rel, None, "id != f+sha256(fleet_path)[:12]")
    if meta is None:
        return JoinResult("unjoined", rel, None, "lifecycle source unavailable")
    if rel in meta.duplicate_paths:
        return JoinResult("unjoined", rel, None, "duplicate lifecycle source path")
    item = meta.lookup(rel)
    if item is None:
        return JoinResult("unjoined", rel, None, "fleet_path not in lifecycle source")
    return JoinResult("ok", rel, item, "ok")


def tag_privacy_class(tags: Iterable[str]) -> PrivacyClass:
    """Privacy from vault ``sensitivity:*`` tags (fail closed).

    No tag → ``none``; only ``sensitivity:private`` (any case) → ``local-private``;
    any other ``sensitivity:`` value (``secret``, ``local-private``, …) → ``unknown``.
    """

    values = [
        tag[len(SENSITIVITY_PREFIX) :].strip().lower()
        for tag in tags
        if tag.lower().startswith(SENSITIVITY_PREFIX)
    ]
    if not values:
        return "none"
    if all(value == "private" for value in values):
        return "local-private"
    return "unknown"


def classify_privacy(
    raw: Any, *, tagged_private: bool = False, tag_class: PrivacyClass | None = None
) -> tuple[PrivacyClass, bool]:
    """Return ``(privacy_class, source_mismatch)``; disagreement takes the stricter.

    ``tag_class`` (from :func:`tag_privacy_class`) wins over ``tagged_private``.
    """

    meta_class: PrivacyClass
    if raw is None or (isinstance(raw, str) and raw.strip().lower() in _PRIVACY_NONE_VALUES):
        meta_class = "none"
    elif isinstance(raw, str) and raw.strip().lower() == "local-private":
        meta_class = "local-private"
    else:
        meta_class = "unknown"
    if tag_class is None:
        tag_class = "local-private" if tagged_private else "none"
    mismatch = (meta_class == "none") != (tag_class == "none")
    if _PRIVACY_STRICTNESS[tag_class] > _PRIVACY_STRICTNESS[meta_class]:
        return tag_class, mismatch
    return meta_class, mismatch


def hit_to_id(hit: str) -> str | None:
    """Map one backend hit to a memfw id, or ``None`` when it is not resolvable.

    Accepts a bare id, ``memories/<id>.md`` with any directory or ``scheme://``
    prefix (``fleet-memory/memories/<id>.md``, ``qmd://fleet-memory/…``), and an
    ``index search`` line ``<id>\\t<title>``. ``files/`` paths are not ids: map
    them with :func:`stable_id` after checking the file exists.
    """

    text = hit.strip()
    if "\0" in text or len(text) > MAX_HIT_LINE_CHARS:
        return None
    if "\t" in text:
        text = text.split("\t", 1)[0].strip()
    if not text:
        return None
    if _ID_RE.fullmatch(text):
        return text
    text = _SCHEME_RE.sub("", text).split("?", 1)[0].split("#", 1)[0]
    parts = [part for part in text.replace("\\", "/").split("/") if part]
    if len(parts) >= 2 and parts[-2] == "memories" and parts[-1].endswith(".md"):
        stem = parts[-1][: -len(".md")]
        if _ID_RE.fullmatch(stem):
            return stem
    return None


# ---------------------------------------------------------------------- vault read


@dataclass(frozen=True)
class VaultEntry:
    memory_id: str
    raw: bytes
    document: MemoryDocument | None
    error: str | None = None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.raw).hexdigest()


def _check_vault(root: Path) -> Path:
    root = Path(root).expanduser()
    if not root.is_dir():
        raise ProjectionError(f"vault root unreadable: {root}")
    if not (root / "events.jsonl").exists() and not (root / "vault.yaml").exists():
        raise ProjectionError(f"not a memfw vault: {root}")
    if (root / ".pending.json").exists() or (root / ".event-append.pending.json").exists():
        raise ProjectionError("vault has a pending transaction; run `memfw recover` first")
    return root


def _memory_files(root: Path) -> list[Path]:
    memories = root / "memories"
    if not memories.is_dir():
        return []
    try:
        return sorted(path for path in memories.glob("*.md") if path.is_file())
    except OSError as exc:
        raise ProjectionError(f"vault memories unreadable: {exc}") from exc


def read_entry(path: Path) -> VaultEntry:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ProjectionError(f"memory unreadable: {path.name}: {exc}") from exc
    try:
        document = parse_memory_markdown(raw.decode("utf-8"), expected_id=path.stem)
    except (IntegrityError, UnicodeDecodeError, ValueError) as exc:
        return VaultEntry(path.stem, raw, None, str(exc).splitlines()[0][:200])
    return VaultEntry(path.stem, raw, document)


def authority_head(root: Path) -> str | None:
    """The event-log head hash (observational only: reads also append events)."""

    state = root / "events.state.json"
    try:
        if state.exists():
            value = json.loads(state.read_text(encoding="utf-8")).get("last_hash")
            return value if isinstance(value, str) else None
        head = root / "events.head"
        if head.exists():
            return head.read_text(encoding="ascii").strip() or None
    except (OSError, ValueError):
        return None
    return None


def authority_content_head(files: Sequence[tuple[str, bytes]], meta: MetaIndex | None) -> str:
    """Content head over every vault Markdown file and the lifecycle-source items.

    ``sha256`` of a domain line, one ``<id>\\t<sha256(bytes)>`` line per file
    sorted by id, and ``meta\\t<sha256(canonical sorted items)>``. Every input
    of the joined metadata is covered, ``generated_at`` is not (a rebuild with
    the same items keeps the head), and the event log is not (reads append).
    """

    digest = hashlib.sha256()
    digest.update(f"{AUTHORITY_CONTENT_DOMAIN}\n".encode())
    for memory_id, raw in sorted(files, key=lambda pair: pair[0]):
        digest.update(f"{memory_id}\t{hashlib.sha256(raw).hexdigest()}\n".encode())
    digest.update(f"meta\t{meta.items_sha256 if meta else 'unavailable'}\n".encode())
    return digest.hexdigest()


def _parse_instant(value: Any) -> datetime | None:
    """ISO-8601 instant or date → aware UTC datetime; ``None`` when unparsable."""

    if isinstance(value, datetime):
        stamp = value
    elif isinstance(value, date):
        stamp = datetime(value.year, value.month, value.day)
    elif isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            stamp = datetime.fromisoformat(text)
        except ValueError:
            try:
                parsed = date.fromisoformat(text)
            except ValueError:
                return None
            stamp = datetime(parsed.year, parsed.month, parsed.day)
    else:
        return None
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC)


def latest_change(entries: Iterable[VaultEntry]) -> tuple[datetime | None, bool]:
    """Newest parsed ``updated_at`` and whether every file parsed.

    A file that does not parse has no trustworthy ``updated_at``; callers treat
    an incomplete scan as ``stale`` (fail closed) rather than skip it.
    """

    latest: datetime | None = None
    complete = True
    for entry in entries:
        if entry.document is None:
            complete = False
            continue
        stamp = entry.document.updated_at
        if latest is None or stamp > latest:
            latest = stamp
    return latest, complete


def lifecycle_source_state(
    meta: MetaIndex | None,
    latest_change: datetime | None,
    max_lag_seconds: int,
    *,
    complete: bool = True,
) -> tuple[LifecycleStatus, int | None]:
    """``ok`` / ``stale`` / ``unavailable`` and the lag in seconds (§3.5).

    Stale = the source was generated more than ``max_lag_seconds`` before the
    newest vault change, its ``generated_at`` is missing/unparsable, or some
    vault file could not be parsed (``complete=False``).
    """

    if meta is None:
        return "unavailable", None
    generated = _parse_instant(meta.generated_at)
    if generated is None or not complete:
        return "stale", None
    if latest_change is None:
        return "ok", 0
    lag = max(0, int((latest_change - generated).total_seconds()))
    return ("stale" if lag > max_lag_seconds else "ok"), lag


# -------------------------------------------------------------------- joined record


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _json_scalar(value: Any) -> Any:
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, date):
        return value.isoformat()
    return value


def label_tags(tags: Iterable[str]) -> list[str]:
    """Tags without the join and sensitivity markers (those have their own fields)."""

    return [
        tag
        for tag in tags
        if not tag.startswith(FLEET_PATH_PREFIX) and not tag.lower().startswith(SENSITIVITY_PREFIX)
    ]


def is_change_event(document: MemoryDocument, policy: ProjectionPolicy) -> bool:
    marker = policy.change_event_marker
    return document.type == marker.type or any(
        tag.startswith(marker.tag_prefix) for tag in document.tags
    )


def joined_record(
    document: MemoryDocument, join: JoinResult, policy: ProjectionPolicy
) -> dict[str, Any]:
    """The authoritative metadata of one joined document (no body)."""

    item = join.item if join.status == "ok" else None
    privacy_class: PrivacyClass | None = None
    mismatch = False
    if item is not None:
        privacy_class, mismatch = classify_privacy(
            item.get("privacy"), tag_class=tag_privacy_class(document.tags)
        )
    meta_superseded = item.get("superseded_by") if item is not None else None
    return {
        "id": document.id,
        "path": f"memories/{document.id}.md",
        "fleet_path": join.fleet_path,
        "join_status": join.status,
        "join_reason": join.reason,
        "title": document.title,
        "lifecycle": item.get("lifecycle") if item is not None else None,
        "superseded_by": document.superseded_by or meta_superseded or None,
        "valid_to": _iso(document.valid_to),
        "valid_until": _json_scalar(item.get("valid_until")) if item is not None else None,
        "privacy_class": privacy_class,
        "privacy_raw": item.get("privacy") if item is not None else None,
        "privacy_source_mismatch": mismatch,
        "tenant": document.tenant,
        "scope": item.get("scope") if item is not None else None,
        "status": document.status,
        "layer": document.layer.value,
        "tier": document.tier.value,
        "is_stub": document.is_stub,
        "type": document.type,
        "epistemic": document.epistemic if policy.includes_epistemic else None,
        "causal": document.causal if policy.includes_epistemic else None,
        "updated_at": _iso(document.updated_at),
        "tags": label_tags(document.tags),
    }


# ------------------------------------------------------------------------ rendering


def _one_line(value: str) -> str:
    return " ".join(value.split())


def _label_value(values: Sequence[str]) -> str:
    cleaned = [_one_line(value).replace("]", ")").replace("|", "/") for value in values if value]
    return ",".join(cleaned) if cleaned else "none"


def render_projection(document: MemoryDocument, fleet_path: str, policy: ProjectionPolicy) -> str:
    """One projection file (§3.3): frontmatter, two stable label lines, H1, sections.

    Lifecycle, privacy, validity, tenant and verification state are never in
    the body text; they live in the frontmatter (not used for filtering) and
    the manifest / ``resolve`` answer.
    """

    v2 = policy.includes_epistemic
    change_event = is_change_event(document, policy)
    memory_type = policy.change_event_marker.type if change_event else document.type
    frontmatter: dict[str, Any] = {
        "id": document.id,
        "fleet_path": fleet_path,
        "title": _one_line(document.title),
    }
    if v2:
        frontmatter["epistemic"] = document.epistemic
        frontmatter["causal"] = document.causal
    frontmatter.update(
        {
            "status": document.status,
            "layer": document.layer.value,
            "tier": document.tier.value,
            "valid_to": _iso(document.valid_to),
            "superseded_by": document.superseded_by,
            "tenant": document.tenant,
            "type": memory_type,
            "projection_policy_version": policy.projection_policy_version,
        }
    )
    epistemic: str
    epistemic_zh: str
    if v2 and document.epistemic:
        epistemic, epistemic_zh = document.epistemic, _EPISTEMIC_ZH[document.epistemic]
    else:
        epistemic, epistemic_zh = "unspecified", "未声明"
    causal = document.causal if v2 else "none"
    label_one = (
        f"[memfw 认知={epistemic_zh} epistemic={epistemic} | "
        f"因果={_CAUSAL_ZH.get(causal, causal)} causal={causal} | "
        f"type={_label_value([memory_type] if memory_type else [])}]"
    )
    label_two = (
        f"[memfw tags={_label_value(label_tags(document.tags))} | "
        f"about={_label_value(document.about)} | "
        f"applies_to={_label_value(document.applies_to)} | "
        f"anchor={_label_value([document.anchor] if document.anchor else [])}]"
    )
    parts = [
        "---\n" + yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False) + "---\n",
        f"<!-- memfw-stable-labels v{policy.label_line_version}; "
        "供 BM25 召回，不参与过滤 -->\n" + label_one + "\n" + label_two + "\n",
        f"# {_one_line(document.title)}\n",
    ]
    if document.summary.strip():
        parts.append("## Summary\n" + document.summary.strip() + "\n")
    parts.append("## Body\n" + document.body.strip() + "\n")
    if v2 and document.causal in {"hypothesis", "verified"} and document.causal_record:
        parts.append(_causal_section(document))
    return "\n".join(parts)


def _causal_section(document: MemoryDocument) -> str:
    record = document.causal_record
    assert record is not None
    ident = record.identification
    identification = "; ".join(
        piece
        for piece in (
            ("声明了: " + ", ".join(ident.assumptions)) if ident.assumptions else "",
            ("未检验: " + ", ".join(ident.untested)) if ident.untested else "",
        )
        if piece
    )
    robustness = (
        ", ".join(
            f"{check.check}={'failed' if check.failed else 'ok'}" for check in record.robustness
        )
        or "未做"
    )
    lines = [
        "## Causal claim",
        f"- treatment: {_one_line(record.treatment or 'unspecified')}",
        f"- outcome: {_one_line(record.outcome or 'unspecified')}",
        f"- estimand: {_one_line(record.estimand or 'unspecified')}",
        f"- identification: {identification or '未声明'}",
        f"- robustness: {robustness}",
    ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- export


def _sha256(text: str | bytes) -> str:
    data = text.encode("utf-8") if isinstance(text, str) else text
    return hashlib.sha256(data).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _skip_reason(
    document: MemoryDocument, record: Mapping[str, Any], policy: ProjectionPolicy
) -> str | None:
    """First matching exclusion, in a fixed order; ``None`` = project it."""

    if not policy.include_change_events and is_change_event(document, policy):
        return "change_event"
    if policy.exclude_stubbed and document.is_stub:
        return "stub"
    if document.tier.value in policy.exclude_tiers:
        return "cold"
    if record["join_status"] != "ok":
        return str(record["join_status"])
    lifecycle = record["lifecycle"]
    if isinstance(lifecycle, str) and lifecycle in policy.exclude_lifecycle:
        return lifecycle
    if record["privacy_class"] in policy.skip_privacy:
        return "privacy"
    return None


def _read_previous(previous: Path | None) -> tuple[dict[str, Any] | None, str | None]:
    if previous is None:
        return None, None
    manifest_path = previous / "manifest.json" if previous.is_dir() else previous
    if not manifest_path.exists():
        return None, None
    try:
        raw = manifest_path.read_bytes()
        data = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise ProjectionError(f"previous manifest unreadable: {manifest_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ProjectionError("previous manifest must be a JSON object")
    return data, hashlib.sha256(raw).hexdigest()


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _make_private_dirs(path: Path) -> None:
    """Create ``path`` and any missing parents with mode 0700 (existing ones untouched)."""

    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def export_projection(
    root: Path,
    out: Path,
    *,
    policy: ProjectionPolicy | None = None,
    lifecycle_source: Path | None = None,
    previous: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Export the projection of ``root`` into a new generation under ``out``.

    Returns the one-line summary (§3.6). With an unchanged content head,
    policy and lifecycle status versus ``previous`` it returns
    ``status=no_change_fast_path`` and writes nothing. The vault is only read.
    """

    started = time.monotonic()
    policy = policy or ProjectionPolicy()
    root = _check_vault(root)
    out = Path(out).expanduser()
    if _inside(out, root):
        raise ProjectionError("--out must not be inside the vault")
    meta = load_meta_index(lifecycle_source)
    moment = (now or datetime.now(UTC)).astimezone(UTC)

    entries = [read_entry(path) for path in _memory_files(root)]
    head = authority_content_head([(entry.memory_id, entry.raw) for entry in entries], meta)
    latest, complete = latest_change(entries)
    status, lag = lifecycle_source_state(
        meta, latest, policy.lifecycle_max_lag_seconds, complete=complete
    )
    previous_manifest, previous_sha = _read_previous(
        Path(previous).expanduser() if previous else None
    )

    if (
        previous_manifest is not None
        and previous_manifest.get("schema_version") == MANIFEST_SCHEMA_VERSION
        and previous_manifest.get("authority_content_head") == head
        and previous_manifest.get("policy_sha256") == policy.sha256
        and previous_manifest.get("lifecycle_source_status") == status
    ):
        return {
            "schema": EXPORT_SCHEMA,
            "status": "no_change_fast_path",
            "authority": policy.authority,
            "lifecycle_source_status": status,
            "projection_policy_version": policy.projection_policy_version,
            "authority_content_head": head,
            "manifest_sha256": previous_sha,
            "counts": previous_manifest.get("counts"),
            "join": previous_manifest.get("join"),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }

    skipped = {
        "change_event": 0,
        "cold": 0,
        "stub": 0,
        "unjoined": 0,
        "id_mismatch": 0,
        "invalid": 0,
    }
    for lifecycle in policy.exclude_lifecycle:
        skipped.setdefault(lifecycle, 0)
    if policy.skip_privacy:
        skipped["privacy"] = 0
    join_counts = {"unjoined": 0, "id_mismatch": 0}
    privacy_counts: dict[str, int] = {}
    privacy_mismatch = 0
    failures: list[dict[str, Any]] = []
    rendered: dict[str, bytes] = {}
    documents: list[dict[str, Any]] = []

    for entry in entries:
        if entry.document is None:
            skipped["invalid"] += 1
            failures.append({"id": entry.memory_id, "reason": "invalid", "detail": entry.error})
            continue
        document = entry.document
        join = join_meta(document.id, document.tags, meta)
        record = joined_record(document, join, policy)
        if join.status != "ok":
            join_counts[join.status] += 1
            failures.append(
                {
                    "id": document.id,
                    "fleet_path": join.fleet_path,
                    "reason": join.status,
                    "detail": join.reason,
                }
            )
        else:
            klass = str(record["privacy_class"])
            privacy_counts[klass] = privacy_counts.get(klass, 0) + 1
            privacy_mismatch += int(bool(record["privacy_source_mismatch"]))
        reason = _skip_reason(document, record, policy)
        if reason is not None:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        assert join.fleet_path is not None
        text = render_projection(document, join.fleet_path, policy).encode("utf-8")
        rendered[document.id] = text
        meta_block = {
            key: record[key]
            for key in (
                "status",
                "layer",
                "tier",
                "valid_to",
                "valid_until",
                "superseded_by",
                "tenant",
                "privacy_raw",
                "privacy_class",
                "scope",
                "type",
                "lifecycle",
                "epistemic",
                "causal",
            )
        }
        meta_block["privacy"] = meta_block.pop("privacy_raw")
        documents.append(
            {
                "id": document.id,
                "path": record["path"],
                "fleet_path": join.fleet_path,
                "content_sha256": hashlib.sha256(text).hexdigest(),
                "meta": meta_block,
            }
        )

    documents.sort(key=lambda item: str(item["id"]))
    projection_sha = _sha256(
        _canonical(
            {
                "policy": policy.sha256,
                "files": [[item["path"], item["content_sha256"]] for item in documents],
            }
        )
    )
    previous_docs = {
        str(item.get("id")): item.get("content_sha256")
        for item in (previous_manifest or {}).get("documents", [])
        if isinstance(item, dict)
    }
    current_docs = {str(item["id"]): item["content_sha256"] for item in documents}
    delta: dict[str, Any] = {
        "schema_version": 1,
        "previous_manifest_sha256": previous_sha,
        "added": sorted(set(current_docs) - set(previous_docs)),
        "changed": sorted(
            key
            for key in set(current_docs) & set(previous_docs)
            if current_docs[key] != previous_docs[key]
        ),
        "deleted": sorted(set(previous_docs) - set(current_docs)),
    }
    counts = {
        "vault_documents": len(entries),
        "projected": len(documents),
        "skipped": skipped,
        "privacy": dict(sorted(privacy_counts.items())),
        "privacy_source_mismatch": privacy_mismatch,
        "deleted_since_previous": len(delta["deleted"]),
    }
    join_block = {
        "key": "fleet_path",
        "id_rule": "f+sha256(rel)[:12]",
        "unjoined": join_counts["unjoined"],
        "id_mismatch": join_counts["id_mismatch"],
    }
    # The name covers everything a manifest records about content, so a
    # re-export whose only change is metadata never collides with its parent.
    generation_sha = _sha256(_canonical([projection_sha, head, status, _canonical(counts)]))
    tree_name = f"tree-{moment.strftime('%Y%m%dT%H%M%SZ')}-{generation_sha[:8]}"
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "projection_policy_version": policy.projection_policy_version,
        "policy_sha256": policy.sha256,
        "authority": policy.authority,
        "authority_head": authority_head(root),
        "authority_content_head": head,
        "authority_sync_lag_seconds": None,
        "lifecycle_source": policy.lifecycle_source,
        "lifecycle_source_generated_at": meta.generated_at if meta else None,
        "lifecycle_source_status": status,
        "lifecycle_source_lag_seconds": lag,
        "join": join_block,
        "generated_at_utc": _iso(moment),
        "config": {
            "include_change_events": policy.include_change_events,
            "exclude_tiers": list(policy.exclude_tiers),
            "exclude_stubbed": policy.exclude_stubbed,
            "exclude_lifecycle": list(policy.exclude_lifecycle),
            "tenant_map": policy.tenant_map,
            "skip_privacy": list(policy.skip_privacy),
        },
        "counts": counts,
        "projection_sha256": projection_sha,
        "tree": tree_name,
        "documents": documents,
        "previous_manifest_sha256": previous_sha,
    }
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    _make_private_dirs(out)
    final = out / tree_name
    if final.exists():
        raise ProjectionError(f"generation already exists: {final}")
    staging = out / f".staging-{tree_name}-{secrets.token_hex(4)}"
    try:
        memories = staging / "files" / "memories"
        for path in (staging, staging / "files", memories):
            path.mkdir(mode=0o750)
            path.chmod(0o750)
        for memory_id, text in sorted(rendered.items()):
            _write_private(memories / f"{memory_id}.md", text)
        _write_private(
            staging / "export-failures.jsonl",
            "".join(_canonical(item) + "\n" for item in failures).encode("utf-8"),
        )
        _write_private(
            staging / "delta.json",
            (json.dumps(delta, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
        _write_private(staging / "manifest.json", manifest_bytes)
        os.replace(staging, final)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return {
        "schema": EXPORT_SCHEMA,
        "status": "exported",
        "authority": policy.authority,
        "lifecycle_source_status": status,
        "projection_policy_version": policy.projection_policy_version,
        "counts": counts,
        "join": {"unjoined": join_block["unjoined"], "id_mismatch": join_block["id_mismatch"]},
        "authority_content_head": head,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "tree": str(final),
        "manifest_path": str(final / "manifest.json"),
        "delta_path": str(final / "delta.json"),
        "failures_path": str(final / "export-failures.jsonl"),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


# -------------------------------------------------------------------------- resolve


def parse_hits(lines: Iterable[str]) -> list[str]:
    """Non-empty stripped stdin lines, duplicates removed in order.

    A line longer than :data:`MAX_HIT_LINE_CHARS` is kept only as its first
    ``MAX_HIT_LINE_CHARS`` characters, which never form a valid id or path, so
    it lands in ``unresolved``.
    """

    seen: dict[str, None] = {}
    for line in lines:
        text = line.strip()
        if len(text) > MAX_HIT_LINE_CHARS:
            text = "\0" + text[:MAX_HIT_LINE_CHARS]
        if text:
            seen.setdefault(text, None)
    return list(seen)


def read_hits(stream: Any) -> list[str]:
    """Read resolve input from a text stream, capped at :data:`MAX_STDIN_BYTES`."""

    data = stream.read(MAX_STDIN_BYTES + 1)
    if len(data.encode("utf-8")) > MAX_STDIN_BYTES:
        raise ProjectionError(f"resolve input exceeds {MAX_STDIN_BYTES} bytes")
    return parse_hits(data.splitlines())


def _truncate(body: str, max_lines: int) -> tuple[str | None, bool]:
    if max_lines <= 0:
        return None, bool(body)
    lines = body.splitlines()
    if len(lines) <= max_lines:
        return body, False
    return "\n".join(lines[:max_lines]), True


def _withheld_record(memory_id: str, join: JoinResult, hit: str) -> dict[str, Any]:
    """A non-``ok`` join: every key present, only id, path and join outcome set."""

    record: dict[str, Any] = dict.fromkeys(RESOLVE_DOCUMENT_KEYS)
    record.update(
        {
            "id": memory_id,
            "path": f"memories/{memory_id}.md",
            "join_status": join.status,
            "join_reason": join.reason,
            "privacy_source_mismatch": False,
            "body_truncated": False,
            "tags": [],
            "hit": hit,
        }
    )
    return record


def resolve(
    root: Path,
    hits: Sequence[str],
    *,
    lifecycle_source: Path | None = None,
    policy: ProjectionPolicy | None = None,
    max_lines: int = DEFAULT_MAX_LINES,
) -> dict[str, Any]:
    """Batch, read-only authoritative lookup of backend hits (§5.3).

    ``documents`` keeps the input order; ids that are well-formed but absent go
    to ``missing``; hits that are not an id or ``memories/<id>.md`` path go to
    ``unresolved`` (each echoed entry cut to 200 characters). A document whose
    join is not ``ok`` carries only ``id``, ``path``, ``join_status`` and
    ``join_reason``; every other field is ``null`` (``tags`` empty) — callers
    drop it. The scan parses every vault file; one that does not parse makes
    ``lifecycle_source_status`` ``stale``.
    """

    started = time.monotonic()
    policy = policy or ProjectionPolicy()
    root = _check_vault(root)
    if len(hits) > MAX_RESOLVE_BATCH:
        raise ProjectionError(f"at most {MAX_RESOLVE_BATCH} ids per resolve call")
    meta = load_meta_index(lifecycle_source)
    entries = [read_entry(path) for path in _memory_files(root)]
    raws = [(entry.memory_id, entry.raw) for entry in entries]
    by_id = {entry.memory_id: entry for entry in entries}
    latest, complete = latest_change(entries)
    status, lag = lifecycle_source_state(
        meta, latest, policy.lifecycle_max_lag_seconds, complete=complete
    )

    documents: list[dict[str, Any]] = []
    missing: list[str] = []
    unresolved: list[str] = []
    invalid: list[str] = []
    emitted: set[str] = set()
    for hit in hits:
        memory_id = hit_to_id(hit)
        if memory_id is None:
            unresolved.append(hit[:MAX_ECHO_CHARS])
            continue
        if memory_id in emitted:
            continue
        emitted.add(memory_id)
        found = by_id.get(memory_id)
        if found is None:
            missing.append(memory_id)
            continue
        entry = found
        if entry.document is None:
            invalid.append(memory_id)
            continue
        document = entry.document
        join = join_meta(document.id, document.tags, meta)
        if join.status != "ok":
            documents.append(_withheld_record(document.id, join, hit))
            continue
        record = joined_record(document, join, policy)
        body, truncated = _truncate(document.body, max_lines)
        record.update(
            {
                "content_sha256": entry.sha256,
                "body": body,
                "body_truncated": truncated,
                "hit": hit,
            }
        )
        documents.append(record)

    return {
        "schema": RESOLVE_SCHEMA,
        "authority_head": authority_head(root),
        "authority_content_head": authority_content_head(raws, meta),
        "authority_sync_lag_seconds": None,
        "lifecycle_source_generated_at": meta.generated_at if meta else None,
        "lifecycle_source_status": status,
        "lifecycle_source_lag_seconds": lag,
        "projection_policy_version": policy.projection_policy_version,
        "documents": documents,
        "missing": missing,
        "unresolved": unresolved,
        "invalid": invalid,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


# --------------------------------------------------------------------------- filter


def parse_clearance(raw: str | None) -> frozenset[str]:
    """Privacy clearance from a comma list; unset, empty or malformed → ``{none}``."""

    if raw is None or not raw.strip():
        return DEFAULT_CLEARANCE
    tokens = [token.strip() for token in raw.split(",")]
    if not tokens or any(not _CLEARANCE_TOKEN_RE.fullmatch(token) for token in tokens):
        return DEFAULT_CLEARANCE
    return frozenset(tokens)


def parse_tenants(raw: str | None) -> frozenset[str]:
    """Allowed tenants from a comma list (an empty item allows ``""``); unset → default."""

    if raw is None:
        return DEFAULT_TENANTS
    return frozenset(token.strip() for token in raw.split(","))


@dataclass(frozen=True)
class FilterSettings:
    clearance: frozenset[str] = DEFAULT_CLEARANCE
    tenants: frozenset[str] = DEFAULT_TENANTS
    include_retired: bool = False
    now: datetime | None = None


FILTER_COUNTERS = (
    "filtered_unjoined",
    "filtered_archived",
    "filtered_privacy",
    "filtered_tenant",
    "filtered_lifecycle_unknown",
    "filtered_expiry_unparsable",
    "filtered_retired",
    "filtered_superseded",
    "filtered_expired",
    "filtered_missing",
    "filtered_unresolved",
)
_KNOWN_LIFECYCLE = frozenset({"active", "retired", "superseded"})


def _expiry(record: Mapping[str, Any]) -> tuple[datetime | None, bool]:
    """Earliest of ``valid_to`` / ``valid_until`` and whether any is unparsable."""

    stamps: list[datetime] = []
    for key in ("valid_to", "valid_until"):
        value = record.get(key)
        if value in (None, ""):
            continue
        stamp = _parse_instant(value)
        if stamp is None:
            return None, True
        stamps.append(stamp)
    return (min(stamps) if stamps else None), False


def filter_decision(
    record: Mapping[str, Any], settings: FilterSettings
) -> tuple[str | None, list[str]]:
    """``(drop_counter, labels)`` for one resolved document (§5.4); ``None`` = keep.

    Privacy, tenant and join failures are never bypassed by ``include_retired``.
    """

    labels: list[str] = []
    if record.get("join_status") != "ok":
        return "filtered_unjoined", labels
    if record.get("is_stub") or record.get("tier") == "cold":
        return "filtered_archived", labels
    privacy = record.get("privacy_class")
    if not isinstance(privacy, str) or privacy not in settings.clearance:
        return "filtered_privacy", labels
    if record.get("tenant") not in settings.tenants:
        return "filtered_tenant", labels
    lifecycle = record.get("lifecycle")
    if lifecycle not in _KNOWN_LIFECYCLE:
        return "filtered_lifecycle_unknown", labels
    expires, unparsable = _expiry(record)
    if unparsable:
        return "filtered_expiry_unparsable", labels
    if lifecycle == "retired":
        if not settings.include_retired:
            return "filtered_retired", labels
        labels.append("retired")
    if lifecycle == "superseded" or record.get("superseded_by"):
        if not settings.include_retired:
            return "filtered_superseded", labels
        labels.append("superseded")
    now = (settings.now or datetime.now(UTC)).astimezone(UTC)
    if expires is not None and now > expires:
        if not settings.include_retired:
            return "filtered_expired", labels
        labels.append("expired")
    if record.get("status") == "disputed":
        labels.append("disputed")
    if record.get("layer") in {"anchor", "consolidated"}:
        labels.append(str(record["layer"]))
    if record.get("epistemic"):
        labels.append(f"epistemic:{record['epistemic']}")
    if record.get("causal") not in (None, "none"):
        labels.append(f"causal:{record['causal']}")
    return None, labels


def filter_resolved(payload: Mapping[str, Any], settings: FilterSettings) -> dict[str, Any]:
    """Apply §5.4 to a :func:`resolve` payload; fail closed when the source is not ok."""

    counts = dict.fromkeys(FILTER_COUNTERS, 0)
    counts["filtered_missing"] = len(payload.get("missing") or [])
    counts["filtered_unresolved"] = len(payload.get("unresolved") or []) + len(
        payload.get("invalid") or []
    )
    status = payload.get("lifecycle_source_status")
    if status != "ok":
        return {
            "documents": [],
            "output_meta": {
                **counts,
                "fail_closed": True,
                "fallback_reason": f"lifecycle_source_{status or 'unavailable'}",
            },
        }
    kept: list[dict[str, Any]] = []
    for record in payload.get("documents") or []:
        counter, labels = filter_decision(record, settings)
        if counter is not None:
            counts[counter] += 1
            continue
        adjust = 0.0
        if "disputed" in labels:
            adjust += DISPUTED_RANK_PENALTY
        if "anchor" in labels:
            adjust += ANCHOR_RANK_BONUS
        kept.append({**record, "labels": labels, "rank_adjust": adjust})
    return {"documents": kept, "output_meta": {**counts, "fail_closed": False}}

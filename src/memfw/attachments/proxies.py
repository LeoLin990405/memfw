"""Attachment proxies (MM2a): text segments derived from an original, stored in the vault.

A proxy file arrives in the canonical import format (``memfw.ports.proxies``,
``docs/attachment-proxies.md``) and is stored as one small Markdown file,
``attachments/proxies/<prx-id>.md``: YAML frontmatter for the metadata, then
one ``<!-- memfw:segment {...} -->`` marker line per segment followed by its
text. Proxies are never under ``memories/`` and never in a memory body.

Rules (design v2 "代理版本", "覆盖范围", v2.1 "预算", "定位"):

* ``coverage`` is required; ``source_sha256`` must equal the attachment's
  sha256; ``supersedes`` names an active proxy of the same attachment, which
  then stays on disk but is marked superseded (a ``proxy_supersede`` event)
  and leaves search.
* Locators are stored in canonical units only (PDF points from the bottom-left
  corner, image pixels from the top-left, seconds) and are checked against
  the dimensions the import declares.
* A proxy is ``local-private`` unless the import says otherwise, and never
  less private than its attachment.
* Proxies have their own byte budget (``attachments_proxy_max_bytes``); they
  are not part of the vault's physical budget, so they can never make the
  forgetting policies pick a memory.

Write order mirrors ``attach add``: under the vault lock, the proxy file is
written and then one ``proxy_add`` event (plus a ``proxy_supersede`` for the
replaced proxy) is appended. ``memfw recover`` removes a proxy file no event
committed.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

import yaml

from memfw.core.errors import IntegrityError, NotFoundError
from memfw.core.models import Event, EventKind
from memfw.ports.privacy import (
    LEGACY_PRIVACY_FALLBACK,
    PRIVACY_LEVELS,
    PrivacyLevel,
    field_level,
    is_looser,
    privacy_value,
    strictest,
)
from memfw.ports.proxies import (
    PROXY_FORMAT_VERSION,
    PROXY_ID_RE,
    PROXY_KINDS,
    Coverage,
    Locator,
    ProxyFormatError,
    ProxyImport,
    ProxySegment,
    SourceDimensions,
    check_locator,
    check_producer,
    load_proxy_import,
    medium_for,
)
from memfw.storage.transactions import _serialized

from .records import ATTACHMENTS_DIR, AttachmentRecord, check_attachment_id, new_attachment_id

if TYPE_CHECKING:
    from memfw.core.context import RepositoryHost

PROXIES_DIR: Final = "proxies"
PROXY_TYPE: Final = "attachment_proxy"
#: ``vault.yaml``: the most bytes all proxy files together may take.
PROXY_MAX_BYTES_KEY: Final = "attachments_proxy_max_bytes"
DEFAULT_PROXY_MAX_BYTES: Final = 64 * 1024 * 1024
PROXY_STATUSES: Final = ("unverified", "reviewed")
#: The new proxy's level when neither the import nor the command sets one.
DEFAULT_PROXY_PRIVACY: Final = LEGACY_PRIVACY_FALLBACK

_MARKER_RE: Final = re.compile(r"^<!-- memfw:segment (\{.*\}) -->$")
_SHA_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED: Final = (
    "id",
    "attachment",
    "kind",
    "medium",
    "source_sha256",
    "coverage",
    "recorded_at",
    "segment_count",
)
_KNOWN_KEYS: Final = frozenset(
    {
        *_REQUIRED,
        "type",
        "format_version",
        "producer",
        "created_at",
        "status",
        "privacy",
        "supersedes",
        "language",
        "speaker_labels",
        "source",
    }
)


def check_proxy_id(value: str) -> str:
    """``prx-<uuid7>``; anything else (including a path) raises ``IntegrityError``."""

    if not isinstance(value, str) or not PROXY_ID_RE.fullmatch(value):
        raise IntegrityError(f"Invalid proxy id: {value!r}")
    return value


def validate_proxy_config(raw: dict[str, Any]) -> None:
    """Check the proxy keys of a ``vault.yaml`` mapping (absent keys are fine)."""

    value = raw.get(PROXY_MAX_BYTES_KEY)
    if PROXY_MAX_BYTES_KEY in raw and (
        isinstance(value, bool) or not isinstance(value, int) or value < 1
    ):
        raise IntegrityError(f"{PROXY_MAX_BYTES_KEY} must be a positive integer")


@dataclass(frozen=True)
class ProxyRecord:
    """One stored proxy: the import's content plus what memfw adds (id, times, status)."""

    id: str
    attachment_id: str
    kind: str
    medium: str
    source_sha256: str
    coverage: Coverage
    segments: tuple[ProxySegment, ...]
    recorded_at: str
    producer: str | dict[str, str] | None = None
    created_at: str | None = None
    status: str = "unverified"
    privacy: str | None = DEFAULT_PROXY_PRIVACY
    supersedes: str | None = None
    language: str | None = None
    source: SourceDimensions = field(default_factory=SourceDimensions)
    ignored_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        check_proxy_id(self.id)
        check_attachment_id(self.attachment_id)
        if self.supersedes is not None:
            check_proxy_id(self.supersedes)
        if self.kind not in PROXY_KINDS:
            raise ValueError("kind must be one of: " + ", ".join(PROXY_KINDS))
        if not _SHA_RE.fullmatch(self.source_sha256):
            raise ValueError("source_sha256 must be 64 lowercase hex characters")
        if self.status not in PROXY_STATUSES:
            raise ValueError("status must be unverified or reviewed")
        if not self.segments:
            raise ValueError("a proxy has at least one segment")
        for segment in self.segments:
            check_locator(segment.locator, medium=self.medium, dimensions=self.source)

    @property
    def privacy_level(self) -> PrivacyLevel:
        """The proxy's own level (a missing field reads ``local-private``)."""

        return field_level(self.privacy if self.privacy is not None else DEFAULT_PROXY_PRIVACY)

    def effective_level(self, attachment: AttachmentRecord | None) -> PrivacyLevel:
        """Strictest of the proxy's and its attachment's level (no attachment: ``unknown``)."""

        if attachment is None:
            return "unknown"
        return strictest([self.privacy_level, attachment.privacy_level])

    def frontmatter(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "type": PROXY_TYPE,
            "format_version": PROXY_FORMAT_VERSION,
            "attachment": self.attachment_id,
            "kind": self.kind,
            "medium": self.medium,
            "source_sha256": self.source_sha256,
            "coverage": self.coverage.to_json(),
        }
        if self.producer is not None:
            data["producer"] = (
                dict(self.producer) if isinstance(self.producer, dict) else self.producer
            )
        if self.created_at is not None:
            data["created_at"] = self.created_at
        data["recorded_at"] = self.recorded_at
        data["status"] = self.status
        if self.privacy is not None:
            data["privacy"] = self.privacy
        if self.supersedes is not None:
            data["supersedes"] = self.supersedes
        if self.language is not None:
            data["language"] = self.language
        if self.kind == "transcript":
            # Speaker labels are never taken as verified (design 1.2).
            data["speaker_labels"] = "unverified"
        source = self.source.to_json()
        if source:
            data["source"] = source
        data["segment_count"] = len(self.segments)
        return data

    def summary(self) -> dict[str, Any]:
        """Metadata without segment text (for listings)."""

        data = self.frontmatter()
        data["privacy_level"] = self.privacy_level
        return data

    def to_json(self) -> dict[str, Any]:
        data = self.summary()
        data["segments"] = [
            {"n": index, **segment.to_json(), "fragment": segment.locator.fragment()}
            for index, segment in enumerate(self.segments, start=1)
        ]
        return data


def _marker(index: int, segment: ProxySegment) -> str:
    meta: dict[str, Any] = {"n": index}
    fragment = segment.locator.fragment()
    if fragment:
        meta["locator"] = fragment
    if segment.speaker is not None:
        meta["speaker"] = segment.speaker
    if segment.confidence is not None:
        meta["confidence"] = segment.confidence
    text = json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
    # ``-->`` (or ``<``/``>``) inside a speaker name must not close the comment.
    return "<!-- memfw:segment " + text.replace("<", "\\u003c").replace(">", "\\u003e") + " -->"


def render_proxy(record: ProxyRecord) -> bytes:
    """The proxy file's bytes. Refuses (``ValueError``) a rendering that does not read back."""

    front = yaml.safe_dump(record.frontmatter(), allow_unicode=True, sort_keys=False).rstrip()
    parts = [f"---\n{front}\n---\n"]
    for index, segment in enumerate(record.segments, start=1):
        parts.append(f"{_marker(index, segment)}\n{segment.text}\n")
    raw = "\n".join(parts).encode("utf-8")
    try:
        parsed = parse_proxy(raw, record.id)
    except IntegrityError as exc:
        raise ValueError(f"proxy does not read back: {exc}") from None
    if replace(parsed, ignored_keys=()) != replace(record, ignored_keys=()):
        raise ValueError("proxy does not read back identically")
    return raw


def _split(text: str) -> tuple[str, list[str]]:
    lines = text.split("\n")
    if lines[0] != "---":
        raise ValueError("missing YAML frontmatter")
    for index in range(1, len(lines)):
        if lines[index] == "---":
            return "\n".join(lines[1:index]), lines[index + 1 :]
    raise ValueError("unterminated YAML frontmatter")


def _segments(lines: list[str]) -> list[ProxySegment]:
    segments: list[ProxySegment] = []
    meta: dict[str, Any] | None = None
    text: list[str] = []

    def flush() -> None:
        if meta is None:
            if "\n".join(text).strip():
                raise ValueError("text before the first segment marker")
            return
        if meta.get("n") != len(segments) + 1:
            raise ValueError("segment markers are out of order")
        unknown = set(meta) - {"n", "locator", "speaker", "confidence"}
        if unknown:
            raise ValueError("unknown segment keys: " + ", ".join(sorted(unknown)))
        segments.append(
            ProxySegment(
                text="\n".join(text),
                locator=Locator.parse(str(meta.get("locator", ""))),
                speaker=meta.get("speaker"),
                confidence=meta.get("confidence"),
            )
        )

    for line in lines:
        match = _MARKER_RE.fullmatch(line)
        if match is None:
            text.append(line)
            continue
        flush()
        loaded = json.loads(match.group(1))
        if not isinstance(loaded, dict):
            raise ValueError("a segment marker holds one JSON object")
        meta, text = loaded, []
    flush()
    return segments


def parse_proxy(raw: bytes, expected_id: str) -> ProxyRecord:
    """Parse a stored proxy file; anything malformed raises ``IntegrityError``."""

    try:
        front, body = _split(raw.decode("utf-8"))
        meta = yaml.safe_load(front)
        if not isinstance(meta, dict):
            raise ValueError("frontmatter must be a mapping")
        missing = [key for key in _REQUIRED if key not in meta]
        if missing:
            raise ValueError("missing " + ", ".join(missing))
        if meta["id"] != expected_id:
            raise ValueError("frontmatter id does not match the file name")
        if meta.get("type", PROXY_TYPE) != PROXY_TYPE:
            raise ValueError(f"type must be {PROXY_TYPE}")
        if meta.get("format_version", PROXY_FORMAT_VERSION) != PROXY_FORMAT_VERSION:
            raise ValueError("unsupported format_version")
        medium = str(meta["medium"])
        if medium != medium_for_name(medium):
            raise ValueError("unknown medium")
        segments = _segments(body)
        if len(segments) != meta["segment_count"]:
            raise ValueError("segment_count does not match the segments")
        producer = meta.get("producer")
        privacy = meta.get("privacy")
        return ProxyRecord(
            id=str(meta["id"]),
            attachment_id=str(meta["attachment"]),
            kind=str(meta["kind"]),
            medium=medium,
            source_sha256=str(meta["source_sha256"]),
            coverage=Coverage.from_json(meta["coverage"]),
            segments=tuple(segments),
            recorded_at=str(meta["recorded_at"]),
            producer=None if producer is None else check_producer(producer),
            created_at=None if meta.get("created_at") is None else str(meta["created_at"]),
            status=str(meta.get("status", "unverified")),
            privacy=None if privacy is None else str(privacy),
            supersedes=None if meta.get("supersedes") is None else str(meta["supersedes"]),
            language=None if meta.get("language") is None else str(meta["language"]),
            source=SourceDimensions.from_json(meta.get("source")),
            ignored_keys=tuple(sorted(str(key) for key in meta if key not in _KNOWN_KEYS)),
        )
    except IntegrityError:
        raise
    except (UnicodeDecodeError, KeyError, TypeError, ValueError, yaml.YAMLError) as exc:
        raise IntegrityError(f"Invalid proxy {expected_id}: {exc}") from exc


def medium_for_name(name: str) -> str:
    return name if name in ("pdf", "image", "audio", "video", "other") else ""


@dataclass(frozen=True)
class ProxyLog:
    """Proxy state from the event log: committed file hashes, owners, supersessions."""

    committed: dict[str, str]
    attachments: dict[str, str]
    superseded_by: dict[str, str]

    def active(self, proxy_id: str) -> bool:
        return proxy_id in self.committed and proxy_id not in self.superseded_by


def proxy_log(events: Iterable[Event]) -> ProxyLog:
    committed: dict[str, str] = {}
    attachments: dict[str, str] = {}
    superseded: dict[str, str] = {}
    for event in events:
        if event.kind == EventKind.PROXY_ADD and event.after_hash is not None:
            committed[event.memory_id] = event.after_hash
            owner = event.details.get("attachment")
            if isinstance(owner, str):
                attachments[event.memory_id] = owner
        elif event.kind == EventKind.PROXY_SUPERSEDE:
            by = event.details.get("by")
            superseded[event.memory_id] = by if isinstance(by, str) else ""
    return ProxyLog(committed, attachments, superseded)


def proxies_dir(host: RepositoryHost) -> Path:
    return host.root / ATTACHMENTS_DIR / PROXIES_DIR


def _proxy_path(host: RepositoryHost, proxy_id: str) -> Path:
    return proxies_dir(host) / f"{check_proxy_id(proxy_id)}.md"


def proxy_bytes(host: RepositoryHost) -> tuple[int, int]:
    """(files, bytes) of the stored proxy files, superseded ones included."""

    directory = proxies_dir(host)
    if not directory.is_dir():
        return 0, 0
    sizes = [path.stat().st_size for path in directory.glob("*.md") if path.is_file()]
    return len(sizes), sum(sizes)


def _max_bytes(host: RepositoryHost) -> int:
    config = host.read_config() if host.config_path.exists() else {}
    value = config.get(PROXY_MAX_BYTES_KEY, DEFAULT_PROXY_MAX_BYTES)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _read_capped(path: Path, limit: int) -> bytes:
    with Path(path).open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError(
            f"the proxy file is over {PROXY_MAX_BYTES_KEY} ({limit} bytes); proxies have "
            "their own budget, separate from memories"
        )
    return data


def _one(option: str | None, from_file: str | None, what: str) -> str | None:
    if option is not None and from_file is not None and option != from_file:
        raise ValueError(f"--{what} ({option}) and the file's {what} ({from_file}) differ")
    return option if option is not None else from_file


class ProxyStoreMixin:
    """Proxy files under ``attachments/proxies/`` and their events."""

    def import_proxy(
        self: RepositoryHost,
        attachment_id: str,
        source: Path,
        *,
        privacy: str | None = None,
        supersedes: str | None = None,
    ) -> ProxyRecord:
        """Validate a proxy import file and store it (a human write).

        Every check that needs only the file and the attachment record runs
        before anything is written; nothing is accepted partially.
        """

        identifier = check_attachment_id(attachment_id)
        attachment = cast(Any, self).get_attachment(identifier)
        raw = _read_capped(source, _max_bytes(self))
        try:
            parsed = load_proxy_import(raw, media_type=attachment.media_type)
        except ProxyFormatError as exc:
            raise ValueError(f"invalid proxy file: {exc}") from None
        if parsed.attachment_id is not None and parsed.attachment_id != identifier:
            raise ValueError(f"the file is for {parsed.attachment_id}, not {identifier}")
        if parsed.source_sha256 != attachment.sha256:
            raise ValueError(
                "source_sha256 does not match the attachment: the proxy was made from other bytes"
            )
        if supersedes is not None:
            check_proxy_id(supersedes)
        replaced = _one(supersedes, parsed.supersedes, "supersedes")
        level = _one(privacy_value(privacy), parsed.privacy, "privacy") or DEFAULT_PROXY_PRIVACY
        if level not in PRIVACY_LEVELS:  # pragma: no cover - checked by the format
            raise ValueError("invalid privacy level")
        if is_looser(level, attachment.privacy_level):
            raise ValueError(
                f"a proxy cannot be less private ({level}) than its attachment "
                f"({attachment.privacy_level})"
            )
        draft = _draft(self, identifier, attachment, parsed, level, replaced)
        # Refused before anything is written when it would not read back.
        render_proxy(draft)
        result: ProxyRecord = cast(Any, self).add_proxy_record(draft)
        return result

    @_serialized
    def add_proxy_record(self: RepositoryHost, draft: ProxyRecord) -> ProxyRecord:
        """Allocate an id, write the proxy file, append ``proxy_add`` (and ``proxy_supersede``)."""

        events = self.events()
        attachment = cast(Any, self).get_attachment(draft.attachment_id)
        if attachment.sha256 != draft.source_sha256:
            raise IntegrityError("the attachment changed while the proxy was checked; retry")
        log = proxy_log(events)
        if draft.supersedes is not None:
            if log.attachments.get(draft.supersedes) != draft.attachment_id:
                raise ValueError(f"{draft.supersedes} is not a proxy of {draft.attachment_id}")
            if not log.active(draft.supersedes):
                raise ValueError(f"{draft.supersedes} is already superseded")
        used = {event.memory_id for event in events}
        for _ in range(16):
            entropy = hashlib.sha256(self.id_factory().encode("utf-8")).digest()
            proxy_id = "prx-" + new_attachment_id(self._now(), entropy)[4:]
            if proxy_id not in used and not _proxy_path(self, proxy_id).exists():
                break
        else:  # pragma: no cover - 74 random bits
            raise IntegrityError("could not allocate an unused proxy id")
        record = replace(draft, id=proxy_id)
        raw = render_proxy(record)
        _, total = proxy_bytes(self)
        limit = _max_bytes(self)
        if total + len(raw) > limit:
            raise ValueError(
                f"proxies would take {total + len(raw)} bytes, over {PROXY_MAX_BYTES_KEY} "
                f"({limit}); proxies have their own budget and never cause memories to be "
                "forgotten"
            )
        self._atomic_write(_proxy_path(self, proxy_id), raw)
        # Event details carry ids and counts only, never text (design H7).
        # ``projection: none`` is the forward-compat marker: an older reader
        # that does not know the kind may skip it (it never touches a memory).
        details: dict[str, Any] = {
            "projection": "none",
            "attachment": record.attachment_id,
            "kind": record.kind,
            "segments": len(record.segments),
        }
        if record.supersedes is not None:
            details["supersedes"] = record.supersedes
        self._append_event(
            EventKind.PROXY_ADD,
            proxy_id,
            actor="human",
            after_hash=hashlib.sha256(raw).hexdigest(),
            details=details,
        )
        if record.supersedes is not None:
            self._append_event(
                EventKind.PROXY_SUPERSEDE,
                record.supersedes,
                actor="human",
                before_hash=log.committed[record.supersedes],
                details={
                    "projection": "none",
                    "attachment": record.attachment_id,
                    "by": proxy_id,
                },
            )
        return record

    # -- reads --------------------------------------------------------------------

    def proxy_state(self: RepositoryHost) -> ProxyLog:
        return proxy_log(self.events())

    def get_proxy(self: RepositoryHost, proxy_id: str) -> ProxyRecord:
        """A committed proxy whose bytes match its ``proxy_add`` event."""

        identifier = check_proxy_id(proxy_id)
        expected = proxy_log(self.events()).committed.get(identifier)
        if expected is None:
            raise NotFoundError(f"Proxy not found: {identifier}")
        return _load_proxy(self, identifier, expected)

    def proxy_records(
        self: RepositoryHost,
        attachment_id: str | None = None,
        *,
        include_superseded: bool = False,
    ) -> list[tuple[str, ProxyRecord | IntegrityError | NotFoundError, str | None]]:
        """Committed proxies, oldest first: (id, record or why not, superseded by)."""

        log = proxy_log(self.events())
        owner = None if attachment_id is None else check_attachment_id(attachment_id)
        rows: list[tuple[str, ProxyRecord | IntegrityError | NotFoundError, str | None]] = []
        for identifier, expected in log.committed.items():
            if owner is not None and log.attachments.get(identifier) != owner:
                continue
            by = log.superseded_by.get(identifier)
            if by is not None and not include_superseded:
                continue
            try:
                loaded: ProxyRecord | IntegrityError | NotFoundError = _load_proxy(
                    self, identifier, expected
                )
            except (IntegrityError, NotFoundError) as exc:
                loaded = exc
            rows.append((identifier, loaded, by))
        return rows

    def proxy_usage(self: RepositoryHost) -> dict[str, int]:
        files, size = proxy_bytes(self)
        return {"files": files, "bytes": size, "max_bytes": _max_bytes(self)}

    # -- integrity ----------------------------------------------------------------

    def proxy_fsck_issues(self: RepositoryHost) -> list[dict[str, str | None]]:
        """``fsck`` rows for proxies; empty (no I/O past the log) on a vault without any."""

        events = self.events()
        log = proxy_log(events)
        directory = proxies_dir(self)
        if not log.committed and not directory.exists():
            return []
        issues: list[dict[str, str | None]] = []

        def issue(proxy_id: str | None, kind: str, **extra: str | None) -> None:
            issues.append(
                {
                    "memory_id": proxy_id,
                    "kind": f"proxy_{kind}",
                    "expected_hash": extra.get("expected"),
                    "actual_hash": extra.get("actual"),
                    "message": extra.get("message"),
                }
            )

        attachments: dict[str, AttachmentRecord | None] = {}
        for identifier, expected in log.committed.items():
            path = _proxy_path(self, identifier)
            if not path.exists():
                issue(identifier, "record_missing", expected=expected)
                continue
            raw = path.read_bytes()
            actual = hashlib.sha256(raw).hexdigest()
            if actual != expected:
                issue(identifier, "record_modified", expected=expected, actual=actual)
                continue
            try:
                record = parse_proxy(raw, identifier)
            except IntegrityError as exc:
                issue(identifier, "record_invalid", message=str(exc))
                continue
            owner = record.attachment_id
            if owner not in attachments:
                try:
                    attachments[owner] = cast(Any, self).get_attachment(owner)
                except (IntegrityError, NotFoundError):
                    attachments[owner] = None
            attachment = attachments[owner]
            if attachment is None:
                issue(identifier, "attachment_missing", message=owner)
            elif attachment.sha256 != record.source_sha256:
                issue(identifier, "source_mismatch", message=owner)
        if directory.is_dir():
            for path in sorted(directory.glob("*.md")):
                if path.stem not in log.committed:
                    issue(
                        path.stem if PROXY_ID_RE.fullmatch(path.stem) else None,
                        "record_untracked",
                        message=path.name,
                    )
        return issues

    def _reconcile_proxy_records(self: RepositoryHost, events: list[Event]) -> list[str]:
        """Recovery: drop proxy files no ``proxy_add`` event committed, and temp files."""

        directory = proxies_dir(self)
        removed: list[str] = []
        if not directory.is_dir():
            return removed
        committed = proxy_log(events).committed
        for path in sorted(directory.glob("*.md.tmp")):
            path.unlink(missing_ok=True)
            removed.append(path.relative_to(self.root).as_posix())
        for path in sorted(directory.glob("*.md")):
            if PROXY_ID_RE.fullmatch(path.stem) and path.stem not in committed:
                path.unlink()
                removed.append(path.stem)
        return removed


def _draft(
    host: RepositoryHost,
    attachment_id: str,
    attachment: AttachmentRecord,
    parsed: ProxyImport,
    level: str,
    supersedes: str | None,
) -> ProxyRecord:
    return ProxyRecord(
        # Replaced under the lock; a valid placeholder lets the draft render.
        id="prx-00000000-0000-7000-8000-000000000000",
        attachment_id=attachment_id,
        kind=parsed.kind,
        medium=medium_for(attachment.media_type),
        source_sha256=parsed.source_sha256,
        coverage=parsed.coverage,
        segments=parsed.segments,
        recorded_at=host._now().isoformat(),
        producer=None
        if parsed.producer is None
        else (dict(parsed.producer) if not isinstance(parsed.producer, str) else parsed.producer),
        created_at=parsed.created_at,
        privacy=level,
        supersedes=supersedes,
        language=parsed.language,
        source=parsed.source,
    )


def _load_proxy(host: RepositoryHost, identifier: str, expected: str) -> ProxyRecord:
    try:
        raw = _proxy_path(host, identifier).read_bytes()
    except FileNotFoundError:
        raise NotFoundError(f"Proxy file is missing: {identifier}") from None
    if hashlib.sha256(raw).hexdigest() != expected:
        raise IntegrityError(f"Proxy hash mismatch: {identifier}")
    return parse_proxy(raw, identifier)


__all__ = [
    "DEFAULT_PROXY_MAX_BYTES",
    "DEFAULT_PROXY_PRIVACY",
    "PROXIES_DIR",
    "PROXY_MAX_BYTES_KEY",
    "ProxyLog",
    "ProxyRecord",
    "ProxyStoreMixin",
    "check_proxy_id",
    "parse_proxy",
    "proxies_dir",
    "proxy_bytes",
    "proxy_log",
    "render_proxy",
    "validate_proxy_config",
]

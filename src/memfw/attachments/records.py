"""Attachment records: one small Markdown file per registered original (MM1a).

A record lives at ``<vault>/attachments/<att-id>.md`` (never under
``memories/``). Its YAML frontmatter is the original's fingerprint and
metadata; its body is empty or a human note. The original's bytes are never in
the record: they are a blob outside the vault (``memfw.attachments.blobs``).

Privacy follows ``memfw.ports.privacy``: a new record always carries
``privacy`` (default ``local-private``). A record read without the field is
``local-private`` whatever the vault's ``legacy_privacy_default`` says (that
key covers only pre-MM0 memories, design v2.1 N1), and ``privacy:*`` /
``sensitivity:*`` tags and a top-level ``sensitivity`` key can only make it
stricter.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Final

import yaml

from memfw.core.errors import IntegrityError
from memfw.ports.privacy import (
    LEGACY_PRIVACY_FALLBACK,
    SENSITIVITY_KEY,
    PrivacyLevel,
    metadata_level,
)

from .locations import Location, LocationError

ATTACHMENTS_DIR: Final = "attachments"
RECORD_TYPE: Final = "attachment"
#: Storage kinds: ``embedded`` (a blob in the blob store) or ``referenced``
#: (a ``location`` under a configured root, ``memfw.attachments.locations``).
STORAGE_KINDS: Final = ("embedded", "referenced")
#: The level of a new attachment that sets none (design v2.1: not the legacy default).
DEFAULT_ATTACHMENT_PRIVACY: Final = LEGACY_PRIVACY_FALLBACK

_ID_RE: Final = re.compile(
    r"^att-[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_SHA_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_MEDIA_RE: Final = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")
_RETENTION_RE: Final = re.compile(r"^(keep|review|until:\d{4}-\d{2}-\d{2})$")
_REQUIRED: Final = ("id", "sha256", "size", "media_type", "storage", "recorded_at")
_KNOWN_KEYS: Final = frozenset(
    {
        "id",
        "type",
        "sha256",
        "size",
        "media_type",
        "storage",
        "location",
        "original_name",
        "origin",
        "captured_at",
        "recorded_at",
        "privacy",
        "retention",
        "rights",
        "tags",
        SENSITIVITY_KEY,
    }
)


def check_attachment_id(value: str) -> str:
    """``att-<uuid7>``; anything else (including a path) raises ``IntegrityError``."""

    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise IntegrityError(f"Invalid attachment id: {value!r}")
    return value


def new_attachment_id(now: datetime, entropy: bytes) -> str:
    """``att-<uuid7>``: 48-bit Unix milliseconds of ``now`` plus 74 bits of ``entropy``."""

    if len(entropy) < 10:
        raise ValueError("uuid7 needs at least 10 bytes of entropy")
    millis = int(now.timestamp() * 1000) & ((1 << 48) - 1)
    rand = int.from_bytes(entropy[:10], "big")
    rand_a = (rand >> 68) & 0xFFF
    rand_b = rand & ((1 << 62) - 1)
    value = (millis << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return f"att-{uuid.UUID(int=value)}"


def check_media_type(value: str) -> str:
    text = value.strip().lower() if isinstance(value, str) else ""
    if not _MEDIA_RE.fullmatch(text) or len(text) > 255:
        raise ValueError(f"invalid media type: {value!r}")
    return text


def check_retention(value: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not _RETENTION_RE.fullmatch(text):
        raise ValueError("retention must be keep, review or until:YYYY-MM-DD")
    if text.startswith("until:"):
        date.fromisoformat(text.split(":", 1)[1])
    return text


def clean_name(value: str) -> str:
    """A file's base name for the record: no directories, no control characters."""

    name = value.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(char for char in name if char.isprintable())
    return name.strip()[:255]


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("timestamps must include a timezone")
        return value.isoformat()
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timestamps must include a timezone")
        return value
    raise ValueError("timestamp must be an ISO 8601 string")


@dataclass(frozen=True)
class AttachmentRecord:
    """One attachment record (frontmatter fields plus an optional note)."""

    id: str
    sha256: str
    size: int
    media_type: str
    recorded_at: str
    storage: str = "embedded"
    #: ``<root>:<relative path>``; set exactly when ``storage`` is ``referenced``.
    location: str | None = None
    privacy: str | None = DEFAULT_ATTACHMENT_PRIVACY
    original_name: str | None = None
    origin: str | None = None
    captured_at: str | None = None
    retention: str = "keep"
    rights: str | None = None
    tags: tuple[str, ...] = ()
    note: str = ""
    #: A top-level ``sensitivity`` key as read (hand edits); ``None`` when absent.
    sensitivity: Any = None
    #: Keys this version does not know, kept out of every output.
    ignored_keys: tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        check_attachment_id(self.id)
        if not _SHA_RE.fullmatch(self.sha256):
            raise ValueError("sha256 must be 64 lowercase hex characters")
        if isinstance(self.size, bool) or not isinstance(self.size, int) or self.size < 0:
            raise ValueError("size must be a non-negative integer")
        if self.storage not in STORAGE_KINDS:
            raise ValueError("storage must be embedded or referenced")
        if self.storage == "referenced":
            if self.location is None:
                raise ValueError("a referenced attachment needs a location")
            try:
                Location.parse(self.location)
            except LocationError as exc:
                raise ValueError(f"invalid location: {exc}") from None
        elif self.location is not None:
            raise ValueError("only a referenced attachment has a location")
        check_media_type(self.media_type)
        check_retention(self.retention)
        _iso(self.recorded_at)
        if self.captured_at is not None:
            _iso(self.captured_at)

    @property
    def privacy_level(self) -> PrivacyLevel:
        """The level readers act on (strictest of field, tags and ``sensitivity``)."""

        metadata: dict[str, Any] = {"tags": list(self.tags)}
        if self.privacy is not None:
            metadata["privacy"] = self.privacy
        if self.sensitivity is not None:
            metadata[SENSITIVITY_KEY] = self.sensitivity
        return metadata_level(metadata, legacy=DEFAULT_ATTACHMENT_PRIVACY)

    def frontmatter(self) -> dict[str, Any]:
        """The frontmatter mapping in its written key order (optional keys only when set)."""

        data: dict[str, Any] = {
            "id": self.id,
            "type": RECORD_TYPE,
            "sha256": self.sha256,
            "size": self.size,
            "media_type": self.media_type,
            "storage": self.storage,
        }
        if self.location is not None:
            data["location"] = self.location
        if self.original_name:
            data["original_name"] = self.original_name
        if self.origin:
            data["origin"] = self.origin
        if self.captured_at:
            data["captured_at"] = self.captured_at
        data["recorded_at"] = self.recorded_at
        if self.privacy is not None:
            data["privacy"] = self.privacy
        data["retention"] = self.retention
        if self.rights:
            data["rights"] = self.rights
        if self.tags:
            data["tags"] = list(self.tags)
        if self.sensitivity is not None:
            data[SENSITIVITY_KEY] = self.sensitivity
        return data

    def to_json(self) -> dict[str, Any]:
        data = self.frontmatter()
        data["privacy_level"] = self.privacy_level
        if self.note:
            data["note"] = self.note
        return data


def render_record(record: AttachmentRecord) -> bytes:
    """The record file's bytes. Refuses (``ValueError``) a rendering that does not read back.

    The rendered bytes are parsed again and must give back the same
    frontmatter and note, so no field value (``---`` in a name, origin,
    rights or tag, for example) can ever corrupt a committed record.
    """

    front = yaml.safe_dump(record.frontmatter(), allow_unicode=True, sort_keys=False).rstrip()
    note = record.note.strip()
    raw = f"---\n{front}\n---\n\n{note + chr(10) if note else ''}".encode()
    try:
        parsed = parse_record(raw, record.id)
    except IntegrityError as exc:
        raise ValueError(f"attachment record does not read back: {exc}") from None
    if parsed.frontmatter() != record.frontmatter() or parsed.note != note:
        raise ValueError("attachment record does not read back identically")
    return raw


def _split_frontmatter(text: str) -> tuple[str, str]:
    """(frontmatter, body): the first line is exactly ``---``, so is the closing one.

    Lines are compared whole, so ``---`` inside a value never ends the block.
    """

    lines = text.split("\n")
    if lines[0].rstrip("\r") != "---":
        raise ValueError("missing YAML frontmatter")
    for index in range(1, len(lines)):
        if lines[index].rstrip("\r") == "---":
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1 :])
    raise ValueError("unterminated YAML frontmatter")


def parse_record(raw: bytes, expected_id: str) -> AttachmentRecord:
    """Parse a record file; anything malformed raises ``IntegrityError``."""

    try:
        text = raw.decode("utf-8")
        front, body = _split_frontmatter(text)
        meta = yaml.safe_load(front)
        if not isinstance(meta, dict):
            raise ValueError("frontmatter must be a mapping")
        missing = [key for key in _REQUIRED if key not in meta]
        if missing:
            raise ValueError("missing " + ", ".join(missing))
        if meta["id"] != expected_id:
            raise ValueError("frontmatter id does not match the file name")
        if meta.get("type", RECORD_TYPE) != RECORD_TYPE:
            raise ValueError("type must be attachment")
        tags = meta.get("tags") or []
        if not isinstance(tags, list):
            raise ValueError("tags must be a list")
        privacy = meta.get("privacy")
        return AttachmentRecord(
            id=str(meta["id"]),
            sha256=str(meta["sha256"]),
            size=meta["size"],
            media_type=str(meta["media_type"]),
            recorded_at=_iso(meta["recorded_at"]),
            storage=str(meta["storage"]),
            location=None if meta.get("location") is None else str(meta["location"]),
            # A present but unrecognised value stays as is (read as unknown).
            privacy=None if privacy is None else str(privacy),
            original_name=None if meta.get("original_name") is None else str(meta["original_name"]),
            origin=None if meta.get("origin") is None else str(meta["origin"]),
            captured_at=None if meta.get("captured_at") is None else _iso(meta["captured_at"]),
            retention=str(meta.get("retention", "keep")),
            rights=None if meta.get("rights") is None else str(meta["rights"]),
            tags=tuple(str(tag) for tag in tags),
            note=body.strip(),
            sensitivity=meta.get(SENSITIVITY_KEY),
            ignored_keys=tuple(sorted(str(key) for key in meta if key not in _KNOWN_KEYS)),
        )
    except IntegrityError:
        raise
    except (UnicodeDecodeError, KeyError, TypeError, ValueError, yaml.YAMLError) as exc:
        raise IntegrityError(f"Invalid attachment record {expected_id}: {exc}") from exc


__all__ = [
    "ATTACHMENTS_DIR",
    "DEFAULT_ATTACHMENT_PRIVACY",
    "RECORD_TYPE",
    "STORAGE_KINDS",
    "AttachmentRecord",
    "check_attachment_id",
    "check_media_type",
    "check_retention",
    "clean_name",
    "new_attachment_id",
    "parse_record",
    "render_record",
]

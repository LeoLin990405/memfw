"""Evidence that names a registered original: resolution, gate classes, backlinks (MM3).

The write gate (:func:`memfw.core.epistemic.validate_epistemic_write`) stays
pure: it classifies a pointer by its prefix, and ``att:`` is ``observation``.
This module is the repository half of design v2 H3 / v2.1:

* :func:`gate_evidence_classes` checks every ``att:`` pointer a write *adds*
  (the attachment exists and the locator fits it) and finds the pointers
  that name a registered attachment under another prefix — ``file:`` (the
  blob, a copy of the original, its recorded origin), ``sha256:`` (its hash
  or a prefix of it), or any raw/link pointer carrying its id or full hash —
  and hands them to the gate as ``observation``. Spelling is folded first
  (case, percent-encoding, Unicode compatibility forms, invisible format
  characters), so ``FILE:``, ``%73ha256:`` or a U+200B inside a hash do not
  get around it.
* The legacy rule: a memory that is *already* ``causal=verified`` or
  ``epistemic=fact`` and that the observation rule would now refuse (its
  cited file was registered as an attachment later) is not refused on later
  writes that keep its level: the evidence it already had keeps its old
  class, and only evidence the write adds is reclassified. A write that
  *raises* the level (into ``fact`` or ``verified``) reclassifies all of its
  evidence, so a pointer added at ``hypothesis`` cannot be carried into
  ``verified``. ``fsck`` lists the grandfathered memories for human review
  (:func:`evidence_fsck_issues`).
* :func:`attachment_backlinks` is the read-only backlink query (attachment →
  memories whose evidence names it). It is computed from the memories'
  ``evidence``; nothing is written.

Proxy segments (MM2) are not required: a locator is checked against the
attachment record and resolves at ``attachment`` level; a caller that has a
segment index passes ``segment_exists`` to get ``segment`` level.

When no pointer of a write could name an attachment, nothing is read and the
gate gets no classes, so its result is byte-identical to before MM3.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, cast

from ..core.epistemic import (
    EvidenceClass,
    evidence_source_class,
    validate_epistemic_write,
)
from ..core.errors import IntegrityError, MemfwError
from ..core.evidence_refs import (
    MIN_SHA_PREFIX,
    Locator,
    canonical_reference,
    check_locator,
    embedded_attachment_ids,
    embedded_attachment_references,
    embedded_hashes,
    parse_attachment_reference,
    parse_locator,
    path_pointer,
    sha256_pointer_hex,
    split_scheme,
)

#: ``vault.yaml`` key of the largest original (``memfw.attachments.store``;
#: a test keeps them equal). A cited file larger than this is not hashed.
_MAX_BYTES_KEY: Final = "attachments_max_bytes"
_DEFAULT_MAX_BYTES: Final = 25 * 1024 * 1024
_CHUNK: Final = 1024 * 1024
#: Write-time hashing budget (review M3): at most this many bytes and files are
#: hashed to match a cited file against the originals, per write (and per
#: memory in ``fsck``). A candidate file left unhashed is treated as naming an
#: attachment (fail closed).
MAX_HASH_BYTES: Final = 64 * 1024 * 1024
MAX_HASH_FILES: Final = 32
#: Schemes whose value may name a registered location (MM1b ``referenced``).
_LOCATION_SCHEMES: Final = ("kh", "ha500")

ResolutionLevel = Literal["attachment", "segment"]
#: How a pointer names an attachment: by ``att:``, by hash, by path or content
#: of a local file, by a registered location, or by carrying its id.
CitationVia = Literal["att", "sha256", "file", "content", "location", "embedded"]


@dataclass(frozen=True)
class AttachmentEvidence:
    """One ``att:`` pointer resolved against the vault."""

    reference: str
    attachment_id: str
    media_type: str
    locator: Locator | None
    level: ResolutionLevel


@dataclass(frozen=True)
class AttachmentBacklink:
    """One memory pointer that names an attachment (read-only projection)."""

    memory_id: str
    attachment_id: str
    reference: str
    via: CitationVia
    locator: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "attachment_id": self.attachment_id,
            "reference": self.reference,
            "via": self.via,
            "locator": self.locator,
        }


@dataclass
class _AttachmentIndex:
    """The committed attachments of one vault, loaded once per write."""

    records: dict[str, Any] = field(default_factory=dict)
    broken: dict[str, str] = field(default_factory=dict)
    by_sha: dict[str, list[str]] = field(default_factory=dict)
    by_location: dict[str, list[str]] = field(default_factory=dict)
    blob_dir: Path | None = None
    max_bytes: int = _DEFAULT_MAX_BYTES
    vault: Path | None = None
    sizes: set[int] = field(default_factory=set)
    #: Hash budget left (bytes, files); reset per write / per fsck memory.
    bytes_left: int = MAX_HASH_BYTES
    files_left: int = MAX_HASH_FILES
    #: A candidate file was left unhashed (budget spent) since the last reset.
    unchecked: bool = False
    #: ``False`` for read queries: never hash, never count unchecked files.
    hashing: bool = True
    _content: dict[tuple[int, int, int, int], str | None] = field(default_factory=dict)

    def reset_budget(self) -> None:
        self.bytes_left = MAX_HASH_BYTES
        self.files_left = MAX_HASH_FILES

    @property
    def empty(self) -> bool:
        return not self.records and not self.broken

    def known(self, attachment_id: str) -> bool:
        return attachment_id in self.records or attachment_id in self.broken

    def match_sha_prefix(self, digits: str) -> list[str]:
        if len(digits) < MIN_SHA_PREFIX:
            return []
        found: list[str] = []
        for sha, ids in self.by_sha.items():
            if sha.startswith(digits):
                found.extend(ids)
        return found

    def content_match(self, path: Path) -> list[str]:
        """Ids whose original has the bytes of the regular file ``path``.

        Only a regular file whose size equals a registered original's is
        hashed, within the budget; past it, ``unchecked`` is set instead.
        """

        if not self.by_sha or not self.hashing:
            return []
        try:
            info = os.stat(path)
        except (OSError, ValueError):
            return []
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size > self.max_bytes
            or info.st_size not in self.sizes
        ):
            return []
        key = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        if key not in self._content:
            if self.files_left < 1 or info.st_size > self.bytes_left:
                self.unchecked = True
                return []
            self.files_left -= 1
            self.bytes_left -= info.st_size
            self._content[key] = _hash_file(path)
        digest = self._content[key]
        return list(self.by_sha.get(digest, [])) if digest else []


def _hash_file(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(_CHUNK):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _load_index(host: Any) -> _AttachmentIndex:
    index = _AttachmentIndex(vault=Path(host.root))
    index.reset_budget()
    loader = getattr(host, "attachment_records", None)
    if loader is None:
        return index
    for identifier, record in loader():
        if isinstance(record, MemfwError):
            index.broken[identifier] = str(record)
            continue
        index.records[identifier] = record
        index.by_sha.setdefault(str(record.sha256), []).append(identifier)
        index.sizes.add(int(record.size))
        for text in (getattr(record, "location", None), getattr(record, "origin", None)):
            if text:
                index.by_location.setdefault(_fold_location(str(text)), []).append(identifier)
    if index.empty:
        return index
    try:
        store = host.attachment_blob_store()
    except (IntegrityError, OSError):
        store = None
    if store is not None:
        index.blob_dir = Path(store.directory)
    if host.config_path.exists():
        value = host.read_config().get(_MAX_BYTES_KEY, _DEFAULT_MAX_BYTES)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            index.max_bytes = value
    return index


def _fold_location(text: str) -> str:
    return canonical_reference(text).replace("\\", "/").rstrip("/").lower()


def _real(path: Path) -> Path:
    try:
        return Path(os.path.realpath(path))
    except (OSError, ValueError):
        return path


def _candidate_paths(raw: str, vault: Path | None) -> list[Path]:
    if raw.startswith("~"):
        return [Path(raw).expanduser()]
    path = Path(raw)
    if path.is_absolute():
        return [path]
    paths = [path]
    if vault is not None:
        paths.append(vault / path)
    return paths


def _could_name_attachment(reference: str) -> bool:
    """Whether ``reference`` may name an attachment: decided without any I/O."""

    if embedded_attachment_references(reference):
        return True
    scheme = split_scheme(reference)
    if scheme is None:
        return False
    name = scheme[0]
    if name in ("att", "file", "sha256", *_LOCATION_SCHEMES):
        return True
    if evidence_source_class(reference) not in {"raw", "link"}:
        return False
    return bool(
        embedded_hashes(reference) or embedded_attachment_ids(reference) or path_pointer(reference)
    )


def _citations(reference: str, index: _AttachmentIndex) -> list[tuple[str, CitationVia]]:
    """The attachments ``reference`` names, other than through ``att:``."""

    found: list[tuple[str, CitationVia]] = []
    scheme = split_scheme(reference)
    if scheme is None:
        return found
    name, rest = scheme
    if name == "att":
        return found
    digits = sha256_pointer_hex(reference)
    if digits is not None:
        found.extend((item, "sha256") for item in index.match_sha_prefix(digits))
    for sha in embedded_hashes(reference):
        found.extend((item, "embedded") for item in index.by_sha.get(sha, []))
    for identifier in embedded_attachment_ids(reference):
        if index.known(identifier):
            found.append((identifier, "embedded"))
    if name in _LOCATION_SCHEMES or name == "file":
        location = _fold_location(f"{name}:{rest}")
        found.extend((item, "location") for item in index.by_location.get(location, []))
    raw_path = path_pointer(reference)
    if raw_path is not None:
        folded = _fold_location(raw_path)
        found.extend((item, "location") for item in index.by_location.get(folded, []))
        for path in _candidate_paths(raw_path, index.vault):
            real = _real(path)
            if index.blob_dir is not None:
                blob_dir = _real(index.blob_dir)
                if real == blob_dir or blob_dir in real.parents:
                    # The blob directory itself, a blob, or anything under it.
                    named = index.by_sha.get(real.name, [])
                    found.extend((item, "file") for item in (named or list(index.records)))
                    continue
            found.extend((item, "content") for item in index.content_match(real))
    return found


def resolve_attachment_reference(
    host: Any,
    reference: str,
    *,
    segment_exists: Callable[[str, Locator], bool] | None = None,
    _index: _AttachmentIndex | None = None,
) -> AttachmentEvidence:
    """Resolve one ``att:<id>[#locator]`` pointer; ``IntegrityError`` when it does not.

    The attachment must be committed and its record readable; a locator must
    parse and fit the record's media type (and its ``pages``/``width``/
    ``height``/``duration`` when the record has them). The result is at
    ``segment`` level only when ``segment_exists`` says the locator names a
    proxy segment; otherwise at ``attachment`` level.
    """

    try:
        parsed = parse_attachment_reference(reference)
    except ValueError as exc:
        raise IntegrityError(f"evidence {reference!r} is not a valid att: pointer: {exc}") from None
    if parsed is None:
        raise IntegrityError(f"evidence {reference!r} is not an att: pointer")
    index = _index if _index is not None else _load_index(host)
    if parsed.attachment_id in index.broken:
        raise IntegrityError(
            f"evidence {reference!r} names an attachment whose record cannot be read: "
            + parsed.attachment_id
        )
    record = index.records.get(parsed.attachment_id)
    if record is None:
        raise IntegrityError(f"evidence {reference!r} names a missing attachment")
    locator: Locator | None = None
    level: ResolutionLevel = "attachment"
    if parsed.locator is not None:
        try:
            locator = parse_locator(parsed.locator)
            check_locator(
                locator,
                str(record.media_type),
                pages=getattr(record, "pages", None),
                width=getattr(record, "width", None),
                height=getattr(record, "height", None),
                duration=getattr(record, "duration", None),
            )
        except ValueError as exc:
            raise IntegrityError(f"evidence {reference!r} has an invalid locator: {exc}") from None
        if segment_exists is not None and segment_exists(parsed.attachment_id, locator):
            level = "segment"
    return AttachmentEvidence(
        reference=reference,
        attachment_id=parsed.attachment_id,
        media_type=str(record.media_type),
        locator=locator,
        level=level,
    )


def _att_pointers(reference: str) -> list[str]:
    """The ``att:`` pointers ``reference`` is or carries (``[shot](att:<id>)``)."""

    scheme = split_scheme(reference)
    if scheme is not None and scheme[0] == "att":
        return [reference]
    return [
        f"att:{item.attachment_id}" + (f"#{item.locator}" if item.locator else "")
        for item in embedded_attachment_references(reference)
    ]


def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, Sequence):
        return [str(item) for item in value if str(item).strip()]
    return []


def _observation_overrides(
    references: Iterable[str], index: _AttachmentIndex
) -> dict[str, EvidenceClass]:
    """Pointers that name an attachment under another spelling or prefix → observation."""

    overrides: dict[str, EvidenceClass] = {}
    for reference in references:
        if evidence_source_class(reference) == "observation":
            continue
        scheme = split_scheme(reference)
        index.unchecked = False
        if (
            (scheme is not None and scheme[0] == "att")
            or embedded_attachment_references(reference)
            or _citations(reference, index)
            or index.unchecked
        ):
            overrides[reference] = "observation"
    return overrides


def _raises_level(values: Mapping[str, Any], previous: Mapping[str, Any]) -> bool:
    new_fact = str(values.get("epistemic") or "") == "fact"
    new_verified = str(values.get("causal") or "none") == "verified"
    old_fact = str(previous.get("epistemic") or "") == "fact"
    old_verified = str(previous.get("causal") or "none") == "verified"
    return (new_fact and not old_fact) or (new_verified and not old_verified)


def _legacy_classes(references: Iterable[str]) -> dict[str, EvidenceClass]:
    """How the gate classified these pointers before MM3 (``att:`` was ``other``)."""

    return {
        reference: "other"
        for reference in references
        if evidence_source_class(reference) == "observation"
    }


def _passes(values: Mapping[str, Any], classes: Mapping[str, EvidenceClass]) -> bool:
    try:
        validate_epistemic_write(
            values,
            actor="human",
            previous_causal=str(values.get("causal") or "none"),
            evidence_classes=classes,
        )
    except (IntegrityError, ValueError, TypeError):
        return False
    return True


def _grandfathered(values: Mapping[str, Any], index: _AttachmentIndex) -> bool:
    """Whether a stored state is refused by the observation rule but was valid before it."""

    references = _strings(values.get("evidence"))
    fresh = _observation_overrides(references, index)
    if _passes(values, fresh):
        return False
    return _passes(values, _legacy_classes(references))


def gate_evidence_classes(
    host: Any,
    values: Mapping[str, Any],
    previous: Mapping[str, Any] | None = None,
) -> dict[str, EvidenceClass]:
    """The ``evidence_classes`` to hand the gate for one write (and its ``att:`` checks).

    ``values`` is the prospective document state, ``previous`` the stored one
    (``None`` for a new memory). Raises ``IntegrityError`` when a pointer the
    write adds is an ``att:`` that does not resolve. Returns ``{}`` without
    any I/O when no pointer could name an attachment.
    """

    references = _strings(values.get("evidence"))
    if not any(_could_name_attachment(item) for item in references):
        return {}
    old = set(_strings(previous.get("evidence"))) if previous is not None else set()
    added = [item for item in references if item not in old]
    index = _load_index(host)
    for reference in added:
        for pointer in _att_pointers(reference):
            resolve_attachment_reference(host, pointer, _index=index)
    overrides = _observation_overrides(references, index)
    carried = [item for item in references if item in old]
    if (
        previous is not None
        and carried
        and not _raises_level(values, previous)
        and _grandfathered(previous, index)
    ):
        # Legacy rule (v2.1 H3): only the evidence this write adds is
        # reclassified; what the memory already cited keeps its old class.
        for reference in carried:
            overrides.pop(reference, None)
        overrides.update(_legacy_classes(carried))
    return overrides


def attachment_backlinks(
    host: Any,
    attachment_id: str | None = None,
    *,
    documents: Iterable[Any] | None = None,
) -> list[AttachmentBacklink]:
    """Memory pointers that name an attachment (all, or ``attachment_id``'s). Read-only.

    Matches ``att:`` pointers in any spelling, ``sha256:`` hashes and
    prefixes, the blob path, a recorded location and an embedded id or hash.
    A ``file:`` pointer to a *copy* of an original is matched by content in
    the write gate only; backlinks do not hash files. Sorted by memory id,
    then pointer.
    """

    wanted = attachment_id.strip().lower() if attachment_id is not None else None
    if documents is None:
        documents = host.list()
    index: _AttachmentIndex | None = None
    links: list[AttachmentBacklink] = []
    for document in documents:
        for reference in _strings(getattr(document, "evidence", None)):
            if not _could_name_attachment(reference):
                continue
            scheme = split_scheme(reference)
            if scheme is not None and scheme[0] == "att":
                try:
                    parsed = parse_attachment_reference(reference)
                except ValueError:
                    continue
                if parsed is not None and wanted in (None, parsed.attachment_id):
                    links.append(
                        AttachmentBacklink(
                            str(document.id), parsed.attachment_id, reference, "att", parsed.locator
                        )
                    )
                continue
            for embedded in embedded_attachment_references(reference):
                if wanted in (None, embedded.attachment_id):
                    links.append(
                        AttachmentBacklink(
                            str(document.id),
                            embedded.attachment_id,
                            reference,
                            "att",
                            embedded.locator,
                        )
                    )
            if index is None:
                index = _load_index(host)
                index.hashing = False  # a read query never hashes files
            seen: set[str] = set()
            for identifier, via in _citations(reference, index):
                if identifier in seen or wanted not in (None, identifier):
                    continue
                seen.add(identifier)
                links.append(AttachmentBacklink(str(document.id), identifier, reference, via))
    return sorted(links, key=lambda item: (item.memory_id, item.reference, item.attachment_id))


def evidence_fsck_issues(host: Any, documents: Iterable[Any]) -> list[dict[str, str | None]]:
    """``fsck`` rows for attachment evidence; empty (no I/O past the scan) without any.

    * ``evidence_attachment_invalid`` — an ``att:`` pointer that is malformed,
      names a missing or unreadable attachment, or has a locator that does
      not fit it;
    * ``evidence_observation_review`` — a memory kept by the legacy rule: its
      ``fact``/``verified`` rests only on evidence now classified as
      observation. It stays writable; a human should review it.
    """

    candidates = [
        document
        for document in documents
        if any(
            _could_name_attachment(item) for item in _strings(getattr(document, "evidence", None))
        )
    ]
    if not candidates:
        return []
    index = _load_index(host)
    issues: list[dict[str, str | None]] = []
    for document in candidates:
        index.reset_budget()
        for pointer in (
            item for reference in _strings(document.evidence) for item in _att_pointers(reference)
        ):
            try:
                resolve_attachment_reference(host, pointer, _index=index)
            except IntegrityError as exc:
                issues.append(
                    {
                        "memory_id": str(document.id),
                        "kind": "evidence_attachment_invalid",
                        "expected_hash": None,
                        "actual_hash": None,
                        "message": str(exc),
                    }
                )
        values = cast(Any, document).model_dump()
        if _grandfathered(values, index):
            issues.append(
                {
                    "memory_id": str(document.id),
                    "kind": "evidence_observation_review",
                    "expected_hash": None,
                    "actual_hash": None,
                    "message": (
                        "epistemic/causal rests only on evidence now classified as "
                        "observation (a registered attachment); kept by the legacy rule, "
                        "review it"
                    ),
                }
            )
    return issues


__all__ = [
    "AttachmentBacklink",
    "AttachmentEvidence",
    "CitationVia",
    "ResolutionLevel",
    "attachment_backlinks",
    "evidence_fsck_issues",
    "gate_evidence_classes",
    "resolve_attachment_reference",
]

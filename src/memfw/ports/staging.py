"""Reference staging for ingestors: candidates only, idempotent, contained.

An ingestor may call ``stage_candidates`` instead of writing files itself.
It enforces the ingest contract: a proposal id and a source are required,
files land only under ``<vault>/_candidates/<ingestor>/``, and staging the
same record twice returns the same candidate id without writing again.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from collections.abc import Callable, Sequence
from pathlib import Path

from .types import DerivedView, IngestItem, StageResult, check_name

CANDIDATES_DIR = "_candidates"

_PROPOSAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SOURCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


class StagingError(ValueError):
    """The ingest contract was not met; nothing was written."""


def content_sha256(item: IngestItem) -> str:
    """Digest of what an item says: title, body and metadata."""

    payload = json.dumps(
        {"title": item.title, "body": item.body, "metadata": dict(sorted(item.metadata.items()))},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def candidate_id(ingestor: str, source: str, item: IngestItem) -> str:
    """Stable id from who staged it, where it came from, its ref and its content.

    The content is part of the id: the same ``external_ref`` with changed
    content is a new candidate, never silently dropped as a duplicate.
    """

    key = "\x00".join((ingestor, source, item.external_ref, content_sha256(item)))
    return f"cand-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:24]}"


def candidate_directory(vault: Path, ingestor: str) -> Path:
    """``<vault>/_candidates/<ingestor>``; refuses symlinks on the way."""

    check_name(ingestor, what="ingestor")
    root = Path(vault).expanduser().resolve()
    if not (root / "vault.yaml").is_file():
        raise StagingError("vault root does not exist")
    base = root / CANDIDATES_DIR
    target = base / ingestor
    for part in (base, target):
        if part.is_symlink():
            raise StagingError(f"{part.name} must not be a symlink")
    target.mkdir(parents=True, exist_ok=True)
    resolved = target.resolve()
    if resolved.parent != base.resolve() or base.resolve().parent != root:
        raise StagingError("candidate directory escapes the vault")
    return resolved


def check_ingest_args(proposal_id: str, source: str) -> None:
    """The ingest contract's required arguments. Raises ``StagingError``."""

    if not isinstance(proposal_id, str) or not _PROPOSAL_RE.fullmatch(proposal_id):
        raise StagingError("proposal_id required")
    if not isinstance(source, str) or not _SOURCE_RE.fullmatch(source):
        raise StagingError("source required")


def stage_candidates(
    vault: Path,
    ingestor: str,
    items: Sequence[IngestItem],
    *,
    proposal_id: str,
    source: str,
) -> StageResult:
    """Write one JSON candidate per item; identical existing candidates are reused.

    Every item is validated and serialised before anything is written, so a
    bad item leaves the directory untouched. Each file is written to a
    temporary name and linked into place, which fails if the name exists;
    an existing file counts as a duplicate only when its content digest
    matches, otherwise staging stops with ``StagingError``.
    """

    check_ingest_args(proposal_id, source)
    check_name(ingestor, what="ingestor")
    prepared: list[tuple[str, str, bytes]] = []
    batch: set[str] = set()
    duplicates: list[str] = []
    for item in items:
        if not isinstance(item, IngestItem):
            raise StagingError("items must be IngestItem values")
        try:
            digest = content_sha256(item)
            identifier = candidate_id(ingestor, source, item)
            record = {
                "candidate_id": identifier,
                "content_sha256": digest,
                "ingestor": ingestor,
                "proposal_id": proposal_id,
                "source": source,
                "external_ref": item.external_ref,
                "title": item.title,
                "body": item.body,
                "metadata": dict(sorted(item.metadata.items())),
                "status": "staged",
            }
            data = json.dumps(
                record, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise StagingError("item is not serialisable") from exc
        if identifier in batch:
            duplicates.append(identifier)
            continue
        batch.add(identifier)
        prepared.append((identifier, digest, data + b"\n"))
    directory = candidate_directory(vault, ingestor)
    staged: list[str] = []
    for identifier, digest, data in prepared:
        if _write_new(directory, identifier, data):
            staged.append(identifier)
            continue
        if _existing_digest(directory / f"{identifier}.json") != digest:
            raise StagingError("a different candidate already uses this id")
        duplicates.append(identifier)
    return StageResult(staged=tuple(staged), duplicates=tuple(duplicates))


def _write_new(directory: Path, identifier: str, data: bytes) -> bool:
    """Atomically create ``<identifier>.json``. ``False`` when it already exists."""

    return create_exclusive(directory, identifier, ".json", data)


def create_exclusive(directory: Path, stem: str, suffix: str, data: bytes) -> bool:
    """Atomically create ``<stem><suffix>`` in ``directory``; ``False`` if it exists.

    The data is written and fsynced under a temporary name first and then
    linked into place, so a reader never sees a partial file and an existing
    file is never replaced.
    """

    temporary = directory / f".{stem}.{secrets.token_hex(8)}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, directory / f"{stem}{suffix}")
        except FileExistsError:
            return False
        return True
    finally:
        temporary.unlink(missing_ok=True)


def _existing_digest(path: Path) -> str:
    if path.is_symlink():
        return ""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    value = record.get("content_sha256") if isinstance(record, dict) else None
    return value if isinstance(value, str) else ""


def check_view_citations(view: DerivedView, exists: Callable[[str], bool]) -> DerivedView:
    """Reject a view that cites a memory the vault does not have.

    A citation that names no memory (empty ``memory_id``) or whose ``ref`` is
    not a ``mem:`` ref is refused before ``exists`` is asked (ports 0.4.0), so
    a path-based ``exists`` can never accept a ``kh:``/``att:``/``wiki:`` ref.
    """

    for citation in view.citations:
        if not citation.memory_id or citation.ref_kind not in (None, "memory"):
            raise ValueError("view cites something that is not a memory")
    missing = [memory_id for memory_id in view.cited_ids() if not exists(memory_id)]
    if missing:
        raise ValueError(f"view cites {len(missing)} unknown memory id(s)")
    return view


__all__ = [
    "CANDIDATES_DIR",
    "StagingError",
    "candidate_directory",
    "candidate_id",
    "check_ingest_args",
    "content_sha256",
    "create_exclusive",
    "check_view_citations",
    "stage_candidates",
]

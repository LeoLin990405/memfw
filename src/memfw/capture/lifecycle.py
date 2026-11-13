"""Lifecycle helpers for staged memory candidates."""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from ..core.context import RepositoryHost
from ..core.errors import IntegrityError
from .review import (
    CANDIDATE_TTL_DAYS,
    Candidate,
    candidate_content_hash,
    candidate_dict,
    candidate_is_expired,
    candidate_provenance,
    parse_candidate_provenance,
    read_candidate,
    staged_candidate_paths,
    validate_candidate_hash,
)


def _candidate_raw_body(candidate: Candidate) -> str:
    try:
        text = candidate.path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IntegrityError(f"Invalid candidate: {candidate.path}") from exc
    pieces = text.split("---\n", 2)
    if len(pieces) != 3 or pieces[0]:
        raise IntegrityError(f"Invalid candidate: {candidate.path}")
    return pieces[2]


def _replace_candidate(candidate: Candidate, frontmatter: dict[str, Any], raw_body: str) -> None:
    """Rewrite the frontmatter atomically. The body bytes are kept as they are."""

    header = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False)
    updated = f"---\n{header}---\n{raw_body}".encode()

    descriptor, temporary_name = tempfile.mkstemp(
        dir=candidate.path.parent,
        prefix=f".{candidate.path.name}.",
        suffix=".tmp",
    )
    temporary = candidate.path.parent / temporary_name
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, candidate.path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def mark_candidate_applied(candidate: Candidate, memory_id: str) -> None:
    validate_candidate_hash(candidate)
    current = read_candidate(candidate.path)
    validate_candidate_hash(current)
    if current.body != candidate.body:
        raise IntegrityError("hash_mismatch")
    raw_body = _candidate_raw_body(candidate)

    frontmatter = dict(current.frontmatter)
    # Applying an expired candidate lifts the expiry; ``expired_at`` stays as history.
    frontmatter.pop("expired", None)
    if frontmatter.get("lifecycle") == "expired":
        frontmatter["lifecycle"] = "active"
    metadata = frontmatter.get("metadata")
    if isinstance(metadata, dict) and metadata.get("lifecycle") == "expired":
        frontmatter["metadata"] = {**metadata, "lifecycle": "active"}
    frontmatter.update(
        {
            "applied": True,
            "applied_memory_id": memory_id,
            "applied_at": datetime.now(UTC).isoformat(),
            "status": "applied",
        }
    )
    _replace_candidate(candidate, frontmatter, raw_body)

    candidate.frontmatter = frontmatter
    candidate.status = "applied"
    candidate.applied_memory_id = memory_id


def mark_expired_candidates(
    root: Path | str,
    *,
    now: datetime | None = None,
    ttl_days: int = CANDIDATE_TTL_DAYS,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Mark pending candidates past the TTL as expired. Nothing is deleted.

    The mark is ``lifecycle: expired``, ``expired: true``, ``expired_at`` and
    ``status: expired`` in the frontmatter; the body and its hash stay as they
    are, so ``candidate-apply`` can still apply the file by path. Symlinks,
    unreadable files and files whose body no longer matches ``content_hash``
    are skipped and listed.
    """

    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must include a timezone")
    marked: list[str] = []
    skipped: list[str] = []
    for path in staged_candidate_paths(root):
        if path.is_symlink() or not path.is_file():
            skipped.append(path.stem)
            continue
        try:
            raw = path.read_bytes()
            candidate = read_candidate(path)
            validate_candidate_hash(candidate)
        except (OSError, IntegrityError):
            skipped.append(path.stem)
            continue
        # The parsed frontmatter must come from the same bytes the body does.
        if path.read_bytes() != raw:
            skipped.append(path.stem)
            continue
        if candidate.status != "pending" or not candidate_is_expired(
            candidate, now=current, ttl_days=ttl_days
        ):
            continue
        marked.append(candidate.candidate_id)
        if dry_run:
            continue
        raw_body = raw.decode("utf-8").split("---\n", 2)[2]
        frontmatter = dict(candidate.frontmatter)
        frontmatter.update(
            {
                "lifecycle": "expired",
                "expired": True,
                "expired_at": current.astimezone(UTC).isoformat(),
                "status": "expired",
            }
        )
        metadata = frontmatter.get("metadata")
        if isinstance(metadata, dict) and "lifecycle" in metadata:
            frontmatter["metadata"] = {**metadata, "lifecycle": "expired"}
        # A concurrent candidate-apply rewrote the file: leave its mark alone.
        # This narrows the window; candidate-apply takes no lock on the file.
        if path.read_bytes() != raw:
            marked.pop()
            skipped.append(candidate.candidate_id)
            continue
        _replace_candidate(candidate, frontmatter, raw_body)
    return {
        "ttl_days": ttl_days,
        "dry_run": dry_run,
        "expired": marked,
        "skipped": skipped,
        "deleted": 0,
    }


def recoverable_identity(candidate: Candidate) -> bool:
    """Whether a retry could find the memory this candidate already wrote.

    The recovery path matches on the three identity fields, so a candidate that
    is missing one of them -- a legacy candidate with no ``proposal_id`` -- has
    nothing to match on. Applying it anyway would write a second, duplicate
    memory if the first write succeeded but the marking failed, so the caller
    refuses such a candidate before the first write.
    """
    expected = parse_candidate_provenance(candidate_provenance(candidate))
    if expected is None:
        return False
    return all(field in expected for field in _IDENTITY_FIELDS)


def find_applied_memory_id(repository: RepositoryHost, candidate: Candidate) -> str | None:
    expected = parse_candidate_provenance(candidate_provenance(candidate))
    if expected is None or any(field not in expected for field in _IDENTITY_FIELDS):
        return None
    for document in repository.list():
        actual = parse_candidate_provenance(document.provenance)
        if actual is not None and all(
            actual.get(field) == expected[field] for field in _IDENTITY_FIELDS
        ):
            return document.id
    return None


#: The provenance fields a retry matches on. A candidate missing one of them
#: cannot be recovered, so it is refused before the first write.
_IDENTITY_FIELDS = ("candidate_id", "proposal_id", "content_hash")

__all__ = [
    "Candidate",
    "recoverable_identity",
    "candidate_content_hash",
    "candidate_dict",
    "candidate_provenance",
    "find_applied_memory_id",
    "mark_candidate_applied",
    "mark_expired_candidates",
    "parse_candidate_provenance",
    "read_candidate",
    "validate_candidate_hash",
]

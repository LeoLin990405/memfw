"""Lifecycle helpers for staged memory candidates."""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime

import yaml

from ..core.context import RepositoryHost
from ..core.errors import IntegrityError
from .review import (
    Candidate,
    candidate_content_hash,
    candidate_dict,
    candidate_provenance,
    parse_candidate_provenance,
    read_candidate,
    validate_candidate_hash,
)


def mark_candidate_applied(candidate: Candidate, memory_id: str) -> None:
    validate_candidate_hash(candidate)
    current = read_candidate(candidate.path)
    validate_candidate_hash(current)
    if current.body != candidate.body:
        raise IntegrityError("hash_mismatch")

    try:
        text = candidate.path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IntegrityError(f"Invalid candidate: {candidate.path}") from exc
    pieces = text.split("---\n", 2)
    if len(pieces) != 3 or pieces[0]:
        raise IntegrityError(f"Invalid candidate: {candidate.path}")
    raw_body = pieces[2]

    frontmatter = dict(current.frontmatter)
    frontmatter.update(
        {
            "applied": True,
            "applied_memory_id": memory_id,
            "applied_at": datetime.now(UTC).isoformat(),
            "status": "applied",
        }
    )
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

    candidate.frontmatter = frontmatter
    candidate.status = "applied"
    candidate.applied_memory_id = memory_id


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
    "parse_candidate_provenance",
    "read_candidate",
    "validate_candidate_hash",
]

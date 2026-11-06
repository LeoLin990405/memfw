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


def find_applied_memory_id(repository: RepositoryHost, candidate: Candidate) -> str | None:
    expected = parse_candidate_provenance(candidate_provenance(candidate))
    identity_fields = ("candidate_id", "proposal_id", "content_hash")
    if expected is None or any(field not in expected for field in identity_fields):
        return None
    for document in repository.list():
        actual = parse_candidate_provenance(document.provenance)
        if actual is not None and all(
            actual.get(field) == expected[field] for field in identity_fields
        ):
            return document.id
    return None


__all__ = [
    "Candidate",
    "candidate_content_hash",
    "candidate_dict",
    "candidate_provenance",
    "find_applied_memory_id",
    "mark_candidate_applied",
    "parse_candidate_provenance",
    "read_candidate",
    "validate_candidate_hash",
]

"""Read-only review view over staged memory candidates."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from ..core.errors import IntegrityError


@dataclass
class Candidate:
    path: Path
    candidate_id: str
    proposal_id: str | None
    source: str
    title: str
    body: str
    tags: list[str]
    related: list[str]
    suggestions: list[Any]
    tenant: str
    content_hash: str | None
    created_at: str | None
    status: str
    applied_memory_id: str | None
    frontmatter: dict[str, Any]


def _field(frontmatter: dict[str, Any], metadata: dict[str, Any], key: str) -> Any:
    if key in frontmatter:
        return frontmatter[key]
    return metadata.get(key)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return [str(value)]


def _suggestions(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def read_candidate(path: Path | str) -> Candidate:
    candidate_path = Path(path)
    try:
        text = candidate_path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IntegrityError(f"Invalid candidate: {candidate_path}") from exc
    if not text.startswith("---\n"):
        raise IntegrityError(f"Candidate has no frontmatter: {candidate_path}")
    pieces = text.split("---\n", 2)
    if len(pieces) != 3:
        raise IntegrityError(f"Invalid candidate: {candidate_path}")
    header, raw_body = pieces[1], pieces[2]
    try:
        parsed = yaml.safe_load(header) or {}
    except yaml.YAMLError as exc:
        raise IntegrityError(f"Invalid candidate: {candidate_path}") from exc
    if not isinstance(parsed, dict):
        raise IntegrityError(f"Invalid candidate: {candidate_path}")
    frontmatter = dict(parsed)
    raw_metadata = frontmatter.get("metadata")
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}

    body = raw_body[1:] if raw_body.startswith("\n") else raw_body
    body = body[:-1] if body.endswith("\n") else body
    title_value = frontmatter["title"] if "title" in frontmatter else frontmatter.get("name")
    applied_memory_id = _optional_text(frontmatter.get("applied_memory_id"))
    applied = frontmatter.get("applied") is True
    return Candidate(
        path=candidate_path,
        candidate_id=candidate_path.stem,
        proposal_id=_optional_text(_field(frontmatter, metadata, "proposal_id")),
        source=str(_field(frontmatter, metadata, "source") or "candidate"),
        title=str(title_value or "memory"),
        body=body,
        tags=_string_list(_field(frontmatter, metadata, "tags")),
        related=_string_list(_field(frontmatter, metadata, "related")),
        suggestions=_suggestions(_field(frontmatter, metadata, "suggestions")),
        tenant=str(_field(frontmatter, metadata, "tenant") or ""),
        content_hash=_optional_text(_field(frontmatter, metadata, "content_hash")),
        created_at=_optional_text(_field(frontmatter, metadata, "created_at")),
        status="applied" if applied else "pending",
        applied_memory_id=applied_memory_id,
        frontmatter=frontmatter,
    )


def candidate_dict(candidate: Candidate) -> dict[str, Any]:
    return {
        "path": str(candidate.path),
        "title": candidate.title,
        "body": candidate.body,
        "tags": list(candidate.tags),
        "related": list(candidate.related),
        "suggestions": list(candidate.suggestions),
        "tenant": candidate.tenant,
        "candidate_id": candidate.candidate_id,
        "proposal_id": candidate.proposal_id,
        "source": candidate.source,
        "content_hash": candidate.content_hash,
        "created_at": candidate.created_at,
        "status": candidate.status,
        "applied_memory_id": candidate.applied_memory_id,
    }


def candidate_content_hash(candidate: Candidate) -> str:
    return "sha256:" + hashlib.sha256(candidate.body.encode("utf-8")).hexdigest()


def validate_candidate_hash(candidate: Candidate) -> None:
    if candidate.content_hash is not None and candidate.content_hash != candidate_content_hash(
        candidate
    ):
        raise IntegrityError("hash_mismatch")


def _escape_provenance_value(value: str) -> str:
    return value.replace("%", "%25").replace(";", "%3B").replace("=", "%3D")


def _unescape_provenance_value(value: str) -> str | None:
    if not value or "=" in value:
        return None
    index = value.find("%")
    while index >= 0:
        if value[index : index + 3] not in ("%25", "%3B", "%3D"):
            return None
        index = value.find("%", index + 3)
    return value.replace("%3B", ";").replace("%3D", "=").replace("%25", "%")


def candidate_provenance(candidate: Candidate) -> str:
    parts = [f"candidate:{_escape_provenance_value(candidate.candidate_id)}"]
    if candidate.proposal_id:
        parts.append(f"proposal_id={_escape_provenance_value(candidate.proposal_id)}")
    parts.append(f"source={_escape_provenance_value(candidate.source or 'candidate')}")
    if candidate.content_hash:
        parts.append(f"content_hash={_escape_provenance_value(candidate.content_hash)}")
    return ";".join(parts)


def parse_candidate_provenance(provenance: str) -> dict[str, str] | None:
    parts = provenance.split(";")
    if not 2 <= len(parts) <= 4 or not parts[0].startswith("candidate:"):
        return None

    raw_values = {"candidate_id": parts[0].removeprefix("candidate:")}
    index = 1
    if parts[index].startswith("proposal_id="):
        raw_values["proposal_id"] = parts[index].removeprefix("proposal_id=")
        index += 1
    if index >= len(parts) or not parts[index].startswith("source="):
        return None
    raw_values["source"] = parts[index].removeprefix("source=")
    index += 1
    if index < len(parts):
        if not parts[index].startswith("content_hash="):
            return None
        raw_values["content_hash"] = parts[index].removeprefix("content_hash=")
        index += 1
    if index != len(parts):
        return None

    values: dict[str, str] = {}
    for key, raw_value in raw_values.items():
        value = _unescape_provenance_value(raw_value)
        if value is None:
            return None
        values[key] = value
    return values


def list_staged_candidates(root: Path | str) -> list[dict[str, Any]]:
    base = Path(root).expanduser() / "_candidates" / "remember"
    if not base.is_dir():
        return []
    output: list[dict[str, Any]] = []
    for path in sorted(base.glob("*.md")):
        candidate = read_candidate(path)
        validate_candidate_hash(candidate)
        output.append(candidate_dict(candidate))
    return output


__all__ = [
    "Candidate",
    "candidate_content_hash",
    "candidate_dict",
    "candidate_provenance",
    "list_staged_candidates",
    "parse_candidate_provenance",
    "read_candidate",
    "validate_candidate_hash",
]

"""Stage experience-point proposals under ``_candidates/experience/``.

A staged file is a *candidate*: a human review surface, never the vault. The
authoritative write stays a separate human action (see
``docs/experience-points.md``). Files are published with ``O_EXCL`` so two
drafts never silently overwrite each other, and the vault is not touched.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from ..core.errors import IntegrityError
from .model import DraftDocument, candidate_filename

CANDIDATE_DIR = Path("_candidates") / "experience"


def render_candidate(draft: DraftDocument, *, created_at: str) -> str:
    """One candidate Markdown file: the reviewed record and the ready payload."""

    experience = draft.values["experience"]
    review = experience.review
    front: dict[str, Any] = {
        "type": "experience-point-candidate",
        "authority": False,
        "epistemic": "experience",
        "causal": "none",
        "title": draft.title,
        "created_at": created_at,
        "verdict": review.verdict,
        "reviewer": review.reviewer,
        "experience": experience.model_dump(mode="json"),
        "payload": draft.payload,
    }
    header = yaml.safe_dump(front, allow_unicode=True, sort_keys=False).rstrip()
    if "\n---\n" in header:
        raise IntegrityError("experience candidate frontmatter is not safe to publish")
    return f"---\n{header}\n---\n\n{draft.body.rstrip()}\n"


def _publish(directory: Path, filename: str, text: str) -> None:
    descriptor = os.open(
        directory / filename,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def stage_candidate(
    root: Path | str,
    draft: DraftDocument,
    *,
    now: datetime | None = None,
) -> Path:
    """Write one candidate file; return its path relative to ``root``.

    Never writes ``memories/`` or the event log. Existing files are left alone:
    a name collision adds a numeric suffix instead of overwriting.
    """

    base = Path(root).expanduser()
    directory = base / CANDIDATE_DIR
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise IntegrityError("refusing to stage into a symlinked candidate directory")
    created_at = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    filename = candidate_filename(draft, now)
    stem, suffix = filename.rsplit(".", 1)
    for attempt in range(100):
        name = filename if attempt == 0 else f"{stem}-{attempt}.{suffix}"
        try:
            _publish(directory, name, render_candidate(draft, created_at=created_at))
        except FileExistsError:
            continue
        return CANDIDATE_DIR / name
    raise IntegrityError("could not find a free candidate filename")


def render_invalidation(
    *,
    memory_id: str,
    title: str,
    reason: str,
    experience: Any,
    created_at: str,
) -> str:
    """A pending-revision proposal for an experience point whose conditions changed."""

    front: dict[str, Any] = {
        "type": "experience-invalidation",
        "authority": False,
        "experience_id": memory_id,
        "title": title,
        "action": "revise",
        "reason": reason,
        "created_at": created_at,
        "epistemic": "experience",
        "causal": "none",
        "suggested_changes": {
            "experience": {
                **experience.model_dump(mode="json"),
                "invalidated_when": [
                    *list(experience.invalidated_when),
                    f"invalidated: {reason}",
                ],
            },
            "valid_to": created_at,
        },
    }
    header = yaml.safe_dump(front, allow_unicode=True, sort_keys=False).rstrip()
    body = (
        "Experience-point invalidation proposal.\n\n"
        f"Reason: {reason}\n\n"
        "This is a proposal only. It does not edit the vault; a human decides "
        "whether to revise the record or supersede it with a new experience point.\n"
    )
    return f"---\n{header}\n---\n\n{body}"


def stage_invalidation(
    root: Path | str,
    *,
    memory_id: str,
    title: str,
    reason: str,
    experience: Any,
    now: datetime | None = None,
) -> Path:
    """Write one invalidation proposal and return its path relative to ``root``."""

    base = Path(root).expanduser()
    directory = base / CANDIDATE_DIR
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise IntegrityError("refusing to stage into a symlinked candidate directory")
    created_at = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    stamp = datetime.fromisoformat(created_at).strftime("%Y%m%dT%H%M%SZ")
    filename = f"{stamp}-invalidate-{memory_id}.md"
    stem, suffix = filename.rsplit(".", 1)
    for attempt in range(100):
        name = filename if attempt == 0 else f"{stem}-{attempt}.{suffix}"
        text = render_invalidation(
            memory_id=memory_id,
            title=title,
            reason=reason,
            experience=experience,
            created_at=created_at,
        )
        try:
            _publish(directory, name, text)
        except FileExistsError:
            continue
        return CANDIDATE_DIR / name
    raise IntegrityError("could not find a free invalidation filename")


__all__ = [
    "CANDIDATE_DIR",
    "render_candidate",
    "render_invalidation",
    "stage_candidate",
    "stage_invalidation",
]

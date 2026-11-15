"""Experience points: what a human accepted for one task in one reviewed scope.

An experience point is written only after a task ended *and* a human reviewed
it. It says "this was acceptable here, this time", never "this causes that" and
never "do this automatically next time" (see the fixed disclaimer in
:data:`memfw.core.epistemic.EXPERIENCE_DISCLAIMER`).

The JSON draft input documented in ``docs/experience-points.md`` is validated
here; ``draft`` turns it into a memory document payload and a staged proposal
candidate. Nothing in this module writes the authoritative vault.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..core.epistemic import (
    EXPERIENCE_DISCLAIMER,
    EXPERIENCE_POINT_TYPE,
    ExperiencePoint,
    ExperienceReview,
    validate_epistemic_write,
)
from ..core.errors import IntegrityError

SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


class ExperienceDraftInput(BaseModel):
    """The documented ``--from`` schema. Unknown keys are rejected."""

    model_config = ConfigDict(extra="forbid")

    task: str
    conditions: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    review: ExperienceReview
    version: int = 1
    counterexamples: list[str] = Field(default_factory=list)
    invalidated_when: list[str] = Field(default_factory=list)
    # Presentation only: both have safe defaults.
    title: str | None = None
    body: str = ""


@dataclass(frozen=True)
class DraftDocument:
    """A validated, not-yet-written experience point."""

    title: str
    body: str
    values: dict[str, Any]
    payload: dict[str, Any]


def load_draft_input(path: Path | str) -> ExperienceDraftInput:
    """Read and validate one draft JSON file.

    The two preconditions the task requires are part of the schema:
    ``review.verdict`` is ``accepted`` or ``failed`` and ``review.reviewer`` is
    non-empty. A draft that cannot be tied to a review is refused here.
    """

    source = Path(path).expanduser()
    try:
        raw = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise IntegrityError(f"cannot read experience draft: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise IntegrityError(f"experience draft is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise IntegrityError("experience draft must be a JSON object")
    try:
        return ExperienceDraftInput.model_validate(data)
    except ValidationError as exc:
        raise IntegrityError(f"experience draft is invalid: {exc}") from exc


def build_draft(draft: ExperienceDraftInput) -> DraftDocument:
    """Turn a validated draft into the document payload for the write gate."""

    experience = ExperiencePoint(
        task=draft.task,
        conditions=list(draft.conditions),
        actions=list(draft.actions),
        evidence=list(draft.evidence),
        review=draft.review,
        version=draft.version,
        counterexamples=list(draft.counterexamples),
        invalidated_when=list(draft.invalidated_when),
    )
    title = (draft.title or f"Experience point: {draft.task}").strip()
    body = _compose_body(draft, experience)
    values: dict[str, Any] = {
        "title": title,
        "body": body,
        "type": EXPERIENCE_POINT_TYPE,
        "epistemic": "experience",
        "causal": "none",
        "experience": experience,
        "evidence": list(draft.evidence),
        "source": "experience-draft",
        "provenance": "human-reviewed",
        "tags": [f"type:{EXPERIENCE_POINT_TYPE}", f"verdict:{draft.review.verdict}"],
    }
    validate_epistemic_write(values, actor="human")
    payload = {
        "title": title,
        "body": body,
        "type": EXPERIENCE_POINT_TYPE,
        "epistemic": "experience",
        "causal": "none",
        "experience": experience.model_dump(mode="json"),
        "tags": values["tags"],
        "source": values["source"],
        "provenance": values["provenance"],
    }
    return DraftDocument(title=title, body=body, values=values, payload=payload)


def _compose_body(draft: ExperienceDraftInput, experience: ExperiencePoint) -> str:
    """The fixed disclaimer first, then the reviewed facts, never a causal claim."""

    lines = [EXPERIENCE_DISCLAIMER, "", f"Task: {experience.task}"]
    if experience.conditions:
        lines.append("Conditions:")
        lines.extend(f"- {item}" for item in experience.conditions)
    if experience.actions:
        lines.append("Actions:")
        lines.extend(f"- {item}" for item in experience.actions)
    review = experience.review
    lines.append(f"Review: {review.verdict} by {review.reviewer}")
    if review.scope_reviewed:
        lines.append("Reviewed: " + "; ".join(review.scope_reviewed))
    if review.not_reviewed:
        lines.append("Not reviewed: " + "; ".join(review.not_reviewed))
    if experience.invalidated_when:
        lines.append("Invalidated when: " + "; ".join(experience.invalidated_when))
    if draft.body.strip():
        lines.extend(["", draft.body.strip()])
    return "\n".join(lines).rstrip() + "\n"


def candidate_filename(draft: DraftDocument, now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    slug = SLUG_RE.sub("-", draft.title).strip("-")[:60] or "experience-point"
    return f"{stamp}-{slug}.md"


__all__ = [
    "DraftDocument",
    "ExperienceDraftInput",
    "build_draft",
    "candidate_filename",
    "load_draft_input",
]

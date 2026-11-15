"""Epistemic type and causal grade for memory documents.

Three things are kept apart (see ``docs/epistemic-and-experience.md``):

* ``association`` — two things were seen together;
* ``hypothesis`` — a causal claim that has not passed its identification checks;
* ``verified`` — a causal claim whose identification and robustness were checked.

An *experience point* is a fourth thing: what a human accepted for one task in
one reviewed scope. It is never a causal conclusion and it never authorises
automatic reuse.

The same gate (:func:`validate_epistemic_write`) runs for every write path:
``Repository.add``, ``Repository.update``, agent proposals, the human decision
that applies a proposal, and dream/curation candidates. A document that cannot
pass the gate is rejected before anything is written.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .errors import IntegrityError

EpistemicType = Literal["experience", "fact", "judgment", "inference"]
CausalLevel = Literal["none", "association", "hypothesis", "verified"]
ExperienceVerdict = Literal["accepted", "failed"]

EPISTEMIC_VALUES: tuple[str, ...] = ("experience", "fact", "judgment", "inference")
CAUSAL_VALUES: tuple[str, ...] = ("none", "association", "hypothesis", "verified")

EXPERIENCE_POINT_TYPE = "experience-point"

#: Written into every experience-point body. The gate refuses an experience
#: point without it, so the disclaimer cannot be dropped by an edit.
EXPERIENCE_DISCLAIMER = (
    "仅说明这一次、在此范围内人工认为可接受；不是因果证明，不授权自动复用。"
)

#: Fields the gate owns. A write touching any of them is validated.
EPISTEMIC_FIELDS: frozenset[str] = frozenset(
    {
        "type",
        "epistemic",
        "causal",
        "causal_record",
        "limitations",
        "experience",
        "evidence",
        "source",
        "provenance",
        "body",
    }
)


class Confounders(BaseModel):
    """Known, suspected, and unobserved confounders of one causal claim."""

    model_config = ConfigDict(extra="forbid")

    known: list[str] = Field(default_factory=list)
    suspected: list[str] = Field(default_factory=list)
    unobserved: list[str] = Field(default_factory=list)


class Identification(BaseModel):
    """Which identification assumptions are claimed, and which are untested.

    ``untested`` being non-empty means the claim has not finished its
    identification argument. ``causal=verified`` refuses such a claim.
    """

    model_config = ConfigDict(extra="forbid")

    assumptions: list[str] = Field(default_factory=list)
    untested: list[str] = Field(default_factory=list)


class RobustnessCheck(BaseModel):
    """One refutation attempt or sensitivity check and what it found."""

    model_config = ConfigDict(extra="forbid")

    check: str
    result: str = ""
    failed: bool = False


class CausalRecord(BaseModel):
    """The minimal causal-inference record a hypothesis or verified claim carries."""

    model_config = ConfigDict(extra="forbid")

    treatment: str | None = None
    outcome: str | None = None
    confounders: Confounders = Field(default_factory=Confounders)
    selection: str | None = None
    temporal_order: str | None = None
    estimand: str | None = None
    identification: Identification = Field(default_factory=Identification)
    robustness: list[RobustnessCheck] = Field(default_factory=list)
    proposed_by: str | None = None
    alternatives: list[str] = Field(default_factory=list)


class ExperienceReview(BaseModel):
    """The human review that creates an experience point: what was and was not seen."""

    model_config = ConfigDict(extra="forbid")

    reviewer: str
    scope_reviewed: list[str] = Field(default_factory=list)
    not_reviewed: list[str] = Field(default_factory=list)
    verdict: ExperienceVerdict


class ExperiencePoint(BaseModel):
    """What a human accepted (or rejected) for one task in one reviewed scope."""

    model_config = ConfigDict(extra="forbid")

    task: str
    conditions: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    review: ExperienceReview
    version: int = 1
    counterexamples: list[str] = Field(default_factory=list)
    invalidated_when: list[str] = Field(default_factory=list)


# --- evidence source classification -------------------------------------------
#
# ``evidence`` entries are opaque pointers. The gate reads only their prefix (or
# the document's ``source``/``provenance``) to decide whether a claim cites
# something outside the conversation. Rules, also documented in
# ``docs/epistemic-and-experience.md``:
#
# * a chat pointer is ``chat:``, ``conversation:``, ``session:``,
#   ``transcript:`` or ``message:``;
# * a generated pointer is ``llm:``, ``reflection:``, ``reasoning:``,
#   ``explanation:`` or ``dream:``;
# * a raw pointer is ``data:``, ``dataset:``, ``design:``, ``experiment:``,
#   ``result:``, ``test:``, ``code:``, ``file:``, ``sha256:``, ``pr:``,
#   ``commit:``, ``issue:``, ``repo:``, or any ``http(s)://`` URL;
# * anything else (including a bare memory id) is not raw evidence.

_CHAT_PREFIXES = ("chat:", "conversation:", "session:", "transcript:", "message:", "turn:")
_GENERATED_PREFIXES = (
    "llm:",
    "reflection:",
    "reasoning:",
    "explanation:",
    "dream:",
    "distil:",
    "summary:",
)
_RAW_PREFIXES = (
    "data:",
    "dataset:",
    "design:",
    "experiment:",
    "result:",
    "test:",
    "code:",
    "file:",
    "sha256:",
    "pr:",
    "commit:",
    "issue:",
    "repo:",
    "http://",
    "https://",
)
_CHAT_SOURCES = frozenset({"chat", "conversation", "transcript", "session", "history", "message"})
_GENERATED_PROVENANCES = frozenset(
    {
        "llm",
        "llm-generated",
        "agent",
        "reflection",
        "dream",
        "distilled",
        "dream-distilled",
        "model",
    }
)


def evidence_source_class(reference: str) -> Literal["chat", "generated", "raw", "other"]:
    """Classify one evidence pointer. Only ``raw`` proves a source outside memory."""

    value = reference.strip().lower()
    if not value:
        return "other"
    if value.startswith(_CHAT_PREFIXES):
        return "chat"
    if value.startswith(_GENERATED_PREFIXES):
        return "generated"
    if value.startswith(_RAW_PREFIXES):
        return "raw"
    return "other"


def _has_raw_evidence(references: Sequence[str]) -> bool:
    return any(evidence_source_class(item) == "raw" for item in references)


def _narrative_only_reason(references: Sequence[str]) -> str:
    classes = {evidence_source_class(item) for item in references}
    if classes and classes <= {"chat"}:
        return "evidence points only at chat text"
    if classes and classes <= {"generated"}:
        return "evidence points only at LLM-generated explanations or reflections"
    if classes and classes <= {"chat", "generated", "other"}:
        return "evidence has no pointer to raw data or a design"
    return "evidence has no pointer to raw data or a design"


# --- normalization helpers ----------------------------------------------------


def _coerce_causal_record(raw: Any) -> CausalRecord:
    if isinstance(raw, CausalRecord):
        return raw
    try:
        return CausalRecord.model_validate(raw)
    except ValidationError as exc:
        raise IntegrityError(f"causal_record is invalid: {exc}") from exc


def _coerce_experience(raw: Any) -> ExperiencePoint:
    if isinstance(raw, ExperiencePoint):
        return raw
    try:
        return ExperiencePoint.model_validate(raw)
    except ValidationError as exc:
        raise IntegrityError(f"experience is invalid: {exc}") from exc


def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, Sequence):
        return [str(item) for item in value if str(item).strip()]
    raise IntegrityError("expected a string or a list of strings")


def _non_empty(value: Any) -> bool:
    return bool(str(value or "").strip())


# --- the shared write gate ----------------------------------------------------


def validate_epistemic_write(
    values: Mapping[str, Any],
    *,
    actor: str,
    human_decision: bool = False,
) -> None:
    """Reject an inconsistent epistemic/causal state before any write.

    ``values`` is the complete prospective document state (for a partial
    update: the old document merged with the changes). ``actor`` is the actor
    of the write. Only a human actor, or an agent write covered by a recorded
    ``HUMAN_DECISION`` (``human_decision=True``), may reach ``causal=verified``.

    Raises :class:`IntegrityError` naming every missing or contradictory item.
    """

    epistemic = values.get("epistemic")
    causal = values.get("causal", "none") or "none"
    raw_record = values.get("causal_record")
    limitations = _strings(values.get("limitations"))
    document_type = values.get("type")
    body = str(values.get("body") or "")

    if epistemic is not None and str(epistemic) not in EPISTEMIC_VALUES:
        raise IntegrityError(
            "epistemic must be one of: " + ", ".join(EPISTEMIC_VALUES)
        )
    if str(causal) not in CAUSAL_VALUES:
        raise IntegrityError("causal must be one of: " + ", ".join(CAUSAL_VALUES))

    record = _coerce_causal_record(raw_record) if raw_record is not None else None

    # An experience point never carries a causal claim.
    if document_type == EXPERIENCE_POINT_TYPE:
        experience = _coerce_experience(values.get("experience"))
        _validate_experience_point(experience=experience, epistemic=epistemic, causal=str(causal))
        if EXPERIENCE_DISCLAIMER not in body:
            raise IntegrityError(
                "type=experience-point body must carry the fixed disclaimer: "
                + EXPERIENCE_DISCLAIMER
            )
    elif values.get("experience") is not None:
        raise IntegrityError(
            "experience requires type=" + EXPERIENCE_POINT_TYPE
        )

    if str(causal) in {"hypothesis", "verified"} and record is None:
        raise IntegrityError(
            "causal=" + str(causal) + " requires a causal_record with treatment, outcome, and "
            "identification"
        )
    if str(causal) in {"none"} and record is not None:
        raise IntegrityError("causal_record requires causal=hypothesis or causal=verified")

    if str(causal) != "verified":
        return

    # ---- causal=verified: everything below is mandatory -------------------
    missing: list[str] = []
    if record is None:
        missing.append("causal_record")
    else:
        if not _non_empty(record.estimand):
            missing.append("causal_record.estimand")
        if not (record.identification.assumptions or record.identification.untested):
            missing.append("causal_record.identification")
        if not (record.robustness or limitations):
            missing.append("causal_record.robustness or limitations")
    references = _strings(values.get("evidence"))
    if not references:
        missing.append("evidence (at least one pointer to raw data or a design)")
    if missing:
        raise IntegrityError("causal=verified requires: " + ", ".join(missing))

    assert record is not None  # narrowed by the missing check above
    if record.identification.untested:
        raise IntegrityError(
            "causal=verified requires tested identification assumptions; "
            "causal_record.identification.untested is non-empty, so record "
            "causal=hypothesis until they are tested"
        )
    if not _has_raw_evidence(references):
        raise IntegrityError(
            "causal=verified requires evidence that points outside the vault: "
            + _narrative_only_reason(references)
        )
    provenance = str(values.get("provenance") or "").strip().lower()
    source = str(values.get("source") or "").strip().lower()
    if provenance in _GENERATED_PROVENANCES and not _has_raw_evidence(references):
        raise IntegrityError(
            "an LLM-derived memory (provenance="
            + provenance
            + ") cannot be causal=verified without raw-data evidence"
        )
    if source in _CHAT_SOURCES and not _has_raw_evidence(references):
        raise IntegrityError(
            "a chat-sourced memory (source="
            + source
            + ") cannot be causal=verified without raw-data evidence"
        )
    failed = [item.check for item in record.robustness if item.failed]
    if failed:
        raise IntegrityError(
            "a failed robustness check requires demotion to causal=hypothesis: "
            + ", ".join(failed)
        )
    if not (actor == "human" or human_decision):
        raise IntegrityError(
            "only a human actor or a recorded HUMAN_DECISION may set causal=verified; "
            "agent and dream proposals stop at causal=hypothesis"
        )


def validate_proposed_write(
    current: Mapping[str, Any] | None,
    changes: Mapping[str, Any],
    *,
    actor: str,
) -> None:
    """Gate one proposal-shaped change set against the current document state.

    Used by agent proposals and by dream/curation candidate staging, where the
    change is not the whole document. Anything outside :data:`EPISTEMIC_FIELDS`
    is ignored, so a write of unrelated fields is never re-validated.
    """

    if not set(changes) & EPISTEMIC_FIELDS:
        return
    values: dict[str, Any] = dict(current) if current is not None else {}
    values.setdefault("causal", "none")
    values.update({key: value for key, value in changes.items() if key in EPISTEMIC_FIELDS})
    if values.get("causal") in (None, ""):
        values["causal"] = "none"
    validate_epistemic_write(values, actor=actor)


def _validate_experience_point(
    *, experience: ExperiencePoint, epistemic: Any, causal: str
) -> None:
    """The experience-point contract: reviewed, scoped, and never causal."""

    missing: list[str] = []
    if str(epistemic or "") != "experience":
        missing.append("epistemic=experience")
    if causal != "none":
        missing.append("causal=none")
    if not _non_empty(experience.task):
        missing.append("experience.task")
    if not experience.conditions:
        missing.append("experience.conditions")
    if not experience.actions:
        missing.append("experience.actions")
    if not _non_empty(experience.review.reviewer):
        missing.append("experience.review.reviewer")
    if experience.review.verdict not in {"accepted", "failed"}:
        missing.append("experience.review.verdict=accepted|failed")
    if missing:
        raise IntegrityError(
            "type=" + EXPERIENCE_POINT_TYPE + " requires: " + ", ".join(missing)
        )


__all__ = [
    "CAUSAL_VALUES",
    "EPISTEMIC_FIELDS",
    "EPISTEMIC_VALUES",
    "EXPERIENCE_DISCLAIMER",
    "EXPERIENCE_POINT_TYPE",
    "CausalLevel",
    "CausalRecord",
    "Confounders",
    "EpistemicType",
    "ExperiencePoint",
    "ExperienceReview",
    "ExperienceVerdict",
    "Identification",
    "RobustnessCheck",
    "evidence_source_class",
    "validate_epistemic_write",
    "validate_proposed_write",
]

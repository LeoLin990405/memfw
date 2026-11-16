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

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .errors import IntegrityError

EpistemicType = Literal["experience", "fact", "judgment", "inference"]
CausalLevel = Literal["none", "association", "hypothesis", "verified"]
ExperienceVerdict = Literal["accepted", "failed"]

EPISTEMIC_VALUES: tuple[str, ...] = ("experience", "fact", "judgment", "inference")
CAUSAL_VALUES: tuple[str, ...] = ("none", "association", "hypothesis", "verified")

EXPERIENCE_POINT_TYPE = "experience-point"

#: A memory that states what is *not* known: one question, the memories that
#: conflict on it, and what evidence is missing. It never states a fact, so it
#: carries no epistemic type and no causal claim (see ``OpenQuestion``).
OPEN_QUESTION_TYPE = "open-question"

#: Most memories one open question may list as conflicting.
MAX_OPEN_QUESTION_CONFLICTS = 20
#: Longest ``question`` and ``missing`` text, in characters.
MAX_OPEN_QUESTION_TEXT = 300

_MEMORY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

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
        "open_question",
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


def _check_memory_id(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not _MEMORY_ID_RE.fullmatch(value):
        raise ValueError(f"{field} must be a memory id, got {value!r}")
    return value


class OpenQuestion(BaseModel):
    """What one ``type: open-question`` memory says is unknown.

    ``conflicts`` lists memories that contradict each other on the question
    (order kept, duplicates dropped). ``resolved_by`` names the memory that
    answered it; a resolved question also has ``valid_to`` set and its file is
    kept, and its version before resolution stays in the history.
    """

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=MAX_OPEN_QUESTION_TEXT)
    conflicts: list[str] = Field(default_factory=list)
    missing: str = Field(default="", max_length=MAX_OPEN_QUESTION_TEXT)
    resolved_by: str | None = None

    @field_validator("question")
    @classmethod
    def question_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("question must not be blank")
        return value

    @field_validator("missing", mode="before")
    @classmethod
    def missing_none_is_empty(cls, value: Any) -> Any:
        return "" if value is None else value

    @field_validator("conflicts", mode="before")
    @classmethod
    def conflicts_are_unique_ids(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str) or not isinstance(value, Sequence):
            raise ValueError("conflicts must be a list of memory ids")
        unique: list[str] = []
        for item in value:
            identifier = _check_memory_id(item, field="conflicts")
            if identifier not in unique:
                unique.append(identifier)
        if len(unique) > MAX_OPEN_QUESTION_CONFLICTS:
            raise ValueError(
                f"conflicts lists at most {MAX_OPEN_QUESTION_CONFLICTS} memories, "
                + f"got {len(unique)}"
            )
        return unique

    @field_validator("resolved_by")
    @classmethod
    def resolved_by_is_an_id(cls, value: str | None) -> str | None:
        return None if value is None else _check_memory_id(value, field="resolved_by")


# --- evidence source classification -------------------------------------------
#
# ``evidence`` entries are opaque pointers. The gate reads only their prefix (or
# the document's ``source``/``provenance``) to decide whether a claim cites
# something outside the conversation. Authority, strongest first, also
# documented in ``docs/epistemic-and-experience.md``:
#
# * a recorded human decision (``human_decision=True``) overrides the pointer
#   rules below for a chat-sourced or LLM-derived memory;
# * a raw pointer is ``data:``, ``dataset:``, ``design:``, ``experiment:``,
#   ``result:``, ``test:``, ``code:``, ``file:``, ``sha256:``, ``pr:``,
#   ``commit:``, ``issue:``, or ``repo:`` — something that can be replayed or
#   checked, not just read;
# * a link pointer is any ``http(s)://`` URL. It points outside the vault, but
#   the vault cannot verify what sits behind it; when the memory is chat-sourced
#   or LLM-derived, a link alone is not enough;
# * a chat pointer is ``chat:``, ``conversation:``, ``session:``,
#   ``transcript:``, ``message:`` or ``turn:``;
# * a generated pointer is ``llm:``, ``reflection:``, ``reasoning:``,
#   ``explanation:``, ``dream:``, ``distil:`` or ``summary:``;
# * anything else (including a bare memory id) is not outside-vault evidence.
#
# A prefix with nothing after it (``data:``, ``https://``) is not a pointer.

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
_LINK_PREFIXES = ("http://", "https://")
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
)
EvidenceClass = Literal["chat", "generated", "link", "raw", "other"]
_PREFIX_CLASSES: tuple[tuple[EvidenceClass, tuple[str, ...]], ...] = (
    ("chat", _CHAT_PREFIXES),
    ("generated", _GENERATED_PREFIXES),
    ("link", _LINK_PREFIXES),
    ("raw", _RAW_PREFIXES),
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


def evidence_source_class(reference: str) -> EvidenceClass:
    """Classify one evidence pointer.

    Only ``raw`` and ``link`` point outside the vault; only ``raw`` points at
    something the vault can replay or check. A prefix with an empty remainder is
    ``other``, so ``data:`` and ``https://`` are not evidence.
    """

    value = reference.strip().lower()
    if not value:
        return "other"
    for label, prefixes in _PREFIX_CLASSES:
        for prefix in prefixes:
            if value.startswith(prefix):
                if not value[len(prefix) :].strip():
                    return "other"
                return label
    return "other"


def _has_raw_evidence(references: Sequence[str]) -> bool:
    """Whether some pointer points outside the vault (raw data or a link)."""

    return any(evidence_source_class(item) in {"raw", "link"} for item in references)


def _has_controlled_evidence(references: Sequence[str]) -> bool:
    """Whether some pointer names replayable/checkable raw data, not just a URL."""

    return any(evidence_source_class(item) == "raw" for item in references)


def _narrative_only_reason(references: Sequence[str]) -> str:
    classes = {evidence_source_class(item) for item in references}
    if classes and classes <= {"chat"}:
        return "evidence points only at chat text"
    if classes and classes <= {"generated"}:
        return "evidence points only at LLM-generated explanations or reflections"
    if classes and classes <= {"link"}:
        return "evidence points only at web links, not at raw data or a design"
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


def _coerce_open_question(raw: Any) -> OpenQuestion:
    if isinstance(raw, OpenQuestion):
        return raw
    try:
        return OpenQuestion.model_validate(raw)
    except ValidationError as exc:
        raise IntegrityError(f"open_question is invalid: {exc}") from exc


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
    previous_causal: str | None = None,
) -> None:
    """Reject an inconsistent epistemic/causal state before any write.

    ``values`` is the complete prospective document state (for a partial
    update: the old document merged with the changes). ``actor`` is the actor
    of the write. ``previous_causal`` is the document's causal level before this
    write, or ``None`` when the document does not exist yet.

    Only a write that *turns* a document into ``causal=verified`` needs a human:
    a human actor, or an agent write covered by a recorded ``HUMAN_DECISION``
    (``human_decision=True``). A document that is already ``verified`` may still
    be corrected, annotated, or demoted by an agent or dream; the verified
    invariants below keep applying, so such a write cannot strip the raw-data
    evidence or the identification/robustness record.

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

    # An open question states what is unknown; it never carries a claim.
    raw_question = values.get("open_question")
    if document_type == OPEN_QUESTION_TYPE:
        if raw_question is None:
            raise IntegrityError("type=" + OPEN_QUESTION_TYPE + " requires an open_question block")
        question = _coerce_open_question(raw_question)
        _validate_open_question(
            question,
            memory_id=values.get("id"),
            epistemic=epistemic,
            causal=str(causal),
            has_causal_record=record is not None,
            has_experience=values.get("experience") is not None,
        )
    elif raw_question is not None:
        raise IntegrityError("open_question requires type=" + OPEN_QUESTION_TYPE)

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
    if record is not None:
        if not _non_empty(record.estimand):
            missing.append("causal_record.estimand")
        # A blank string is not an assumption; drop it before counting.
        if not (
            any(item.strip() for item in record.identification.assumptions)
            or any(item.strip() for item in record.identification.untested)
        ):
            missing.append("causal_record.identification")
        if not (any(item.check.strip() for item in record.robustness) or limitations):
            missing.append("causal_record.robustness or limitations")
    else:
        missing.append("causal_record")
    references = _strings(values.get("evidence"))
    if not references:
        missing.append("evidence (at least one pointer to raw data or a design)")
    if missing:
        raise IntegrityError("causal=verified requires: " + ", ".join(missing))

    assert record is not None  # narrowed by the missing check above
    untested = [item.strip() for item in record.identification.untested if item.strip()]
    if untested:
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
    # A URL is a pointer, not a replayable source: it can say anything. When the
    # prose itself came from a chat or a model, the claim needs either a raw
    # pointer the vault can check or an explicit recorded HUMAN_DECISION. This is
    # the rule that stops "someone pasted a chat link" from becoming verified.
    if (provenance in _GENERATED_PROVENANCES or source in _CHAT_SOURCES) and not (
        _has_controlled_evidence(references) or human_decision
    ):
        attribution = "source=" + (source or "unset") + ", provenance=" + (provenance or "unset")
        raise IntegrityError(
            "a chat-sourced or LLM-derived memory ("
            + attribution
            + ") cannot be causal=verified on a bare web link: cite a replayable raw "
            "pointer (data:, dataset:, design:, experiment:, result:, test:, code:, "
            "file:, sha256:, pr:, commit:, issue:, repo:) or carry a recorded "
            "HUMAN_DECISION"
        )
    failed = [item.check for item in record.robustness if item.failed]
    if failed:
        raise IntegrityError(
            "a failed robustness check requires demotion to causal=hypothesis: "
            + ", ".join(failed)
        )
    # Only the transition into ``verified`` needs a human. An already-verified
    # document stays writable by agent/dream writes (correction, appended
    # evidence, demotion); the invariants above still guard its content.
    if previous_causal != "verified" and not (actor == "human" or human_decision):
        raise IntegrityError(
            "only a human actor or a recorded HUMAN_DECISION may set causal=verified; "
            "agent and dream proposals stop at causal=hypothesis"
        )


_OPEN_QUESTION_PROPOSAL_KEYS = EPISTEMIC_FIELDS | {"tenant", "valid_to"}


def _merge_proposal(
    current: Mapping[str, Any] | None, changes: Mapping[str, Any], *, keys: frozenset[str]
) -> dict[str, Any]:
    values: dict[str, Any] = dict(current) if current is not None else {}
    values.setdefault("causal", "none")
    values.update({key: value for key, value in changes.items() if key in keys})
    if values.get("causal") in (None, ""):
        values["causal"] = "none"
    return values


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

    ``current`` is the document as stored before the change; its ``causal`` level
    decides whether the change *turns* the document into ``verified`` (which an
    agent may not do without a human decision) or merely edits one that already
    is.
    """

    if not set(changes) & EPISTEMIC_FIELDS:
        return
    previous_causal: str | None = None
    if current is not None:
        previous_causal = str(current.get("causal") or "none")
    values = _merge_proposal(current, changes, keys=EPISTEMIC_FIELDS)
    validate_epistemic_write(values, actor=actor, previous_causal=previous_causal)


def validate_proposed_open_question(
    current: Mapping[str, Any] | None,
    changes: Mapping[str, Any],
    *,
    lookup: Callable[[str], Any],
    actor: str,
) -> None:
    """Check a proposal-shaped change to an open question against the vault.

    The vault half of the gate for agent proposals and dream candidates, run
    after :func:`validate_proposed_write`: the same checks a direct write gets
    (:func:`validate_open_question_references`). ``lookup`` maps a memory id to
    its current document or ``None``. ``valid_to`` and ``tenant`` in
    ``changes`` count too: resolving a question sets ``valid_to``.
    """

    if not set(changes) & EPISTEMIC_FIELDS:
        return
    values = _merge_proposal(current, changes, keys=_OPEN_QUESTION_PROPOSAL_KEYS)
    validate_open_question_references(values, lookup=lookup, previous=current, actor=actor)


def _validate_open_question(
    question: OpenQuestion,
    *,
    memory_id: Any,
    epistemic: Any,
    causal: str,
    has_causal_record: bool,
    has_experience: bool,
) -> None:
    """The open-question contract: no claim of any kind, and no self-reference."""

    forbidden: list[str] = []
    if epistemic is not None:
        forbidden.append("epistemic (must be absent)")
    if causal != "none":
        forbidden.append("causal (must be none)")
    if has_causal_record:
        forbidden.append("causal_record (must be absent)")
    if has_experience:
        forbidden.append("experience (must be absent)")
    if forbidden:
        raise IntegrityError(
            f"type={OPEN_QUESTION_TYPE} states what is unknown and cannot carry: "
            + ", ".join(forbidden)
        )
    if memory_id:
        if str(memory_id) in question.conflicts:
            raise IntegrityError(
                f"open_question.conflicts cannot name the question itself: {memory_id}"
            )
        if question.resolved_by == str(memory_id):
            raise IntegrityError(
                f"open_question.resolved_by cannot name the question itself: {memory_id}"
            )


def validate_open_question_references(
    values: Mapping[str, Any],
    *,
    lookup: Callable[[str], Any],
    previous: Mapping[str, Any] | None = None,
    actor: str = "human",
) -> None:
    """Check an open question against the vault: what it names must exist.

    ``values`` is the complete prospective document state, already through
    :func:`validate_epistemic_write`. ``lookup`` returns the current document
    for a memory id, or ``None`` when no such memory exists. ``previous`` is the
    stored state before this write (``None`` for a new memory); references it
    already carried are not re-checked unless the question's tenant or type
    changes, so an edit is not blocked by a later change elsewhere (``fsck``
    reports those).

    * every ``conflicts`` id must exist and share the question's tenant;
    * ``resolved_by`` requires ``valid_to`` on the question, and the answer must
      exist, share the tenant, be current (no ``valid_to``/``superseded_by``)
      and not be another open question.

    For any ``actor`` other than ``human`` a missing id and an id in another
    tenant fail with the same message, so the gate is not an existence oracle
    for memories the writer cannot see.
    """

    raw_question = values.get("open_question")
    if values.get("type") != OPEN_QUESTION_TYPE or raw_question is None:
        # A missing block is the shared gate's (and ``fsck``'s) to report.
        return
    question = _coerce_open_question(raw_question)
    memory_id = str(values.get("id") or "")
    tenant = str(values.get("tenant") or "")
    if memory_id:
        _validate_open_question(
            question,
            memory_id=memory_id,
            epistemic=None,
            causal="none",
            has_causal_record=False,
            has_experience=False,
        )
    old: OpenQuestion | None = None
    recheck_all = True
    if previous is not None and previous.get("type") == OPEN_QUESTION_TYPE:
        raw_old = previous.get("open_question")
        if raw_old is not None:
            old = _coerce_open_question(raw_old)
            recheck_all = str(previous.get("tenant") or "") != tenant
    known = set() if recheck_all or old is None else set(old.conflicts)
    for conflict in question.conflicts:
        if conflict in known:
            continue
        _check_visible(lookup(conflict), conflict, "conflicts", tenant=tenant, actor=actor)
    answer_id = question.resolved_by
    if answer_id is None:
        return
    if values.get("valid_to") in (None, ""):
        raise IntegrityError(
            "open_question.resolved_by requires valid_to: a resolved question stops being "
            "current when it is answered"
        )
    if not recheck_all and old is not None and old.resolved_by == answer_id:
        return
    answer = lookup(answer_id)
    _check_visible(answer, answer_id, "resolved_by", tenant=tenant, actor=actor)
    if getattr(answer, "valid_to", None) is not None or getattr(answer, "superseded_by", None):
        raise IntegrityError(f"open_question.resolved_by names a superseded memory: {answer_id}")
    if getattr(answer, "type", None) == OPEN_QUESTION_TYPE:
        raise IntegrityError(
            "open_question.resolved_by must name an answer, not another open question: " + answer_id
        )


def _check_visible(target: Any, memory_id: str, field: str, *, tenant: str, actor: str) -> None:
    """A referenced memory must exist in the question's tenant."""

    missing = target is None
    elsewhere = not missing and str(getattr(target, "tenant", "") or "") != tenant
    if not (missing or elsewhere):
        return
    if actor != "human":
        raise IntegrityError(
            f"open_question.{field} names a memory that is missing or not visible: {memory_id}"
        )
    if missing:
        raise IntegrityError(f"open_question.{field} names a missing memory: {memory_id}")
    raise IntegrityError(f"open_question.{field} names a memory in another tenant: {memory_id}")


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
    "MAX_OPEN_QUESTION_CONFLICTS",
    "MAX_OPEN_QUESTION_TEXT",
    "OPEN_QUESTION_TYPE",
    "CausalLevel",
    "CausalRecord",
    "Confounders",
    "EpistemicType",
    "EvidenceClass",
    "ExperiencePoint",
    "ExperienceReview",
    "ExperienceVerdict",
    "Identification",
    "OpenQuestion",
    "RobustnessCheck",
    "evidence_source_class",
    "validate_epistemic_write",
    "validate_open_question_references",
    "validate_proposed_open_question",
    "validate_proposed_write",
]

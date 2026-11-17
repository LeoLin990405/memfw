"""Dream step ②b — a conflict becomes an open-question candidate (U3).

When curate finds two current memories about one concept that contradict each
other (the pairs it proposes a ``supersede`` for), the dream also proposes a
``type: open-question`` memory that names both. It is a candidate like every
other dream output: nothing is written to the vault, no winner is picked, and
neither original changes (their ``epistemic``, ``status`` and ``evidence`` stay
as they are). A human reviews it (see ``docs/dream.md``):

* **accept the question** — :func:`accept_question` (``memfw dream
  accept-question``) writes the open question as the human, through the same
  write gate as any other open question;
* **answer and supersede** — write (or pick) the memory that is right,
  ``supersede`` the wrong one; if the question was accepted, close it with
  ``Repository.resolve_open_question``;
* **reject** — remove the staged concept file.

Rules (all deterministic, no model):

* **Dedupe.** No candidate for an unordered pair while a current, unresolved
  open question in the tenant lists both memories in ``conflicts``, or while a
  staged dream holds an ``open-question`` candidate for the pair that has not
  been accepted (an accepted one is linked by the ``dream:<dream_id>`` evidence
  pointer :func:`accept_question` records).
* **Same tenant only.** A pair whose memories are in different tenants (or not
  in the dream's tenant) is skipped.
* **Privacy.** A candidate names its memories and its concept, and the question
  it proposes is recalled like any memory. A pair in which either memory is
  private in any way (a ``sensitivity:*`` tag, or a ``privacy:`` tag other than
  ``none``; unknown values fail closed) is skipped, so a question never reveals
  a private memory's id.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

import yaml

from memfw.core.epistemic import (
    MAX_OPEN_QUESTION_TEXT,
    OPEN_QUESTION_TYPE,
    OpenQuestion,
    validate_epistemic_write,
    validate_open_question_references,
)
from memfw.core.errors import IntegrityError, NotFoundError
from memfw.core.models import MemoryDocument
from memfw.core.repository import Repository
from memfw.projection import classify_privacy, tag_privacy_class

from .curate import conflict_reasons
from .model import Candidate
from .stage import DREAM_DIR, check_dream_id

ACTION: Final[Literal["open-question"]] = "open-question"
"""The candidate action this step stages."""

EVIDENCE_PREFIX = "dream:"
"""Evidence pointer an accepted question carries: ``dream:<dream_id>``."""

MISSING = (
    "A dated, checkable source (a record, a test, a config or a decision) that shows "
    "which memory holds now, or that each holds in a different scope."
)

Pair = tuple[str, str]


@dataclass
class QuestionStats:
    conflicts: int = 0
    proposed: int = 0
    deduplicated: int = 0
    skipped_private: int = 0
    skipped_tenant: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "conflicts": self.conflicts,
            "proposed": self.proposed,
            "deduplicated": self.deduplicated,
            "skipped_private": self.skipped_private,
            "skipped_tenant": self.skipped_tenant,
        }


def pair_key(left: str, right: str) -> Pair:
    """The unordered pair, as a sorted tuple."""

    low, high = sorted((left, right))
    return low, high


def is_private(document: MemoryDocument) -> bool:
    """Whether a memory is private in any way (fail closed on unknown values)."""

    tag_class = tag_privacy_class(document.tags)
    if tag_class != "none":
        return True
    for tag in document.tags:
        if tag.lower().startswith("privacy:"):
            privacy, _ = classify_privacy(tag.split(":", 1)[1], tag_class="none")
            if privacy != "none":
                return True
    return False


def _is_unresolved(document: MemoryDocument) -> bool:
    return (
        document.type == OPEN_QUESTION_TYPE
        and document.open_question is not None
        and document.open_question.resolved_by is None
        and document.valid_to is None
        and document.superseded_by is None
    )


def _covers(document: MemoryDocument, pair: Pair) -> bool:
    if document.open_question is None:
        return False
    return set(pair) <= set(document.open_question.conflicts)


def _accepted_from(document: MemoryDocument, dream_id: str) -> bool:
    return f"{EVIDENCE_PREFIX}{dream_id}" in document.evidence


# --- staged candidates -----------------------------------------------------------


@dataclass(frozen=True)
class StagedQuestion:
    """One staged ``open-question`` candidate."""

    dream_id: str
    path: Path
    pair: Pair
    tenant: str
    candidate: Mapping[str, Any]


def _plain_dir(path: Path) -> bool:
    return not path.is_symlink() and path.is_dir()


def _plain_file(path: Path) -> bool:
    return not path.is_symlink() and path.is_file()


def _frontmatter(text: str) -> Mapping[str, Any] | None:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end < 0:
        return None
    try:
        front = yaml.safe_load(text[4:end])
    except yaml.YAMLError:
        return None
    return front if isinstance(front, Mapping) else None


def _questions_in(directory: Path, dream_id: str) -> list[StagedQuestion]:
    found: list[StagedQuestion] = []
    for path in sorted(directory.glob("*.md")):
        if not _plain_file(path):
            continue
        try:
            front = _frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if front is None:
            continue
        for raw in front.get("candidates") or []:
            if not isinstance(raw, Mapping) or raw.get("action") != ACTION:
                continue
            ids = raw.get("memory_ids")
            if not isinstance(ids, list) or len(ids) != 2:
                continue
            if not all(isinstance(item, str) for item in ids):
                continue
            changes = raw.get("changes")
            new_memory = changes.get("new_memory") if isinstance(changes, Mapping) else None
            if not isinstance(new_memory, Mapping):
                continue
            found.append(
                StagedQuestion(
                    dream_id=dream_id,
                    path=path,
                    pair=pair_key(ids[0], ids[1]),
                    tenant=str(new_memory.get("tenant") or ""),
                    candidate=raw,
                )
            )
    return found


def staged_questions(root: Path, *, tenant: str | None = None) -> list[StagedQuestion]:
    """Every staged ``open-question`` candidate under ``_candidates/dream/``.

    Read-only. Symlinked or hidden dream directories and symlinked files are
    skipped, never followed; an unreadable file is skipped.
    """

    base = Path(root) / DREAM_DIR
    if not _plain_dir(Path(root) / DREAM_DIR.parts[0]) or not _plain_dir(base):
        return []
    found: list[StagedQuestion] = []
    for directory in sorted(base.iterdir()):
        if directory.name.startswith(".") or not _plain_dir(directory):
            continue
        for item in _questions_in(directory, directory.name):
            if tenant is None or item.tenant == tenant:
                found.append(item)
    return found


# --- proposing -------------------------------------------------------------------


def _question_text(left: str, right: str, oid: str) -> str:
    text = f"Which of {left} and {right} is right about {oid}?"
    if len(text) <= MAX_OPEN_QUESTION_TEXT:
        return text
    return "Which of the two conflicting memories is right about this concept?"


def _candidate(
    oid: str, left: MemoryDocument, right: MemoryDocument, reasons: Sequence[str], tenant: str
) -> Candidate:
    ids = list(pair_key(left.id, right.id))
    detail = "; ".join(reasons) or "they disagree"
    question = _question_text(ids[0], ids[1], oid)
    body = (
        f"{ids[0]} and {ids[1]} are both current, are about {oid}, and disagree "
        f"({detail}). Which one holds is not known yet; neither has been chosen.\n"
    )
    new_memory: dict[str, Any] = {
        "type": OPEN_QUESTION_TYPE,
        "title": f"Open question: {oid}"[:200],
        "body": body,
        "tenant": tenant,
        "about": [oid],
        "open_question": {
            "question": question,
            "conflicts": ids,
            "missing": MISSING,
            "resolved_by": None,
        },
    }
    return Candidate(
        operation="curate",
        action=ACTION,
        memory_ids=ids,
        oid=oid,
        reason=(
            f"{ids[0]} and {ids[1]} under '{oid}' conflict ({detail}); proposal: record "
            "an open question that names both, instead of guessing which is right. "
            "Missing: " + MISSING + " A human decides; nothing is written and neither "
            "memory changes."
        ),
        changes={"new_memory": new_memory, "conflicts": ids},
    )


def conflict_questions(
    curated: Iterable[Candidate],
    documents: Sequence[MemoryDocument],
    *,
    tenant: str,
    staged: Sequence[StagedQuestion] = (),
) -> tuple[list[Candidate], QuestionStats]:
    """Open-question candidates for the conflicts curate found.

    ``curated`` is curate's output (its ``supersede`` candidates name the
    conflicting pairs). ``documents`` is every memory of ``tenant`` the dream
    read, historical ones included, so an accepted (even resolved) question
    still links its dream. ``staged`` is :func:`staged_questions` for the
    tenant. Returns the candidates (sorted by oid, then pair) and the counts.
    """

    stats = QuestionStats()
    by_id = {document.id: document for document in documents}
    questions = [
        document
        for document in documents
        if document.type == OPEN_QUESTION_TYPE
        and document.open_question is not None
        and document.tenant == tenant
    ]
    seen: set[Pair] = set()
    candidates: list[Candidate] = []
    for item in curated:
        if item.action != "supersede" or len(item.memory_ids) != 2:
            continue
        pair = pair_key(*item.memory_ids)
        if pair in seen:
            continue
        seen.add(pair)
        stats.conflicts += 1
        left, right = by_id.get(pair[0]), by_id.get(pair[1])
        if left is None or right is None or left.tenant != tenant or right.tenant != tenant:
            stats.skipped_tenant += 1
            continue
        if is_private(left) or is_private(right):
            stats.skipped_private += 1
            continue
        if any(_is_unresolved(question) and _covers(question, pair) for question in questions):
            stats.deduplicated += 1
            continue
        pending = [
            entry
            for entry in staged
            if entry.pair == pair
            and entry.tenant == tenant
            and not any(
                _covers(question, pair) and _accepted_from(question, entry.dream_id)
                for question in questions
            )
        ]
        if pending:
            stats.deduplicated += 1
            continue
        candidates.append(_candidate(item.oid, left, right, conflict_reasons(left, right), tenant))
        stats.proposed += 1
    candidates.sort(key=lambda candidate: (candidate.oid, candidate.memory_ids))
    return candidates, stats


def check_candidate(candidate: Candidate, *, lookup: Any, actor: str = "mechanism") -> None:
    """Run the open-question write gate over a staged candidate's new memory.

    The new memory is checked as a new document (no current state): no claim
    of any kind, and every ``conflicts`` id exists in its tenant. Raises
    :class:`IntegrityError`.
    """

    new_memory = candidate.changes.get("new_memory")
    if not isinstance(new_memory, Mapping):
        raise IntegrityError("open-question candidate has no new_memory")
    values = _gate_values(new_memory)
    validate_epistemic_write(values, actor=actor)
    validate_open_question_references(values, lookup=lookup, actor=actor)


def _gate_values(new_memory: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": new_memory.get("type"),
        "tenant": str(new_memory.get("tenant") or ""),
        "epistemic": new_memory.get("epistemic"),
        "causal": new_memory.get("causal") or "none",
        "causal_record": new_memory.get("causal_record"),
        "experience": new_memory.get("experience"),
        "open_question": new_memory.get("open_question"),
        "evidence": list(new_memory.get("evidence") or []),
        "body": str(new_memory.get("body") or ""),
    }


# --- human review ----------------------------------------------------------------


def _current(repository: Repository, memory_id: str) -> MemoryDocument | None:
    try:
        document = repository.get(memory_id)
    except (NotFoundError, OSError):
        return None
    if document.valid_to is not None or document.superseded_by is not None:
        return None
    return document


def accept_question(
    repository: Repository,
    dream_id: str,
    memory_ids: Sequence[str],
    *,
    actor: str = "human",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Accept one staged open-question candidate: the human writes the question.

    Finds the ``open-question`` candidate for the unordered pair in dream
    ``dream_id`` and adds it with ``Repository.add`` as ``actor`` (only a human
    may create an open question), so the full U1 gate runs: no claim fields,
    conflicts exist and share the tenant. The question records
    ``dream:<dream_id>`` in ``evidence``, which also tells later dreams that
    this staged candidate was consumed. Refused when either memory is no longer
    current (the conflict was already settled) or has become private, when an
    unresolved question for the pair exists, or when this candidate was already
    accepted.
    """

    check_dream_id(dream_id)
    if len(memory_ids) != 2 or memory_ids[0] == memory_ids[1]:
        raise ValueError("an open-question candidate names exactly two memories")
    if actor != "human":
        raise IntegrityError("only a human accepts an open-question candidate")
    pair = pair_key(memory_ids[0], memory_ids[1])
    directory = repository.root / DREAM_DIR / dream_id
    if not _plain_dir(repository.root / DREAM_DIR) or not _plain_dir(directory):
        raise NotFoundError(f"no staged dream {dream_id}")
    matches = [item for item in _questions_in(directory, dream_id) if item.pair == pair]
    if not matches:
        raise NotFoundError(
            f"dream {dream_id} has no open-question candidate for {pair[0]} and {pair[1]}"
        )
    staged = matches[0]
    new_memory = staged.candidate["changes"]["new_memory"]
    tenant = staged.tenant
    for memory_id in pair:
        document = _current(repository, memory_id)
        if document is None or document.tenant != tenant:
            raise IntegrityError(
                f"{memory_id} is no longer current in this tenant; the conflict is settled"
            )
        if is_private(document):
            raise IntegrityError(f"{memory_id} is private; an open question may not name it")
    existing = [
        document
        for document in repository.list()
        if document.type == OPEN_QUESTION_TYPE
        and document.open_question is not None
        and document.tenant == tenant
        and _covers(document, pair)
    ]
    if any(_accepted_from(document, dream_id) for document in existing):
        raise IntegrityError(f"this candidate was already accepted from dream {dream_id}")
    if any(_is_unresolved(document) for document in existing):
        raise IntegrityError(f"an unresolved open question already names {pair[0]} and {pair[1]}")
    question = OpenQuestion.model_validate(new_memory.get("open_question"))
    if question.resolved_by is not None or set(question.conflicts) != set(pair):
        raise IntegrityError("the staged open question does not match its candidate")
    about = [str(item) for item in new_memory.get("about") or []]
    plan = {
        "dream_id": dream_id,
        "candidate": os.fspath(staged.path.relative_to(repository.root)),
        "memory_ids": list(pair),
        "tenant": tenant,
        "about": about,
        "open_question": question.model_dump(),
    }
    if dry_run:
        return {**plan, "dry_run": True, "writes": False}
    document = repository.add(
        str(new_memory.get("title") or "Open question"),
        str(new_memory.get("body") or ""),
        memory_type=OPEN_QUESTION_TYPE,
        tenant=tenant,
        about=about,
        open_question=question,
        evidence=[f"{EVIDENCE_PREFIX}{dream_id}"],
        source="dream",
        provenance="dream",
        actor=actor,
    )
    return {**plan, "dry_run": False, "memory_id": document.id, "status": "accepted"}


__all__ = [
    "ACTION",
    "EVIDENCE_PREFIX",
    "MISSING",
    "QuestionStats",
    "StagedQuestion",
    "accept_question",
    "check_candidate",
    "conflict_questions",
    "is_private",
    "pair_key",
    "staged_questions",
]

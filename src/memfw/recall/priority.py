"""Open-question review order (U5 reverse signal): corroborated conflicts first.

Pure functions over prepared data; nothing here reads the vault or the
corroboration projection. The builder is ``Runtime.open_question_queue``: it
lists the open questions the caller's ``FilterPolicy`` keeps and passes in
corroboration already trimmed to what the caller may read, so a hidden app
can never move a question up.

**The rule.** A memory in a question's ``conflicts`` is *corroborated* when
its visible corroboration names at least :data:`CORROBORATED_MIN_APPS`
distinct apps. Questions are ordered by, in this order:

1. more corroborated conflicts first;
2. then more ``support`` first: the sum of ``count`` (independent app and
   session pairs) over the corroborated conflicts only;
3. then older first (``created_at``, UTC ISO text);
4. then by question id.

A question without corroborated conflicts has ``support`` 0, so without any
corroboration (no projection, nothing visible) the order is the plain
``(created_at, id)`` order. The rule only reorders the queue: it never hides
a question, never changes one and never decides a conflict.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

#: A conflict counts as corroborated from this many distinct visible apps.
CORROBORATED_MIN_APPS = 2


@dataclass(frozen=True, slots=True)
class QuestionEntry:
    """One open question as the queue needs it (already filtered by the caller)."""

    question_id: str
    conflicts: tuple[str, ...]
    created_at: str


@dataclass(frozen=True, slots=True)
class Visible:
    """The caller-visible corroboration of one memory: pairs and distinct apps."""

    count: int
    apps: int


@dataclass(frozen=True, slots=True)
class QuestionPriority:
    """Why a question sits where it does in the queue."""

    question_id: str
    corroborated_conflicts: tuple[str, ...]
    support: int
    created_at: str

    def sort_key(self) -> tuple[int, int, str, str]:
        return (-len(self.corroborated_conflicts), -self.support, self.created_at, self.question_id)

    def as_dict(self) -> dict[str, Any]:
        return {
            "corroborated_conflicts": list(self.corroborated_conflicts),
            "support": self.support,
        }


def question_priority(entry: QuestionEntry, visible: Mapping[str, Visible]) -> QuestionPriority:
    """The priority of one question from its conflicts' visible corroboration."""

    corroborated: list[str] = []
    support = 0
    for memory_id in dict.fromkeys(entry.conflicts):
        seen = visible.get(memory_id)
        if seen is None or seen.apps < CORROBORATED_MIN_APPS:
            continue
        corroborated.append(memory_id)
        support += seen.count
    return QuestionPriority(
        question_id=entry.question_id,
        corroborated_conflicts=tuple(sorted(corroborated)),
        support=support,
        created_at=entry.created_at,
    )


def order_questions(
    entries: Iterable[QuestionEntry], visible: Mapping[str, Visible]
) -> list[QuestionPriority]:
    """Every entry with its priority, in review order (see the module docstring)."""

    ranked = [question_priority(entry, visible) for entry in entries]
    return sorted(ranked, key=QuestionPriority.sort_key)


__all__ = [
    "CORROBORATED_MIN_APPS",
    "QuestionEntry",
    "QuestionPriority",
    "Visible",
    "order_questions",
    "question_priority",
]

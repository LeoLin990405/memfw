"""Open-question recall labels (U2): show "unknown" next to a hit.

Pure functions over prepared data; nothing here reads the vault. The builder
that turns a vault into an ``OpenQuestionIndex`` is
``memfw.recall.builtin.open_question_index``: it keeps only open questions
that are unresolved, current and kept by the caller's ``FilterPolicy``, so a
question from another tenant, or one the policy drops (strict: privacy,
status, lifecycle, archive), never reaches this module and its id cannot
appear in a label.

Labels, in this order:

- ``open_question:<id>`` for each such question (sorted by id) whose ``about``
  shares a concept with the hit's ``about``; a question never labels itself;
- ``conflicted`` when the hit is listed in the ``conflicts`` of such a
  question.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

#: Prefix of the label that names an open question about a hit's concepts.
OPEN_QUESTION_LABEL_PREFIX = "open_question:"
#: Label for a hit that an open question lists in its ``conflicts``.
CONFLICTED_LABEL = "conflicted"


@dataclass(frozen=True, slots=True)
class OpenQuestionIndex:
    """Visible, unresolved open questions, keyed for labelling.

    ``by_concept`` maps an ``about`` concept to the ids of the questions about
    it (sorted); ``conflicted`` holds every memory id some question lists in
    its ``conflicts``. Empty means "no labels for anyone".
    """

    by_concept: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    conflicted: frozenset[str] = frozenset()

    def __bool__(self) -> bool:
        return bool(self.by_concept) or bool(self.conflicted)

    @classmethod
    def build(
        cls, questions: Iterable[tuple[str, Iterable[str], Iterable[str]]]
    ) -> OpenQuestionIndex:
        """From ``(question_id, about, conflicts)`` triples the caller already filtered."""

        concepts: dict[str, set[str]] = {}
        conflicted: set[str] = set()
        for question_id, about, conflicts in questions:
            for concept in about:
                if concept:
                    concepts.setdefault(concept, set()).add(question_id)
            conflicted.update(item for item in conflicts if item)
        return cls(
            by_concept={key: tuple(sorted(ids)) for key, ids in sorted(concepts.items())},
            conflicted=frozenset(conflicted),
        )


def open_question_labels(
    memory_id: str, about: Iterable[str], index: OpenQuestionIndex
) -> tuple[str, ...]:
    """The open-question labels of one hit (see the module docstring)."""

    if not index:
        return ()
    ids: set[str] = set()
    for concept in about:
        ids.update(index.by_concept.get(concept, ()))
    ids.discard(memory_id)
    labels = [f"{OPEN_QUESTION_LABEL_PREFIX}{item}" for item in sorted(ids)]
    if memory_id in index.conflicted:
        labels.append(CONFLICTED_LABEL)
    return tuple(labels)


__all__ = [
    "CONFLICTED_LABEL",
    "OPEN_QUESTION_LABEL_PREFIX",
    "OpenQuestionIndex",
    "open_question_labels",
]

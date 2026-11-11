"""Run ``memfw.dream.curate`` on the synthetic cases and turn its candidates
back into the gold label shape, so ``score_cases`` can score the real dream.

Mapping:

* a ``merge`` candidate ``[keep, drop...]`` -> gold pairs ``(drop, keep)``;
* a ``supersede`` candidate -> a conflict on the shared concept, with the
  attribute taken as the last word of the detected key (``cluster_color`` ->
  ``color``); proposing ``valid_to`` on the older memory also counts as a
  stale mark for it;
* a ``mark-stale`` candidate -> a stale mark.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from eval.dreams.cases import Case, Conflict, Memory
from memfw.core.models import MemoryDocument
from memfw.dream.curate import curate

_ATTRIBUTE = re.compile(r"attribute '([^']+)' differs")
NOW = datetime(2026, 8, 1, tzinfo=UTC)


def _document(memory: Memory) -> MemoryDocument:
    recorded = datetime.fromisoformat(memory.recorded_at)
    return MemoryDocument(
        id=memory.memory_id,
        title=memory.attribute,
        body=memory.body,
        about=[memory.about],
        valid_from=recorded,
        valid_to=datetime.fromisoformat(memory.valid_to) if memory.valid_to else None,
        reference_count=1,
        created_at=recorded,
        updated_at=recorded,
    )


def predict(case: Case) -> Case:
    documents = [_document(memory) for memory in case.memories]
    # The dream only curates current memories; a closed one counts as already stale.
    current = [document for document in documents if document.valid_to is None]
    stale = {document.id for document in documents if document.valid_to is not None}
    merges: list[tuple[str, str]] = []
    conflicts: list[Conflict] = []
    for candidate in curate(current, now=NOW):
        if candidate.action == "merge":
            keep, *drops = candidate.memory_ids
            merges.extend((drop, keep) for drop in drops)
        elif candidate.action == "supersede":
            newer, older = candidate.memory_ids
            for key in _ATTRIBUTE.findall(candidate.reason) or ["value"]:
                conflicts.append(
                    Conflict(candidate.oid, key.split("_")[-1], tuple(sorted((newer, older))))
                )
            stale.add(older)
        elif candidate.action == "mark-stale":
            stale.update(candidate.memory_ids)
    return Case(
        case_id=case.case_id,
        kind=case.kind,
        memories=case.memories,
        merges=tuple(merges),
        conflicts=tuple(conflicts),
        stale=tuple(sorted(stale)),
    )


def predict_all(gold: tuple[Case, ...]) -> tuple[Case, ...]:
    return tuple(predict(case) for case in gold)

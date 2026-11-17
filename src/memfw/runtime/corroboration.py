"""Corroboration as the calling app may see it (U5: display and review order).

The wiring between the U4 projection (``memfw.corroboration``), the share
policy in ``vault.yaml`` (``conversation_share``) and the two places that use
it:

* ``memory_search`` with ``labels=true`` gives every row a ``corroboration``
  field (:func:`rows_corroboration`), in every mode, two_stage included;
* the open-question review queue (:func:`open_question_queue`) puts questions
  whose ``conflicts`` include a corroborated memory first
  (``memfw.recall.priority`` has the rule).

Both read the projection trimmed to the caller's ``can_read``
(``corroboration_for``): only apps the caller may read, plus its own, appear in
``apps``, ``sessions`` and ``count``, so a hidden app's existence never shows.
Nothing here writes the vault or the projection, and nothing changes a
ranking, an ``epistemic`` value or a question.

When corroboration cannot be shown the field is ``{"degraded": <code>}`` (no
counts), never an error: :data:`CALLER_APP_REQUIRED` (no calling app, or the
reserved operator name over MCP), :data:`SHARE_POLICY_INVALID`,
:data:`PROJECTION_MISSING` (``memfw corroboration rebuild`` has not run) and
:data:`PROJECTION_UNREADABLE`.

Deferred (need evaluation data and approval first, design U5): promotion
candidates (``inference`` to ``fact``) and ranking weights.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC
from typing import Any, Final

from memfw.conversations.policy import OPERATOR, SharePolicy
from memfw.core.errors import MemfwError
from memfw.core.models import MemoryDocument
from memfw.core.repository import Repository
from memfw.corroboration import CorroborationError, corroboration_for, projection_path

CALLER_APP_REQUIRED: Final = "caller_app_required"
SHARE_POLICY_INVALID: Final = "share_policy_invalid"
PROJECTION_MISSING: Final = "projection_missing"
PROJECTION_UNREADABLE: Final = "projection_unreadable"

#: The review order, as the queue states it.
QUEUE_RULE: Final = (
    "corroborated conflicts (>= 2 distinct visible apps) first, then more support "
    "(their independent app/session pairs), then older, then id"
)


def caller_corroboration(
    repository: Repository,
    memory_ids: Sequence[str],
    *,
    app: str | None,
    allow_operator: bool = False,
) -> tuple[dict[str, dict[str, Any]], str | None]:
    """``({id: corroboration dict}, None)``, or ``({}, code)`` when degraded.

    ``app`` is the calling app; the answer is trimmed to what it may read.
    ``allow_operator`` lets the memfw CLI read as the operator (every readable
    app); an MCP caller can never name it.
    """

    caller = (app or "").strip().lower()
    if not caller or (caller == OPERATOR and not allow_operator):
        return {}, CALLER_APP_REQUIRED
    try:
        config = repository.read_config() if repository.config_path.exists() else {}
        rules = SharePolicy.from_config(config)
    except (MemfwError, OSError, ValueError):
        return {}, SHARE_POLICY_INVALID
    path = projection_path(repository.root)
    if path.is_symlink():
        return {}, PROJECTION_UNREADABLE
    if not path.is_file():
        return {}, PROJECTION_MISSING
    try:
        answer = corroboration_for(memory_ids, caller_app=caller, rules=rules, path=path)
    except (CorroborationError, OSError, ValueError):
        return {}, PROJECTION_UNREADABLE
    return {memory_id: value.as_dict() for memory_id, value in answer.items()}, None


def rows_corroboration(
    repository: Repository, rows: Sequence[dict[str, Any]], *, app: str | None
) -> None:
    """Set ``corroboration`` on every row (``labels=true``; see the module docstring)."""

    if not rows:
        return
    found, degraded = caller_corroboration(repository, [str(row["id"]) for row in rows], app=app)
    for row in rows:
        if degraded is not None:
            row["corroboration"] = {"degraded": degraded}
        else:
            row["corroboration"] = dict(found[str(row["id"])])


def _created(document: MemoryDocument) -> str:
    moment = document.created_at
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def open_question_queue(
    repository: Repository,
    questions: Sequence[MemoryDocument],
    *,
    app: str | None,
    visible: Callable[[MemoryDocument], bool],
    allow_operator: bool = False,
) -> dict[str, Any]:
    """The review queue of ``questions`` (already filtered by the caller's policy).

    Only conflicts that exist and ``visible`` keeps are looked up, so a
    memory the caller cannot see never moves a question. Degraded
    corroboration leaves the plain ``(created_at, id)`` order and says why.
    """

    from memfw.recall.priority import QuestionEntry, Visible, order_questions

    by_id = {document.id: document for document in questions}
    entries = [
        QuestionEntry(
            question_id=document.id,
            conflicts=tuple(document.open_question.conflicts if document.open_question else ()),
            created_at=_created(document),
        )
        for document in questions
    ]
    wanted: list[str] = []
    for entry in entries:
        for memory_id in entry.conflicts:
            if memory_id in wanted:
                continue
            try:
                document = repository.get(memory_id)
            except (MemfwError, OSError, UnicodeDecodeError, ValueError):
                continue
            if visible(document):
                wanted.append(memory_id)
    # Called even with no conflicts to look up, so a missing caller or
    # projection is still reported.
    found, degraded = caller_corroboration(
        repository, wanted, app=app, allow_operator=allow_operator
    )
    seen = {
        memory_id: Visible(count=int(value["count"]), apps=len(value["apps"]))
        for memory_id, value in found.items()
    }
    rows: list[dict[str, Any]] = []
    for priority in order_questions(entries, seen):
        document = by_id[priority.question_id]
        block = document.open_question
        rows.append(
            {
                "id": document.id,
                "title": document.title,
                "question": block.question if block else "",
                "missing": block.missing if block else "",
                "about": list(document.about),
                "conflicts": list(block.conflicts) if block else [],
                "created_at": priority.created_at,
                "priority": priority.as_dict(),
            }
        )
    return {
        "questions": rows,
        "rule": QUEUE_RULE,
        "corroboration": {"degraded": degraded},
    }


__all__ = [
    "CALLER_APP_REQUIRED",
    "PROJECTION_MISSING",
    "PROJECTION_UNREADABLE",
    "QUEUE_RULE",
    "SHARE_POLICY_INVALID",
    "caller_corroboration",
    "open_question_queue",
    "rows_corroboration",
]

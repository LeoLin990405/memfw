"""Dream step ②b — a conflict becomes an open-question candidate (U3).

When curate finds two current memories about one concept that contradict each
other (the pairs it proposes a ``supersede`` for), the dream also proposes a
``type: open-question`` memory that names both. It is a candidate like every
other dream output: nothing is written to the vault, no winner is picked, and
neither original changes (their ``epistemic``, ``status`` and ``evidence`` stay
as they are). A human reviews it (see ``docs/dream.md``):

* **accept the question** — :func:`accept_question` (``memfw dream
  accept-question``) writes the open question as the human, through the same
  write gate as any other open question. Its title, body, ``about`` and block
  are rebuilt from the two memories, never copied from the staged file;
* **answer and supersede** — write (or pick) the memory that is right,
  ``supersede`` the wrong one; if the question was accepted, close it with
  ``Repository.resolve_open_question``;
* **reject** — :func:`reject_question` (``memfw dream reject-question``)
  removes only that candidate from its concept file and records a rejection
  for the pair at the memories' current versions.

Rules (all deterministic, no model):

* **Dedupe.** No candidate for an unordered pair while
  - a current, unresolved open question in the tenant lists both memories in
    ``conflicts``;
  - a resolved (or closed) question lists both, and neither memory changed
    after it was closed (``updated_at`` after the question's ``valid_to``);
  - a rejection is recorded for the pair and both memories are still at the
    recorded ``version`` and ``updated_at``;
  - a staged dream holds an ``open-question`` candidate for the pair that has
    not been accepted (an accepted one is linked by the ``dream:<dream_id>``
    evidence pointer :func:`accept_question` records).
* **Same tenant only.** A pair whose memories are in different tenants (or not
  in the dream's tenant) is skipped.
* **Privacy.** A candidate names its memories and its concept, and the question
  it proposes is recalled like any memory. A pair in which either memory is
  private in any way is skipped, so a question never reveals a private
  memory's id: a ``sensitivity:*`` tag, a ``privacy:`` tag other than ``none``,
  or a lifecycle meta-index item (joined by ``fleet_path``) whose privacy is not
  ``none``. Unknown values, an id that does not match its ``fleet_path``, and a
  meta-index that cannot be read all count as private (fail closed).
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
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
from memfw.projection import (
    MetaIndex,
    ProjectionError,
    classify_privacy,
    fleet_paths,
    join_meta,
    load_meta_index,
    tag_privacy_class,
)

from .curate import conflict_reasons
from .model import Candidate, EvidenceRef, concept_key
from .stage import DREAM_DIR, check_dream_id, render_concept

ACTION: Final[Literal["open-question"]] = "open-question"
"""The candidate action this step stages."""

EVIDENCE_PREFIX = "dream:"
"""Evidence pointer an accepted question carries: ``dream:<dream_id>``."""

REJECTIONS_DIR = Path("_candidates") / "dream-rejections"
"""Where :func:`reject_question` records rejected pairs (one JSON file each)."""

DEFAULT_LIFECYCLE_SOURCE = Path("status") / "memory-meta-index.json"
"""The lifecycle meta-index read when none is named (relative to the vault)."""

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


# --- privacy ---------------------------------------------------------------------


@dataclass(frozen=True)
class Lifecycle:
    """The lifecycle meta-index the privacy check joins (``None``: not present)."""

    meta: MetaIndex | None = None
    unreadable: bool = False


def load_lifecycle(root: Path, source: Path | None = None) -> Lifecycle:
    """Read the lifecycle meta-index: ``source``, else the vault's default file.

    No file is no meta-index (tags decide). A named or default file that
    cannot be read, or a symlinked default, makes every memory with a
    ``fleet_path`` tag count as private.
    """

    path = source
    if path is None:
        default = Path(root) / DEFAULT_LIFECYCLE_SOURCE
        if default.is_symlink():
            return Lifecycle(unreadable=True)
        if not default.is_file():
            return Lifecycle()
        path = default
    try:
        return Lifecycle(meta=load_meta_index(path))
    except ProjectionError:
        return Lifecycle(unreadable=True)


def is_private(document: MemoryDocument, lifecycle: Lifecycle | None = None) -> bool:
    """Whether a memory is private in any way (fail closed on unknown values)."""

    tag_class = tag_privacy_class(document.tags)
    if tag_class != "none":
        return True
    for tag in document.tags:
        if tag.lower().startswith("privacy:"):
            privacy, _ = classify_privacy(tag.split(":", 1)[1], tag_class="none")
            if privacy != "none":
                return True
    if lifecycle is None or not fleet_paths(document.tags):
        return False
    if lifecycle.unreadable:
        return True
    join = join_meta(document.id, document.tags, lifecycle.meta)
    if join.status == "id_mismatch":
        return True
    if join.status == "ok" and join.item is not None:
        privacy, _ = classify_privacy(join.item.get("privacy"), tag_class=tag_class)
        return privacy != "none"
    return False


# --- what blocks a new candidate ---------------------------------------------------


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


def _closed_and_unchanged(question: MemoryDocument, members: Sequence[MemoryDocument]) -> bool:
    """A closed question still blocks while neither memory changed after it closed."""

    cutoff = question.valid_to or question.updated_at
    return all(member.updated_at <= cutoff for member in members)


def _question_blocks(
    questions: Iterable[MemoryDocument], pair: Pair, members: Sequence[MemoryDocument]
) -> str | None:
    """Why an existing question blocks a new one for ``pair`` (``None``: it does not)."""

    for question in questions:
        if not _covers(question, pair):
            continue
        if _is_unresolved(question):
            return "unresolved"
        if _closed_and_unchanged(question, members):
            return "resolved"
    return None


def _version_of(document: MemoryDocument) -> dict[str, Any]:
    return {"version": document.version, "updated_at": document.updated_at.isoformat()}


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


def _split(text: str) -> tuple[Mapping[str, Any], str] | None:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end < 0:
        return None
    try:
        front = yaml.safe_load(text[4:end])
    except yaml.YAMLError:
        return None
    if not isinstance(front, Mapping):
        return None
    return front, text[end + 5 :]


def _is_question(raw: Any) -> bool:
    if not isinstance(raw, Mapping) or raw.get("action") != ACTION:
        return False
    ids = raw.get("memory_ids")
    if not isinstance(ids, list) or len(ids) != 2:
        return False
    if not all(isinstance(item, str) for item in ids):
        return False
    changes = raw.get("changes")
    return isinstance(changes, Mapping) and isinstance(changes.get("new_memory"), Mapping)


def _questions_in(directory: Path, dream_id: str) -> list[StagedQuestion]:
    found: list[StagedQuestion] = []
    for path in sorted(directory.glob("*.md")):
        if not _plain_file(path):
            continue
        try:
            parsed = _split(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if parsed is None:
            continue
        for raw in parsed[0].get("candidates") or []:
            if not _is_question(raw):
                continue
            ids = raw["memory_ids"]
            found.append(
                StagedQuestion(
                    dream_id=dream_id,
                    path=path,
                    pair=pair_key(ids[0], ids[1]),
                    tenant=str(raw["changes"]["new_memory"].get("tenant") or ""),
                    candidate=raw,
                )
            )
    return found


def _candidates_root_ok(root: Path) -> bool:
    return _plain_dir(Path(root) / DREAM_DIR.parts[0])


def staged_questions(root: Path, *, tenant: str | None = None) -> list[StagedQuestion]:
    """Every staged ``open-question`` candidate under ``_candidates/dream/``.

    Read-only. Symlinked or hidden dream directories and symlinked files are
    skipped, never followed; an unreadable file is skipped.
    """

    base = Path(root) / DREAM_DIR
    if not _candidates_root_ok(root) or not _plain_dir(base):
        return []
    found: list[StagedQuestion] = []
    for directory in sorted(base.iterdir()):
        if directory.name.startswith(".") or not _plain_dir(directory):
            continue
        for item in _questions_in(directory, directory.name):
            if tenant is None or item.tenant == tenant:
                found.append(item)
    return found


# --- rejections ------------------------------------------------------------------


def _rejection_name(tenant: str, pair: Pair) -> str:
    digest = hashlib.sha256("\0".join((tenant, *pair)).encode("utf-8")).hexdigest()
    return f"{digest[:32]}.json"


def rejections(root: Path, *, tenant: str) -> dict[Pair, dict[str, Any]]:
    """Recorded rejections for ``tenant``: pair -> the versions it was rejected at."""

    base = Path(root) / REJECTIONS_DIR
    if not _candidates_root_ok(root) or not _plain_dir(base):
        return {}
    found: dict[Pair, dict[str, Any]] = {}
    for path in sorted(base.glob("*.json")):
        if not _plain_file(path):
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if not isinstance(raw, Mapping) or raw.get("tenant") != tenant:
            continue
        ids = raw.get("memory_ids")
        versions = raw.get("versions")
        if not isinstance(ids, list) or len(ids) != 2 or not isinstance(versions, Mapping):
            continue
        if not all(isinstance(item, str) for item in ids):
            continue
        found[pair_key(ids[0], ids[1])] = dict(versions)
    return found


def _rejected(record: Mapping[str, Any] | None, members: Sequence[MemoryDocument]) -> bool:
    if record is None:
        return False
    return all(record.get(member.id) == _version_of(member) for member in members)


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
    rejected: Mapping[Pair, Mapping[str, Any]] | None = None,
    lifecycle: Lifecycle | None = None,
) -> tuple[list[Candidate], QuestionStats]:
    """Open-question candidates for the conflicts curate found.

    ``curated`` is curate's output (its ``supersede`` candidates name the
    conflicting pairs). ``documents`` is every memory of ``tenant`` the dream
    read, historical ones included, so an accepted (even resolved) question
    still links its dream. ``staged`` is :func:`staged_questions` and
    ``rejected`` :func:`rejections` for the tenant; ``lifecycle`` the
    meta-index the privacy check joins. Returns the candidates (sorted by oid,
    then pair) and the counts.
    """

    stats = QuestionStats()
    rejected = rejected or {}
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
        if is_private(left, lifecycle) or is_private(right, lifecycle):
            stats.skipped_private += 1
            continue
        members = (left, right)
        if _question_blocks(questions, pair, members) or _rejected(rejected.get(pair), members):
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


def _find_staged(repository: Repository, dream_id: str, pair: Pair) -> StagedQuestion:
    directory = repository.root / DREAM_DIR / dream_id
    if (
        not _candidates_root_ok(repository.root)
        or not _plain_dir(repository.root / DREAM_DIR)
        or not _plain_dir(directory)
    ):
        raise NotFoundError(f"no staged dream {dream_id}")
    matches = [item for item in _questions_in(directory, dream_id) if item.pair == pair]
    if not matches:
        raise NotFoundError(
            f"dream {dream_id} has no open-question candidate for {pair[0]} and {pair[1]}"
        )
    return matches[0]


def _check_args(dream_id: str, memory_ids: Sequence[str], actor: str) -> Pair:
    check_dream_id(dream_id)
    if len(memory_ids) != 2 or memory_ids[0] == memory_ids[1]:
        raise ValueError("an open-question candidate names exactly two memories")
    if actor != "human":
        raise IntegrityError("only a human reviews an open-question candidate")
    return pair_key(memory_ids[0], memory_ids[1])


def _differences(staged: Mapping[str, Any], rebuilt: Candidate) -> list[str]:
    expected = rebuilt.as_dict()
    fields = [
        key
        for key in ("operation", "memory_ids", "oid", "reason", "evidence")
        if staged.get(key) != expected[key]
    ]
    staged_changes = staged.get("changes")
    if not isinstance(staged_changes, Mapping):
        return [*fields, "changes"]
    staged_memory = staged_changes.get("new_memory")
    expected_memory = expected["changes"]["new_memory"]
    if not isinstance(staged_memory, Mapping):
        return [*fields, "changes.new_memory"]
    keys = sorted(set(staged_memory) | set(expected_memory))
    fields += [
        f"new_memory.{key}" for key in keys if staged_memory.get(key) != expected_memory.get(key)
    ]
    other = sorted(
        key
        for key in set(staged_changes) | set(expected["changes"])
        if key != "new_memory" and staged_changes.get(key) != expected["changes"].get(key)
    )
    fields += [f"changes.{key}" for key in other]
    return fields


def accept_question(
    repository: Repository,
    dream_id: str,
    memory_ids: Sequence[str],
    *,
    actor: str = "human",
    dry_run: bool = False,
    lifecycle_source: Path | None = None,
) -> dict[str, Any]:
    """Accept one staged open-question candidate: the human writes the question.

    The question is **rebuilt** from the two memories as they are now (concept,
    conflict description, ids), exactly as the dream builds it; the staged file
    only identifies the candidate. If the staged candidate differs from the
    rebuild (the file was edited, or a memory changed since the dream), it is
    refused: re-run the dream. The write is ``Repository.add`` as ``actor``
    (only a human may create an open question), so the full U1 gate runs. The
    question records ``dream:<dream_id>`` in ``evidence``, which also tells
    later dreams this candidate was consumed. All checks and the write run in
    one section under the repository lock, so two concurrent accepts write at
    most one question.

    Also refused when either memory is no longer current, has become private,
    or the two no longer conflict; when a question for the pair blocks a new
    one; or when this candidate was already accepted. ``dry_run`` returns the
    full memory that would be written.
    """

    pair = _check_args(dream_id, memory_ids, actor)
    lifecycle = load_lifecycle(repository.root, lifecycle_source)
    with repository._locked():
        staged = _find_staged(repository, dream_id, pair)
        members: list[MemoryDocument] = []
        for memory_id in pair:
            document = _current(repository, memory_id)
            if document is None or document.tenant != staged.tenant:
                raise IntegrityError(
                    f"{memory_id} is no longer current in this tenant; the conflict is settled"
                )
            if is_private(document, lifecycle):
                raise IntegrityError(f"{memory_id} is private; an open question may not name it")
            members.append(document)
        left, right = members
        oid = concept_key(left)
        if concept_key(right) != oid:
            raise IntegrityError(
                f"{left.id} and {right.id} are no longer about the same concept; re-run the dream"
            )
        reasons = conflict_reasons(left, right)
        if not reasons:
            raise IntegrityError(
                f"{left.id} and {right.id} no longer conflict; the conflict is settled"
            )
        rebuilt = _candidate(oid, left, right, reasons, left.tenant)
        changed = _differences(staged.candidate, rebuilt)
        if changed:
            raise IntegrityError(
                "the staged candidate differs from what the dream proposes for these "
                f"memories now ({', '.join(changed)}): it was edited, or a memory changed "
                "since the dream; re-run the dream and review the new candidate"
            )
        existing = [
            document
            for document in repository.list()
            if document.type == OPEN_QUESTION_TYPE
            and document.open_question is not None
            and document.tenant == left.tenant
            and _covers(document, pair)
        ]
        if any(_accepted_from(document, dream_id) for document in existing):
            raise IntegrityError(f"this candidate was already accepted from dream {dream_id}")
        blocked = _question_blocks(existing, pair, members)
        if blocked == "unresolved":
            raise IntegrityError(
                f"an unresolved open question already names {pair[0]} and {pair[1]}"
            )
        if blocked == "resolved":
            raise IntegrityError(
                f"a resolved open question already names {pair[0]} and {pair[1]}, and "
                "neither memory changed since"
            )
        new_memory = rebuilt.changes["new_memory"]
        question = OpenQuestion.model_validate(new_memory["open_question"])
        memory = {
            "type": OPEN_QUESTION_TYPE,
            "title": new_memory["title"],
            "body": new_memory["body"],
            "tenant": left.tenant,
            "about": list(new_memory["about"]),
            "open_question": question.model_dump(),
            "evidence": [f"{EVIDENCE_PREFIX}{dream_id}"],
            "source": "dream",
            "provenance": "dream",
        }
        plan = {
            "dream_id": dream_id,
            "candidate": os.fspath(staged.path.relative_to(repository.root)),
            "memory_ids": list(pair),
            "new_memory": memory,
        }
        if dry_run:
            return {**plan, "dry_run": True, "writes": False}
        document = repository.add(
            memory["title"],
            memory["body"],
            memory_type=OPEN_QUESTION_TYPE,
            tenant=memory["tenant"],
            about=memory["about"],
            open_question=question,
            evidence=memory["evidence"],
            source=memory["source"],
            provenance=memory["provenance"],
            actor=actor,
        )
    return {**plan, "dry_run": False, "memory_id": document.id, "status": "accepted"}


def _as_candidate(raw: Mapping[str, Any]) -> Candidate:
    try:
        return Candidate(
            operation=raw["operation"],
            action=raw["action"],
            memory_ids=[str(item) for item in raw.get("memory_ids") or []],
            reason=str(raw.get("reason") or ""),
            oid=str(raw.get("oid") or ""),
            evidence=[EvidenceRef(**item) for item in raw.get("evidence") or []],
            changes=dict(raw.get("changes") or {}),
        )
    except (KeyError, TypeError) as exc:
        raise IntegrityError(f"the staged concept file has an unreadable candidate: {exc}") from exc


def _write_replacing(path: Path, text: str) -> None:
    """Replace ``path`` atomically with a new file in its directory (no symlinks)."""

    temporary = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def reject_question(
    repository: Repository,
    dream_id: str,
    memory_ids: Sequence[str],
    *,
    actor: str = "human",
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reject one staged open-question candidate. Never writes the vault.

    Removes only the ``open-question`` candidate(s) for the pair from its
    concept file (the other candidates there stay; a file left empty is
    removed) and records a rejection under ``_candidates/dream-rejections/``:
    the tenant, the pair and both memories' ``version`` and ``updated_at``.
    Later dreams propose no question for the pair while both memories are
    still at those versions. ``dry_run`` reports what would change.
    """

    pair = _check_args(dream_id, memory_ids, actor)
    with repository._locked():
        staged = _find_staged(repository, dream_id, pair)
        versions: dict[str, Any] = {}
        for memory_id in pair:
            try:
                document = repository.get(memory_id)
            except NotFoundError:
                continue
            if document.tenant == staged.tenant:
                versions[memory_id] = _version_of(document)
        parsed = _split(staged.path.read_text(encoding="utf-8"))
        if parsed is None:
            raise IntegrityError(f"cannot read the staged concept file {staged.path.name}")
        front, _ = parsed
        kept = [
            raw
            for raw in front.get("candidates") or []
            if not (_is_question(raw) and pair_key(*raw["memory_ids"]) == pair)
        ]
        record = {
            "kind": "open-question-rejection",
            "tenant": staged.tenant,
            "memory_ids": list(pair),
            "versions": versions,
            "dream_id": dream_id,
            "rejected_at": (now or datetime.now(UTC)).isoformat(),
        }
        relative = os.fspath(staged.path.relative_to(repository.root))
        plan = {
            "dream_id": dream_id,
            "candidate": relative,
            "memory_ids": list(pair),
            "candidates_left_in_file": len(kept),
            "rejection": record,
        }
        if dry_run:
            return {**plan, "dry_run": True, "writes": False}
        if kept:
            text = render_concept(
                str(front.get("dream_id") or dream_id),
                str(front.get("oid") or ""),
                [_as_candidate(raw) for raw in kept],
                str(front.get("created_at") or ""),
                str(front["recorded_at"]) if front.get("recorded_at") else None,
            )
        base = repository.root / REJECTIONS_DIR
        if not _candidates_root_ok(repository.root):
            raise IntegrityError("refusing to record a rejection: _candidates is not a directory")
        if base.is_symlink() or (base.exists() and not base.is_dir()):
            raise IntegrityError(
                f"refusing to record a rejection: {REJECTIONS_DIR} is not a plain directory"
            )
        base.mkdir(mode=0o700, exist_ok=True)
        target = base / _rejection_name(staged.tenant, pair)
        if target.is_symlink():
            raise IntegrityError(f"refusing to record a rejection: {target.name} is a symlink")
        _write_replacing(
            target, json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
        if kept:
            _write_replacing(staged.path, text)
        else:
            staged.path.unlink()
    return {**plan, "dry_run": False, "status": "rejected"}


__all__ = [
    "ACTION",
    "DEFAULT_LIFECYCLE_SOURCE",
    "EVIDENCE_PREFIX",
    "MISSING",
    "REJECTIONS_DIR",
    "Lifecycle",
    "QuestionStats",
    "StagedQuestion",
    "accept_question",
    "check_candidate",
    "conflict_questions",
    "is_private",
    "load_lifecycle",
    "pair_key",
    "reject_question",
    "rejections",
    "staged_questions",
]

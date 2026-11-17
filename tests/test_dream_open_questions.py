"""U3: a conflict the dream finds becomes an open-question candidate (proposal-only)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from memfw.cli import app
from memfw.core.epistemic import OPEN_QUESTION_TYPE, validate_epistemic_write
from memfw.core.errors import IntegrityError, NotFoundError
from memfw.core.models import MemoryDocument
from memfw.core.repository import Repository
from memfw.dream import DreamOptions, run_dream
from memfw.dream import run as dream_run_module
from memfw.dream.model import Candidate
from memfw.dream.questions import (
    ACTION,
    MISSING,
    accept_question,
    conflict_questions,
    staged_questions,
)

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
CONCEPT = "concept:svc:retry"
OLD = "Retry Count: 3\nowner = team red"
NEW = "retry-count: 5\nOwner = team red"


def _vault(tmp_path: Path, **extra: Any) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    tags_old = extra.pop("tags_old", None)
    tags_new = extra.pop("tags_new", None)
    repository.add(
        "Retry policy",
        OLD,
        memory_id="m-old",
        about=[CONCEPT],
        epistemic="fact",
        tags=tags_old,
    )
    repository.add(
        "Retry policy",
        NEW,
        memory_id="m-new",
        about=[CONCEPT],
        epistemic="inference",
        tags=tags_new,
    )
    return repository


def _questions(result: Any) -> list[Candidate]:
    return [item for item in result.candidates if item.action == ACTION]


def _state(root: Path) -> dict[str, str]:
    state = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((root / "memories").glob("*.md"))
    }
    state["events"] = hashlib.sha256((root / "events.jsonl").read_bytes()).hexdigest()
    return state


def _dream(root: Path, dream_id: str | None = None, **options: Any) -> Any:
    return run_dream(
        root, now=NOW, dry_run=dream_id is None, options=DreamOptions(dream_id=dream_id, **options)
    )


# -- emission ---------------------------------------------------------------------------


def test_conflict_emits_one_open_question_candidate(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    result = _dream(repository.root)

    (question,) = _questions(result)
    assert question.operation == "curate"
    assert question.memory_ids == ["m-new", "m-old"]
    assert question.oid == CONCEPT
    new_memory = question.changes["new_memory"]
    assert new_memory["type"] == OPEN_QUESTION_TYPE
    assert new_memory["about"] == [CONCEPT]
    assert new_memory["tenant"] == ""
    assert new_memory["open_question"] == {
        "question": f"Which of m-new and m-old is right about {CONCEPT}?",
        "conflicts": ["m-new", "m-old"],
        "missing": MISSING,
        "resolved_by": None,
    }
    assert question.changes["conflicts"] == ["m-new", "m-old"]
    # No claim of any kind, and nothing that would pick a winner.
    for key in ("epistemic", "causal", "causal_record", "experience", "status"):
        assert key not in new_memory
    assert "superseded_by" not in question.changes
    # The new memory passes the shared gate as a new document.
    validate_epistemic_write(dict(new_memory), actor="mechanism")
    # The supersede proposal is still there, unchanged; the human picks.
    assert [item.action for item in result.candidates if item.oid == CONCEPT].count(
        "supersede"
    ) == 1
    assert result.report["open_questions"] == {
        "conflicts": 1,
        "proposed": 1,
        "deduplicated": 0,
        "skipped_private": 0,
        "skipped_tenant": 0,
    }
    assert result.report["candidates"][ACTION] == 1
    assert result.report["skipped_candidates"] == []


def test_staged_candidate_lands_in_the_concept_file(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    before = _state(repository.root)
    result = _dream(repository.root, "dream-a")
    assert _state(repository.root) == before
    (staged,) = staged_questions(repository.root, tenant="")
    assert staged.dream_id == "dream-a"
    assert staged.pair == ("m-new", "m-old")
    front = yaml.safe_load(staged.path.read_text(encoding="utf-8").split("---\n")[1])
    assert front["authority"] is False
    assert ACTION in {item["action"] for item in front["candidates"]}
    assert result.staged == "_candidates/dream/dream-a"


def test_originals_keep_their_epistemic_and_bytes(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    before = _state(repository.root)
    _dream(repository.root, "dream-a")
    accept_question(repository, "dream-a", ["m-old", "m-new"], dry_run=False)
    after = _state(repository.root)
    assert after["m-old.md"] == before["m-old.md"]
    assert after["m-new.md"] == before["m-new.md"]
    assert repository.get("m-old").epistemic == "fact"
    assert repository.get("m-new").epistemic == "inference"
    assert repository.get("m-old").status == "unconfirmed"


# -- dedupe -----------------------------------------------------------------------------


def test_pending_staged_candidate_blocks_a_new_one(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    _dream(repository.root, "dream-a")
    again = _dream(repository.root)
    assert _questions(again) == []
    assert again.report["open_questions"]["deduplicated"] == 1
    # The supersede proposal is not deduplicated: only the question is.
    assert any(item.action == "supersede" for item in again.candidates)


def test_unresolved_question_blocks_and_resolution_reopens(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    _dream(repository.root, "dream-a")
    accepted = accept_question(repository, "dream-a", ["m-new", "m-old"], dry_run=False)
    # The accepted question exists and is unresolved: still no new candidate.
    assert _questions(_dream(repository.root)) == []

    # Resolve it with an answer while both originals stay current: dream-a's
    # candidate was consumed, so the persisting conflict is proposed again.
    repository.add("Answer", "Ask the owners first.", memory_id="m-answer")
    repository.resolve_open_question(accepted["memory_id"], "m-answer")
    reopened = _dream(repository.root, "dream-b")
    (question,) = _questions(reopened)
    assert question.memory_ids == ["m-new", "m-old"]
    # ... and dream-b's candidate is pending now.
    assert _questions(_dream(repository.root)) == []


def test_removing_the_staged_file_is_a_rejection(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    _dream(repository.root, "dream-a")
    shutil.rmtree(repository.root / "_candidates" / "dream" / "dream-a")
    assert len(_questions(_dream(repository.root))) == 1


def test_symlinked_dream_directory_is_not_followed(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    _dream(repository.root, "dream-a")
    base = repository.root / "_candidates" / "dream"
    moved = tmp_path / "elsewhere"
    shutil.move(base / "dream-a", moved)
    os.symlink(moved, base / "dream-link")
    assert staged_questions(repository.root) == []
    assert len(_questions(_dream(repository.root))) == 1


# -- tenant and privacy -----------------------------------------------------------------


def _doc(memory_id: str, tenant: str = "", tags: list[str] | None = None) -> MemoryDocument:
    return MemoryDocument(
        id=memory_id,
        title="Retry policy",
        body=OLD if memory_id == "a" else NEW,
        about=[CONCEPT],
        tenant=tenant,
        tags=tags or [],
        created_at=NOW,
        updated_at=NOW,
    )


def _supersede(left: str, right: str) -> Candidate:
    return Candidate(
        operation="curate",
        action="supersede",
        memory_ids=[left, right],
        reason="conflict",
        oid=CONCEPT,
    )


def test_cross_tenant_pair_is_skipped() -> None:
    documents = [_doc("a", tenant="alpha"), _doc("b", tenant="beta")]
    candidates, stats = conflict_questions([_supersede("b", "a")], documents, tenant="alpha")
    assert candidates == []
    assert stats.skipped_tenant == 1
    # A pair wholly in another tenant than the dream's is skipped too.
    candidates, stats = conflict_questions(
        [_supersede("b", "a")], [_doc("a", "beta"), _doc("b", "beta")], tenant="alpha"
    )
    assert candidates == [] and stats.skipped_tenant == 1


def test_dream_never_pairs_memories_across_tenants(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("Retry policy", OLD, memory_id="m-old", about=[CONCEPT], tenant="alpha")
    repository.add("Retry policy", NEW, memory_id="m-new", about=[CONCEPT], tenant="beta")
    for tenant in ("alpha", "beta"):
        result = _dream(repository.root, tenant=tenant)
        assert _questions(result) == []
        assert "open_questions" not in result.report


def test_staged_candidate_of_another_tenant_does_not_dedupe(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    for tenant in ("alpha", "beta"):
        repository.add("Retry", OLD, memory_id=f"{tenant}-old", about=[CONCEPT], tenant=tenant)
        repository.add("Retry", NEW, memory_id=f"{tenant}-new", about=[CONCEPT], tenant=tenant)
    _dream(repository.root, "dream-alpha", tenant="alpha")
    assert [item.tenant for item in staged_questions(repository.root)] == ["alpha"]
    assert staged_questions(repository.root, tenant="beta") == []
    assert len(_questions(_dream(repository.root, tenant="beta"))) == 1


@pytest.mark.parametrize(
    "tags",
    [
        ["sensitivity:private"],
        ["sensitivity:secret"],
        ["privacy:local-private"],
        ["privacy:something-unknown"],
        ["privacy:none", "privacy:local-private"],
    ],
)
def test_private_memory_is_never_named_in_a_question(tmp_path: Path, tags: list[str]) -> None:
    repository = _vault(tmp_path, tags_new=tags)
    result = _dream(repository.root)
    assert _questions(result) == []
    assert result.report["open_questions"]["skipped_private"] == 1


def test_public_privacy_tags_still_emit(tmp_path: Path) -> None:
    repository = _vault(tmp_path, tags_old=["privacy:none"], tags_new=["privacy:shared"])
    assert len(_questions(_dream(repository.root))) == 1


# -- the gate on staging ----------------------------------------------------------------


def test_candidate_failing_the_gate_is_skipped_not_staged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _vault(tmp_path)
    original = dream_run_module.conflict_questions

    def tampered(*args: Any, **kwargs: Any) -> Any:
        candidates, stats = original(*args, **kwargs)
        for candidate in candidates:
            candidate.changes["new_memory"]["epistemic"] = "fact"
        return candidates, stats

    monkeypatch.setattr(dream_run_module, "conflict_questions", tampered)
    result = _dream(repository.root)
    assert _questions(result) == []
    (skipped,) = [item for item in result.report["skipped_candidates"] if item["action"] == ACTION]
    assert "epistemic" in skipped["reason"]


# -- human review: accept ---------------------------------------------------------------


def test_accept_writes_a_valid_open_question(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    _dream(repository.root, "dream-a")
    before = _state(repository.root)
    plan = accept_question(repository, "dream-a", ["m-old", "m-new"], dry_run=True)
    assert plan["writes"] is False
    assert _state(repository.root) == before

    result = accept_question(repository, "dream-a", ["m-old", "m-new"], dry_run=False)
    question = repository.get(result["memory_id"])
    assert question.type == OPEN_QUESTION_TYPE
    assert question.epistemic is None
    assert question.causal == "none"
    assert question.about == [CONCEPT]
    assert question.evidence == ["dream:dream-a"]
    assert question.open_question is not None
    assert question.open_question.conflicts == ["m-new", "m-old"]
    assert question.open_question.missing == MISSING
    assert question.open_question.resolved_by is None
    create = [event for event in repository.events() if event.memory_id == question.id]
    assert [event.actor for event in create] == ["human"]
    assert not [issue for issue in repository.fsck() if "open_question" in str(issue)]


def test_accept_refusals(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    _dream(repository.root, "dream-a")
    with pytest.raises(IntegrityError):
        accept_question(repository, "dream-a", ["m-old", "m-new"], actor="mechanism")
    with pytest.raises(NotFoundError):
        accept_question(repository, "dream-a", ["m-old", "m-other"])
    with pytest.raises(NotFoundError):
        accept_question(repository, "dream-missing", ["m-old", "m-new"])
    with pytest.raises(ValueError):
        accept_question(repository, "dream-a", ["m-old", "m-old"])
    accept_question(repository, "dream-a", ["m-old", "m-new"], dry_run=False)
    with pytest.raises(IntegrityError, match="already accepted"):
        accept_question(repository, "dream-a", ["m-old", "m-new"], dry_run=False)


def test_accept_refuses_a_settled_conflict(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    _dream(repository.root, "dream-a")
    # Answer + supersede: the human settles it without the question.
    repository.supersede("m-old", "Retry policy", "retry-count: 5\nowner = team red")
    with pytest.raises(IntegrityError, match="settled"):
        accept_question(repository, "dream-a", ["m-old", "m-new"], dry_run=False)
    # And no new question is proposed for a pair that no longer conflicts.
    assert all(
        set(item.memory_ids) != {"m-old", "m-new"} for item in _questions(_dream(repository.root))
    )


def _tamper(path: Path, **fields: Any) -> None:
    _, raw, rest = path.read_text(encoding="utf-8").split("---\n", 2)
    front = yaml.safe_load(raw)
    for candidate in front["candidates"]:
        if candidate["action"] == ACTION:
            candidate["changes"]["new_memory"].update(fields)
    dumped = yaml.safe_dump(front, sort_keys=False)
    path.write_text(f"---\n{dumped}---\n{rest}", encoding="utf-8")


def test_accept_never_copies_claims_or_trusts_a_tampered_file(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    _dream(repository.root, "dream-a")
    (staged,) = staged_questions(repository.root)
    original = staged.path.read_text(encoding="utf-8")
    _tamper(
        staged.path,
        open_question={"question": "q?", "conflicts": ["m-new", "m-ghost"], "missing": ""},
    )
    with pytest.raises(IntegrityError, match="does not match"):
        accept_question(repository, "dream-a", ["m-old", "m-new"], dry_run=False)
    staged.path.write_text(original, encoding="utf-8")
    _tamper(staged.path, tenant="beta")
    with pytest.raises(IntegrityError, match="settled"):
        accept_question(repository, "dream-a", ["m-old", "m-new"], dry_run=False)
    staged.path.write_text(original, encoding="utf-8")
    _tamper(staged.path, epistemic="fact", causal="hypothesis")
    result = accept_question(repository, "dream-a", ["m-old", "m-new"], dry_run=False)
    question = repository.get(result["memory_id"])
    assert question.epistemic is None and question.causal == "none"


def test_accept_refuses_a_memory_that_became_private(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    _dream(repository.root, "dream-a")
    repository.update("m-new", tags=["sensitivity:private"], actor="human")
    with pytest.raises(IntegrityError, match="private"):
        accept_question(repository, "dream-a", ["m-old", "m-new"], dry_run=False)


def test_cli_accept_question(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    _dream(repository.root, "dream-a")
    runner = CliRunner()
    root = str(repository.root)
    dry = runner.invoke(app, ["dream", "accept-question", "dream-a", "m-old", "m-new", "-r", root])
    assert dry.exit_code == 0, dry.output
    assert json.loads(dry.output)["writes"] is False
    applied = runner.invoke(
        app, ["dream", "accept-question", "dream-a", "m-old", "m-new", "-r", root, "--apply"]
    )
    assert applied.exit_code == 0, applied.output
    memory_id = json.loads(applied.output)["memory_id"]
    assert repository.get(memory_id).type == OPEN_QUESTION_TYPE
    again = runner.invoke(
        app, ["dream", "accept-question", "dream-a", "m-old", "m-new", "-r", root, "--apply"]
    )
    assert again.exit_code == 2


# -- no conflicts: byte-identical -------------------------------------------------------


def _quiet_vault(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("Cache", "The build cache lives in the shared volume.", about=["c:a"])
    repository.add("Cache", "the build cache  lives in the shared volume.", about=["c:a"])
    repository.add("Plant", "The office plant needs water on Fridays.", about=["c:b"])
    return repository


def test_no_conflict_means_no_question_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _quiet_vault(tmp_path)
    baseline = json.dumps(_dream(repository.root).as_dict(), sort_keys=True)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the open-question step must not run without a conflict")

    monkeypatch.setattr(dream_run_module, "conflict_questions", forbidden)
    monkeypatch.setattr(dream_run_module, "staged_questions", forbidden)
    result = _dream(repository.root)
    assert json.dumps(result.as_dict(), sort_keys=True) == baseline
    assert "open_questions" not in result.report
    assert ACTION not in result.report["candidates"]


@pytest.mark.skipif(
    not os.environ.get("MEMFW_ARCH_BASE_REF"),
    reason="opt-in (slow): set MEMFW_ARCH_BASE_REF to compare dream output against that ref",
)
def test_dream_without_conflicts_byte_identical_to_base_ref() -> None:
    from tests.harness import byte_identical

    if not byte_identical.git_available():
        pytest.skip("git is not available")
    payloads = [
        {
            "memories": [
                ["m1", "Cache", "The build cache lives in the shared volume.", ["c:a"], ""],
                ["m2", "Cache", "the build cache  lives in the shared volume.", ["c:a"], ""],
                ["m3", "Plant", "The office plant needs water on Fridays.", ["c:b"], ""],
            ]
        },
        {
            "memories": [
                ["m1", "Retry", OLD, [CONCEPT], "alpha"],
                ["m2", "Retry", NEW, [CONCEPT], "beta"],
                ["m3", "Plant", "The office plant needs water on Fridays.", [], "alpha"],
            ],
            "tenant": "alpha",
        },
        {"memories": []},
    ]
    results = byte_identical.compare(
        "tests.harness.probes:dream_run", payloads, old_ref=os.environ["MEMFW_ARCH_BASE_REF"]
    )
    for result in results:
        assert result.identical, f"{result.payload}:\n{result.diff[:4000]}"

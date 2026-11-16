"""``type: open-question``: contract, write gate, resolution, and fsck lint (U1)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from memfw.agents.models import canonical_payload_hash
from memfw.core.epistemic import (
    EXPERIENCE_DISCLAIMER,
    MAX_OPEN_QUESTION_CONFLICTS,
    OPEN_QUESTION_TYPE,
    OpenQuestion,
    validate_epistemic_write,
    validate_proposed_open_question,
    validate_proposed_write,
)
from memfw.core.errors import IntegrityError
from memfw.core.markdown_parse import _OPTIONAL_DEFAULT_FIELDS
from memfw.core.models import MemoryDocument
from memfw.core.repository import Repository
from memfw.storage.markdown import MarkdownStorageMixin
from memfw.storage.operations import lookup_current

HYPOTHESIS_RECORD: dict[str, Any] = {
    "treatment": "a",
    "outcome": "b",
    "identification": {"assumptions": ["x"], "untested": ["y"]},
}
EXPERIENCE: dict[str, Any] = {
    "task": "t",
    "conditions": ["c"],
    "actions": ["a"],
    "review": {"reviewer": "human", "verdict": "accepted"},
}


def _vault(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    return repository


def _raw(repository: Repository, memory_id: str) -> str:
    return (repository.root / "memories" / f"{memory_id}.md").read_text(encoding="utf-8")


def _question(repository: Repository, *conflicts: str, **kwargs: Any) -> Any:
    block = {"question": "Which port does the service use?", "conflicts": list(conflicts)}
    block.update(kwargs.pop("block", {}))
    return repository.add(
        "port question",
        "Two memories disagree on the port.",
        memory_type=OPEN_QUESTION_TYPE,
        open_question=block,
        **kwargs,
    )


def _kinds(repository: Repository) -> list[str]:
    return sorted(
        str(issue["kind"])
        for issue in repository.fsck()
        if str(issue["kind"]).startswith("open_question")
    )


# --- the model -----------------------------------------------------------------


def test_block_validates_lengths_ids_and_dedupes_conflicts() -> None:
    block = OpenQuestion.model_validate(
        {"question": "q?", "conflicts": ["a", "b", "a"], "missing": None}
    )
    assert block.conflicts == ["a", "b"]
    assert block.missing == ""
    assert block.resolved_by is None
    for bad in (
        {"question": ""},
        {"question": "   "},
        {"question": "x" * 301},
        {"question": "q", "missing": "x" * 301},
        {"question": "q", "conflicts": ["../escape"]},
        {"question": "q", "conflicts": "a"},
        {"question": "q", "resolved_by": "not an id"},
        {"question": "q", "extra": 1},
        {"question": "q", "conflicts": [f"m{i}" for i in range(MAX_OPEN_QUESTION_CONFLICTS + 1)]},
    ):
        with pytest.raises(ValueError):
            OpenQuestion.model_validate(bad)
    # 20 distinct ids plus duplicates is still 20.
    ids = [f"m{i}" for i in range(MAX_OPEN_QUESTION_CONFLICTS)]
    assert (
        len(OpenQuestion.model_validate({"question": "q", "conflicts": ids + ids}).conflicts) == 20
    )


# --- parse / render --------------------------------------------------------------


def test_open_question_round_trips_byte_identically(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    left = repository.add("port 8000", "The service listens on 8000.")
    right = repository.add("port 8080", "The service listens on 8080.")
    question = _question(
        repository,
        left.id,
        right.id,
        about=["concept:service-port"],
        block={"missing": "the deployed config file"},
    )
    raw = _raw(repository, question.id)
    assert "open_question:" in raw
    assert "epistemic" not in raw and "causal" not in raw
    parsed = MarkdownStorageMixin.parse(raw, expected_id=question.id)
    assert parsed.type == OPEN_QUESTION_TYPE
    assert parsed.about == ["concept:service-port"]
    assert parsed.open_question is not None
    assert parsed.open_question.conflicts == [left.id, right.id]
    assert parsed.open_question.missing == "the deployed config file"
    assert MarkdownStorageMixin.render(parsed) == raw


LEGACY = """---
id: legacy-note
title: Legacy note
created_at: '2026-11-01T00:00:00Z'
updated_at: '2026-11-01T00:00:00Z'
layer: current
tier: hot
source: manual
provenance: human-authored
anchor: null
tags: []
version: 1
summary: ''
is_stub: false
archive_ref: null
archived_hash: null
strength: 0.5
reference_count: 0
last_access_interval_days: null
manually_marked: false
group_key: null
---

Old body.
"""


def test_existing_files_render_byte_identically(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    plain = repository.add("plain", "a plain record")
    tenanted = repository.add("tenanted", "x", tenant="team-a", tags=["a"])
    hypothesis = repository.add(
        "claim", "a may cause b", causal="hypothesis", causal_record=HYPOTHESIS_RECORD
    )
    experience = repository.add(
        "exp",
        "worked here\n\n" + EXPERIENCE_DISCLAIMER,
        memory_type="experience-point",
        epistemic="experience",
        experience=EXPERIENCE,
    )
    samples = [LEGACY] + [
        _raw(repository, doc.id) for doc in (plain, tenanted, hypothesis, experience)
    ]
    for raw in samples:
        document = MarkdownStorageMixin.parse(raw)
        assert document.open_question is None
        assert MarkdownStorageMixin.render(document) == raw
        assert "open_question" not in raw


def test_post_default_file_keeps_its_default_lines() -> None:
    # A file written after 2026-11-06 spells out the six optional default lines.
    document = MarkdownStorageMixin.parse(LEGACY)
    document._observed_default_fields = _OPTIONAL_DEFAULT_FIELDS
    raw = MarkdownStorageMixin.render(document)
    assert "tenant: ''" in raw and "superseded_by: null" in raw
    assert MarkdownStorageMixin.render(MarkdownStorageMixin.parse(raw)) == raw
    assert "open_question" not in raw


# --- the pure gate ---------------------------------------------------------------


def _values(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "id": "q1",
        "type": OPEN_QUESTION_TYPE,
        "open_question": {"question": "q?"},
        "causal": "none",
        "body": "unknown",
    }
    values.update(overrides)
    return values


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"epistemic": "fact"}, "epistemic"),
        ({"causal": "association"}, "causal"),
        ({"causal": "hypothesis", "causal_record": HYPOTHESIS_RECORD}, "causal"),
        ({"causal_record": HYPOTHESIS_RECORD}, "causal_record"),
        ({"experience": EXPERIENCE}, "experience"),
        ({"open_question": None}, "requires an open_question block"),
        ({"open_question": {"question": "q", "conflicts": ["q1"]}}, "itself"),
        ({"open_question": {"question": "q", "resolved_by": "q1"}}, "itself"),
        ({"open_question": {"question": ""}}, "open_question is invalid"),
    ],
)
def test_gate_rejects_claims_on_an_open_question(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(IntegrityError, match=message):
        validate_epistemic_write(_values(**overrides), actor="human")


def test_block_requires_the_type() -> None:
    with pytest.raises(IntegrityError, match="open_question requires type=open-question"):
        validate_epistemic_write(_values(type="claim"), actor="human")
    validate_epistemic_write(_values(), actor="human")


# --- repository writes -----------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"epistemic": "fact"}, "epistemic"),
        ({"causal": "hypothesis", "causal_record": HYPOTHESIS_RECORD}, "causal"),
        ({"experience": EXPERIENCE}, "experience"),
    ],
)
def test_add_rejects_each_forbidden_field(
    tmp_path: Path, kwargs: dict[str, Any], message: str
) -> None:
    repository = _vault(tmp_path)
    with pytest.raises(IntegrityError, match=message):
        _question(repository, **kwargs)
    assert not list((repository.root / "memories").glob("*.md"))


def test_add_checks_conflicts_against_the_vault(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    same = repository.add("a", "x", tenant="team-a")
    other = repository.add("b", "y", tenant="team-b")
    with pytest.raises(IntegrityError, match="missing memory: ghost"):
        _question(repository, "ghost", tenant="team-a")
    with pytest.raises(IntegrityError, match="another tenant"):
        _question(repository, same.id, other.id, tenant="team-a")
    with pytest.raises(IntegrityError, match="itself"):
        _question(repository, "self-q", memory_id="self-q", tenant="team-a")
    with pytest.raises(IntegrityError, match="requires valid_to"):
        _question(repository, same.id, tenant="team-a", block={"resolved_by": same.id})
    question = _question(repository, same.id, tenant="team-a")
    assert question.open_question is not None
    assert question.open_question.conflicts == [same.id]


def test_only_a_human_creates_an_open_question(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    with pytest.raises(IntegrityError, match="human"):
        _question(repository, actor="mechanism")


def test_update_is_gated(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    plain = repository.add("plain", "x", tenant="team-a")
    question = _question(repository, plain.id, tenant="team-a")
    with pytest.raises(IntegrityError, match="epistemic"):
        repository.update(question.id, epistemic="fact")
    with pytest.raises(IntegrityError, match="missing memory"):
        repository.update(
            question.id, open_question={"question": "q?", "conflicts": [plain.id, "ghost"]}
        )
    with pytest.raises(IntegrityError, match="itself"):
        repository.update(question.id, open_question={"question": "q?", "conflicts": [question.id]})
    # Moving the question to another tenant re-checks every conflict.
    with pytest.raises(IntegrityError, match="another tenant"):
        repository.update(question.id, tenant="team-b")
    with pytest.raises(IntegrityError, match="open_question requires type"):
        repository.update(plain.id, open_question={"question": "q?"})
    with pytest.raises(IntegrityError, match="requires an open_question block"):
        repository.update(plain.id, type=OPEN_QUESTION_TYPE)
    updated = repository.update(question.id, open_question={"question": "Which port, really?"})
    assert updated.open_question is not None
    assert updated.open_question.question == "Which port, really?"


def test_supersede_keeps_the_question(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    plain = repository.add("plain", "x", tenant="team-a")
    question = _question(repository, plain.id, tenant="team-a", about=["concept:port"])
    new = repository.supersede(question.id, "port question v2", "Reworded.")
    assert new.type == OPEN_QUESTION_TYPE
    assert new.tenant == "team-a"
    assert new.about == ["concept:port"]
    assert new.open_question is not None
    assert new.open_question.conflicts == [plain.id]
    assert repository.get(question.id).valid_to is not None
    assert _kinds(repository) == []


# --- resolution ------------------------------------------------------------------


def test_resolution_happy_path_keeps_the_question_file(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    left = repository.add("8000", "port 8000", tenant="team-a")
    right = repository.add("8080", "port 8080", tenant="team-a")
    question = _question(repository, left.id, right.id, tenant="team-a")
    answer = repository.add("port is 8080", "Config says 8080.", tenant="team-a")
    resolved = repository.resolve_open_question(question.id, answer.id)
    assert resolved.open_question is not None
    assert resolved.open_question.resolved_by == answer.id
    assert resolved.valid_to is not None
    assert resolved.version == question.version + 1
    assert (repository.root / "memories" / f"{question.id}.md").exists()
    assert _kinds(repository) == []
    with pytest.raises(IntegrityError, match="already resolved"):
        repository.resolve_open_question(question.id, answer.id)


def test_resolution_rejections(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    question = _question(repository, tenant="team-a")
    other_tenant = repository.add("b", "y", tenant="team-b")
    old = repository.add("old", "x", tenant="team-a")
    repository.supersede(old.id, "new", "x2")
    another_question = _question(repository, tenant="team-a")
    plain = repository.add("plain", "x", tenant="team-a")
    with pytest.raises(IntegrityError, match="human"):
        repository.resolve_open_question(question.id, plain.id, actor="agent:planner")
    with pytest.raises(IntegrityError, match="missing memory"):
        repository.resolve_open_question(question.id, "ghost")
    with pytest.raises(IntegrityError, match="another tenant"):
        repository.resolve_open_question(question.id, other_tenant.id)
    with pytest.raises(IntegrityError, match="superseded"):
        repository.resolve_open_question(question.id, old.id)
    with pytest.raises(IntegrityError, match="itself"):
        repository.resolve_open_question(question.id, question.id)
    with pytest.raises(IntegrityError, match="not another open question"):
        repository.resolve_open_question(question.id, another_question.id)
    with pytest.raises(IntegrityError, match="Not an open question"):
        repository.resolve_open_question(plain.id, question.id)
    # A plain update must also set valid_to when it sets resolved_by.
    with pytest.raises(IntegrityError, match="requires valid_to"):
        repository.update(question.id, open_question={"question": "q?", "resolved_by": plain.id})
    assert repository.get(question.id).open_question.resolved_by is None  # type: ignore[union-attr]


# --- lint (fsck) -----------------------------------------------------------------


def _rewrite(repository: Repository, memory_id: str, old: str, new: str) -> None:
    path = repository.root / "memories" / f"{memory_id}.md"
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")


def test_lint_reports_dangling_and_self_conflicts(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    plain = repository.add("plain", "x")
    question = _question(repository, plain.id)
    _rewrite(
        repository, question.id, f"- {plain.id}\n", f"- {plain.id}\n  - ghost\n  - {question.id}\n"
    )
    assert _kinds(repository) == ["open_question_dangling_conflict", "open_question_self_conflict"]


def test_lint_reports_a_superseded_answer(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    question = _question(repository)
    answer = repository.add("answer", "x")
    repository.resolve_open_question(question.id, answer.id)
    assert _kinds(repository) == []
    # A revised answer is fine: fsck follows the chain to the version in force.
    revised = repository.supersede(answer.id, "answer v2", "x2")
    assert _kinds(repository) == []
    # An answer that simply stopped being valid is not.
    repository.update(revised.id, valid_to=revised.created_at)
    assert _kinds(repository) == ["open_question_superseded_resolution"]


def test_lint_reports_a_resolved_question_without_valid_to(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    question = _question(repository)
    _rewrite(repository, question.id, "resolved_by: null", "resolved_by: ghost")
    assert _kinds(repository) == [
        "open_question_dangling_resolution",
        "open_question_resolved_but_current",
    ]


def test_lint_reports_two_unresolved_versions_of_one_question(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    first = _question(repository, memory_id="qa")
    second = repository.supersede(first.id, "v2", "reworded", memory_id_new="qb")
    assert second.open_question is not None
    assert _kinds(repository) == []
    # A hand edit drops valid_to from the old version but keeps the link: the
    # chain qa -> qb now has two unresolved versions that are both in force.
    path = repository.root / "memories" / "qa.md"
    text = path.read_text(encoding="utf-8")
    start = text.index("valid_to: ")
    end = text.index("\n", start)
    path.write_text(text[:start] + "valid_to: null" + text[end:], encoding="utf-8")
    assert _kinds(repository) == [
        "open_question_multiple_unresolved",
        "open_question_multiple_unresolved",
    ]


def test_lint_reports_block_on_the_wrong_type(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    question = _question(repository)
    _rewrite(repository, question.id, "type: open-question", "type: claim")
    assert _kinds(repository) == ["open_question_wrong_type"]


# --- candidate / proposal paths ----------------------------------------------------


def test_agent_proposal_is_gated(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    question = _question(repository)
    with pytest.raises(IntegrityError, match="epistemic"):
        repository.record_proposal(
            memory_id=question.id,
            operation="update",
            changes={"epistemic": "fact"},
            rationale="it is settled",
            agent_id="planner",
            signature="mock:planner",
        )
    with pytest.raises(IntegrityError, match="causal"):
        repository.record_proposal(
            memory_id=question.id,
            operation="update",
            changes={"causal": "hypothesis", "causal_record": HYPOTHESIS_RECORD},
            rationale="maybe causal",
            agent_id="planner",
            signature="mock:planner",
        )


def test_proposed_write_checks_the_vault_when_given_a_lookup(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    same = repository.add("a", "x", tenant="team-a")
    other = repository.add("b", "y", tenant="team-b")
    question = _question(repository, same.id, tenant="team-a")
    current = repository.get(question.id).model_dump()

    def lookup(ref: str) -> Any:
        return lookup_current(repository, ref)

    # A create-shaped candidate carrying a block without the type is refused.
    with pytest.raises(IntegrityError, match="open_question requires type"):
        validate_proposed_write(None, {"open_question": {"question": "q?"}}, actor="mechanism")
    with pytest.raises(IntegrityError, match="experience"):
        validate_proposed_write(current, {"experience": EXPERIENCE}, actor="mechanism")
    candidate = {
        "type": OPEN_QUESTION_TYPE,
        "tenant": "team-a",
        "open_question": {"question": "q?", "conflicts": [same.id]},
    }
    validate_proposed_write(None, candidate, actor="mechanism")
    validate_proposed_open_question(None, candidate, lookup=lookup, actor="mechanism")
    crossing = {**candidate, "open_question": {"question": "q?", "conflicts": [other.id]}}
    validate_proposed_write(None, crossing, actor="mechanism")  # pure half passes
    ghost = {"open_question": {"question": "q?", "conflicts": [same.id, "ghost"]}}
    # A non-human writer gets one message for "missing" and "other tenant".
    for current_state, change in ((None, crossing), (current, ghost)):
        with pytest.raises(IntegrityError, match="missing or not visible"):
            validate_proposed_open_question(current_state, change, lookup=lookup, actor="mechanism")
    with pytest.raises(IntegrityError, match="another tenant"):
        validate_proposed_open_question(None, crossing, lookup=lookup, actor="human")
    with pytest.raises(IntegrityError, match="missing memory"):
        validate_proposed_open_question(current, ghost, lookup=lookup, actor="human")


def test_a_block_dropped_by_an_older_writer(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    question = _question(repository)
    path = repository.root / "memories" / f"{question.id}.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(text[: text.index("open_question:")] + "---\n\nbody\n", encoding="utf-8")
    assert _kinds(repository) == ["open_question_missing_block"]
    # Ungated writes still work; gated ones need the block back.
    repository.update(question.id, title="renamed")
    with pytest.raises(IntegrityError, match="requires an open_question block"):
        repository.update(question.id, body="new body")
    repository.update(question.id, body="new body", open_question={"question": "q?"})
    assert _kinds(repository) == []


# --- review fixes ------------------------------------------------------------------


def _full_permissions(repository: Repository) -> None:
    repository.config_path.write_text(
        repository.config_path.read_text(encoding="utf-8").replace(
            "permissions: propose_then_confirm", "permissions: full"
        ),
        encoding="utf-8",
    )


AGENT = {"actor": "agent:writer", "agent_id": "writer", "signature": "sig-v1"}


@pytest.mark.parametrize("who", ["mechanism", "agent"])
def test_only_a_human_changes_type_or_resolution(tmp_path: Path, who: str) -> None:
    repository = _vault(tmp_path)
    _full_permissions(repository)
    actor: dict[str, Any] = {"actor": "mechanism"} if who == "mechanism" else dict(AGENT)
    plain = repository.add("plain", "x")
    answer = repository.add("answer", "y")
    question = _question(repository)
    with pytest.raises(IntegrityError, match="change type"):
        repository.update(
            plain.id, type=OPEN_QUESTION_TYPE, open_question={"question": "q?"}, **actor
        )
    with pytest.raises(IntegrityError, match="change type"):
        repository.update(question.id, type="note", open_question=None, **actor)
    with pytest.raises(IntegrityError, match="resolve or reopen"):
        repository.update(
            question.id,
            open_question={"question": "q?", "resolved_by": answer.id},
            valid_to=repository._now(),
            **actor,
        )
    # Other edits of an open question stay open to the same writer.
    edited = repository.update(question.id, title="renamed", **actor)
    assert edited.title == "renamed"
    resolved = repository.resolve_open_question(question.id, answer.id)
    with pytest.raises(IntegrityError, match="resolve or reopen"):
        repository.update(question.id, open_question={"question": "q?"}, valid_to=None, **actor)
    assert resolved.open_question is not None


def test_a_resolve_proposal_applies_after_human_approval(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    question = _question(repository)
    answer = repository.add("answer", "the port is 8080")
    block = {
        "question": "Which port does the service use?",
        "conflicts": [],
        "missing": "",
        "resolved_by": answer.id,
    }
    changes = {"open_question": block, "valid_to": "2026-11-16T00:00:00+00:00"}
    proposal = repository.record_proposal(
        memory_id=question.id,
        operation="update",
        changes=changes,
        rationale="the config file settles it",
        agent_id="planner",
        signature="mock:planner",
    )
    result = repository.decide_proposal(
        proposal.event_id,
        payload={"memory_id": question.id, "operation": "update", "changes": changes},
    )
    assert result is not None
    assert result.open_question is not None
    assert result.open_question.resolved_by == answer.id
    assert result.valid_to is not None
    # valid_to alone is not proposable.
    with pytest.raises(IntegrityError, match="valid_to only to resolve"):
        repository.record_proposal(
            memory_id=answer.id,
            operation="update",
            changes={"valid_to": "2026-11-16T00:00:00+00:00"},
            rationale="expire it",
            agent_id="planner",
            signature="mock:planner",
        )


def test_fsck_skips_conflicts_of_historical_versions(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    left = repository.add("left", "x")
    right = repository.add("right", "y")
    question = _question(repository, left.id, right.id)
    answer = repository.add("answer", "z")
    repository.resolve_open_question(question.id, answer.id)
    repository.delete(left.id)
    assert _kinds(repository) == []


def test_supersede_drops_conflicts_that_no_longer_resolve(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    left = repository.add("left", "x")
    right = repository.add("right", "y")
    question = _question(repository, left.id, right.id)
    repository.delete(left.id)
    assert _kinds(repository) == ["open_question_dangling_conflict"]
    new = repository.supersede(question.id, "v2", "reworded")
    assert new.open_question is not None
    assert new.open_question.conflicts == [right.id]
    event = next(e for e in reversed(repository.events()) if e.kind.value == "supersede")
    assert event.details["dropped_conflicts"] == [left.id]
    assert _kinds(repository) == []


def test_unrelated_legacy_open_question_key_is_ignored(tmp_path: Path) -> None:
    raw = LEGACY.replace("group_key: null\n", "group_key: null\nopen_question: maybe later\n")
    document = MarkdownStorageMixin.parse(raw, expected_id="legacy-note")
    assert document.open_question is None
    assert document.ignored_keys == ("open_question",)
    repository = _vault(tmp_path)
    path = repository.root / "memories" / "legacy-note.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")
    assert [doc.id for doc in repository.list()] == ["legacy-note"]
    assert repository.get("legacy-note").title == "Legacy note"
    assert _kinds(repository) == ["open_question_invalid_block"]
    assert path.read_text(encoding="utf-8") == raw
    # On an open question a malformed block is still an error.
    bad = raw.replace("group_key: null\n", "group_key: null\ntype: open-question\n")
    with pytest.raises(IntegrityError):
        MarkdownStorageMixin.parse(bad, expected_id="legacy-note")


def test_snapshot_hash_of_a_pre_u1_memory_is_unchanged() -> None:
    stamp = datetime(2026, 11, 1, tzinfo=UTC)
    document = MemoryDocument(
        id="pinned",
        title="Pinned",
        body="body",
        tenant="team-a",
        tags=["x"],
        created_at=stamp,
        updated_at=stamp,
    )
    # Computed with origin/main df4958b (before U1) over ``model_dump(mode="json")``.
    assert canonical_payload_hash(document.model_dump(mode="json")) == (
        "c49a12552b694feb69e20f1fb34f6aaca2d7bc2e9f8e713dd8378663af0cb4c2"
    )
    question = document.model_copy(
        update={"type": OPEN_QUESTION_TYPE, "open_question": OpenQuestion(question="q?")}
    )
    assert "open_question" not in document.model_dump()
    assert "open_question" in question.model_dump(mode="json")


def test_okf_export_marks_open_questions(tmp_path: Path) -> None:
    import yaml

    from memfw.okf import export_okf

    repository = _vault(tmp_path)
    plain = repository.add("plain", "x")
    question = _question(repository, plain.id)
    bundle = tmp_path / "okf"
    assert export_okf(repository.root, bundle).valid
    front = yaml.safe_load(
        (bundle / f"{question.id}.md").read_text(encoding="utf-8").split("---\n")[1]
    )
    assert front["memfw"]["type"] == OPEN_QUESTION_TYPE
    assert front["memfw"]["open_question"]["conflicts"] == [plain.id]
    plain_text = (bundle / f"{plain.id}.md").read_text(encoding="utf-8")
    assert "open_question" not in plain_text


def test_rollback_restores_an_absent_block(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    plain = repository.add("plain", "x")
    converted = repository.update(
        plain.id, type=OPEN_QUESTION_TYPE, open_question={"question": "q?"}
    )
    assert converted.open_question is not None
    restored = repository.rollback(repository.events()[-1].event_id)
    assert restored.type is None
    assert restored.open_question is None


def _propose(repository: Repository, memory_id: str, changes: dict[str, Any]) -> Any:
    return repository.record_proposal(
        memory_id=memory_id,
        operation="update",
        changes=changes,
        rationale="r",
        agent_id="planner",
        signature="mock:planner",
    )


def test_valid_to_is_proposable_only_as_a_resolution(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    plain = repository.add("plain", "x")
    question = _question(repository)
    stamp = "2026-11-16T00:00:00+00:00"
    for target, changes in (
        (plain.id, {"open_question": None, "valid_to": stamp}),
        (question.id, {"open_question": {"question": "q?"}, "valid_to": stamp}),
        (
            question.id,
            {"open_question": {"question": "q?", "resolved_by": None}, "valid_to": stamp},
        ),
    ):
        with pytest.raises(IntegrityError, match="valid_to only to resolve"):
            _propose(repository, target, changes)
    assert repository.get(plain.id).valid_to is None


@pytest.mark.parametrize("who", ["mechanism", "agent"])
def test_valid_to_and_closed_conflicts_need_a_human(tmp_path: Path, who: str) -> None:
    repository = _vault(tmp_path)
    _full_permissions(repository)
    actor: dict[str, Any] = {"actor": "mechanism"} if who == "mechanism" else dict(AGENT)
    left = repository.add("left", "x")
    right = repository.add("right", "y")
    answer = repository.add("answer", "z")
    question = _question(repository, left.id)
    with pytest.raises(IntegrityError, match="change valid_to"):
        repository.update(question.id, valid_to=repository._now(), **actor)
    repository.resolve_open_question(question.id, answer.id)
    block = {"question": "Which port does the service use?", "resolved_by": answer.id}
    with pytest.raises(IntegrityError, match="change the conflicts"):
        repository.update(question.id, open_question={**block, "conflicts": [right.id]}, **actor)
    with pytest.raises(IntegrityError, match="change valid_to"):
        repository.update(question.id, valid_to=None, **actor)
    # Unchanged conflicts and valid_to are still fine for the same writer.
    edited = repository.update(
        question.id, open_question={**block, "conflicts": [left.id], "missing": "m"}, **actor
    )
    assert edited.open_question is not None and edited.open_question.missing == "m"


def test_lint_reports_a_deleted_answer_head_as_dangling(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    question = _question(repository)
    answer = repository.add("answer", "x")
    repository.resolve_open_question(question.id, answer.id)
    revised = repository.supersede(answer.id, "answer v2", "x2")
    # Deleting the revised answer clears the pointer to it; the chain still ends
    # at a deleted memory, and stays reported that way on every fsck.
    repository.delete(revised.id)
    for _ in range(2):
        assert _kinds(repository) == ["open_question_dangling_resolution"]


def test_valid_block_on_another_type_is_ignored(tmp_path: Path) -> None:
    block = "open_question:\n  question: q?\n  conflicts: []\n  missing: ''\n  resolved_by: null\n"
    raw = LEGACY.replace("group_key: null\n", "group_key: null\n" + block)
    document = MarkdownStorageMixin.parse(raw, expected_id="legacy-note")
    assert document.open_question is None
    assert document.ignored_keys == ("open_question",)
    assert document.stray_open_question == "block"
    repository = _vault(tmp_path)
    path = repository.root / "memories" / "legacy-note.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")
    assert _kinds(repository) == ["open_question_wrong_type"]
    # The stray key does not block gated edits.
    updated = repository.update("legacy-note", body="new body", epistemic="fact")
    assert updated.epistemic == "fact"


def test_lint_accepts_an_answer_restored_after_its_successor_was_deleted(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    question = _question(repository)
    answer = repository.add("answer", "x")
    repository.resolve_open_question(question.id, answer.id)
    revised = repository.supersede(answer.id, "answer v2", "x2")
    repository.delete(revised.id)
    assert _kinds(repository) == ["open_question_dangling_resolution"]
    repository.update(answer.id, valid_to=None, superseded_by=None)
    assert _kinds(repository) == []

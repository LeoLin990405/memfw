"""Epistemic type and causal grade: fields, gate, retrieval labels, OKF export."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from memfw.core.epistemic import (
    EXPERIENCE_DISCLAIMER,
    evidence_source_class,
    validate_epistemic_write,
)
from memfw.core.errors import IntegrityError
from memfw.core.models import MemoryDocument
from memfw.core.repository import Repository
from memfw.mcp.server import Gateway
from memfw.okf import export_okf
from memfw.storage.markdown import MarkdownStorageMixin

VERIFIED_RECORD: dict[str, Any] = {
    "treatment": "assignment A vs B",
    "outcome": "synthetic outcome",
    "confounders": {"known": [], "suspected": [], "unobserved": []},
    "selection": "randomised assignment",
    "temporal_order": "assignment recorded before outcome",
    "estimand": "ATE of assignment on the outcome",
    "identification": {
        "assumptions": ["randomised assignment", "stable unit treatment value"],
        "untested": [],
    },
    "robustness": [
        {"check": "covariate balance", "result": "balanced", "failed": False},
    ],
    "proposed_by": "test",
    "alternatives": ["no effect"],
}


def _vault(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    return repository


# --- round trip and backward compatibility -----------------------------------


def test_fields_round_trip_and_defaults_stay_off_the_file(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    plain = repository.add("plain", "a plain record")
    raw_plain = (repository.root / "memories" / f"{plain.id}.md").read_text(encoding="utf-8")
    assert "epistemic" not in raw_plain
    assert "causal" not in raw_plain
    assert "causal_record" not in raw_plain

    hypothesis = repository.add(
        "hypothesis",
        "coffee correlates with the risk score",
        epistemic="inference",
        causal="hypothesis",
        causal_record={
            "treatment": "coffee",
            "outcome": "risk score",
            "identification": {"assumptions": ["no unmeasured confounding"], "untested": ["age"]},
        },
        limitations=["single cohort"],
    )
    reloaded = repository.get(hypothesis.id)
    assert reloaded.epistemic == "inference"
    assert reloaded.causal == "hypothesis"
    assert reloaded.causal_record is not None
    assert reloaded.causal_record.identification.untested == ["age"]
    assert reloaded.limitations == ["single cohort"]
    raw = (repository.root / "memories" / f"{hypothesis.id}.md").read_text(encoding="utf-8")
    assert "epistemic: inference" in raw
    assert "causal: hypothesis" in raw
    assert "limitations:" in raw


LEGACY_MARKDOWN = """---
id: legacy-note
title: Legacy note
created_at: '2025-01-02T03:04:05Z'
updated_at: '2025-01-02T03:04:05Z'
layer: current
tier: hot
type: note
tenant: ''
related: []
source: chat
provenance: human-authored
anchor: null
tags:
- type:note
version: 2
summary: old summary
is_stub: false
archive_ref: null
archived_hash: null
strength: 0.5
reference_count: 0
last_access_interval_days: null
manually_marked: false
group_key: null
valid_from: null
valid_to: null
superseded_by: null
recorded_at: '2025-01-02T03:04:05Z'
status: confirmed
evidence:
- session:one
---

Old body text.
"""


def test_legacy_markdown_parses_and_renders_byte_identically() -> None:
    document = MarkdownStorageMixin.parse(LEGACY_MARKDOWN, expected_id="legacy-note")
    assert document.epistemic is None
    assert document.causal == "none"
    assert document.causal_record is None
    assert document.limitations == []
    assert document.experience is None
    assert MarkdownStorageMixin.render(document) == LEGACY_MARKDOWN


def test_legacy_file_on_disk_is_not_rewritten_by_a_read(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    repository = Repository(root)
    repository.init()
    (root / "memories").mkdir(parents=True, exist_ok=True)
    path = root / "memories" / "legacy-note.md"
    path.write_text(LEGACY_MARKDOWN, encoding="utf-8")
    documents = repository.list()
    assert [item.id for item in documents] == ["legacy-note"]
    assert path.read_text(encoding="utf-8") == LEGACY_MARKDOWN
    # A metadata-only update keeps the file free of the new keys.
    repository.update("legacy-note", title="Legacy note v2")
    updated = path.read_text(encoding="utf-8")
    assert "epistemic" not in updated
    assert "causal" not in updated


# --- the write gate ----------------------------------------------------------


def _verified_values(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "type": "claim",
        "epistemic": "inference",
        "causal": "verified",
        "causal_record": VERIFIED_RECORD,
        "limitations": [],
        "evidence": ["data:synthetic-experiment"],
        "source": "synthetic",
        "provenance": "human-authored",
        "body": "body",
    }
    values.update(overrides)
    return values


def test_verified_with_complete_record_passes() -> None:
    validate_epistemic_write(_verified_values(), actor="human")


@pytest.mark.parametrize(
    ("overrides", "missing"),
    [
        ({"causal_record": None}, "causal_record"),
        (
            {"causal_record": {**VERIFIED_RECORD, "estimand": ""}},
            "causal_record.estimand",
        ),
        (
            {"causal_record": {**VERIFIED_RECORD, "identification": {"assumptions": []}}},
            "causal_record.identification",
        ),
        (
            {"causal_record": {**VERIFIED_RECORD, "robustness": []}, "limitations": []},
            "causal_record.robustness or limitations",
        ),
        ({"evidence": []}, "evidence"),
    ],
)
def test_verified_names_every_missing_item(overrides: dict[str, Any], missing: str) -> None:
    with pytest.raises(IntegrityError) as caught:
        validate_epistemic_write(_verified_values(**overrides), actor="human")
    assert missing in str(caught.value)


def test_limitations_can_replace_robustness() -> None:
    values = _verified_values(
        causal_record={**VERIFIED_RECORD, "robustness": []},
        limitations=["the sample is a single synthetic batch"],
    )
    validate_epistemic_write(values, actor="human")


def test_untested_identification_blocks_verified() -> None:
    values = _verified_values(
        causal_record={
            **VERIFIED_RECORD,
            "identification": {
                "assumptions": ["no unmeasured confounding"],
                "untested": ["confounder adjustment"],
            },
        }
    )
    with pytest.raises(IntegrityError, match="tested identification assumptions"):
        validate_epistemic_write(values, actor="human")


def test_failed_robustness_check_requires_demotion() -> None:
    values = _verified_values(
        causal_record={
            **VERIFIED_RECORD,
            "robustness": [{"check": "placebo", "result": "not null", "failed": True}],
        }
    )
    with pytest.raises(IntegrityError, match="demotion to causal=hypothesis"):
        validate_epistemic_write(values, actor="human")
    demoted = {
        **values,
        "causal": "hypothesis",
        "causal_record": values["causal_record"],
    }
    validate_epistemic_write(demoted, actor="human")


@pytest.mark.parametrize(
    "evidence",
    [
        ["chat:thread-1"],
        ["session:one", "conversation:two"],
        ["llm:explanation-1"],
        ["reflection:note"],
        ["chat:thread-1", "llm:summary"],
        ["bare-memory-id"],
        [],
    ],
)
def test_narrative_or_derived_evidence_cannot_reach_verified(evidence: list[str]) -> None:
    with pytest.raises(IntegrityError):
        validate_epistemic_write(_verified_values(evidence=evidence), actor="human")


@pytest.mark.parametrize(
    "evidence",
    [
        ["data:cohort"],
        ["design:ab-assignment"],
        ["test:tests/test_x.py"],
        ["pr:owner/repo#7"],
        ["https://example.test/analysis"],
        ["chat:thread-1", "data:cohort"],
    ],
)
def test_raw_evidence_allows_verified(evidence: list[str]) -> None:
    validate_epistemic_write(_verified_values(evidence=evidence), actor="human")


def test_llm_provenance_with_only_generated_evidence_is_refused() -> None:
    # The narrative-only rule already refuses generated/chat pointers; an
    # LLM-derived memory additionally has to point at raw data.
    with pytest.raises(IntegrityError):
        validate_epistemic_write(
            _verified_values(provenance="llm", evidence=["llm:analysis"]), actor="human"
        )
    # A raw pointer is enough, whichever provenance wrote the prose.
    validate_epistemic_write(
        _verified_values(provenance="llm", evidence=["data:cohort"]), actor="human"
    )


def test_chat_source_with_only_chat_evidence_is_refused() -> None:
    with pytest.raises(IntegrityError):
        validate_epistemic_write(
            _verified_values(source="chat", evidence=["chat:thread"]), actor="human"
        )
    validate_epistemic_write(
        _verified_values(source="chat", evidence=["data:cohort"]), actor="human"
    )


def test_agents_and_dreams_stop_at_hypothesis() -> None:
    with pytest.raises(IntegrityError, match="HUMAN_DECISION"):
        validate_epistemic_write(_verified_values(), actor="agent:planner")
    validate_epistemic_write(_verified_values(), actor="agent:planner", human_decision=True)
    validate_epistemic_write(
        _verified_values(causal="hypothesis"),
        actor="agent:planner",
    )


def test_hypothesis_requires_a_record() -> None:
    with pytest.raises(IntegrityError, match="requires a causal_record"):
        validate_epistemic_write(
            _verified_values(causal="hypothesis", causal_record=None), actor="human"
        )


def test_causal_record_without_causal_level_is_refused() -> None:
    with pytest.raises(IntegrityError, match="causal_record requires"):
        validate_epistemic_write(_verified_values(causal="none"), actor="human")


def test_unknown_levels_are_refused() -> None:
    with pytest.raises(IntegrityError, match="causal must be one of"):
        validate_epistemic_write(_verified_values(causal="proven"), actor="human")
    with pytest.raises(IntegrityError, match="epistemic must be one of"):
        validate_epistemic_write(_verified_values(epistemic="rumour"), actor="human")


def test_evidence_source_classification() -> None:
    assert evidence_source_class("chat:abc") == "chat"
    assert evidence_source_class("reflection:x") == "generated"
    assert evidence_source_class("data:x") == "raw"
    assert evidence_source_class("https://example.test/x") == "raw"
    assert evidence_source_class("memory-abc") == "other"


# --- repository write paths --------------------------------------------------


def test_repository_add_refuses_incomplete_verified(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    with pytest.raises(IntegrityError, match="causal=verified requires"):
        repository.add(
            "claim",
            "because X, Y",
            causal="verified",
            evidence=["chat:thread"],
            causal_record={**VERIFIED_RECORD, "robustness": []},
        )
    assert repository.list() == []


def test_repository_update_can_demote_a_failed_claim(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    document = repository.add(
        "claim",
        "the assignment raises the outcome",
        epistemic="inference",
        causal="verified",
        causal_record=VERIFIED_RECORD,
        evidence=["data:experiment"],
    )
    rejected = {
        **VERIFIED_RECORD,
        "robustness": [{"check": "placebo", "result": "not null", "failed": True}],
    }
    with pytest.raises(IntegrityError, match="demotion"):
        repository.update(document.id, causal_record=rejected)
    updated = repository.update(document.id, causal="hypothesis", causal_record=rejected)
    assert updated.causal == "hypothesis"
    assert repository.get(document.id).causal == "hypothesis"


def test_repository_rejects_agent_verified_proposal(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    target = repository.add("claim", "coffee raises risk")
    with pytest.raises(IntegrityError, match="HUMAN_DECISION"):
        repository.record_proposal(
            memory_id=target.id,
            operation="update",
            changes={
                "causal": "verified",
                "causal_record": VERIFIED_RECORD,
                "evidence": ["data:cohort"],
            },
            rationale="the analysis looks complete",
            agent_id="planner",
            signature="mock:planner",
        )


# --- experience points are never causal -------------------------------------


def test_experience_point_requires_its_contract(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    body = EXPERIENCE_DISCLAIMER + "\n\nran the checklist"
    record = {
        "task": "fix lint",
        "conditions": ["repo clean"],
        "actions": ["run ruff"],
        "review": {"reviewer": "reviewer", "verdict": "accepted"},
    }
    document = repository.add(
        "experience",
        body,
        memory_type="experience-point",
        epistemic="experience",
        experience=record,
    )
    assert document.type == "experience-point"
    assert document.causal == "none"
    assert repository.get(document.id).experience is not None

    with pytest.raises(IntegrityError, match="causal=none"):
        repository.add(
            "bad experience",
            body,
            memory_type="experience-point",
            epistemic="experience",
            causal="association",
            experience=record,
        )
    with pytest.raises(IntegrityError, match="disclaimer"):
        repository.add(
            "no disclaimer",
            "ran the checklist",
            memory_type="experience-point",
            epistemic="experience",
            experience=record,
        )
    with pytest.raises(IntegrityError, match="reviewer"):
        repository.add(
            "no reviewer",
            body,
            memory_type="experience-point",
            epistemic="experience",
            experience={"task": "x", "review": {"verdict": "accepted"}},
        )


# --- retrieval output --------------------------------------------------------


def _labelled_vault(tmp_path: Path) -> tuple[Repository, str, str]:
    repository = _vault(tmp_path)
    hypothesis = repository.add(
        "coffee and risk score",
        "The synthetic cohort links coffee and the risk score; smoking confounds it.",
        epistemic="inference",
        causal="hypothesis",
        causal_record={
            "treatment": "coffee",
            "outcome": "risk score",
            "identification": {"assumptions": ["no unmeasured confounding"], "untested": ["age"]},
        },
        evidence=["data:synthetic-cohort"],
    )
    verified = repository.add(
        "randomised assignment and outcome",
        "A synthetic randomised experiment raises the outcome.",
        epistemic="inference",
        causal="verified",
        causal_record=VERIFIED_RECORD,
        evidence=["data:synthetic-experiment"],
    )
    return repository, hypothesis.id, verified.id


def test_mcp_search_rows_carry_labels(tmp_path: Path) -> None:
    repository, hypothesis_id, verified_id = _labelled_vault(tmp_path)
    gateway = Gateway(repository.root)
    found = gateway.search("synthetic", limit=10)
    by_id = {row["id"]: row for row in found["results"]}
    assert by_id[hypothesis_id]["causal"] == "hypothesis"
    assert by_id[hypothesis_id]["epistemic"] == "inference"
    assert by_id[verified_id]["causal"] == "verified"

    document = gateway.get(hypothesis_id)
    assert document["causal"] == "hypothesis"
    assert document["causal_record"]["identification"]["untested"] == ["age"]


def test_mcp_index_backed_rows_carry_labels(tmp_path: Path) -> None:
    repository, hypothesis_id, _verified_id = _labelled_vault(tmp_path)
    gateway = Gateway(repository.root, tenant="")
    found = gateway.search("synthetic", limit=10, mode="multihop")
    rows = {row["id"]: row for row in found["results"]}
    if hypothesis_id not in rows:  # pragma: no cover - index-dependent
        pytest.skip("multihop did not return the fixture row")
    assert rows[hypothesis_id]["causal"] == "hypothesis"


def test_retriever_rows_carry_labels(tmp_path: Path) -> None:
    repository, hypothesis_id, _verified_id = _labelled_vault(tmp_path)
    from memfw.memory_hybrid import MemoryHybridRetriever

    result = MemoryHybridRetriever(repository).search("synthetic", limit=10, mode="fts")
    rows = {row["id"]: row for row in result["results"]}
    assert rows[hypothesis_id]["causal"] == "hypothesis"
    assert rows[hypothesis_id]["epistemic"] == "inference"


# --- OKF export --------------------------------------------------------------


def test_okf_export_carries_epistemic_fields(tmp_path: Path) -> None:
    repository, hypothesis_id, _ = _labelled_vault(tmp_path)
    plain = repository.add("plain note", "an ordinary note")
    bundle = tmp_path / "okf"
    result = export_okf(repository.root, bundle)
    assert result.valid
    exported = bundle / f"{hypothesis_id}.md"
    text = exported.read_text(encoding="utf-8")
    assert "epistemic: inference" in text
    assert "causal: hypothesis" in text
    assert "causal_record:" in text
    plain_text = (bundle / f"{plain.id}.md").read_text(encoding="utf-8")
    assert "epistemic" not in plain_text
    assert "causal" not in plain_text


def test_okf_memfw_envelope_keeps_concept_schema(tmp_path: Path) -> None:
    repository, hypothesis_id, _ = _labelled_vault(tmp_path)
    bundle = tmp_path / "okf"
    export_okf(repository.root, bundle)
    import yaml

    text = (bundle / f"{hypothesis_id}.md").read_text(encoding="utf-8")
    front = yaml.safe_load(text.split("---\n")[1])
    assert front["type"] == "Memory"
    assert front["memfw"]["epistemic"] == "inference"
    assert front["memfw"]["causal"] == "hypothesis"
    assert "causal_record" in front["memfw"]


def test_okf_manifest_stays_valid(tmp_path: Path) -> None:
    repository, _, _ = _labelled_vault(tmp_path)
    bundle = tmp_path / "okf"
    result = export_okf(repository.root, bundle)
    manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
    assert manifest["valid"] is True
    assert manifest["concept_count"] == len(repository.list())


def test_memory_document_defaults_match_old_documents() -> None:
    document = MarkdownStorageMixin.parse(LEGACY_MARKDOWN, expected_id="legacy-note")
    dumped: dict[str, Any] = document.model_dump()
    assert dumped["causal"] == "none"
    assert dumped["epistemic"] is None
    assert dumped["causal_record"] is None
    assert MemoryDocument.model_validate(dumped).id == "legacy-note"


# --- CLI write paths ---------------------------------------------------------


def _cli() -> Any:
    from typer.testing import CliRunner

    return CliRunner()


def test_cli_add_accepts_epistemic_and_causal(tmp_path: Path) -> None:
    from memfw.cli import app

    repository = _vault(tmp_path)
    record = json.dumps(
        {
            "treatment": "checklist",
            "outcome": "rollbacks",
            "identification": {"assumptions": ["no other change"], "untested": ["seasonality"]},
        }
    )
    result = _cli().invoke(
        app,
        [
            "add",
            "--title",
            "checklist and rollbacks",
            "--body",
            "the checklist was followed by fewer rollbacks",
            "--type",
            "claim",
            "--epistemic",
            "inference",
            "--causal",
            "hypothesis",
            "--causal-record",
            record,
            "--root",
            str(repository.root),
        ],
    )
    assert result.exit_code == 0, result.output
    document = repository.get(result.stdout.strip())
    assert document.causal == "hypothesis"
    assert document.causal_record is not None


def test_cli_add_dry_run_refuses_incomplete_verified(tmp_path: Path) -> None:
    from memfw.cli import app

    repository = _vault(tmp_path)
    result = _cli().invoke(
        app,
        [
            "add",
            "--title",
            "claim",
            "--body",
            "because X, Y",
            "--causal",
            "verified",
            "--root",
            str(repository.root),
            "--dry-run",
        ],
    )
    assert result.exit_code == 2
    assert "causal=verified requires" in result.output or "causal=verified requires" in (
        result.stderr or ""
    )
    assert repository.list() == []


def test_cli_experience_flag_promotes_an_experience_point(tmp_path: Path) -> None:
    from memfw.cli import app

    repository = _vault(tmp_path)
    record = json.dumps(
        {
            "task": "run the checklist",
            "conditions": ["fixture vault"],
            "actions": ["run the checklist"],
            "review": {"reviewer": "reviewer", "verdict": "accepted"},
        }
    )
    result = _cli().invoke(
        app,
        [
            "add",
            "--title",
            "experience",
            "--body",
            EXPERIENCE_DISCLAIMER + "\n\nran it",
            "--type",
            "experience-point",
            "--epistemic",
            "experience",
            "--experience",
            record,
            "--root",
            str(repository.root),
        ],
    )
    assert result.exit_code == 0, result.output
    document = repository.get(result.stdout.strip())
    assert document.type == "experience-point"
    assert document.causal == "none"
    assert document.experience is not None

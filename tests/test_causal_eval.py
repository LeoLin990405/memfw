"""The pseudo-causality experiment: write-gate and retrieval-layer numbers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from memfw.cli import app
from memfw.memory_eval import (
    CAUSAL_FIXTURE_DIR,
    compose_answer,
    load_causal_materials,
    run_causal_promotion_eval,
)

runner = CliRunner()


def _report() -> Any:
    return run_causal_promotion_eval(CAUSAL_FIXTURE_DIR)


EXPECTED_NAMES = {
    "identified_analysis",
    "identified_replay",
    "confounded_observational",
    "simpson_paradox",
    "narrative_only",
    "chat_link_only",
    "empty_pointer",
    "blank_assumption",
}
EXPECTED_GROUPS = {"identified": 2, "confounded": 2, "narrative": 2, "malformed": 2}


def test_fixtures_cover_grouped_material_classes() -> None:
    materials = load_causal_materials(CAUSAL_FIXTURE_DIR)
    assert {material.name for material in materials} == EXPECTED_NAMES
    counts: dict[str, int] = {}
    for material in materials:
        assert material.group, f"{material.name} has no group"
        counts[material.group] = counts.get(material.group, 0) + 1
    assert counts == EXPECTED_GROUPS
    assert sum(1 for material in materials if material.expected_verified) == 2
    assert all(
        material.expected_block
        for material in materials
        if not material.expected_verified
    )


def test_write_layer_has_no_false_accepts_or_false_rejects() -> None:
    report = _report()
    payload = report.as_dict()
    assert payload["materials"] == 8
    assert payload["false_accepts"] == 0
    assert payload["false_rejects"] == 0
    assert payload["spurious_promotions"] == 0
    write = payload["write_layer"]
    assert write["attempts"] == 8
    assert write["accepted"] == 2
    assert write["rejected"] == 6
    by_name = {item["name"]: item for item in write["materials"]}
    for accepted_name in ("identified_analysis", "identified_replay"):
        assert by_name[accepted_name]["accepted"] is True
        assert by_name[accepted_name]["resulting_causal"] == "verified"
    assert by_name["confounded_observational"]["accepted"] is False
    assert "untested" in str(by_name["confounded_observational"]["error"])
    assert by_name["simpson_paradox"]["accepted"] is False
    assert "untested" in str(by_name["simpson_paradox"]["error"])
    # The narrative and malformed groups must fail on the rule they were built
    # for, not early on a structural check.
    assert by_name["narrative_only"]["accepted"] is False
    assert "chat text" in str(by_name["narrative_only"]["error"])
    assert by_name["chat_link_only"]["accepted"] is False
    assert "replayable raw" in str(by_name["chat_link_only"]["error"])
    assert by_name["empty_pointer"]["accepted"] is False
    assert "evidence" in str(by_name["empty_pointer"]["error"])
    assert by_name["blank_assumption"]["accepted"] is False
    assert "identification" in str(by_name["blank_assumption"]["error"])


def test_retrieval_layer_labels_every_hit_and_never_concludes_a_hypothesis() -> None:
    payload = _report().as_dict()
    retrieval = payload["retrieval_layer"]
    assert retrieval["queries"] == 8
    assert retrieval["missing_labels"] == 0
    assert retrieval["hypothesis_as_conclusion"] == 0
    for item in retrieval["materials"]:
        assert item["mismatched_labels"] == []
        assert item["expected_label"] in {"hypothesis", "association", "verified"}


def test_answer_template_hedges_hypotheses() -> None:
    rows = [
        {"id": "m-verified", "causal": "verified"},
        {"id": "m-hypothesis", "causal": "hypothesis"},
        {"id": "m-association", "causal": "association"},
    ]
    answer, hypothesis_as_conclusion = compose_answer("does X cause Y", rows)
    assert "Verified causal claims" in answer
    assert "m-verified" in answer.split("Not causal conclusions")[0]
    assert "Not causal conclusions" in answer
    assert "m-hypothesis" not in answer.split("Not causal conclusions")[0]
    assert hypothesis_as_conclusion == 0


def test_cli_prints_the_three_numbers(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["eval", "causal", "--json", "--workdir", str(tmp_path / "scratch")]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["spurious_promotions"] == 0
    assert payload["false_accepts"] == 0
    assert payload["false_rejects"] == 0
    assert (tmp_path / "scratch").is_dir()


def test_cli_human_output_names_the_layers() -> None:
    result = runner.invoke(app, ["eval", "causal"])
    assert result.exit_code == 0, result.output
    assert "materials: 8" in result.stdout
    assert "spurious_promotions: 0" in result.stdout
    assert "false_accepts: 0" in result.stdout
    assert "false_rejects: 0" in result.stdout
    assert "hypothesis_as_conclusion: 0" in result.stdout

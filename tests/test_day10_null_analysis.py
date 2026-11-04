from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from day10_null_analysis import CATEGORIES, POLICIES, _power, _signflip_p, build_report  # noqa: E402  # noqa: E501


def test_exact_signflip_identity_has_p_one_and_unidentifiable_mde() -> None:
    rows = [{"sample_index": i, "evidence_total": 2, "hits": {"a": 1, "b": 1}} for i in range(10)]
    assert _signflip_p(rows, "a", "b") == 1.0
    power = _power(rows, "a", "b", 0.05)
    assert power["identical_question_outcomes"] is True
    assert power["mde80_pp_cluster_se_bonferroni_10"] is None


def test_exact_signflip_detects_ten_same_direction_clusters() -> None:
    rows = [{"sample_index": i, "evidence_total": 1, "hits": {"a": 1, "b": 0}} for i in range(10)]
    assert _signflip_p(rows, "a", "b") == 2 / 1024
    assert _signflip_p(rows, "b", "a") == 2 / 1024


def _mini_result(sha: str, ratios: tuple[str, str]) -> dict[str, object]:
    samples = []
    for sample_index in range(2):
        sample: dict[str, Any] = {
            "sample_index": sample_index,
            "sample_id": f"c{sample_index}",
            "turn_count": 2,
            "adapter_counts": {
                "category_evidence_counts": {
                    str(cat): {"valid_evidence_questions": 1} for cat in CATEGORIES
                }
            },
            "scopes": {},
        }
        for condition in ("keyness_only", "keyness_plus_shared_access"):
            profiles: dict[str, dict[str, Any]] = {}
            for ratio in ratios:
                per_question = [
                    {
                        "question_index": cat - 1,
                        "category": cat,
                        "evidence_total": 1,
                        "hits": {
                            policy: int((cat + sample_index + j) % 3 != 0)
                            for j, policy in enumerate(POLICIES)
                        },
                    }
                    for cat in CATEGORIES
                ]
                systems: dict[str, Any] = {}
                for j, policy in enumerate(POLICIES):
                    ids = [f"id{(sample_index + j) % 3}"]
                    systems[policy] = {
                        "retained_ids": ids,
                        "retained_count": len(ids),
                        "retained_body_utf8_bytes": 10 + j,
                    }
                profiles[ratio] = {
                    "capacity_bytes": 20,
                    "initial_turns": 2,
                    "systems": systems,
                    "per_question": per_question,
                }
            sample["scopes"][condition] = {
                "profiles": profiles,
                "before_budget": {
                    "systems": {"memfw_bm25": {"evidence_retrieved": 1, "evidence_total": 5}}
                },
            }
            for profile in profiles.values():
                profile["systems"]["oracle_question_conditioned"] = {"evidence_retrieved": 2}
        samples.append(sample)
    bootstrap_scopes = {f"category_{cat}": {"evidence_total": 2} for cat in CATEGORIES}
    return {
        "dataset": {"sha256": sha},
        "seed": 20261018,
        "query_context_budget_tokens": 128,
        "strategies": list(POLICIES),
        "samples": samples,
        "bootstrap": {"keyness_only/10%": {"scopes": bootstrap_scopes}},
    }


def test_report_renders_all_categories_and_condition_retention(tmp_path: Path) -> None:
    data = tmp_path / "public.json"
    data.write_text("[]")
    sha = hashlib.sha256(data.read_bytes()).hexdigest()
    history_path = tmp_path / "history.json"
    pressure_path = tmp_path / "pressure.json"
    report_path = tmp_path / "report.md"
    history_path.write_text(json.dumps(_mini_result(sha, ("10%", "25%"))))
    pressure_path.write_text(json.dumps(_mini_result(sha, ("1%", "5%"))))

    build_report(history_path, pressure_path, data, report_path, repeats=5)
    rendered = report_path.read_text()
    output = json.loads(report_path.with_suffix(".json").read_text())
    assert "3 open-domain" in rendered
    assert "5 adversarial" in rendered
    assert "keyness_only/1%" in rendered
    expected_holm = output["conditions"]["keyness_only/10%"]["overall_pairwise"][
        "fifo_bm25__minus__salience_bm25"
    ]["signflip_p_holm_overall_10_contrasts"]
    row = next(line for line in rendered.splitlines() if line.startswith("| keyness_only/10% |"))
    assert f"| {expected_holm:.4f} |" in row
    retention = output["retention"]["10%"]
    assert set(retention["by_condition"]) == {"keyness_only", "keyness_plus_shared_access"}
    assert len(retention["pairwise_retained_id_jaccard_mean"]) == 10
    assert retention["sample_count_budget_triggered"]["fifo_bm25"] == 4


def test_report_rejects_mismatched_dataset_sha(tmp_path: Path) -> None:
    data = tmp_path / "public.json"
    data.write_text("[]")
    history_path = tmp_path / "history.json"
    pressure_path = tmp_path / "pressure.json"
    history_path.write_text(json.dumps(_mini_result("wrong", ("10%", "25%"))))
    pressure_path.write_text(json.dumps(_mini_result("wrong", ("1%", "5%"))))
    try:
        build_report(history_path, pressure_path, data, tmp_path / "report.md", repeats=5)
    except ValueError as exc:
        assert "SHA-256 values must match" in str(exc)
    else:
        raise AssertionError("mismatched source should be rejected")

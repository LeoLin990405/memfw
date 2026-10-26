"""Validate the final Step G artifact set before freezing the report/commit.

Run from the worktree root with the worktree venv. Prints JSON and exits non-zero
when a check fails. Read-only: only writes results/stepG/validation.json.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, ".")
from eval.stepG_runner import SYSTEMS, _resolve, load_config, load_subset  # noqa: E402

CHECKS: list[dict[str, object]] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    CHECKS.append({"name": name, "ok": bool(ok), "detail": detail})


def main() -> int:
    config = load_config(Path("results/stepG/config.yaml"))
    results_dir = _resolve(config["paths"]["results_dir"])
    subset = load_subset(config)
    subset_question_ids = [question["question_id"] for question in subset["questions"]]
    check(
        "subset has 100 turns / 18 questions",
        len(subset["turns"]) == 100 and len(subset_question_ids) == 18,
    )
    check("seed matches config", int(subset["seed"]) == int(config["seed"]), subset["seed"])

    expected_systems = [SYSTEMS.get(name, name) for name in config["runs"]["systems"]]
    repeats = int(config["runs"].get("repeats", 3))
    for run_id in ("rep1", "rep2", "rep3"):
        for system in expected_systems:
            run_dir = results_dir / "runs" / run_id / system
            rows_path = run_dir / "questions.jsonl"
            write_path = run_dir / "write_turns.jsonl"
            summary_path = run_dir / "summary.json"
            check(f"{run_id}/{system}: questions.jsonl exists", rows_path.exists())
            if not rows_path.exists():
                continue
            rows = [json.loads(line) for line in rows_path.read_text().splitlines() if line.strip()]
            check(f"{run_id}/{system}: 18 question rows", len(rows) == 18, len(rows))
            check(
                f"{run_id}/{system}: question ids match subset",
                sorted(row["question_id"] for row in rows) == sorted(subset_question_ids),
            )
            check(
                f"{run_id}/{system}: no answer errors",
                all(str(row.get("answer", "")).strip() != "" for row in rows),
            )
            if write_path.exists():
                writes = [
                    json.loads(line) for line in write_path.read_text().splitlines() if line.strip()
                ]
                dia_ids = [row["dia_id"] for row in writes]
                check(f"{run_id}/{system}: 100 write turns", len(writes) == 100, len(writes))
                check(f"{run_id}/{system}: unique write turns", len(set(dia_ids)) == 100)
                if system == "mem0":
                    prompt_tokens = sum(
                        int((row.get("token_usage") or {}).get("prompt_tokens") or 0)
                        for row in writes
                    )
                    check(
                        f"{run_id}/{system}: ingest token usage recorded",
                        prompt_tokens > 0,
                        prompt_tokens,
                    )
            if summary_path.exists():
                summary = json.loads(summary_path.read_text())
                check(
                    f"{run_id}/{system}: summary model matches config",
                    (summary.get("models") or {}).get("llm") == config["llm"]["model"]
                    and (summary.get("models") or {}).get("embedding")
                    == config["embedding"]["model"],
                    summary.get("models"),
                )
                check(
                    f"{run_id}/{system}: summary has 18 questions",
                    int(summary.get("question_rows") or 0) == 18,
                    summary.get("question_rows"),
                )

    # Expanded question set (exploratory)
    expanded_subset_path = results_dir / "subset_expanded.json"
    check("subset_expanded.json exists", expanded_subset_path.exists())
    if expanded_subset_path.exists():
        expanded_subset = json.loads(expanded_subset_path.read_text())
        check(
            "expanded subset has 41 questions",
            int(expanded_subset.get("scope", {}).get("question_count") or 0) == 41,
            expanded_subset.get("scope", {}).get("question_count"),
        )
        expanded_rows_ok = True
        for arm in expected_systems:
            arm_rows_path = results_dir / "expanded" / "runs" / arm / "questions.jsonl"
            if not arm_rows_path.exists():
                expanded_rows_ok = False
                check(f"expanded/{arm}: questions.jsonl exists", False)
                continue
            arm_rows = [
                json.loads(line) for line in arm_rows_path.read_text().splitlines() if line.strip()
            ]
            check(f"expanded/{arm}: 41 question rows", len(arm_rows) == 41, len(arm_rows))
            expanded_rows_ok = expanded_rows_ok and len(arm_rows) == 41
        expanded_metrics_path = results_dir / "expanded_metrics.json"
        check("expanded_metrics.json exists", expanded_metrics_path.exists())
        if expanded_metrics_path.exists():
            expanded_metrics = json.loads(expanded_metrics_path.read_text())
            check(
                "expanded metrics covers all arms",
                set(expanded_metrics.get("arms") or []) == set(expected_systems),
                expanded_metrics.get("arms"),
            )
            check(
                "expanded metrics has a primary comparison",
                any(
                    "primary" in item.get("label", "")
                    for item in expanded_metrics.get("comparisons") or []
                ),
            )
        check("expanded rows complete", expanded_rows_ok)

    # Generated aggregates
    expected_judge_rows = 18 * repeats * len(expected_systems)
    for judge_name in ("judge_primary.jsonl", "judge_same_model.jsonl"):
        judge_path = results_dir / judge_name
        check(f"{judge_name} exists", judge_path.exists())
        if judge_path.exists():
            verdicts = [
                json.loads(line) for line in judge_path.read_text().splitlines() if line.strip()
            ]
            check(
                f"{judge_name} rows == {expected_judge_rows}",
                len(verdicts) == expected_judge_rows,
                len(verdicts),
            )
            check(
                f"{judge_name} verdicts valid",
                all(row.get("verdict") in {"correct", "incorrect"} for row in verdicts),
            )
            check(
                f"{judge_name} schema v2",
                all(row.get("schema_version") == "stepg-judge-v2" for row in verdicts),
            )
    metrics_path = results_dir / "metrics.json"
    check("metrics.json exists", metrics_path.exists())
    if metrics_path.exists():
        metrics = json.loads(metrics_path.read_text())
        for system in expected_systems:
            aggregate = (metrics.get("systems") or {}).get(system) or {}
            check(f"metrics/{system}: repeats == 3", int(aggregate.get("repeats") or 0) == 3)
            check(
                f"metrics/{system}: 18 questions", int(aggregate.get("question_count") or 0) == 18
            )
        check(
            "metrics: run errors == 0",
            int(metrics.get("run_errors") or 0) == 0,
            metrics.get("run_errors"),
        )
        comparisons = metrics.get("comparisons") or []
        check(
            "metrics: primary comparison present",
            any("primary" in item.get("label", "") for item in comparisons),
        )
        primary = next((item for item in comparisons if "primary" in item.get("label", "")), {})
        check("metrics: primary answer_score present", bool((primary or {}).get("answer_score")))
        check("metrics: primary recall present", bool((primary or {}).get("evidence_recall")))
        check(
            "metrics: judge models recorded",
            len((metrics.get("judge") or {}).get("models") or []) == 2,
        )
    provenance_path = results_dir / "provenance_summary.json"
    check("provenance_summary.json exists", provenance_path.exists())
    if provenance_path.exists():
        provenance = json.loads(provenance_path.read_text())
        for system in expected_systems:
            info = (provenance.get("systems") or {}).get(system) or {}
            for frame in ("derived_frame", "all_frame", "day11_frame"):
                check(f"provenance/{system}: {frame} present", frame in info)
        check("provenance audited run == rep1", provenance.get("audited_run_id") == "rep1")
    leaky = []
    for path in results_dir.rglob("*"):
        if not path.is_file() or path.suffix not in {".md", ".txt", ".json", ".jsonl", ".csv"}:
            continue
        relative = path.relative_to(results_dir)
        if relative.parts[:1] and relative.parts[0] in {"runtime", "data"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        home_marker = "/" + "Users/"
        project_marker = "python" + "-keyness" + "-agent-claude-gpt-deepseek"
        if home_marker in text or project_marker in text:
            leaky.append(str(relative))
    check("no absolute machine paths in committed artifacts", not leaky, leaky[:5])

    report_path = results_dir / "REPORT.md"
    check("REPORT.md exists", report_path.exists())
    test_results = results_dir / "test-results.txt"
    check("test-results.txt exists", test_results.exists())
    if test_results.exists():
        text = test_results.read_text().strip()
        check("pytest summary records passes", "passed" in text, text)

    payload = {
        "schema_version": "stepg-validation-v1",
        "ok": all(bool(item["ok"]) for item in CHECKS),
        "checks": CHECKS,
    }
    (results_dir / "validation.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({"ok": payload["ok"], "failed": [c for c in CHECKS if not c["ok"]]}, indent=2))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

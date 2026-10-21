#!/usr/bin/env python3
# ruff: noqa: E501
"""Prepare an auditable LoCoMo Mem0/memfw comparison without inventing results.

The current implementation intentionally prepares the public-data manifest and
preflight report only. It does not claim to run either system or produce metrics.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import random
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import day11_runner
import eval_locomo

SEED = 20261021
EXPECTED_MEM0_VERSION = "2.2.1"
DEFAULT_DATA = Path.home() / "Documents/memfw-data/locomo10.json"
DEFAULT_MODEL = Path.home() / "Documents/memfw-data/models/Qwen2.5-7B-Instruct-4bit"
SYSTEMS = ["mem0", "memfw"]
ALIGNMENT_FIELDS = (
    "answer_model",
    "answer_model_digest",
    "embedding_model",
    "embedding_model_digest",
    "top_k",
    "context_token_budget",
)


def validate_alignment(mem0_config: dict[str, Any], memfw_config: dict[str, Any]) -> dict[str, Any]:
    missing: list[str] = []
    for field in ALIGNMENT_FIELDS:
        values = (mem0_config.get(field), memfw_config.get(field))
        if (
            field not in mem0_config
            or field not in memfw_config
            or any(value is None or value == "" for value in values)
        ):
            missing.append(field)
    for field in ("top_k", "context_token_budget"):
        for config in (mem0_config, memfw_config):
            value = config.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                if field not in missing:
                    missing.append(field)
    differences = {
        field: {"mem0": mem0_config[field], "memfw": memfw_config[field]}
        for field in ALIGNMENT_FIELDS
        if field in mem0_config
        and field in memfw_config
        and mem0_config[field] != memfw_config[field]
    }
    return {
        "validated": not missing and not differences,
        "missing_fields": missing,
        "differences": differences,
    }


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def build_manifest(data_path: Path, *, seed: int = SEED) -> dict[str, Any]:
    """Select one seeded public conversation and up to four eligible QAs/category."""
    raw_bytes = data_path.read_bytes()
    samples = json.loads(raw_bytes)
    if not isinstance(samples, list) or not samples:
        raise ValueError("LoCoMo input must be a non-empty top-level sample list")
    rng = random.Random(seed)
    sample_index = rng.randrange(len(samples))
    payload = samples[sample_index]
    turns, questions, _ = eval_locomo.adapt_with_stats(payload)
    input_turns = turns[:100]
    input_ids = {turn["dia_id"] for turn in input_turns}
    eligible: dict[int, list[tuple[int, dict[str, Any]]]] = {n: [] for n in range(1, 6)}
    for question_index, question in enumerate(questions):
        category = question.get("category")
        evidence = question.get("evidence", [])
        if (
            isinstance(category, int)
            and category in eligible
            and evidence
            and all(item in input_ids for item in evidence)
        ):
            eligible[category].append((question_index, question))

    selected: list[dict[str, Any]] = []
    for category in range(1, 6):
        candidates = eligible[category][:]
        rng.shuffle(candidates)
        for question_index, question in sorted(candidates[:4], key=lambda row: row[0]):
            selected.append(
                {
                    "qa_index": question_index,
                    "category": category,
                    "question_sha256": sha256_bytes(question["question"].encode("utf-8")),
                    "evidence_turn_ids": question["evidence"],
                }
            )
    sample_id = payload.get("sample_id")
    return {
        "schema_version": "day11-locomo-manifest-v1",
        "seed": seed,
        "selection": {
            "sample_policy": "uniform seeded choice from top-level samples",
            "turn_policy": "first 100 turns in source order",
            "question_policy": (
                "gold evidence fully present in input window; up to 4 per category, "
                "seeded selection"
            ),
            "selection_bias": (
                "Eligibility requires all gold evidence within the first 100 turns; "
                "results do not represent full LoCoMo."
            ),
        },
        "source": {
            "path": str(data_path),
            "sha256": sha256_bytes(raw_bytes),
            "sample_index": sample_index,
            "sample_id": sample_id if isinstance(sample_id, (str, int)) else None,
        },
        "scope": {
            "source_turn_count": len(turns),
            "input_turn_count": len(input_turns),
            "input_turn_ids": [turn["dia_id"] for turn in input_turns],
            "question_count": len(questions),
            "eligible_question_counts_by_category": {
                str(category): len(rows) for category, rows in eligible.items()
            },
            "selected_question_count": len(selected),
            "selected_question_counts_by_category": {
                str(category): sum(row["category"] == category for row in selected)
                for category in range(1, 6)
            },
        },
        "questions": selected,
    }


def mem0_installed_version() -> str | None:
    try:
        return importlib.metadata.version("mem0ai")
    except importlib.metadata.PackageNotFoundError:
        return None


def build_report(
    manifest: dict[str, Any],
    *,
    model_dir: Path,
    embedding_model: str | None,
    answer_model: str,
    top_k: int,
    context_token_budget: int,
    execution_requested: bool = False,
    installation_result: str = "not_observed",
    installation_command: str | None = None,
    installation_interpreter: str | None = None,
    installation_detail: str | None = None,
) -> dict[str, Any]:
    installed_version = mem0_installed_version()
    model_config_path = model_dir / "config.json"
    model_metadata: dict[str, Any] = {"path": str(model_dir), "present": model_dir.is_dir()}
    if model_config_path.is_file():
        try:
            config = json.loads(model_config_path.read_text(encoding="utf-8"))
            model_metadata.update(
                {
                    "config_present": True,
                    "model_type": config.get("model_type"),
                    "architectures": config.get("architectures"),
                    "quantization": config.get("quantization"),
                    "hidden_size": config.get("hidden_size"),
                }
            )
        except (OSError, json.JSONDecodeError):
            model_metadata["config_present"] = False
    else:
        model_metadata["config_present"] = False

    reasons: list[str] = []
    if installed_version != EXPECTED_MEM0_VERSION:
        reasons.append(
            f"mem0ai=={EXPECTED_MEM0_VERSION} unavailable in active interpreter "
            f"(installed={installed_version!r})"
        )
    if not embedding_model:
        reasons.append(
            "no local embedding model was supplied or confirmed at the checked model path"
        )
    if not model_metadata.get("config_present"):
        reasons.append("configured local answer-model directory has no readable config.json")
    if execution_requested:
        reasons.append("real Mem0/memfw inference and retrieval runner is not implemented")

    proposed_alignment = {
        "answer_model": answer_model,
        "answer_model_digest": None,
        "embedding_model": embedding_model,
        "embedding_model_digest": None,
        "top_k": top_k,
        "context_token_budget": context_token_budget,
        "retention_levels_percent": [5, 100],
        "vector_store": (
            "local-only required; candidate is local Qdrant, configuration unverified "
            "against mem0ai 2.2.1"
        ),
        "candidate_local_stack": {
            "llm_provider": "ollama",
            "embedder_provider": "ollama",
            "vector_store_provider": "qdrant",
            "ollama_model_versions": "must be pinned by model digest before execution",
            "configuration_verified_for_expected_mem0ai": False,
        },
        "network_policy": "loopback-only required; no external model API calls",
    }
    if execution_requested:
        status = "not_implemented"
        status_detail = "Preflight completed; no model or retrieval calls were made."
    elif reasons:
        status = "blocked"
        status_detail = "Preflight did not establish all local runtime prerequisites."
    else:
        status = "prepared_not_run"
        status_detail = "Prerequisites appear present; inference remains unimplemented."
    return {
        "schema_version": "day11-baseline-report-v1",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "status": status,
        "status_detail": status_detail,
        "execution_performed": False,
        "metrics": {
            "evidence_recall": None,
            "answer_f1": None,
            "retrieval_latency_seconds": None,
            "answer_latency_seconds": None,
            "retained_memory_count": None,
            "retained_memory_bytes": None,
        },
        "metric_notes": {
            "evidence_recall": "Not measured; do not interpret as answer accuracy.",
            "answer_f1": "Unknown; no generated answers or official scorer run.",
        },
        "preflight": {
            "expected_mem0ai_version": EXPECTED_MEM0_VERSION,
            "installed_mem0ai_version": installed_version,
            "installed_version_scope": "active interpreter only; isolated target not verified",
            "active_interpreter": sys.executable,
            "python_version": platform.python_version(),
            "model_metadata": model_metadata,
            "embedding_model_configured": bool(embedding_model),
            "ollama_loopback_status": (
                "unknown; local endpoint check returned Operation not permitted from the "
                "execution sandbox; this does not establish whether the service is running"
            ),
            "installation_attempt": {
                "command": installation_command,
                "interpreter": installation_interpreter,
                "target_version": EXPECTED_MEM0_VERSION,
                "installed": (
                    None
                    if installation_result == "not_observed"
                    else installation_result == "installed"
                ),
                "result": installation_result,
                "detail": installation_detail,
            },
            "reasons": reasons,
        },
        "alignment": {
            "proposed_shared_settings": proposed_alignment,
            "validation": validate_alignment({}, {}),
            "status": "not_verified; concrete system configurations were not instantiated",
        },
        "manifest": manifest,
        "systems": {name: {"status": status, "metrics": None} for name in SYSTEMS},
        "conditions": {
            system: {
                f"retention_{retention_percent}pct": {
                    "status": status,
                    "metrics": {
                        "evidence_recall": None,
                        "answer_f1": None,
                        "retrieval_latency_seconds": None,
                        "answer_latency_seconds": None,
                        "retained_memory_count": None,
                        "retained_memory_bytes": None,
                        "write_latency_p50_seconds": None,
                        "write_latency_p95_seconds": None,
                        "search_latency_p50_seconds": None,
                        "search_latency_p95_seconds": None,
                        "evidence_recall_by_category": None,
                        "storage_bytes": None,
                        "human_confirmation": None,
                    },
                }
                for retention_percent in (5, 100)
            }
            for system in SYSTEMS
        },
        "statistics": {
            "bootstrap_plan": "1000 paired question resamples; low precision expected for one conversation",
            "bootstrap_confidence_intervals": None,
            "holm_plan": "2 primary comparisons across retention budgets; 10 category-by-budget contrasts exploratory",
            "holm_correction": None,
            "status": "not_run",
        },
        "citation_audit": {
            "requested_sample_size": 30,
            "actual_sample_size": 0,
            "judgments": None,
            "rule": (
                "Only an exact source-text match may be labeled a direct quotation. "
                "Inferences and paraphrases must be labeled derived and linked to a "
                "source. Missing support and unjudgeable items must be classified separately."
            ),
        },
    }


def render_markdown(report: dict[str, Any]) -> str:
    preflight = report["preflight"]
    reasons = preflight["reasons"] or ["No preflight blockers detected."]
    manifest = report["manifest"]
    scope = manifest["scope"]
    rows = "\n".join(
        f"| {item['qa_index']} | {item['category']} | `{item['question_sha256']}` |"
        for item in manifest["questions"]
    )
    matrix = "\n".join(
        f"| {system} | {retention}% | unknown | unknown | unknown | unknown |"
        for system in SYSTEMS
        for retention in (5, 100)
    )
    blockers = "\n".join(f"- {reason}" for reason in reasons)
    return f"""# Day 11 public LoCoMo baseline comparison

Status: **{report["status"]}**. {report["status_detail"]}

No inference was performed. This is a blocked preflight report, not a completed comparison. All performance metrics are unknown.

## Fixed public-data scope

- Seed: `{manifest["seed"]}`; sample index: `{manifest["source"]["sample_index"]}`; sample ID: `{manifest["source"]["sample_id"]}`.
- Adapted source turns: {scope["source_turn_count"]}; input window: first {scope["input_turn_count"]} turns; eligible selected questions: {scope["selected_question_count"]}.
- Dataset SHA-256: `{manifest["source"]["sha256"]}`.
- The first 100 turns restrict eligibility to questions whose complete gold evidence is in that window. This selection can bias results and does not represent full LoCoMo.

Question identifiers below use the adapted `qa_index`; hashes identify question text without storing it.

| Adapted QA index | Category | Question SHA-256 |
|---:|---:|---|
{rows}

## Preflight blockers

{blockers}

The installed-version check applies only to the active interpreter. The isolated target install is recorded separately. The official local-stack candidate is Ollama for LLM and embedding plus local Qdrant; the cited example uses a mutable model tag, and compatibility with `mem0ai==2.2.1` is unverified. Both model digests are unpinned. Sources: [mem0ai 2.2.1 on PyPI](https://pypi.org/project/mem0ai/2.2.1/) and [official local Ollama companion docs](https://github.com/mem0ai/mem0/blob/main/docs/cookbooks/companions/local-companion-ollama.mdx) (main branch; not version-pinned).

Installation attempt: `{preflight["installation_attempt"]["command"]}` using `{preflight["installation_attempt"]["interpreter"]}`. Result: **{preflight["installation_attempt"]["result"]}**; detail: {preflight["installation_attempt"]["detail"]}. The PyPI DNS/connect failure is an environment blocker and says nothing about Mem0 performance. The local Ollama endpoint check returned `Operation not permitted`; service state is unknown.

## Proposed shared settings

The proposed shared settings are answer model `{report["alignment"]["proposed_shared_settings"]["answer_model"]}`, embedding model `{report["alignment"]["proposed_shared_settings"]["embedding_model"]}`, top-k {report["alignment"]["proposed_shared_settings"]["top_k"]}, and context budget {report["alignment"]["proposed_shared_settings"]["context_token_budget"]} tokens. Concrete configurations were not instantiated, so alignment was not verified. Local-only vector storage and loopback-only inference are required.

## Results by system and retention

| System | Retention | Evidence recall | Answer F1 | Search latency p50/p95 | Write latency p50/p95 |
|---|---:|---|---|---|---|
{matrix}

Category recall, storage, retained counts/bytes, and human-confirmation data are unknown for every condition. Evidence recall will indicate retrieval of gold evidence IDs, not answer accuracy. Answer F1 remains unknown because no answer generation or official scoring ran.

Bootstrap plan: 1000 paired question resamples; one conversation with a small question set means low precision. Bootstrap confidence intervals: not run. Holm plan: 2 primary comparisons across retention budgets; 10 category-by-budget contrasts are exploratory. Holm correction: not run.

## Human audit and remaining work

- Requested citation audit: 30; sampled: 0; judgments: unknown. No error rate is estimated.
- Rule: only an exact source-text match is labeled a direct quotation. Inferences and paraphrases require a derived label and source link. Missing support and unjudgeable items are separate outcomes.
- Implement and verify the actual Mem0/memfw write and retrieval runner after installing the pinned dependency and identifying a digest-pinned local embedding model.
- Run model inference and retrieval; compute metrics only from captured results.
- No model calls, memory writes, or retrieval calls were performed.

## Interpretation for research use

This output is a prepared, blocked scaffold. It is not evidence that either system performs better, and it cannot support a workshop-paper performance claim. No significance or error-rate conclusion can be drawn from this run.

## Reproduction and files

Recreate the blocked artifacts with:

```sh
./.venv/bin/python scripts/day11_baseline_comparison.py \\
  --installation-result failed \\
  --installation-command 'UV_CACHE_DIR=/private/tmp/uv-cache uv pip install --python /private/tmp/memfw-day11-env/bin/python mem0ai==2.2.1' \\
  --installation-interpreter /private/tmp/memfw-day11-env/bin/python \\
  --installation-detail 'PyPI DNS/connect error; no package data received'
```

Files: `scripts/day11_baseline_comparison.py`, `tests/test_day11_baseline_comparison.py`, `requirements-day11.txt`, `reports/day11_baseline_manifest.json`, `reports/day11_baseline_comparison.json`, and this report. Focused checks passed; full-repository validation is pending the root agent.
"""


def _without_path(manifest: dict[str, Any]) -> dict[str, Any]:
    """Compare manifests ignoring the machine-specific data path (stored as a placeholder)."""
    source = {k: v for k, v in manifest.get("source", {}).items() if k != "path"}
    return {**manifest, "source": source}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL)
    parser.add_argument(
        "--embedding-model", help="Explicit local embedding model identifier or path"
    )
    parser.add_argument("--answer-model", default="Qwen2.5-7B-Instruct-4bit")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--context-token-budget", type=int, default=128)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Record that execution was requested; runner is not implemented",
    )
    parser.add_argument(
        "--run", action="store_true", help="Run the real local memfw vs Mem0 comparison"
    )
    parser.add_argument("--ollama-url", default="http://localhost:11434")
    parser.add_argument("--run-answer-model", default="qwen3.5:9b")
    parser.add_argument("--run-embedding-model", default="nomic-embed-text")
    parser.add_argument("--work-dir", type=Path, default=Path("/tmp/memfw-day11-run"))
    parser.add_argument(
        "--output-dir", type=Path, default=Path(__file__).resolve().parents[1] / "reports"
    )
    parser.add_argument(
        "--installation-result",
        choices=("not_observed", "installed", "failed"),
        default="not_observed",
    )
    parser.add_argument("--installation-command")
    parser.add_argument("--installation-interpreter")
    parser.add_argument("--installation-detail")
    args = parser.parse_args()
    if args.top_k <= 0 or args.context_token_budget <= 0:
        parser.error("--top-k and --context-token-budget must be positive")
    manifest = build_manifest(args.data)
    if args.run:
        stored = args.output_dir / "day11_baseline_manifest.json"
        if stored.is_file() and _without_path(
            json.loads(stored.read_text(encoding="utf-8"))
        ) != _without_path(manifest):
            parser.error("rebuilt manifest differs from the committed manifest")
        if args.work_dir.exists():
            shutil.rmtree(args.work_dir)
        run_report = day11_runner.execute(
            data_path=args.data,
            manifest=manifest,
            output_dir=args.output_dir,
            base_url=args.ollama_url,
            answer_model=args.run_answer_model,
            embedding_model=args.run_embedding_model,
            top_k=args.top_k,
            context_token_budget=args.context_token_budget,
            command=" ".join(["scripts/day11_baseline_comparison.py", *sys.argv[1:]]),
            work_dir=args.work_dir,
        )
        print(json.dumps({"status": run_report["status"], "failure": run_report["failure"]}))
        return 0 if run_report["status"] == "completed" else 3
    report = build_report(
        manifest,
        model_dir=args.model_dir,
        embedding_model=args.embedding_model,
        answer_model=args.answer_model,
        top_k=args.top_k,
        context_token_budget=args.context_token_budget,
        execution_requested=args.execute,
        installation_result=args.installation_result,
        installation_command=args.installation_command,
        installation_interpreter=args.installation_interpreter,
        installation_detail=args.installation_detail,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "day11_baseline_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (args.output_dir / "day11_baseline_comparison.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (args.output_dir / "day11_baseline_comparison.md").write_text(
        render_markdown(report), encoding="utf-8"
    )
    print(
        json.dumps({"status": report["status"], "metrics": report["metrics"]}, ensure_ascii=False)
    )
    return 2 if report["status"] in {"blocked", "not_implemented"} else 0


if __name__ == "__main__":
    raise SystemExit(main())

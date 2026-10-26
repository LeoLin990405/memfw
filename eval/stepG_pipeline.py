"""One-command Step G pipeline: data -> subset -> check -> runs -> metrics -> report.

Usage::

    python eval/stepG_pipeline.py --stage all

Stages: data, subset, check, run, metrics, provenance, report, all.
Only public LoCoMo data is downloaded; model endpoints must be loopback.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

for _proxy_var in ("NO_PROXY", "no_proxy"):
    _existing = os.environ.get(_proxy_var, "")
    _loopback = "127.0.0.1,localhost,::1"
    os.environ[_proxy_var] = f"{_existing},{_loopback}" if _existing else _loopback

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.stepG_expanded import run_expanded  # noqa: E402
from eval.stepG_metrics import build_metrics  # noqa: E402
from eval.stepG_provenance import run_audit  # noqa: E402
from eval.stepG_report import write_report  # noqa: E402
from eval.stepG_runner import (  # noqa: E402
    _resolve,
    _write_json,
    build_subset,
    check_endpoints,
    data_path,
    execute,
    load_config,
    redact_text,
    sha256_file,
    subset_path,
)

DEFAULT_CONFIG = REPO_ROOT / "results" / "stepG" / "config.yaml"


def download_data(config: dict[str, Any]) -> Path:
    """Download the public LoCoMo JSON once and verify its SHA-256."""
    target = data_path(config)
    expected = str(config["data"]["sha256"])
    if target.exists() and sha256_file(target) == expected:
        print(f"data present: {target} (sha256 verified)")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {config['data']['url']} -> {target}")
    with urllib.request.urlopen(config["data"]["url"], timeout=120) as response:  # noqa: S310
        payload = response.read()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != expected:
        raise ValueError(f"downloaded data sha256 mismatch: {digest} != {expected}")
    target.write_bytes(payload)
    print(f"data saved: {len(payload)} bytes, sha256={digest}")
    return target


def _portable_path(path: str) -> str:
    """Render a path without exposing machine-specific absolute prefixes."""
    resolved = Path(path).resolve()
    try:
        return "$PROJECT_ROOT/" + str(resolved.relative_to(REPO_ROOT.resolve()))
    except ValueError:
        return "$EXTERNAL_PYTHON"


def write_env(results_dir: Path) -> Path:
    """Record the interpreter and model stack used for the formal runs."""
    env_path = results_dir / "env.txt"
    llm_models: list[str] = []
    embed_models: list[str] = []
    model_digests: dict[str, str] = {}
    ollama_version = ""
    try:
        import urllib.request

        with urllib.request.urlopen("http://127.0.0.1:11434/api/version", timeout=5) as response:  # noqa: S310
            ollama_version = str(json.loads(response.read().decode("utf-8")).get("version"))
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=5) as response:  # noqa: S310
            tags = json.loads(response.read().decode("utf-8"))
        embed_models = [str(row.get("name")) for row in tags.get("models", [])]
        model_digests = {
            str(row.get("name")): str(row.get("digest")) for row in tags.get("models", [])
        }
    except Exception as error:  # noqa: BLE001 - recorded, not fatal
        ollama_version = f"unavailable: {type(error).__name__}"
    try:
        import importlib.metadata

        mem0_version = importlib.metadata.version("mem0ai")
        memfw_version = importlib.metadata.version("memfw")
    except Exception:  # noqa: BLE001
        mem0_version = "unknown"
        memfw_version = "unknown"
    try:
        import memfw as _memfw

        memfw_path = str(Path(_memfw.__file__).resolve())
    except Exception:  # noqa: BLE001
        memfw_path = "unknown"
    git_head = "unavailable"
    git_dirty = "unavailable"
    try:
        git_head = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        git_dirty = (
            subprocess.run(
                ["git", "-C", str(REPO_ROOT), "status", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            or "clean"
        )
    except Exception as error:  # noqa: BLE001
        git_head = f"unavailable: {type(error).__name__}"
    digest_text = ", ".join(f"{name}={digest}" for name, digest in sorted(model_digests.items()))
    lines = [
        f"python_version: {sys.version.split()[0]}",
        f"python_executable: {_portable_path(sys.executable)}",
        f"platform: {platform.platform()}",
        f"machine: {platform.machine()}",
        f"processor: {platform.processor() or 'arm64'}",
        f"memfw_version: {memfw_version}",
        f"memfw_module_path: {_portable_path(memfw_path)}",
        f"mem0_version: {mem0_version}",
        f"ollama_server_version: {ollama_version}",
        f"ollama_local_models: {', '.join(sorted(set(llm_models + embed_models))) or 'n/a'}",
        f"ollama_model_digests: {digest_text or 'n/a'}",
        f"git_head: {git_head}",
        f"git_status_porcelain: {git_dirty}",
        "model_config: results/stepG/config.yaml",
    ]
    freeze = ""
    try:
        freeze = subprocess.run(
            [sys.executable, "-m", "pip", "freeze"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except Exception:  # noqa: BLE001 - uv venvs may not ship pip
        try:
            freeze = subprocess.run(
                ["uv", "pip", "freeze", "--python", sys.executable],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        except Exception:  # noqa: BLE001
            import importlib.metadata

            rows = sorted(
                f"{dist.metadata['Name']}=={dist.version}"
                for dist in importlib.metadata.distributions()
                if dist.metadata.get("Name")
            )
            freeze = "\n".join(rows) + "\n"
    (results_dir / "pip-freeze.txt").write_text(redact_text(freeze), encoding="utf-8")
    freeze_text = redact_text(freeze)
    env_path.write_text(
        redact_text("\n".join(lines) + "\n\n## pip freeze\n") + freeze_text,
        encoding="utf-8",
    )
    return env_path


def run_pipeline(
    config: dict[str, Any],
    *,
    stages: set[str],
    repeats: int | None = None,
    run_ids: list[str] | None = None,
    systems: list[str] | None = None,
    dry_run: bool = False,
    no_judge: bool = False,
    limit_turns: int | None = None,
) -> dict[str, Any]:
    results_dir = _resolve(config["paths"]["results_dir"])
    results_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MEM0_DIR", str(results_dir / "runtime" / "mem0_home"))
    results_dir.joinpath("runtime", "mem0_home").mkdir(parents=True, exist_ok=True)
    status: dict[str, Any] = {
        "stages": sorted(stages),
        "artifacts": {},
        "started_at_unix": time.time(),
        "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    if "data" in stages:
        download_data(config)
        status["artifacts"]["data"] = str(data_path(config))
    if "subset" in stages:
        subset = build_subset(config)
        print(
            f"subset: {subset_path(config)} "
            f"({len(subset['turns'])} turns, {len(subset['questions'])} questions)"
        )
        status["artifacts"]["subset"] = str(subset_path(config))
    if "env" in stages:
        status["artifacts"]["env"] = str(write_env(results_dir))
    if "check" in stages:
        report = check_endpoints(config)
        status["endpoint_check"] = report
        print(json.dumps(report, indent=2, sort_keys=True))
    if "run" in stages:
        if not data_path(config).exists():
            download_data(config)
        if not subset_path(config).exists():
            build_subset(config)
        if repeats is not None:
            config.setdefault("runs", {})["repeats"] = int(repeats)
        run_ids = run_ids or [
            f"rep{index}" for index in range(1, int(config["runs"].get("repeats", 3)) + 1)
        ]
        manifest = execute(
            config,
            run_ids=run_ids,
            systems=systems,
            dry_run=dry_run,
            limit_turns=limit_turns,
        )
        status["runs"] = manifest
        status["artifacts"]["run_manifest"] = str(results_dir / "run_manifest.json")
    if "metrics" in stages:
        metrics = build_metrics(
            config, judge=bool((config.get("judge") or {}).get("enabled")) and not no_judge
        )
        status["metrics"] = {
            "systems": list(metrics["systems"]),
            "judge": metrics.get("judge", {}).get("enabled"),
        }
        status["artifacts"]["metrics"] = str(results_dir / "metrics.json")
        status["artifacts"]["metrics_md"] = str(results_dir / "metrics.md")
    if "expanded" in stages:
        expanded = run_expanded(config, judge=not no_judge)
        status["expanded"] = {
            "n_questions": expanded.get("n_questions"),
            "arms": expanded.get("arms"),
        }
        status["artifacts"]["expanded_metrics"] = str(results_dir / "expanded_metrics.json")
        status["artifacts"]["expanded_metrics_md"] = str(results_dir / "expanded_metrics.md")
    if "provenance" in stages:
        summary = run_audit(config, run_id="rep1")
        status["provenance"] = {
            key: value["derived_frame"] for key, value in summary["systems"].items()
        }
        status["artifacts"]["provenance"] = str(results_dir / "provenance_summary.json")
    if "report" in stages:
        report_path = write_report(config)
        status["artifacts"]["report"] = str(report_path)
    status["finished_at_unix"] = time.time()
    status["finished_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    status["elapsed_seconds"] = status["finished_at_unix"] - status["started_at_unix"]
    _write_json(results_dir / "pipeline_status.json", status)
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Step G formal pipeline")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--stage",
        default="all",
        choices=[
            "data",
            "subset",
            "env",
            "check",
            "run",
            "metrics",
            "expanded",
            "provenance",
            "report",
            "all",
        ],
    )
    parser.add_argument("--repeats", type=int, default=None)
    parser.add_argument("--run-ids", type=str, default=None)
    parser.add_argument(
        "--systems",
        type=str,
        default=None,
        help="comma separated arms (memfw,mem0_stock,mem0_custom); default: config runs.systems",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-judge", action="store_true")
    parser.add_argument("--limit-turns", type=int, default=None, help="debug only")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    stages = (
        {"data", "subset", "env", "check", "run", "metrics", "expanded", "provenance", "report"}
        if args.stage == "all"
        else {args.stage}
    )
    status = run_pipeline(
        config,
        stages=stages,
        repeats=args.repeats,
        run_ids=args.run_ids.split(",") if args.run_ids else None,
        systems=args.systems.split(",") if args.systems else None,
        dry_run=args.dry_run,
        no_judge=args.no_judge,
        limit_turns=args.limit_turns,
    )
    print(
        json.dumps(
            {"stages": status["stages"], "artifacts": status["artifacts"]}, indent=2, sort_keys=True
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

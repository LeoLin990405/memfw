"""``memfw-pipeline run|plan|status|unit``.

``run`` prints one JSON line (the status: counts and codes only) and exits with
the runner's code (see :mod:`memfw_pipeline.runner`). ``plan`` and ``status``
only read; ``plan`` exits 2 on a setup error, else 0. ``status`` exits 0 when
it lists at least one stage and every one is ok and fresh, else 1.
``unit`` prints a unit template to review; it installs nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .config import PipelineConfig, load_config
from .contract import SetupError
from .runner import EXIT_SETUP, RunOptions, plan_stage, run_stage, stage_status
from .units import render


def _print(payload: object) -> None:
    sys.stdout.write(json.dumps(payload, sort_keys=True, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memfw-pipeline", description="Run memfw pipeline stages under one contract."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run one stage")
    run.add_argument("stage")
    run.add_argument("--config", required=True, type=Path)
    run.add_argument("--force", action="store_true", help="rebuild even if inputs are unchanged")
    run.add_argument(
        "--allow-shrink",
        action="store_true",
        help="publish an empty build or one under half of the live count",
    )
    run.add_argument(
        "--lock-wait-seconds",
        type=float,
        default=5.0,
        help="retry a held lock this long before exiting 75 (default 5)",
    )
    plan = sub.add_parser("plan", help="what run would do (reads only)")
    plan.add_argument("stage")
    plan.add_argument("--config", required=True, type=Path)
    status = sub.add_parser("status", help="last status and freshness")
    status.add_argument("stage", nargs="?")
    status.add_argument("--config", required=True, type=Path)
    status.add_argument(
        "--no-probe", action="store_true", help="do not read the source for its revision"
    )
    unit = sub.add_parser("unit", help="print a hardened unit template")
    unit.add_argument("stage")
    unit.add_argument("--config", required=True, type=Path)
    kind = unit.add_mutually_exclusive_group(required=True)
    kind.add_argument("--systemd", action="store_const", const="systemd", dest="kind")
    kind.add_argument("--launchd", action="store_const", const="launchd", dest="kind")
    unit.add_argument("--python", help="absolute interpreter path for ExecStart")
    unit.add_argument(
        "--allow-untrusted",
        action="store_true",
        help="print a root-run unit for a stage without trust_paths = true",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config: PipelineConfig = load_config(args.config)
        if args.command == "status":
            names = [args.stage] if args.stage else sorted(config.stages)
            specs = [config.stage(name) for name in names]
        else:
            specs = [config.stage(args.stage)]
    except SetupError as exc:
        _print({"ok": False, "result": "setup_error", "errors": [exc.code]})
        return EXIT_SETUP
    spec = specs[0] if specs else None
    if args.command == "run":
        assert spec is not None
        code, status = run_stage(
            spec,
            RunOptions(
                force=args.force,
                allow_shrink=args.allow_shrink,
                lock_wait_seconds=max(0.0, min(args.lock_wait_seconds, 600.0)),
            ),
        )
        _print(status)
        return code
    if args.command == "plan":
        assert spec is not None
        report = plan_stage(spec)
        _print(report)
        return EXIT_SETUP if report.get("action") == "setup_error" else 0
    if args.command == "status":
        reports = [stage_status(s, probe=not args.no_probe) for s in specs]
        _print({"stages": reports})
        healthy = bool(reports) and all(
            r.get("ok") is True and not r["freshness"]["stale"] for r in reports
        )
        return 0 if healthy else 1
    assert spec is not None
    try:
        sys.stdout.write(
            render(spec, args.kind, python=args.python, allow_untrusted=args.allow_untrusted)
        )
    except SetupError as exc:
        _print({"ok": False, "result": "setup_error", "errors": [exc.code]})
        return EXIT_SETUP
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""``memfw-adapter-tailnet scan``: import the tailnet's machine list as candidates.

    memfw-adapter-tailnet scan (--input FILE | --live) --state DIR
        [--vault VAULT] [--allowlist FILE] [--offline-hours 24]
        [--privacy local-private|shared|public] [--proposal-id ID]
        [--tailscale PATH] [--timeout SECONDS] [--include-shared]

``--input`` reads an exported ``tailscale status --json``; ``--live`` runs
that command once (fixed argv, no shell, bounded time and size). With
``--vault`` each new, gone or changed host is staged as a candidate under
``<vault>/_candidates/tailnet/`` and the snapshot in ``--state`` advances;
without it the run is a preview: nothing is staged and the snapshot stays.
``report.json`` in ``--state`` is rewritten on every run, including failures.
Standard output is one JSON line of counts and codes, never names.

Exit codes:

* ``0``: success, no alert (or no ``--allowlist``);
* ``1``: refused or failed (code in ``error``; ``internal`` for an unexpected
  exception); nothing staged, snapshot unchanged;
* ``2``: bad command line (argparse);
* ``3``: success, and at least one alert (``listed_offline``,
  ``listed_missing``, ``listed_ambiguous`` or ``unknown``) is raised in
  ``report.json``.
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._ports import PRIVACY_LEVELS, StagingError
from .ingestor import SOURCE, Settings, TailnetIngestor
from .scan import (
    DEFAULT_OFFLINE_HOURS,
    build_scan,
    error_report,
    load_allowlist,
    load_snapshot,
    state_lock,
    write_report,
    write_snapshot,
)
from .source import DEFAULT_TIMEOUT, MAX_TIMEOUT, read_input, run_tailscale
from .status import TailnetError, parse_status_bytes

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_ALERT = 3


def _hours(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be a positive number of hours")
    return value


def _seconds(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value) or not 0 < value <= MAX_TIMEOUT:
        raise argparse.ArgumentTypeError(f"must be in (0, {MAX_TIMEOUT:g}]")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memfw-adapter-tailnet")
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan", help="import tailscale status as host-fact candidates")
    where = scan.add_mutually_exclusive_group(required=True)
    where.add_argument("--input", type=Path, help="exported `tailscale status --json`")
    where.add_argument("--live", action="store_true", help="run `tailscale status --json`")
    scan.add_argument("--state", type=Path, required=True, help="private state directory")
    scan.add_argument("--vault", type=Path, help="stage candidates here (else preview)")
    scan.add_argument("--allowlist", type=Path, help="operator file of expected machines")
    scan.add_argument("--offline-hours", type=_hours, default=DEFAULT_OFFLINE_HOURS)
    scan.add_argument("--privacy", choices=PRIVACY_LEVELS, default=None)
    scan.add_argument("--proposal-id", default=None)
    scan.add_argument("--tailscale", default="tailscale", help="the tailscale CLI (live mode)")
    scan.add_argument("--timeout", type=_seconds, default=DEFAULT_TIMEOUT)
    scan.add_argument(
        "--include-shared",
        action="store_true",
        help="keep peers shared in from other tailnets (ShareeNode)",
    )
    return parser


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def run(
    argv: Sequence[str] | None = None,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    fetch: Callable[..., bytes] = run_tailscale,
) -> int:
    args = _parser().parse_args(argv)
    started = now()
    mode = "live" if args.live else "input"
    try:
        with state_lock(args.state) as state:
            try:
                result = _scan(args, state, started, mode, fetch)
            except Exception as exc:  # noqa: BLE001 - every failure gets a report
                code = exc.code if isinstance(exc, TailnetError) else "internal"
                try:
                    write_report(state, error_report(code, now=started, mode=mode))
                except Exception:  # noqa: BLE001 - the exit code still says so
                    pass
                raise TailnetError(code) from exc
    except TailnetError as exc:
        _emit({"error": exc.code, "status": "error"})
        return EXIT_ERROR
    except Exception:  # noqa: BLE001 - outside the lock: no report possible
        _emit({"error": "internal", "status": "error"})
        return EXIT_ERROR
    _emit(
        {
            "status": result["status"],
            "committed": result["committed"],
            "counts": result["counts"],
            "alerts": result["alerts"],
            **({"staged": result["staged"]} if "staged" in result else {}),
        }
    )
    return EXIT_ALERT if result["alerts"] else EXIT_OK


def _scan(
    args: argparse.Namespace,
    state: Path,
    started: datetime,
    mode: str,
    fetch: Callable[..., bytes],
) -> dict[str, Any]:
    options: dict[str, Any] = {"include_shared": bool(args.include_shared)}
    if args.privacy:
        options["privacy"] = args.privacy
    settings = Settings.from_mapping(options)
    allowlist = load_allowlist(args.allowlist) if args.allowlist is not None else None
    previous = load_snapshot(state)
    if args.live:
        data = fetch(args.tailscale, timeout=args.timeout)
    else:
        data = read_input(args.input)
    status = parse_status_bytes(data, include_shared=settings.include_shared)
    result = build_scan(
        status,
        previous,
        now=started,
        privacy=settings.privacy,
        allowlist=allowlist,
        offline_hours=args.offline_hours,
        mode=mode,
    )
    report = result.report
    if args.vault is not None:
        proposal = args.proposal_id or "tailnet-scan-" + started.strftime("%Y%m%dT%H%M%SZ")
        try:
            staged = TailnetIngestor(settings).stage(
                args.vault, result.items, proposal_id=proposal, source=SOURCE
            )
        except (StagingError, ValueError, OSError) as exc:
            raise TailnetError("staging_failed") from exc
        write_snapshot(state, result.snapshot, started)
        report["committed"] = True
        report["staged"] = len(staged.staged)
        report["duplicates"] = len(staged.duplicates)
    write_report(state, report)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    return run(argv)


__all__ = ["EXIT_ALERT", "EXIT_ERROR", "EXIT_OK", "main", "run"]

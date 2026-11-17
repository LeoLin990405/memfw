"""Probe for the CLI tidy-up byte-identical check (tests/test_cli_tidy.py).

Runs a sample of read and dry-run commands with their default output (no
``--json``, no open-question options) on a fixed vault. Uses only CLI options
that exist before the tidy-up, so it runs against an older tree too. ``--help``
is left out: its layout (the panels) changed on purpose.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from typer.testing import CliRunner

NOW = "2026-01-05T10:00:00+00:00"


def cli_tidy_probe(payload: dict[str, Any], workdir: Path) -> str:
    from memfw.cli import app

    runner = CliRunner()
    vault = workdir / "vault"
    outputs: list[str] = []

    def run(*args: str) -> str:
        argv = ["--now", NOW, "--id-seed", "probe", *args]
        result = runner.invoke(app, argv)
        outputs.append(f"$ {' '.join(args[:2])}\nexit={result.exit_code}\n{result.output}")
        return result.output.strip()

    run("init", str(vault))
    root = ("--root", str(vault))
    first = run("add", "--title", "port", "--body", "The service uses port 8080.", *root)
    run("add", "--title", "retry", "--body", "Wrap calls in a retry.", "--type", "note", *root)
    run("add", "--title", "dry", "--body", "x", "--dry-run", *root)
    run("add", "--title", "bad", "--body", "x", "--epistemic", "nonsense", *root)
    run("list", *root)
    run("show", first, *root)
    run("show", "no-such-memory", *root)
    run("update", first, "--title", "port v2", "--dry-run", *root)
    run("update", first, "--body", "Port 8081 now.", *root)
    run("show", first, *root)
    run("index", "rebuild", *root)
    run("index", "search", "port", "--dry-run", *root)
    if payload.get("search"):
        out = run("index", "search", "port", *root)
        # The trailing metrics line carries timings; keep only the id/title rows.
        outputs[-1] = "\n".join(line for line in out.splitlines() if not line.startswith("{"))
    run("delete", first, "--dry-run", *root)
    run("privacy", "legacy-default", *root)
    run("tiers", "status", *root)
    return "\n".join(outputs).replace(str(workdir), "<work>")

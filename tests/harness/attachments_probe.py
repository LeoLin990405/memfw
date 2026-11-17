"""Probes for the MM1a attachment checks against an older tree (tests/test_attachments_compat.py).

``default_commands`` runs memory commands on a vault without attachments using
only options that exist before MM1a, so its output must be byte-identical on
the base ref. ``old_reader`` opens a vault built by this tree (copied into the
work dir) with the old tree's CLI and reports what each command does.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

NOW = "2026-01-05T10:00:00+00:00"


def default_commands(payload: dict[str, Any], workdir: Path) -> str:
    from memfw.cli import app
    from memfw.core.repository import Repository

    runner = CliRunner()
    vault = workdir / "vault"
    outputs: list[str] = []

    def run(*args: str) -> str:
        argv = ["--now", NOW, "--id-seed", "attach-probe", *args]
        result = runner.invoke(app, argv)
        outputs.append(f"$ {' '.join(args[:2])}\nexit={result.exit_code}\n{result.output}")
        return result.output.strip()

    run("init", str(vault))
    root = ("--root", str(vault))
    first = run("add", "--title", "port", "--body", "The service uses port 8080.", *root)
    run("add", "--title", "scan", "--body", "A note about a scan.", "--privacy", "shared", *root)
    run("update", first, "--body", "Port 8081 now.", *root)
    run("list", *root)
    run("show", first, *root)
    run("fsck", "--dry-run", *root)
    run("fsck", *root)
    run("log", "replay", *root)
    run("log", "verify", *root)
    run("recover", "--dry-run", *root)
    run("delete", first, *root)
    run("fsck", *root)
    if payload.get("compact"):
        run("log", "compact", "--keep-recent", "1", *root)
        run("log", "replay", *root)
    repository = Repository(vault)
    outputs.append(json.dumps(repository.replay(), sort_keys=True))
    outputs.append(json.dumps(sorted(p.name for p in vault.iterdir()), sort_keys=True))
    return "\n".join(outputs).replace(str(workdir), "<work>")


COMMANDS: tuple[tuple[str, ...], ...] = (
    ("list",),
    ("show", "m-plain"),
    ("fsck",),
    ("log", "verify"),
    ("index", "rebuild"),
    ("add", "--title", "new", "--body", "written by the old tree"),
)


def old_reader(payload: dict[str, Any], workdir: Path) -> str:
    from memfw.cli import app

    vault = workdir / "vault"
    shutil.copytree(payload["vault"], vault)
    runner = CliRunner()
    report: dict[str, Any] = {}
    for command in COMMANDS:
        result = runner.invoke(app, [*command, "--root", str(vault)])
        error = result.exception
        text = f"{result.output}\n{error}" if error is not None else result.output
        report[" ".join(command[:2])] = {
            "exit": result.exit_code,
            "unknown_event_kind": "input_value='attach'" in text,
        }
    return json.dumps(report, sort_keys=True)


__all__ = ["COMMANDS", "default_commands", "old_reader"]

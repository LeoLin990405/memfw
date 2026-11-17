"""Probe for the M3e-a byte-identical check (tests/test_history_settled_archive.py).

Runs ``memfw history ingest`` (no ``--settled-minutes``) and the default
``memfw conversations list/search/get`` on a fixed synthetic transcript root.
Uses only CLI options that exist before M3e-a, so it runs against an older tree.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from memfw.core.repository import Repository


def _line(session: str, uuid: str, role: str, text: str, ts: str) -> str:
    return (
        json.dumps(
            {
                "sessionId": session,
                "uuid": uuid,
                "cwd": "/work/demo",
                "timestamp": ts,
                "message": {"role": role, "content": text},
            }
        )
        + "\n"
    )


def settled_probe(payload: dict[str, Any], workdir: Path) -> str:
    from memfw.cli import app

    vault = Repository(workdir / "vault")
    vault.init()
    config = vault.read_config()
    config["conversation_share"] = {"enabled": True}
    vault.configure(config)
    projects = workdir / "projects" / "demo"
    projects.mkdir(parents=True)
    session = projects / "s-one.jsonl"
    session.write_text(
        _line("s-one", "a", "user", "please add a retry wrapper", "2026-01-05T10:00:00Z")
        + _line("s-one", "b", "assistant", "retry wrapper added", "2026-01-05T10:01:00Z"),
        encoding="utf-8",
    )
    (projects / "s-two.jsonl").write_text(
        _line("s-two", "a", "user", "no stamps here", "")
        if payload.get("unstamped")
        else _line("s-two", "a", "user", "second session", "2026-01-05T11:00:00Z"),
        encoding="utf-8",
    )
    db = workdir / "data" / "conversations.sqlite"
    runner = CliRunner()
    outputs: list[str] = []

    def run(*args: str) -> None:
        result = runner.invoke(app, list(args))
        outputs.append(f"$ {' '.join(args[:3])}\nexit={result.exit_code}\n{result.output}")

    ingest = (
        "history",
        "ingest",
        "claude",
        "--root",
        str(vault.root),
        "--db",
        str(db),
        "--path",
        str(workdir / "projects"),
    )
    run(*ingest)
    with session.open("a", encoding="utf-8") as handle:
        handle.write(_line("s-one", "c", "user", "and a test", "2026-01-05T10:05:00Z"))
    run(*ingest)
    common = ("--root", str(vault.root), "--store", str(db))
    run("conversations", "list", *common)
    run("conversations", "search", "retry", *common)
    run("conversations", "get", "s-one", "--app", "claude", *common)
    return "\n".join(outputs).replace(str(workdir), "<work>")

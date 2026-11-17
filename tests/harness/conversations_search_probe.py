"""Probe for the M3c byte-identical check (tests/test_conversations_two_stage.py).

Runs the default ``memfw conversations search`` (CLI and the MCP
``conversations_search`` tool) on a synthetic store, with and without worker
summaries and tags in the state file. Uses only options and APIs that exist
before M3c, so it runs against an older tree.
"""

from __future__ import annotations

import io
import json
import sqlite3
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

TAGS = json.dumps(
    {
        "components": ["retry"],
        "intent": "add a retry wrapper",
        "outcome": "done",
        "project": ["uploader"],
        "task_type": "dev",
    },
    sort_keys=True,
)


def search_default(payload: dict[str, Any], workdir: Path) -> str:
    from typer.testing import CliRunner

    from memfw.cli import app
    from memfw.conversations.worker import WorkState
    from memfw.mcp.server import Gateway
    from tests.conversation_fixtures import make_vault, standard_rows, write_store

    store = write_store(workdir / "data" / "conversations.sqlite", standard_rows())
    state = workdir / "data" / "work.sqlite"
    config = {
        "conversation_share": {"enabled": True, **(payload.get("policy") or {})},
        "conversation_store": {"path": str(store)},
        "conversation_worker": {"state_path": str(state)},
    }
    vault = make_vault(workdir / "vault", config)
    if payload.get("labels"):
        with WorkState(state).open() as connection:
            for app_name, session in (("codex", "s-alpha"), ("cursor", "s-gamma")):
                connection.execute(
                    "INSERT INTO summaries (app, session_id, chunk_start, chunk_end,"
                    " prompt_version, model, summary, intent, outcome, created_at, tags)"
                    " VALUES (?, ?, -1, 3, 'conv-v2', 'fake', 'Retry wrapper added.', '', '',"
                    " '2026-01-07T00:00:00+00:00', ?)",
                    (app_name, session, TAGS),
                )
    caller = payload.get("caller") or "operator"
    runner = CliRunner()
    out: dict[str, Any] = {"cli": [], "mcp": []}
    base = ["conversations", "search", "--root", str(vault), "--as-app", caller]
    for extra in (
        ["uploader"],
        ["uploader", "--limit", "1", "--offset", "1"],
        ["uploader", "--app", "codex"],
        ["uploader", "--since", "2026-01-05T10:03:00Z"],
        ["uploader", "--with-archive-state"],
        ["uploader", "--archived-only"],
        [""],
        ["uploader", "--since", "bogus"],
    ):
        result = runner.invoke(app, [*base, *extra])
        out["cli"].append({"exit": result.exit_code, "output": result.output})
    if caller != "operator":
        gateway = Gateway(vault, app=caller)
        for arguments in (
            {"query": "uploader"},
            {"query": "uploader", "limit": 1, "offset": 1},
            {"query": "uploader", "app": "codex"},
            {"query": ""},
        ):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                gateway.handle(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "conversations_search", "arguments": arguments},
                    }
                )
            out["mcp"].append(buffer.getvalue())
    with sqlite3.connect(state) if state.exists() else sqlite3.connect(":memory:") as connection:
        out["state_tables"] = sorted(
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        )
    return json.dumps(out, indent=1, sort_keys=True).replace(str(workdir), "WORKDIR")


def mcp_archive_flags(payload: dict[str, Any], workdir: Path) -> str:
    """MCP ``conversations_search`` with and without ``with_archive_state`` /
    ``archived_only`` on a store where ``codex/s-alpha`` is archived.

    The base ref ignores both arguments (all three answers equal the plain one);
    M3c honours them. The test asserts exactly that difference.
    """

    from memfw.conversations.adapter import SqliteStoreSource
    from memfw.conversations.store import session_fingerprint
    from memfw.mcp.server import Gateway
    from tests.conversation_fixtures import make_vault, standard_rows, write_store

    store = write_store(workdir / "data" / "conversations.sqlite", standard_rows())
    info = SqliteStoreSource(store).session("codex", "s-alpha")
    assert info is not None
    with sqlite3.connect(store) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS conversation_archive (agent TEXT NOT NULL,"
            " session_id TEXT NOT NULL, archived_at TEXT NOT NULL,"
            " archived_fingerprint TEXT NOT NULL, PRIMARY KEY (agent, session_id))"
        )
        connection.execute(
            "INSERT INTO conversation_archive VALUES"
            " ('codex', 's-alpha', '2026-01-06T00:00:00Z', ?)",
            (session_fingerprint(info.turns, info.last_offset, info.updated_at),),
        )
    config = {
        "conversation_share": {"enabled": True},
        "conversation_store": {"path": str(store)},
        "conversation_worker": {"state_path": str(workdir / "data" / "work.sqlite")},
    }
    vault = make_vault(workdir / "vault", config)
    gateway = Gateway(vault, app=payload.get("caller") or "kimi")
    out: dict[str, Any] = {}
    for name, extra in (
        ("plain", {}),
        ("with_archive_state", {"with_archive_state": True}),
        ("archived_only", {"archived_only": True}),
    ):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            gateway.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "conversations_search",
                        "arguments": {"query": "uploader", **extra},
                    },
                }
            )
        response = json.loads(buffer.getvalue())
        out[name] = json.loads(response["result"]["content"][0]["text"])
    return json.dumps(out, indent=1, sort_keys=True).replace(str(workdir), "WORKDIR")

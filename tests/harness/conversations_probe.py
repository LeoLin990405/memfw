"""Probe for ``byte_identical``: the conversation worker with no backend configured.

Runs ``memfw conversations worker`` twice (the second run is the idempotent
no-op) on a synthetic store and dumps the reports and the state rows, with
run ids and wall-clock stamps masked. Only the columns that exist on every
compared tree are dumped (M3a adds ``jobs.prompt_version`` and
``summaries.tags``). Uses only the CLI, ``Repository`` and the synthetic
fixtures, which exist in every tree compared.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

COLUMNS = {
    "jobs": (
        "job_id, kind, app, session_id, fingerprint, chunk_start, chunk_end, status,"
        " attempts, error"
    ),
    "summaries": (
        "app, session_id, chunk_start, chunk_end, prompt_version, model, summary, intent,"
        " outcome, tokens_in, tokens_out"
    ),
    "episodes": (
        "episode_id, app, session_id, fingerprint, intent, outcome, first_offset, last_offset,"
        " status"
    ),
    "session_state": "app, session_id, fingerprint",
    "runs": "status",
}
_RUN_ID = re.compile(r"run-[0-9a-f]{12}")


def worker_default(payload: dict[str, Any], workdir: Path) -> str:
    from typer.testing import CliRunner

    from memfw.cli import app
    from tests.conversation_fixtures import make_vault, standard_rows, write_store

    vault = make_vault(workdir / "vault", payload.get("config") or {})
    store = write_store(workdir / "data" / "conversations.sqlite", standard_rows())
    state = workdir / "data" / "work.sqlite"
    args = [
        "conversations",
        "worker",
        "--root",
        str(vault),
        "--store",
        str(store),
        "--state",
        str(state),
        "--no-ingest",
        "--idle-minutes",
        "0",
    ]
    out: dict[str, Any] = {"cli": []}
    for _ in range(2):
        result = CliRunner().invoke(app, args)
        out["cli"].append({"exit": result.exit_code, "output": _RUN_ID.sub("RUN", result.output)})
    with sqlite3.connect(state) as connection:
        for table, columns in COLUMNS.items():
            out[table] = [
                list(row)
                for row in connection.execute(f"SELECT {columns} FROM {table} ORDER BY {columns}")
            ]
    return json.dumps(out, indent=1, sort_keys=True).replace(str(workdir), "WORKDIR")

"""``memfw handoff``: the stable CLI contract (exit codes, JSON, saved packs, lineage)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from memfw.cli import app
from memfw.handoff.schema import (
    EXIT_NOT_INDEXED,
    EXIT_REFUSED,
    EXIT_STORE_UNAVAILABLE,
    EXIT_USAGE,
    NOTICE,
    SCHEMA,
)
from memfw.history.conversations import ConversationStore
from memfw.history.sources import READERS, store_roots
from tests.conversation_fixtures import make_vault
from tests.handoff_fixtures import SECRET, isolated_home, make_store, standard_session

__all__ = ["isolated_home"]

runner = CliRunner()


@pytest.fixture(autouse=True)
def _home(isolated_home: Path) -> Path:
    return isolated_home


def _invoke(*args: str) -> Any:
    return runner.invoke(app, ["handoff", *args])


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return make_store(tmp_path, {"claude": {"s1": standard_session("claude")}})


def test_json_contract_and_saved_pack(db: Path) -> None:
    result = _invoke("claude", "s1", "--for", "kimi", "--format", "json", "--store", str(db))
    assert result.exit_code == 0, result.output
    pack = json.loads(result.stdout)
    assert pack["schema"] == SCHEMA and pack["for_agent"] == "kimi" and pack["notice"] == NOTICE
    assert pack["saved_as"] == "claude-s1-1.json"
    saved = db.parent / "handoffs" / "claude-s1-1.json"
    assert saved.read_text(encoding="utf-8") == result.stdout
    assert stat.S_IMODE(os.stat(saved).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(saved.parent).st_mode) == 0o700
    assert len(result.stdout.encode()) <= 16384
    assert SECRET not in result.stdout


def test_markdown_default_and_lineage_of_repeated_handoffs(db: Path) -> None:
    first = _invoke("claude", "s1", "--store", str(db))
    assert first.exit_code == 0 and first.stdout.startswith(f"> {NOTICE}")
    second = _invoke("claude", "s1", "--for", "grok", "--format", "json", "--store", str(db))
    pack = json.loads(second.stdout)
    previous = pack["system"]["lineage"]["previous_handoffs"]
    assert [item["n"] for item in previous] == [1] and previous[0]["for_agent"] is None
    assert pack["saved_as"] == "claude-s1-2.json"
    assert (db.parent / "handoffs" / "claude-s1-1.md").is_file()


def test_no_save_writes_nothing(db: Path) -> None:
    result = _invoke("claude", "s1", "--no-save", "--format", "json", "--store", str(db))
    assert result.exit_code == 0 and json.loads(result.stdout)["saved_as"] is None
    assert not (db.parent / "handoffs").exists()


def test_store_is_never_written(db: Path) -> None:
    before = (db.stat().st_mtime_ns, db.stat().st_size)
    assert _invoke("claude", "s1", "--store", str(db)).exit_code == 0
    assert (db.stat().st_mtime_ns, db.stat().st_size) == before


@pytest.mark.parametrize(
    ("args", "code", "reason"),
    [
        (["claude", "missing"], EXIT_NOT_INDEXED, "session_not_indexed"),
        (["claude", "s1", "--max-bytes", "70000"], EXIT_USAGE, "bad_request"),
        (["claude", "../etc"], EXIT_USAGE, "bad_request"),
        (["claude", "s1", "--format", "xml"], EXIT_USAGE, "bad_request"),
    ],
)
def test_failures_have_codes_and_a_json_reason(
    db: Path, args: list[str], code: int, reason: str
) -> None:
    fmt = [] if "--format" in args else ["--format", "json"]
    result = _invoke(*args, *fmt, "--store", str(db))
    assert result.exit_code == code
    if fmt:
        assert json.loads(result.stdout) == {
            "schema": SCHEMA,
            "status": "unavailable",
            "reason": reason,
        }


def test_missing_store_is_unavailable(tmp_path: Path) -> None:
    store = tmp_path / "data" / "absent.sqlite"
    result = _invoke("claude", "s1", "--format", "json", "--no-save", "--store", str(store))
    assert result.exit_code == EXIT_STORE_UNAVAILABLE
    assert json.loads(result.stdout)["reason"] == "store_unavailable"


def test_store_inside_a_git_work_tree_is_refused(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    (tree / ".git").mkdir(parents=True)
    result = _invoke("claude", "s1", "--format", "json", "--store", str(tree / "c.sqlite"))
    assert result.exit_code == EXIT_REFUSED


def _vault(tmp_path: Path, config: dict[str, Any]) -> Path:
    return make_vault(tmp_path / "vault", config)


def test_hidden_app_answers_like_missing_and_for_agent_policy(tmp_path: Path, db: Path) -> None:
    hidden = _vault(
        tmp_path / "a", {"conversation_share": {"apps": {"claude": {"readable": False}}}}
    )
    result = _invoke("claude", "s1", "--format", "json", "--root", str(hidden), "--store", str(db))
    assert result.exit_code == EXIT_NOT_INDEXED
    shared = _vault(
        tmp_path / "b",
        {"conversation_share": {"enabled": True, "apps": {"grok": {"can_read": False}}}},
    )
    refused = _invoke(
        "claude",
        "s1",
        "--for",
        "grok",
        "--format",
        "json",
        "--root",
        str(shared),
        "--store",
        str(db),
    )
    assert refused.exit_code == EXIT_REFUSED
    assert json.loads(refused.stdout)["reason"] == "not_readable"


def test_task_file_becomes_the_goal(tmp_path: Path, db: Path) -> None:
    task = tmp_path / "task.txt"
    task.write_text("Line M2c: wire the quota handoff.", encoding="utf-8")
    result = _invoke(
        "claude",
        "s1",
        "--format",
        "json",
        "--no-save",
        "--task-file",
        str(task),
        "--store",
        str(db),
    )
    system = json.loads(result.stdout)["system"]
    assert system["goal_source"] == "task_file" and system["goal"].startswith("Line M2c")


def test_summarize_bad_spec_is_a_usage_error(db: Path) -> None:
    result = _invoke(
        "claude", "s1", "--summarize", "extractive", "--format", "json", "--store", str(db)
    )
    assert result.exit_code == EXIT_USAGE
    assert not list((db.parent / "handoffs").glob("*.json"))  # the reservation was released


def test_end_to_end_from_a_real_reader(tmp_path: Path, isolated_home: Path) -> None:
    """A synthetic Claude Code store under the temporary HOME, ingested by the real reader."""

    project = isolated_home / ".claude" / "projects" / "-work-demo"
    project.mkdir(parents=True)
    base = {"sessionId": "sess-e2e", "cwd": "/work/demo"}
    rows = [
        base
        | {
            "type": "user",
            "uuid": "u1",
            "timestamp": "2026-01-03T09:00:00Z",
            "message": {"role": "user", "content": "Rename the config loader."},
        },
        base
        | {
            "type": "assistant",
            "uuid": "a1",
            "timestamp": "2026-01-03T09:00:05Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Renaming it now."},
                    {
                        "type": "tool_use",
                        "name": "Edit",
                        "input": {"file_path": "/work/demo/src/config.py", "old_string": "a"},
                    },
                ],
            },
        },
    ]
    (project / "sess-e2e.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    db = tmp_path / "data" / "conversations.sqlite"
    reader = READERS["claude"]
    ConversationStore(db).ingest(reader, store_roots(reader, isolated_home))
    result = _invoke("claude", "sess-e2e", "--format", "json", "--no-save", "--store", str(db))
    assert result.exit_code == 0, result.output
    pack = json.loads(result.stdout)
    assert pack["system"]["goal"] == "Rename the config loader."
    assert pack["system"]["files_touched"] == ["/work/demo/src/config.py"]
    assert [turn["role"] for turn in pack["system"]["recent_turns"]] == ["user", "assistant"]

"""End to end over the integrated M2 + R4 branches, with synthetic data only.

Fake Claude and Codex stores under a temporary HOME -> ``memfw history
ingest`` (the store module's readers and redacted ingest) -> MCP
``conversations_search`` from one app under per-app visibility -> a handoff
pack -> the asynchronous worker (fake model backend) -> a memory candidate
staged with ``recorded_at`` -> ``memfw dream skills --from-queue`` -> a
skill candidate supported by three sessions of two agents.

No network, no real stores: HOME, the vault and every data file live under
``tmp_path``.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from memfw.cli import app as cli_app
from memfw.conversations import open_worker
from memfw.conversations.adapter import SqliteStoreSource
from memfw.conversations.backends import ModelReply
from memfw.conversations.worker import EpisodeQueue, WorkState
from memfw.dream.skills import SkillOptions, run_skills
from memfw.history.conversations import ConversationStore
from memfw.mcp.server import Gateway
from tests.conversation_fixtures import make_vault

NOW = datetime(2026, 1, 6, 12, 0, tzinfo=UTC)
# Assembled at runtime so the repository holds no credential-shaped literal.
SECRET = "pass" + "word=correct-horse-battery-9"
REQUEST = "Run the release checks for the uploader package before tagging."
COMMANDS = ("uv run pytest -q", "uv run ruff check src", "git commit -m release")


def _jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _claude_session(home: Path, session: str, day: int, *, secret: bool = False) -> None:
    ts = f"2026-01-0{day}T10:00:0"
    base = {"sessionId": session, "cwd": "/work/uploader"}
    rows: list[dict[str, Any]] = [
        base
        | {
            "type": "user",
            "uuid": f"{session}-u0",
            "timestamp": ts + "0Z",
            "message": {"role": "user", "content": REQUEST},
        }
    ]
    for index, command in enumerate(COMMANDS, start=1):
        rows.append(
            base
            | {
                "type": "assistant",
                "uuid": f"{session}-a{index}",
                "timestamp": f"{ts}{index}Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Running the next release check."},
                        {"type": "tool_use", "name": "Bash", "input": {"command": command}},
                    ],
                },
            }
        )
        rows.append(
            base
            | {
                "type": "user",
                "uuid": f"{session}-r{index}",
                "timestamp": f"{ts}{index}Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "content": "ok"}],
                },
            }
        )
    closing = "The uploader release uses signed tags and every check passed."
    if secret:
        closing = f"Saved the deploy note: {SECRET}"
    rows.append(
        base
        | {
            "type": "assistant",
            "uuid": f"{session}-end",
            "timestamp": ts + "9Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": closing}]},
        }
    )
    _jsonl(home / ".claude" / "projects" / "uploader" / f"{session}.jsonl", rows)


def _codex_session(home: Path, session: str, day: int) -> None:
    ts = f"2026-01-0{day}T11:00:00Z"
    rows: list[dict[str, Any]] = [
        {"timestamp": ts, "type": "session_meta", "payload": {"id": session, "cwd": "/work/up"}},
        {
            "timestamp": ts,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": REQUEST}],
            },
        },
    ]
    for command in COMMANDS:
        rows.append(
            {
                "timestamp": ts,
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "shell",
                    "arguments": json.dumps({"command": command}),
                },
            }
        )
        rows.append(
            {
                "timestamp": ts,
                "type": "response_item",
                "payload": {"type": "function_call_output", "output": "ok"},
            }
        )
    rows.append(
        {
            "timestamp": ts,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Release checks passed."}],
            },
        }
    )
    _jsonl(home / ".codex" / "sessions" / "2026" / "01" / f"rollout-{session}.jsonl", rows)


class FakeBackend:
    """A deterministic local "model": fixed summaries, one claim per session."""

    name = "fake"
    local = True

    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(self, task: str, data: dict[str, Any], *, max_tokens: int) -> ModelReply:
        self.calls.append(task)
        if task == "chunk_summary":
            body: dict[str, Any] = {"summary": f"{len(data['turns'])} release-check turns"}
        elif task == "session_summary":
            body = {
                "summary": "ran the uploader release checks",
                "intent": "uploader release checks",
                "outcome": "success",
            }
        else:
            body = {"claims": ["Uploader releases are tagged with signed tags."]}
        return ModelReply(json.dumps(body), 10, 10)


def _judge(claim: str, text: str) -> str:
    return "support" if "signed tags" in text else "neutral"


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("MEMFW_APP", raising=False)
    data = tmp_path / "data"
    vault = make_vault(
        tmp_path / "vault",
        {
            "conversation_store": {"path": str(data / "conversations.sqlite")},
            "conversation_worker": {"state_path": str(data / "work.sqlite")},
            "conversation_share": {
                "enabled": True,
                # claude may read codex only; codex may read nothing.
                "apps": {"claude": {"can_read": ["codex"]}, "codex": {"can_read": False}},
            },
        },
    )
    _claude_session(home, "c-one", 1)
    _claude_session(home, "c-two", 2, secret=True)
    _codex_session(home, "x-one", 3)
    return {"home": home, "vault": vault, "data": data}


def _tree(root: Path) -> list[tuple[str, int]]:
    return sorted((str(p.relative_to(root)), p.stat().st_size) for p in root.rglob("*"))


def test_m2_end_to_end(world: dict[str, Path]) -> None:
    home, vault, data = world["home"], world["vault"], world["data"]
    store_file = data / "conversations.sqlite"
    sources_before = _tree(home)

    # 1. ingest through the store module (CLI), redacted at write time
    result = CliRunner().invoke(
        cli_app, ["history", "ingest", "claude", "codex", "--root", str(vault)]
    )
    assert result.exit_code == 0, result.output
    ingested = json.loads(result.output)
    assert ingested["stats"]["claude"]["sessions"] == 2
    assert ingested["stats"]["codex"]["sessions"] == 1
    assert ingested["stats"]["claude"]["withheld"] == 1
    assert SECRET.encode() not in store_file.read_bytes()
    assert oct(store_file.stat().st_mode & 0o777) == "0o600"
    assert _tree(home) == sources_before  # the CLI stores are only read

    # 2. MCP search from one app, under per-app visibility
    claude_tools = Gateway(vault, app="claude").handlers()
    found = claude_tools["conversations_search"]({"query": "release checks"})
    apps = {hit["app"] for hit in found["results"]}
    assert apps == {"codex"}  # claude may read codex only, not its own sessions
    assert SECRET not in json.dumps(found)
    with pytest.raises(Exception, match="may not read"):
        Gateway(vault, app="codex").handlers()["conversations_search"]({"query": "release"})

    # 3. handoff pack of a codex session for claude
    pack = claude_tools["conversations_handoff"]({"session_id": "x-one", "for_agent": "claude"})
    assert pack["kind"] == "handoff" and pack["for_agent"] == "claude"
    assert pack["system"]["goal"] == REQUEST
    assert "not instructions" in pack["notice"]

    # The store's withheld flag reaches the share as a withheld turn, never as text.
    source = SqliteStoreSource(store_file)
    withheld = [t for t in source.turns("claude", "c-two") if t.withheld]
    assert len(withheld) == 1 and SECRET not in withheld[0].text

    # 4. worker run (ingest via the store module again, then summaries and claims)
    backend = FakeBackend()
    worker = open_worker(vault, backend=backend, judge=_judge)
    worker.now = lambda: NOW
    report = worker.run().as_dict()
    assert report["ingest"]["status"] == "ok"
    assert report["ingest"]["agents"]["claude"]["sessions_unchanged"] == 2
    assert report["sessions_queued"] == 3
    assert report["candidates_staged"] >= 1
    assert report["episodes_queued"] == 3
    assert "memory_claims" in backend.calls

    # 5. the staged memory candidate carries recorded_at (server clock, UTC)
    concept_files = sorted((vault / "_candidates" / "dream").glob("conv-*/*.md"))
    assert concept_files
    before_run = datetime.now(UTC).timestamp() - 600
    for concept in concept_files:
        front = yaml.safe_load(concept.read_text("utf-8").split("---\n")[1])
        recorded = datetime.fromisoformat(front["recorded_at"])
        assert recorded.tzinfo is not None and recorded.timestamp() > before_run
        assert front["created_at"] == NOW.isoformat()  # the injected clock stays created_at
        assert front["authority"] is False
        staged_report = json.loads((concept.parent / "report.json").read_text("utf-8"))
        assert staged_report["recorded_at"] == front["recorded_at"]
        assert SECRET not in concept.read_text("utf-8")

    # 6. skill candidate from the worker's episode queue (>= 3 sessions, 2 agents)
    state = WorkState(data / "work.sqlite")
    assert len(EpisodeQueue(state).pending()) == 3
    dry = run_skills(vault, now=NOW, dry_run=True, options=SkillOptions(from_queue=True))
    assert dry.candidates and len(EpisodeQueue(state).pending()) == 3  # dry run claims nothing
    skills = run_skills(vault, now=NOW, options=SkillOptions(from_queue=True))
    assert skills.report["history"]["source"] == "episode_queue"
    assert skills.report["episodes_acked"] == 3
    assert EpisodeQueue(state).pending() == []
    staged = [c for c in skills.candidates if c.status == "staged"]
    assert len(staged) == 1
    evidence = staged[0].evidence
    assert len(evidence["sessions"]) >= 3
    assert sorted(evidence["agents"]) == ["claude", "codex"]
    signatures = [step["signature"] for step in evidence["steps"]]
    assert "cmd:uv run pytest" in signatures
    skill_dir = vault / staged[0].path
    assert (skill_dir / "SKILL.md").is_file()
    record = json.loads((skill_dir / "candidate.json").read_text("utf-8"))
    assert record["approved"] is False and record["installed"] is False
    assert all(SECRET not in p.read_text("utf-8") for p in skill_dir.iterdir())

    # A rerun reads the consumed episodes again and stages nothing new.
    again = run_skills(vault, now=NOW, options=SkillOptions(from_queue=True))
    assert [c.status for c in again.candidates] == ["exists"]

    # Nothing outside tmp_path was written.
    assert Path.home() == home
    assert ConversationStore(store_file).stats()["codex"]["turns"] > 0
    assert os.environ["HOME"] == str(home)


def _queued(world: dict[str, Path]) -> WorkState:
    vault = world["vault"]
    result = CliRunner().invoke(
        cli_app, ["history", "ingest", "claude", "codex", "--root", str(vault)]
    )
    assert result.exit_code == 0, result.output
    worker = open_worker(vault, backend=FakeBackend(), judge=_judge)
    worker.now = lambda: NOW
    assert worker.run().as_dict()["episodes_queued"] == 3
    return WorkState(world["data"] / "work.sqlite")


def test_queue_claims_are_released_on_failure(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from memfw.dream.skills import run as run_module

    state = _queued(world)

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError("unexpected")

    monkeypatch.setattr(run_module, "stage_candidate", boom)
    with pytest.raises(ValueError):
        run_skills(world["vault"], now=NOW, options=SkillOptions(from_queue=True))
    assert len(EpisodeQueue(state).pending()) == 3  # released, not stuck as claimed

    # A crashed consumer's claim is taken back once it is stale.
    queue = EpisodeQueue(state)
    queue.claim("crashed", 10, now="2020-01-01T00:00:00+00:00")
    assert queue.pending() == []
    monkeypatch.undo()
    result = run_skills(world["vault"], now=NOW, options=SkillOptions(from_queue=True))
    assert result.report["episodes_acked"] == 3


def test_queue_skips_apps_no_longer_processable(world: dict[str, Path]) -> None:
    from tests.conversation_fixtures import set_config

    state = _queued(world)
    repository_config = {
        "conversation_share": {"enabled": True, "apps": {"codex": {"process": False}}}
    }
    set_config(world["vault"], repository_config)
    result = run_skills(world["vault"], now=NOW, options=SkillOptions(from_queue=True))
    assert result.report["sessions_not_processable"] == 1
    assert result.report["sessions_read"] == 2
    assert result.candidates == []  # two sessions are below min_sessions
    assert EpisodeQueue(state).pending() == []

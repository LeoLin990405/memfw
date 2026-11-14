"""Registry, Claude/Codex readers, and the redacted incremental conversation store.

Every fixture is synthetic and written under ``tmp_path``. Secret-shaped
values are assembled at runtime so this file holds none.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from memfw.cli import app
from memfw.history import HistoryError
from memfw.history.conversations import ConversationStore, OptOut
from memfw.history.redact import has_secret
from memfw.history.sources import (
    READERS,
    UnknownAgent,
    agent_names,
    default_roots,
    get_reader,
    store_roots,
    worker_homes,
)
from memfw.history.sources.base import (
    ConversationRecord,
    SessionLocator,
    StoreRoot,
    ToolCall,
    iso_timestamp,
)
from memfw.history.store_location import (
    check_store_path,
    default_store_path,
    prepare_store_path,
    store_path_from_config,
)

GH_TOKEN = "gh" + "p_" + "A1b2C3d4" * 5
HOME_PATH = "/" + "Users" + "/alice/work/demo"


def _jsonl(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _claude_rows() -> list[dict[str, Any]]:
    base = {"sessionId": "sess-c1", "cwd": "/work/demo"}
    return [
        {"type": "summary", "summary": "not a turn"},
        base
        | {
            "type": "user",
            "uuid": "u1",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": "user", "content": "hello from user"},
        },
        base
        | {
            "type": "assistant",
            "uuid": "u2",
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "hidden"},
                    {"type": "text", "text": "running a tool"},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                ],
            },
        },
        base
        | {
            "type": "user",
            "uuid": "u3",
            "timestamp": "2026-01-01T00:00:02Z",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "content": "file-a\nfile-b"}],
            },
        },
    ]


def _codex_rows() -> list[dict[str, Any]]:
    ts = "2026-01-01T00:00:00Z"
    return [
        {"timestamp": ts, "type": "session_meta", "payload": {"id": "sess-x1", "cwd": "/work/a"}},
        {"timestamp": ts, "type": "turn_context", "payload": {"cwd": "/work/b"}},
        {
            "timestamp": ts,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hello codex"}],
            },
        },
        {"timestamp": ts, "type": "event_msg", "payload": {"type": "user_message", "message": "x"}},
        {"timestamp": ts, "type": "response_item", "payload": {"type": "reasoning"}},
        {
            "timestamp": ts,
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell",
                "arguments": json.dumps({"command": ["ls"]}),
            },
        },
        {
            "timestamp": ts,
            "type": "response_item",
            "payload": {"type": "function_call_output", "output": "ok"},
        },
    ]


def test_registry_covers_every_worker_cli() -> None:
    expected = {
        "claude",
        "codex",
        "kimi",
        "grok",
        "cursor",
        "deepseek",
        "minimax",
        "glm",
        "stepfun",
        "opencode",
    }
    assert set(agent_names()) == expected
    for name, reader in READERS.items():
        assert reader.agent == name
        assert reader.default_roots
        assert all(not root.startswith("/") for root in reader.default_roots)
    assert get_reader("zcode") is READERS["glm"]
    assert get_reader("DSH") is READERS["deepseek"]
    with pytest.raises(UnknownAgent):
        get_reader("nope")


def test_default_roots_only_existing_directories(tmp_path: Path) -> None:
    reader = READERS["claude"]
    assert default_roots(reader, tmp_path) == []
    (tmp_path / ".claude" / "projects").mkdir(parents=True)
    assert default_roots(reader, tmp_path) == [tmp_path / ".claude" / "projects"]


def test_iso_timestamp_units() -> None:
    assert iso_timestamp(1_700_000_000) == "2023-11-14T22:13:20Z"
    assert iso_timestamp(1_700_000_000_000) == "2023-11-14T22:13:20Z"
    assert iso_timestamp("2026-01-01T00:00:00Z") == "2026-01-01T00:00:00Z"
    assert iso_timestamp(True) == ""
    assert iso_timestamp(None) == ""


def test_claude_reader(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    path = _jsonl(root / "-work-demo" / "sess-c1.jsonl", _claude_rows())
    reader = READERS["claude"]
    [locator] = list(reader.sessions(root))
    assert locator.key == "-work-demo/sess-c1.jsonl"
    records = list(reader.read(locator))
    assert [r.role for r in records] == ["user", "assistant", "tool"]
    assert [r.turn for r in records] == [0, 1, 2]
    assert records[1].text == "running a tool"
    assert records[1].tool_calls[0].name == "Bash"
    assert records[1].tool_calls[0].summary == "command: ls"
    assert all(r.session_id == "sess-c1" and r.cwd == "/work/demo" for r in records)
    assert "hidden" not in "".join(r.text for r in records)
    resumed = list(reader.read(locator, after=records[1].offset))
    assert [r.turn for r in resumed] == [2]
    path.write_text(path.read_text() + "{broken\n", encoding="utf-8")
    assert len(list(reader.read(locator))) == 3


def test_codex_reader(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    _jsonl(root / "2026" / "01" / "01" / "rollout-1.jsonl", _codex_rows())
    reader = READERS["codex"]
    [locator] = list(reader.sessions(root))
    assert locator.session_id == "sess-x1"
    records = list(reader.read(locator))
    assert [r.role for r in records] == ["user", "assistant", "tool"]
    assert records[0].text == "hello codex"
    assert records[0].cwd == "/work/b"
    assert records[1].tool_calls[0].name == "shell"
    assert records[2].text == "ok"
    assert [r.turn for r in reader.read(locator, after=records[0].offset)] == [1, 2]


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test runs with HOME (and Path.home) pointed at a temp directory."""

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


class _ListReader:
    """A reader over an in-memory list, to drive the store directly."""

    agent = "fake"
    parser_version = "1"
    append_only = True
    default_roots: tuple[str, ...] = ("fake",)

    def __init__(self, path: Path) -> None:
        self.path = path
        self.records: list[ConversationRecord] = []
        self.calls: list[int] = []

    def sessions(self, root: Path) -> Any:
        yield SessionLocator(self.agent, self.path, self.path.name, "s1")

    def read(self, locator: SessionLocator, *, after: int = -1) -> Any:
        self.calls.append(after)
        return (r for r in self.records if r.offset > after)


def _record(offset: int, text: str, **extra: Any) -> ConversationRecord:
    return ConversationRecord("fake", "s1", offset, offset, "user", text, **extra)


def _store(tmp_path: Path, **kwargs: Any) -> ConversationStore:
    return ConversationStore(tmp_path / "data" / "conv.sqlite", **kwargs)


def _all_stored_values(db: Path) -> list[str]:
    with sqlite3.connect(db) as connection:
        rows = connection.execute(
            "SELECT text, tool_calls, cwd, project, model, session_id FROM conversation_turns"
        ).fetchall()
    return [value for row in rows for value in row if value]


def test_store_incremental_dedup_withheld_and_redaction(tmp_path: Path) -> None:
    data = tmp_path / "src" / "s1.log"
    data.parent.mkdir()
    data.write_text("a", encoding="utf-8")
    reader = _ListReader(data)
    reader.records = [
        _record(0, f"token {GH_TOKEN} here"),
        _record(10, "second turn", cwd=HOME_PATH, model="model-x"),
        _record(15, "tool turn", tool_calls=(ToolCall("Bash", f"echo {GH_TOKEN}"),)),
    ]
    store = _store(tmp_path)
    first = store.ingest(reader, [data.parent])
    assert (first.records_new, first.records_withheld, first.sessions_read) == (3, 2, 1)

    again = store.ingest(reader, [data.parent])
    assert again.sessions_unchanged == 1 and reader.calls == [-1]

    data.write_text("ab", encoding="utf-8")
    reader.records.append(_record(20, "third turn"))
    grown = store.ingest(reader, [data.parent])
    assert reader.calls[-1] == 14
    assert (grown.records_new, grown.records_duplicate) == (1, 1)

    data.write_text("abc", encoding="utf-8")
    reader.records[-1] = _record(20, "third turn, finished")
    assert store.ingest(reader, [data.parent]).records_updated == 1

    turns = store.turns("fake", "s1")
    assert [t["offset"] for t in turns] == [0, 10, 15, 20]
    assert [t["withheld"] for t in turns] == [1, 0, 1, 0]
    assert turns[0]["text"] == "" and turns[2]["tool_calls"] == []
    assert turns[1]["model"] == "model-x" and "alice" not in turns[1]["cwd"]
    assert turns[3]["text"] == "third turn, finished"
    raw = (tmp_path / "data" / "conv.sqlite").read_bytes()
    assert GH_TOKEN.encode() not in raw and b"alice" not in raw
    assert not any(has_secret(v) for v in _all_stored_values(store.db))
    assert store.stats() == {"fake": {"turns": 4, "sessions": 1, "withheld": 2}}


def test_store_rewrite_replaces_after_read(tmp_path: Path) -> None:
    data = tmp_path / "s1.log"
    data.write_text("abc", encoding="utf-8")
    reader = _ListReader(data)
    reader.records = [_record(0, "one"), _record(10, "two"), _record(20, "three")]
    store = _store(tmp_path)
    store.ingest(reader, [tmp_path])
    # Shrunk: full re-read; offsets gone from the source are dropped afterwards.
    data.write_text("z", encoding="utf-8")
    reader.records = [_record(0, "rewritten")]
    counts = store.ingest(reader, [tmp_path])
    assert reader.calls[-1] == -1 and counts.records_dropped == 2
    assert [t["text"] for t in store.turns("fake", "s1")] == ["rewritten"]
    # Same input from zero gives the same rows.
    fresh = ConversationStore(tmp_path / "fresh" / "conv.sqlite")
    fresh.ingest(reader, [tmp_path])
    strip = ("locator",)
    assert [{k: v for k, v in t.items() if k not in strip} for t in fresh.turns("fake", "s1")] == [
        {k: v for k, v in t.items() if k not in strip} for t in store.turns("fake", "s1")
    ]


def test_store_non_append_only_rereads_everything(tmp_path: Path) -> None:
    data = tmp_path / "s1.db"
    data.write_text("a", encoding="utf-8")
    reader = _ListReader(data)
    reader.append_only = False
    reader.records = [_record(0, "one"), _record(1, "two")]
    store = _store(tmp_path)
    store.ingest(reader, [tmp_path])
    data.write_text("ab", encoding="utf-8")
    reader.records = [_record(0, "one, edited")]  # an undo removed turn 1
    store.ingest(reader, [tmp_path])
    assert reader.calls == [-1, -1]
    assert [t["text"] for t in store.turns("fake", "s1")] == ["one, edited"]


def test_store_keeps_rows_when_reread_is_empty(tmp_path: Path) -> None:
    data = tmp_path / "s1.db"
    data.write_text("a", encoding="utf-8")
    reader = _ListReader(data)
    reader.append_only = False
    reader.records = [_record(0, "one")]
    store = _store(tmp_path)
    store.ingest(reader, [tmp_path])
    data.write_text("ab", encoding="utf-8")
    reader.records = []  # locked / unreadable this round
    assert store.ingest(reader, [tmp_path]).sessions_busy == 1
    assert len(store.turns("fake", "s1")) == 1
    reader.records = [_record(0, "one"), _record(1, "two")]
    assert store.ingest(reader, [tmp_path]).records_new == 1


def test_store_survives_failing_sessions_and_roots(tmp_path: Path) -> None:
    data = tmp_path / "s.log"
    data.write_text("a", encoding="utf-8")

    class Broken(_ListReader):
        def read(self, locator: SessionLocator, *, after: int = -1) -> Any:
            yield _record(0, "ok")
            raise RuntimeError("boom")

    class BrokenRoot(_ListReader):
        def sessions(self, root: Path) -> Any:
            raise RuntimeError("boom")

    store = _store(tmp_path)
    assert store.ingest(Broken(data), [tmp_path]).sessions_failed == 1
    assert store.turns("fake", "s1") == []
    assert store.ingest(BrokenRoot(data), [tmp_path]).roots_failed == 1
    with sqlite3.connect(store.db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM conversation_cursors").fetchone() == (0,)


def test_store_optout_agents_cwd_globs_and_marker(tmp_path: Path) -> None:
    data = tmp_path / "s1.log"
    data.write_text("a", encoding="utf-8")
    reader = _ListReader(data)
    reader.records = [_record(0, "hi", cwd="/work/private-x")]
    optout = OptOut.from_config({"conversation_optout": {"agents": ["Fake"]}})
    assert _store(tmp_path, optout=optout).ingest(reader, [tmp_path]).extra["opted_out"]
    globbed = OptOut.from_config({"conversation_optout": {"cwd_globs": ["*/private-*"]}})
    counts = _store(tmp_path, optout=globbed).ingest(reader, [tmp_path])
    assert counts.sessions_opted_out == 1
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".memfw-no-history").write_text("", encoding="utf-8")
    data.write_text("ab", encoding="utf-8")
    reader.records = [_record(0, "hi", cwd=str(project))]
    assert _store(tmp_path).ingest(reader, [tmp_path]).sessions_opted_out == 1
    assert _store(tmp_path).stats() == {}
    with pytest.raises(HistoryError):
        OptOut.from_config({"conversation_optout": {"agents": "fake"}})


def test_store_location_rules(tmp_path: Path, isolated_home: Path) -> None:
    assert default_store_path() == isolated_home / ".local/share/memfw/conversations.sqlite"
    assert store_path_from_config({}) == default_store_path()
    custom = store_path_from_config({"conversation_store": {"path": "~/x/c.sqlite"}})
    assert custom == isolated_home / "x/c.sqlite"
    vault = tmp_path / "vault"
    vault.mkdir()
    with pytest.raises(HistoryError, match="vault"):
        check_store_path(vault / "c.sqlite", vault_root=vault)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(HistoryError, match="git"):
        check_store_path(repo / "deep" / "c.sqlite")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    with pytest.raises(HistoryError, match="git"):
        check_store_path(worktree / "c.sqlite")
    for synced in ("Library/Mobile Documents/x", "Dropbox", "Library/CloudStorage/GoogleDrive-a"):
        with pytest.raises(HistoryError, match="synced"):
            check_store_path(tmp_path / synced / "c.sqlite")
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / ".stfolder").mkdir()
    with pytest.raises(HistoryError, match="synced"):
        check_store_path(shared / "c.sqlite")
    good = check_store_path(default_store_path(), vault_root=vault)
    prepare_store_path(good)
    assert good.stat().st_mode & 0o777 == 0o600
    assert good.parent.stat().st_mode & 0o777 == 0o700


def test_worker_homes_are_roots(isolated_home: Path) -> None:
    (isolated_home / ".grok" / "sessions").mkdir(parents=True)
    worker = isolated_home / ".conductor-daemon" / "cli-state" / "abc123" / "home"
    (worker / ".grok" / "sessions").mkdir(parents=True)
    assert worker_homes() == [worker]
    roots = store_roots(READERS["grok"])
    assert roots == [
        StoreRoot(isolated_home / ".grok" / "sessions", "interactive"),
        StoreRoot(worker / ".grok" / "sessions", "worker"),
    ]
    assert roots[0].label != roots[1].label


def test_cli_agents_and_ingest(tmp_path: Path, isolated_home: Path) -> None:
    home = isolated_home
    _jsonl(home / ".claude" / "projects" / "-work" / "sess-c1.jsonl", _claude_rows())
    _jsonl(home / ".codex" / "sessions" / "rollout-1.jsonl", _codex_rows())
    worker = home / ".conductor-daemon" / "cli-state" / "w1" / "home"
    _jsonl(worker / ".codex" / "sessions" / "rollout-2.jsonl", _codex_rows())
    runner = CliRunner()

    listed = runner.invoke(app, ["history", "agents"])
    assert listed.exit_code == 0, listed.output
    payload = json.loads(listed.output)
    found = {item["agent"]: item["found"] for item in payload["agents"]}
    assert found["claude"] == [{"origin": "interactive", "path": "~/.claude/projects"}]
    assert [f["origin"] for f in found["codex"]] == ["interactive", "worker"]
    assert found["kimi"] == []
    assert str(home) not in listed.output

    vault = tmp_path / "vault"
    vault.mkdir()
    result = runner.invoke(app, ["history", "ingest", "claude", "codex", "--root", str(vault)])
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    # Two codex rollouts with the same session id dedupe onto one session.
    assert out["stats"]["claude"]["turns"] == 3
    assert out["stats"]["codex"]["sessions"] == 1
    assert out["ingested"]["codex"]["roots"] == {"interactive": 1, "worker": 1}
    assert (home / ".local/share/memfw/conversations.sqlite").is_file()
    assert not (vault / ".memfw-conversations.sqlite").exists()

    again = runner.invoke(app, ["history", "ingest", "claude", "--root", str(vault)])
    assert json.loads(again.output)["ingested"]["claude"]["sessions_unchanged"] == 1

    inside = runner.invoke(
        app, ["history", "ingest", "claude", "--root", str(vault), "--db", str(vault / "c.db")]
    )
    assert inside.exit_code == 2 and "vault" in inside.output
    (vault / "vault.yaml").write_text(
        "conversation_optout:\n  agents: [claude]\n", encoding="utf-8"
    )
    opted = runner.invoke(app, ["history", "ingest", "claude", "--root", str(vault)])
    assert json.loads(opted.output)["ingested"]["claude"]["opted_out"] is True

    bad = runner.invoke(app, ["history", "ingest", "nope", "--root", str(vault)])
    assert bad.exit_code == 2
    two = runner.invoke(
        app, ["history", "ingest", "claude", "codex", "--path", str(home), "--root", str(vault)]
    )
    assert two.exit_code == 2


def test_optout_forgets_stored_rows_and_rule_changes_force_reread(tmp_path: Path) -> None:
    data = tmp_path / "s1.log"
    data.write_text("a", encoding="utf-8")
    reader = _ListReader(data)
    reader.records = [_record(0, "hi", cwd="/work/private-x"), _record(5, "ok", cwd="/work/a")]
    _store(tmp_path).ingest(reader, [tmp_path])
    assert len(_store(tmp_path).turns("fake", "s1")) == 2
    # A cwd rule added later applies to the unchanged session too.
    globbed = OptOut(cwd_globs=("*/private-*",))
    assert _store(tmp_path, optout=globbed).ingest(reader, [tmp_path]).sessions_opted_out == 1
    assert _store(tmp_path).turns("fake", "s1") == []
    # Opting the whole agent out deletes its rows and cursors.
    _store(tmp_path).ingest(reader, [tmp_path])
    assert len(_store(tmp_path).turns("fake", "s1")) == 2
    _store(tmp_path, optout=OptOut(agents=frozenset({"fake"}))).ingest(reader, [tmp_path])
    assert _store(tmp_path).stats() == {}
    with sqlite3.connect(tmp_path / "data" / "conv.sqlite") as connection:
        assert connection.execute("SELECT COUNT(*) FROM conversation_cursors").fetchone() == (0,)


def test_locators_are_opaque_and_mirrored_roots_dedupe(tmp_path: Path) -> None:
    first = tmp_path / "home-a" / "secret-project-name.log"
    second = tmp_path / "home-b" / "secret-project-name.log"
    for path in (first, second):
        path.parent.mkdir()
        path.write_text("a", encoding="utf-8")
    store = _store(tmp_path)
    reader_a, reader_b = _ListReader(first), _ListReader(second)
    reader_a.records = reader_b.records = [_record(0, "same turn")]
    store.ingest(reader_a, [StoreRoot(first.parent, "interactive")])
    mirrored = store.ingest(reader_b, [StoreRoot(second.parent, "worker")])
    assert mirrored.records_duplicate == 1 and mirrored.records_updated == 0
    with sqlite3.connect(store.db) as connection:
        locators = [r[0] for r in connection.execute("SELECT locator FROM conversation_cursors")]
    assert len(locators) == 2
    assert not any("secret-project-name" in item for item in locators)

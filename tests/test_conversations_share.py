"""Read-only sharing of the unified conversation store (CLI + MCP)."""

from __future__ import annotations

import json
import os
import plistlib
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from memfw.cli import app as cli_app
from memfw.conversations import ConversationError, SharePolicy, open_share
from memfw.conversations.adapter import SqliteStoreSource, store_path
from memfw.conversations.schedule import TEMPLATE, render
from memfw.conversations.share import ConversationShare
from memfw.conversations.store import WITHHELD, check_private_location
from memfw.history.redact import has_secret
from memfw.mcp.server import TOOLS, Gateway
from tests.conversation_fixtures import (
    EMAIL,
    SECRET,
    isolated_home,  # noqa: F401  (fixture)
    make_vault,
    standard_rows,
    turn,
    write_store,
)

REAL_HOME = Path(os.path.expanduser("~"))
REAL_DATA = REAL_HOME / ".local" / "share" / "memfw"
REAL_AGENTS = REAL_HOME / "Library" / "LaunchAgents"


def _snapshot() -> tuple[Any, ...]:
    def state(path: Path) -> Any:
        try:
            return sorted(entry.name for entry in path.iterdir())
        except OSError:
            return None

    return state(REAL_DATA), state(REAL_AGENTS)


@pytest.fixture(autouse=True)
def real_home_untouched(isolated_home: Path) -> Any:  # noqa: F811
    before = _snapshot()
    yield
    assert Path.home() == isolated_home
    assert _snapshot() == before


SHARE_ON = {"conversation_share": {"enabled": True}}


def _setup(tmp_path: Path, share: dict[str, Any] | None = None) -> tuple[Path, Path]:
    vault = make_vault(tmp_path / "vault", share if share is not None else SHARE_ON)
    store = write_store(tmp_path / "data" / "conversations.sqlite", standard_rows())
    return vault, store


def _share(tmp_path: Path, caller: str = "claude", **policy: Any) -> ConversationShare:
    vault, store = _setup(tmp_path, {"conversation_share": {"enabled": True, **policy}})
    return open_share(vault, caller, store=store)


def _all_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _all_strings(item)]
    if isinstance(value, list):
        return [s for item in value for s in _all_strings(item)]
    return []


# -- policy ------------------------------------------------------------------------


def test_share_is_off_unless_enabled(tmp_path: Path) -> None:
    vault, store = _setup(tmp_path, {})
    share = open_share(vault, "claude", store=store)
    with pytest.raises(ConversationError, match="sharing is off"):
        share.search("uploader")


def test_policy_rules_and_scope() -> None:
    policy = SharePolicy.from_config(
        {
            "conversation_share": {
                "enabled": True,
                "apps": {
                    "cursor": {"readable": False},
                    "grok": {"can_read": False},
                    "kimi": {"can_read": ["codex", "cursor"]},
                },
            },
            "conversation_optout": {"agents": ["stepfun"]},
        }
    )
    assert policy.scope("claude") == (None, ("cursor", "stepfun"))
    assert policy.scope("kimi") == (("codex",), ("cursor", "stepfun"))
    with pytest.raises(ConversationError, match="may not read"):
        policy.scope("grok")
    with pytest.raises(ConversationError, match="opted out"):
        policy.scope("stepfun")
    assert not policy.processable("stepfun")
    with pytest.raises(ConversationError):
        SharePolicy.from_config({"conversation_share": {"apps": {"x": {"can_read": "yes"}}}})


def test_hidden_app_is_never_returned(tmp_path: Path) -> None:
    share = _share(tmp_path, apps={"cursor": {"readable": False}})
    found = share.search("uploader", limit=50)
    assert {item["app"] for item in found["results"]} == {"codex", "claude"}
    assert "cursor" not in {item["app"] for item in share.list_sessions()["sessions"]}
    assert share.search("uploader", app="cursor")["results"] == []
    with pytest.raises(ConversationError, match="session not found"):
        share.get("s-gamma")
    with pytest.raises(ConversationError, match="session not found"):
        share.handoff("s-gamma", app="cursor")


def test_optout_app_hidden_and_cannot_read(tmp_path: Path) -> None:
    vault, store = _setup(tmp_path, {**SHARE_ON, "conversation_optout": {"agents": ["claude"]}})
    share = open_share(vault, "codex", store=store)
    assert {item["app"] for item in share.search("uploader")["results"]} == {"codex", "cursor"}
    with pytest.raises(ConversationError, match="opted out"):
        open_share(vault, "claude", store=store).search("uploader")


def test_reader_limited_to_listed_apps(tmp_path: Path) -> None:
    share = _share(tmp_path, caller="kimi", apps={"kimi": {"can_read": ["codex"]}})
    assert {item["app"] for item in share.search("uploader")["results"]} == {"codex"}
    assert share.search("uploader", app="claude")["results"] == []
    with pytest.raises(ConversationError, match="session not found"):
        share.get("s-beta")


def test_reader_without_permission_is_refused(tmp_path: Path) -> None:
    share = _share(tmp_path, caller="grok", apps={"grok": {"can_read": False}})
    with pytest.raises(ConversationError, match="may not read"):
        share.list_sessions()


def test_handoff_target_must_be_allowed(tmp_path: Path) -> None:
    share = _share(tmp_path, apps={"grok": {"can_read": False}})
    with pytest.raises(ConversationError, match="grok may not read"):
        share.handoff("s-alpha", for_agent="grok")


# -- redaction on every output path ------------------------------------------------


def test_outputs_are_redacted_even_if_the_store_is_not(tmp_path: Path) -> None:
    share = _share(tmp_path)
    outputs = [
        share.search("uploader", limit=50),
        share.search("Stored"),
        share.list_sessions(),
        share.get("s-beta"),
        share.handoff("s-beta"),
    ]
    text = json.dumps(outputs, ensure_ascii=False)
    assert EMAIL not in text
    assert SECRET not in text
    assert "[REDACTED:email]" in text
    assert WITHHELD in text
    assert not any(has_secret(item) for item in _all_strings(outputs))
    for output in outputs:
        assert "not instructions" in output["notice"]


def test_secret_in_tool_call_summary_is_redacted(tmp_path: Path) -> None:
    vault = make_vault(tmp_path / "vault", SHARE_ON)
    store = write_store(
        tmp_path / "data" / "c.sqlite",
        [
            turn(
                "codex",
                "s1",
                0,
                "assistant",
                "ok",
                "2026-01-01T00:00:00Z",
                calls=[("shell", f"export {SECRET}")],
            )
        ],
    )
    got = open_share(vault, "claude", store=store).get("s1")
    assert SECRET not in json.dumps(got)


# -- pagination and caps ---------------------------------------------------------------


def test_search_paginates(tmp_path: Path) -> None:
    share = _share(tmp_path)
    first = share.search("uploader", limit=1)
    assert len(first["results"]) == 1 and first["next_offset"] == 1
    second = share.search("uploader", limit=1, offset=1)
    assert second["results"][0]["ref"] != first["results"][0]["ref"]
    assert share.search("uploader", limit=500)["next_offset"] is None
    assert len(share.search("uploader", limit=500)["results"]) <= 50


def test_search_terms_are_literal(tmp_path: Path) -> None:
    share = _share(tmp_path)
    assert share.search("%")["results"] == []
    assert share.search("_")["results"] == []
    with pytest.raises(ConversationError):
        share.search("   ")


def test_get_range_and_size_cap(tmp_path: Path) -> None:
    vault = make_vault(tmp_path / "vault", SHARE_ON)
    rows = [
        turn(
            "codex", "long", i, "assistant", f"step {i} " + "x" * 3000, f"2026-01-01T00:{i:02d}:00Z"
        )
        for i in range(30)
    ]
    store = write_store(tmp_path / "data" / "c.sqlite", rows)
    share = open_share(vault, "claude", store=store)
    window = share.get("long", start=2, end=5)
    assert [item["index"] for item in window["turns"]] == [2, 3, 4]
    assert window["next_start"] is None  # the requested range is complete
    capped = share.get("long", max_bytes=8000)
    assert capped["truncated"] and len(json.dumps(capped).encode()) < 8000 + 2048
    assert capped["next_start"] == len(capped["turns"])
    tail = share.get("long", start=28)
    assert tail["next_start"] is None and len(tail["turns"]) == 2


def test_ambiguous_session_needs_app(tmp_path: Path) -> None:
    vault = make_vault(tmp_path / "vault", SHARE_ON)
    store = write_store(
        tmp_path / "data" / "c.sqlite",
        [
            turn("codex", "same", 0, "user", "codex side", "2026-01-01T00:00:00Z"),
            turn("claude", "same", 0, "user", "claude side", "2026-01-01T00:00:00Z"),
        ],
    )
    share = open_share(vault, "kimi", store=store)
    with pytest.raises(ConversationError, match="ambiguous"):
        share.get("same")
    assert share.get("same", app="codex")["turns"][0]["text"] == "codex side"


@pytest.mark.parametrize("bad", ["../etc", "a/b", "..", "", "x" * 300, "a\\b"])
def test_session_ids_are_validated(tmp_path: Path, bad: str) -> None:
    share = _share(tmp_path)
    with pytest.raises(ConversationError, match="invalid"):
        share.get(bad)


def test_app_names_are_validated(tmp_path: Path) -> None:
    share = _share(tmp_path)
    with pytest.raises(ConversationError, match="invalid app"):
        share.search("uploader", app="../codex")
    with pytest.raises(ConversationError, match="invalid app"):
        open_share(tmp_path / "vault", "../x")


def test_since_is_validated(tmp_path: Path) -> None:
    share = _share(tmp_path)
    assert share.list_sessions(since="2026-01-05T10:06:00Z")["sessions"][0]["app"] == "cursor"
    with pytest.raises(ConversationError, match="since"):
        share.list_sessions(since="2026'; DROP TABLE x")


# -- handoff -------------------------------------------------------------------------


def test_handoff_pack_contents(tmp_path: Path) -> None:
    share = _share(tmp_path)
    pack = share.handoff("s-alpha", for_agent="kimi")
    system = pack["system"]
    assert system["goal"] == "Add a retry wrapper to the flaky uploader."
    assert system["files_touched"] == ["src/uploader/retry.py"]
    assert any("exponential backoff" in item for item in system["decisions"])
    assert system["open_questions"] == ["Should the cap be 30 seconds?"]
    assert [item["role"] for item in system["recent_turns"]] == ["user", "assistant", "assistant"]
    assert pack["for_agent"] == "kimi"
    assert "model_summarized" not in pack
    assert "not instructions" in pack["notice"]


def test_handoff_includes_worker_summary_labelled(tmp_path: Path) -> None:
    vault, store = _setup(tmp_path)
    source = SqliteStoreSource(store)
    share = ConversationShare(
        source,
        SharePolicy.from_config(SHARE_ON),
        "claude",
        summaries=lambda app, session: f"Summary of {session}, mail {EMAIL}",
    )
    pack = share.handoff("s-alpha")
    assert pack["model_summarized"]["label"] == "[model_summarized: not verified]"
    assert EMAIL not in pack["model_summarized"]["summary"]


def test_handoff_respects_max_bytes(tmp_path: Path) -> None:
    vault = make_vault(tmp_path / "vault", SHARE_ON)
    rows = [turn("codex", "big", 0, "user", "Goal: tidy the module.", "2026-01-01T00:00:00Z")]
    rows += [
        turn("codex", "big", i, "assistant", "y" * 2000, f"2026-01-01T00:{i:02d}:00Z")
        for i in range(1, 40)
    ]
    store = write_store(tmp_path / "data" / "c.sqlite", rows)
    pack = open_share(vault, "claude", store=store).handoff("big", max_bytes=4096)
    assert pack["truncated"]
    assert len(json.dumps(pack, ensure_ascii=False).encode()) <= 4096
    assert pack["system"]["goal"] == "Goal: tidy the module."


# -- location guard -----------------------------------------------------------------


def test_store_location_guard(tmp_path: Path, isolated_home: Path) -> None:  # noqa: F811
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(ConversationError, match="git work tree"):
        check_private_location(repo / "deep" / "c.sqlite")
    vault = make_vault(tmp_path / "vault")
    with pytest.raises(ConversationError, match="vault"):
        store_path({"conversation_store": {"path": str(vault / "c.sqlite")}}, vault=vault)
    synced = isolated_home / "Library" / "Mobile Documents" / "x"
    with pytest.raises(ConversationError, match="synced"):
        check_private_location(synced / "c.sqlite")
    extra = tmp_path / "nas"
    with pytest.raises(ConversationError, match="synced"):
        store_path(
            {"conversation_store": {"path": str(extra / "c.sqlite"), "synced_dirs": [str(extra)]}}
        )
    default = store_path({})
    assert default == (isolated_home / ".local/share/memfw/conversations.sqlite").resolve()
    with pytest.raises(ConversationError, match="git work tree"):
        open_share(vault, "claude", store=repo / "c.sqlite")


def test_missing_store_is_reported(tmp_path: Path) -> None:
    vault = make_vault(tmp_path / "vault", SHARE_ON)
    share = open_share(vault, "claude", store=tmp_path / "data" / "absent.sqlite")
    with pytest.raises(ConversationError, match="not available"):
        share.search("x")
    assert not (tmp_path / "data" / "absent.sqlite").exists()


# -- MCP ------------------------------------------------------------------------------


def _call(gateway: Gateway, name: str, arguments: dict[str, Any], capsys: Any) -> dict[str, Any]:
    gateway.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    payload = json.loads(capsys.readouterr().out)
    return dict(payload["result"])


def test_mcp_tools_are_read_only_and_listed() -> None:
    names = {tool["name"] for tool in TOOLS if tool["name"].startswith("conversations_")}
    assert names == {
        "conversations_search",
        "conversations_list",
        "conversations_get",
        "conversations_handoff",
    }


def test_mcp_conversation_tools(tmp_path: Path, capsys: Any, monkeypatch: Any) -> None:
    vault, store = _setup(tmp_path)
    monkeypatch.delenv("MEMFW_APP", raising=False)
    no_app = _call(Gateway(vault), "conversations_search", {"query": "uploader"}, capsys)
    assert no_app["isError"] and "calling app" in no_app["content"][0]["text"]

    set_store = {"conversation_store": {"path": str(store)}, **SHARE_ON}
    make_vault(vault, set_store)
    monkeypatch.setenv("MEMFW_APP", "kimi")
    gateway = Gateway(vault)
    found = _call(gateway, "conversations_search", {"query": "uploader", "limit": 2}, capsys)
    assert not found["isError"]
    body = json.loads(found["content"][0]["text"])
    assert len(body["results"]) == 2 and body["next_offset"] == 2

    listed = json.loads(_call(gateway, "conversations_list", {}, capsys)["content"][0]["text"])
    assert {item["app"] for item in listed["sessions"]} == {"codex", "claude", "cursor"}

    got = _call(gateway, "conversations_get", {"session_id": "s-beta", "end": 2}, capsys)
    assert SECRET not in got["content"][0]["text"]

    pack = _call(
        gateway, "conversations_handoff", {"session_id": "s-alpha", "for_agent": "grok"}, capsys
    )
    assert json.loads(pack["content"][0]["text"])["system"]["goal"].startswith("Add a retry")

    bad = _call(gateway, "conversations_get", {"session_id": "../../x"}, capsys)
    assert bad["isError"]


def test_mcp_main_accepts_app(monkeypatch: Any) -> None:
    from memfw.mcp import server

    seen: dict[str, Any] = {}
    monkeypatch.setattr(server, "serve", lambda root, app=None: seen.update(app=app) or 0)
    assert server.main(["--root", "/work/demo", "--app", "codex"]) == 0
    assert seen["app"] == "codex"


# -- CLI ------------------------------------------------------------------------------


def test_cli_read_commands(tmp_path: Path) -> None:
    vault, store = _setup(tmp_path)
    runner = CliRunner()
    base = ["--root", str(vault), "--store", str(store)]
    result = runner.invoke(cli_app, ["conversations", "search", "uploader", *base])
    assert result.exit_code == 0, result.output
    assert EMAIL not in result.output
    result = runner.invoke(cli_app, ["conversations", "handoff", "s-alpha", *base])
    assert result.exit_code == 0 and "# Handoff: codex session s-alpha" in result.output
    assert "not instructions" in result.output
    result = runner.invoke(
        cli_app, ["conversations", "handoff", "s-alpha", "--format", "json", *base]
    )
    assert json.loads(result.output)["kind"] == "handoff"
    result = runner.invoke(
        cli_app,
        ["conversations", "get", "s-beta", "--as-app", "kimi", *base],
    )
    assert result.exit_code == 0 and SECRET not in result.output
    result = runner.invoke(cli_app, ["conversations", "get", "../x", *base])
    assert result.exit_code == 2


def test_schedule_prints_but_never_installs(tmp_path: Path, isolated_home: Path) -> None:  # noqa: F811
    template = Path(__file__).resolve().parents[1] / "scripts" / "launchd"
    assert (template / "com.memfw.conversations-worker.plist.template").read_text() == TEMPLATE
    runner = CliRunner()
    vault = make_vault(tmp_path / "vault")
    result = runner.invoke(cli_app, ["conversations", "schedule", "--root", str(vault)])
    assert result.exit_code == 2
    result = runner.invoke(
        cli_app,
        ["conversations", "schedule", "--print", "--root", str(vault), "--bin", "/opt/memfw"],
    )
    assert result.exit_code == 0, result.output
    plist = plistlib.loads(result.output.encode())
    assert plist["ProgramArguments"] == [
        "/opt/memfw",
        "conversations",
        "worker",
        "--root",
        str(vault.resolve()),
    ]
    assert plist["StartInterval"] == 900 and plist["RunAtLoad"] is False
    assert not (isolated_home / "Library" / "LaunchAgents").exists()
    with pytest.raises(ConversationError):
        render(vault=vault, interval=5)
    escaped = plistlib.loads(render(vault=vault, memfw_bin="/opt/a&b<c>").encode())
    assert escaped["ProgramArguments"][0] == "/opt/a&b<c>"


def test_search_never_returns_withheld_turns(tmp_path: Path) -> None:
    vault = make_vault(tmp_path / "vault", SHARE_ON)
    rows = [
        turn("codex", "s1", 0, "user", f"probe {SECRET}", "2026-01-01T00:00:00Z"),
        {**turn("codex", "s1", 1, "user", "", "2026-01-01T00:01:00Z"), "withheld": 1},
    ]
    store = write_store(tmp_path / "data" / "c.sqlite", rows)
    share = open_share(vault, "claude", store=store)
    assert share.search("probe")["results"] == []
    assert share.search("correct-horse")["results"] == []


def test_pagination_counts_only_visible_rows(tmp_path: Path) -> None:
    vault = make_vault(
        tmp_path / "vault",
        {"conversation_share": {"enabled": True, "apps": {"kimi": {"can_read": ["codex"]}}}},
    )
    rows = [
        turn(
            "claude" if i % 2 else "codex",
            f"s{i}",
            0,
            "user",
            f"needle {i}",
            f"2026-01-01T00:{i:02d}:00Z",
        )
        for i in range(20)
    ]
    store = write_store(tmp_path / "data" / "c.sqlite", rows)
    share = open_share(vault, "kimi", store=store)
    seen: list[str] = []
    offset: int | None = 0
    while offset is not None:
        page = share.search("needle", limit=3, offset=offset)
        assert len(page["results"]) == 3 or page["next_offset"] is None
        seen += [item["session_id"] for item in page["results"]]
        offset = page["next_offset"]
    assert len(seen) == 10 == len(set(seen))
    listed = share.list_sessions(limit=4)
    assert len(listed["sessions"]) == 4 and listed["next_offset"] is not None


def test_get_cap_holds_for_multibyte_text(tmp_path: Path) -> None:
    vault = make_vault(tmp_path / "vault", SHARE_ON)
    calls = [("shell", "汉" * 150)] * 20
    rows = [turn("codex", "cjk", 0, "user", "汉字" * 4000, "2026-01-01T00:00:00Z", calls=calls)]
    store = write_store(tmp_path / "data" / "c.sqlite", rows)
    share = open_share(vault, "claude", store=store)
    for cap in (4096, 8192, 16384):
        got = share.get("cjk", max_bytes=cap)
        assert len(json.dumps(got, ensure_ascii=False).encode()) <= cap
        assert got["truncated"]


def test_mcp_rejects_the_operator_name(tmp_path: Path, capsys: Any, monkeypatch: Any) -> None:
    vault, store = _setup(tmp_path)
    make_vault(vault, {"conversation_store": {"path": str(store)}, **SHARE_ON})
    monkeypatch.setenv("MEMFW_APP", "operator")
    result = _call(Gateway(vault), "conversations_list", {}, capsys)
    assert result["isError"] and "reserved" in result["content"][0]["text"]


def test_handoff_target_status_is_not_disclosed(tmp_path: Path) -> None:
    vault, store = _setup(tmp_path, {**SHARE_ON, "conversation_optout": {"agents": ["grok"]}})
    share = open_share(vault, "claude", store=store)
    with pytest.raises(ConversationError) as caught:
        share.handoff("s-alpha", for_agent="grok")
    assert str(caught.value) == "app grok may not read this session"

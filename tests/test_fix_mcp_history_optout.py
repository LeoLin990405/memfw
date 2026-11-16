"""MCP history tools honour conversation opt-out, ``readable`` and ``can_read``.

``conversation_optout`` excludes an agent everywhere: the transcript tools
(history_list / history_search / history_read) must hide its source just like the
conversation tools, and a caller that may not read is refused.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from memfw.core.repository import Repository
from memfw.mcp.server import Gateway


def _line(**fields: Any) -> str:
    return json.dumps(fields) + "\n"


def _vault(tmp_path: Path) -> Path:
    private = tmp_path / "private-logs"
    private.mkdir()
    (private / "p1.jsonl").write_text(
        _line(session_id="p1", ts="2026-11-16T00:00:00Z", message="private plan: Contoso quietly"),
        encoding="utf-8",
    )
    public = tmp_path / "claude-logs"
    public.mkdir()
    (public / "c1.jsonl").write_text(
        _line(session_id="c1", ts="2026-11-16T00:00:01Z", message="public note about Contoso"),
        encoding="utf-8",
    )
    vault = tmp_path / "vault"
    repository = Repository(vault)
    repository.init()
    config = repository.read_config()
    config["history_sources"] = {"private-app": str(private), "claude": str(public)}
    repository.configure(config)
    return vault


def _configure(vault: Path, **extra: Any) -> None:
    repository = Repository(vault)
    config = repository.read_config()
    config.update(extra)
    repository.configure(config)


def _sources(result: dict[str, Any]) -> set[str]:
    return {str(hit["ref"]).split(":", 1)[0] for hit in result["results"]}


def _private_ref(vault: Path) -> str:
    hits = Gateway(vault, app="claude").history_search("Contoso", source="private-app")["results"]
    assert hits
    return str(hits[0]["ref"])


def test_no_policy_config_is_unchanged(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    for gateway in (Gateway(vault), Gateway(vault, app="kimi")):
        assert _sources(gateway.history_search("Contoso")) == {"private-app", "claude"}
        listed = {item["source"] for item in gateway.history_list()["sessions"]}
        assert listed == {"private-app", "claude"}
        assert gateway.history_read(_private_ref(vault))["entries"]


def test_opted_out_source_is_hidden_from_every_history_tool(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    ref = _private_ref(vault)
    _configure(
        vault,
        conversation_optout={"agents": ["private-app"]},
        conversation_share={"enabled": True},
    )
    for gateway in (Gateway(vault, app="claude"), Gateway(vault)):
        assert _sources(gateway.history_search("Contoso")) == {"claude"}
        assert {item["source"] for item in gateway.history_list()["sessions"]} == {"claude"}
        assert gateway.history_list(session="p1")["entries"] == []
        with pytest.raises(ValueError, match="unknown history source"):
            gateway.history_search("Contoso", source="private-app")
        with pytest.raises(ValueError, match="unknown history source"):
            gateway.history_list(source="private-app")
        with pytest.raises(ValueError, match="unknown history source"):
            gateway.history_read(ref)


def test_unreadable_source_is_hidden(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    ref = _private_ref(vault)
    _configure(
        vault,
        conversation_share={"enabled": True, "apps": {"private-app": {"readable": False}}},
    )
    gateway = Gateway(vault, app="codex")
    assert _sources(gateway.history_search("Contoso")) == {"claude"}
    with pytest.raises(ValueError, match="unknown history source"):
        gateway.history_read(ref)


def test_caller_without_can_read_is_refused(tmp_path: Path) -> None:
    """The audit repro: conversations_list refuses kimi, so history must too."""

    vault = _vault(tmp_path)
    ref = _private_ref(vault)
    _configure(
        vault,
        conversation_optout={"agents": ["private-app"]},
        conversation_share={
            "enabled": True,
            "apps": {"private-app": {"readable": False}, "kimi": {"can_read": False}},
        },
    )
    gateway = Gateway(vault, app="kimi")
    with pytest.raises(Exception, match="may not read"):
        gateway._conversations().list_sessions()
    with pytest.raises(ValueError, match="may not read"):
        gateway.history_search("Contoso")
    with pytest.raises(ValueError, match="may not read"):
        gateway.history_list()
    with pytest.raises(ValueError, match="may not read"):
        gateway.history_read(ref)


def test_opted_out_caller_is_refused(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _configure(vault, conversation_optout={"agents": ["private-app"]})
    with pytest.raises(ValueError, match="opted out"):
        Gateway(vault, app="private-app").history_search("Contoso")


def test_can_read_list_narrows_sources(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    ref = _private_ref(vault)
    _configure(
        vault,
        conversation_share={"enabled": True, "apps": {"kimi": {"can_read": ["claude"]}}},
    )
    gateway = Gateway(vault, app="kimi")
    assert _sources(gateway.history_search("Contoso")) == {"claude"}
    assert {item["source"] for item in gateway.history_list()["sessions"]} == {"claude"}
    with pytest.raises(ValueError, match="unknown history source"):
        gateway.history_read(ref)
    # Other callers keep the full view.
    assert _sources(Gateway(vault, app="codex").history_search("Contoso")) == {
        "private-app",
        "claude",
    }

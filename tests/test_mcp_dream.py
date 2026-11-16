"""MCP tools for dream: dream_run (proposal-only, dry run by default) and dream_list."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from memfw.core.repository import Repository
from memfw.mcp.server import TOOLS, Gateway

NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _set_meta(root: Path, memory_id: str, **fields: Any) -> None:
    path = root / "memories" / f"{memory_id}.md"
    _, raw, body = path.read_text(encoding="utf-8").split("---\n", 2)
    meta = yaml.safe_load(raw)
    meta.update(fields)
    dumped = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).rstrip()
    path.write_text(f"---\n{dumped}\n---\n{body}", encoding="utf-8")


def _vault(tmp_path: Path, **config: Any) -> Path:
    root = tmp_path / "vault"
    repository = Repository(root, clock=lambda: NOW - timedelta(days=5))
    repository.init()
    if config:
        merged = repository.read_config()
        merged.update(config)
        repository.configure(merged)
    repository.add("Lamp", "The lamp color is blue.", memory_id="m-a")
    repository.add("Lamp", "Lamp colour is blue.", memory_id="m-b")
    repository.add("Lamp", "The lamp color is blue.", memory_id="m-other")
    for memory_id in ("m-a", "m-b"):
        _set_meta(root, memory_id, about=["concept:fixture:lamp"])
    _set_meta(root, "m-other", about=["concept:fixture:lamp"], tenant="other-tenant")
    return root


def _gateway(root: Path, tenant: str = "") -> Gateway:
    gateway = Gateway(root, tenant=tenant)
    gateway.dream_clock = lambda: NOW
    return gateway


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


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_dream_tools_are_listed_without_code_parameters() -> None:
    tools = {tool["name"]: tool for tool in TOOLS}
    properties = tools["dream_run"]["inputSchema"]["properties"]
    assert properties["dry_run"]["default"] is True
    assert not {"judge", "allow_config_judge", "dream_id", "tenant"} & properties.keys()
    assert "dream_list" in tools


def test_dry_run_by_default_writes_nothing(tmp_path: Path, capsys: Any) -> None:
    root = _vault(tmp_path)
    before = _snapshot(root)
    result = _call(_gateway(root), "dream_run", {}, capsys)
    assert result["isError"] is False
    data = json.loads(result["content"][0]["text"])
    assert data["dry_run"] is True and data["staged"] is None and data["authority"] is False
    assert [item["action"] for item in data["candidates"]] == ["merge"]
    assert data["candidates_truncated"] == 0
    assert _snapshot(root) == before
    assert not (root / "_candidates").exists()


def test_stage_writes_one_dream_and_no_memory(tmp_path: Path, capsys: Any) -> None:
    root = _vault(tmp_path)
    memories = {k: v for k, v in _snapshot(root).items() if k.startswith("memories")}
    result = _call(_gateway(root), "dream_run", {"dry_run": False}, capsys)
    data = json.loads(result["content"][0]["text"])
    assert data["staged"].startswith("_candidates/dream/dream-")
    assert len(list((root / "_candidates" / "dream").iterdir())) == 1
    assert {k: v for k, v in _snapshot(root).items() if k.startswith("memories")} == memories
    listed = json.loads(_call(_gateway(root), "dream_list", {}, capsys)["content"][0]["text"])
    assert [item["dream_id"] for item in listed["dreams"]] == [data["dream_id"]]
    assert listed["dreams"][0]["candidates"] == {"merge": 1}


def test_judge_arguments_and_config_judge_are_ignored(tmp_path: Path, capsys: Any) -> None:
    root = _vault(tmp_path, dream_judge="memfw_no_such_module_for_tests:judge")
    arguments = {"judge": "memfw_no_such_module_for_tests:judge", "allow_config_judge": True}
    result = _call(_gateway(root), "dream_run", arguments, capsys)
    assert result["isError"] is False
    data = json.loads(result["content"][0]["text"])
    assert data["report"]["judge"].startswith("none")
    assert data["report"]["config_judge_ignored"] is True


def test_other_tenants_are_not_seen(tmp_path: Path, capsys: Any) -> None:
    root = _vault(tmp_path)
    data = json.loads(_call(_gateway(root), "dream_run", {}, capsys)["content"][0]["text"])
    ids = {memory_id for item in data["candidates"] for memory_id in item["memory_ids"]}
    assert "m-other" not in ids
    other = json.loads(
        _call(_gateway(root, "other-tenant"), "dream_run", {"dry_run": False}, capsys)["content"][
            0
        ]["text"]
    )
    assert other["report"]["tenant"] == "other-tenant"
    listed = json.loads(_call(_gateway(root), "dream_list", {}, capsys)["content"][0]["text"])
    assert listed["dreams"] == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"limit": 5000},
        {"max_sessions": 0},
        {"max_sessions": 101},
        {"min_events": 1},
        {"dry_run": "no"},
        {"stale_days": -1},
    ],
)
def test_bounds_are_enforced(tmp_path: Path, capsys: Any, arguments: dict[str, Any]) -> None:
    root = _vault(tmp_path)
    before = _snapshot(root)
    result = _call(_gateway(root), "dream_run", arguments, capsys)
    assert result["isError"] is True
    assert _snapshot(root) == before


def test_candidate_list_is_capped(tmp_path: Path, capsys: Any, monkeypatch: Any) -> None:
    from memfw.mcp import tools_dream

    monkeypatch.setattr(tools_dream, "DREAM_MAX_CANDIDATES", 0)
    root = _vault(tmp_path)
    data = json.loads(_call(_gateway(root), "dream_run", {}, capsys)["content"][0]["text"])
    assert data["candidates"] == [] and data["candidates_truncated"] == 1


def test_dream_list_skips_symlinks(tmp_path: Path, capsys: Any) -> None:
    root = _vault(tmp_path)
    _call(_gateway(root), "dream_run", {"dry_run": False}, capsys)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "report.json").write_text(json.dumps({"dream_id": "planted", "tenant": ""}))
    (root / "_candidates" / "dream" / "planted").symlink_to(outside)
    listed = json.loads(_call(_gateway(root), "dream_list", {}, capsys)["content"][0]["text"])
    assert "planted" in listed["skipped"]
    assert all(item["dream_id"] != "planted" for item in listed["dreams"])


def test_dream_list_refuses_a_symlinked_dream_dir(tmp_path: Path, capsys: Any) -> None:
    root = _vault(tmp_path)
    outside = tmp_path / "elsewhere"
    (outside / "d1").mkdir(parents=True)
    (outside / "d1" / "report.json").write_text(json.dumps({"dream_id": "d1", "tenant": ""}))
    (root / "_candidates").mkdir()
    (root / "_candidates" / "dream").symlink_to(outside)
    listed = json.loads(_call(_gateway(root), "dream_list", {}, capsys)["content"][0]["text"])
    assert listed["dreams"] == []


def _with_history(tmp_path: Path) -> Path:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "s1.jsonl").write_text(
        json.dumps({"session_id": "s1", "ts": "2026-02-01T00:00:00Z", "message": "lamp blue"})
        + "\n",
        encoding="utf-8",
    )
    return _vault(tmp_path, history_sources={"logs": str(logs)})


def test_staging_never_builds_the_transcript_index(tmp_path: Path, capsys: Any) -> None:
    from memfw.history import default_db

    root = _with_history(tmp_path)
    db = default_db(root)
    result = _call(_gateway(root), "dream_run", {"dry_run": False}, capsys)
    data = json.loads(result["content"][0]["text"])
    assert data["staged"]
    assert data["report"]["history"]["available"] is False
    assert not db.exists()
    assert not list(db.parent.glob(db.name + "*"))


def test_staging_reads_an_existing_index_without_changing_it(tmp_path: Path, capsys: Any) -> None:
    from memfw.history import TranscriptIndex, default_db, sources_from_config

    root = _with_history(tmp_path)
    index = TranscriptIndex(default_db(root), sources_from_config(Repository(root).read_config()))
    index.refresh()
    db = default_db(root)
    before = {path.name: path.read_bytes() for path in db.parent.glob(db.name + "*")}
    (tmp_path / "logs" / "s2.jsonl").write_text(
        json.dumps({"session_id": "s2", "ts": "2026-02-02T00:00:00Z", "message": "new"}) + "\n",
        encoding="utf-8",
    )
    result = _call(_gateway(root), "dream_run", {"dry_run": False}, capsys)
    data = json.loads(result["content"][0]["text"])
    assert data["report"]["history"]["read_only"] is True
    assert "refresh" not in data["report"]["history"]
    after = {path.name: path.read_bytes() for path in db.parent.glob(db.name + "*")}
    assert after == before


def test_staging_is_rate_limited(tmp_path: Path, capsys: Any) -> None:
    root = _vault(tmp_path)
    gateway = _gateway(root)
    assert _call(gateway, "dream_run", {"dry_run": False}, capsys)["isError"] is False
    second = _call(gateway, "dream_run", {"dry_run": False}, capsys)
    assert second["isError"] is True and "staged less than" in second["content"][0]["text"]
    assert _call(gateway, "dream_run", {}, capsys)["isError"] is False  # dry runs still work
    assert len(list((root / "_candidates" / "dream").iterdir())) == 1


def test_staged_dreams_per_tenant_are_capped(tmp_path: Path, capsys: Any, monkeypatch: Any) -> None:
    from memfw.mcp import tools_dream

    monkeypatch.setattr(tools_dream, "DREAM_MAX_STAGED", 1)
    monkeypatch.setattr(tools_dream, "DREAM_STAGE_INTERVAL_SECONDS", 0.0)
    root = _vault(tmp_path)
    assert _call(_gateway(root), "dream_run", {"dry_run": False}, capsys)["isError"] is False
    refused = _call(_gateway(root), "dream_run", {"dry_run": False}, capsys)
    assert refused["isError"] is True and "waiting for review" in refused["content"][0]["text"]
    # another tenant has its own allowance
    other = _call(_gateway(root, "other-tenant"), "dream_run", {"dry_run": False}, capsys)
    assert other["isError"] is False

"""C5: ``memory_used`` records a usage signal only, off by default.

- Off (no ``runtime.usage`` in vault.yaml, no ``MEMFW_USAGE_SIGNAL``): the tool
  is not listed (``tools/list`` is exactly ``schema.TOOLS``), calling it is an
  unknown tool, and no ``usage.sqlite3`` appears.
- On: one row per visible id in ``usage.sqlite3`` (mode 0600) with the id,
  session_ref, app and time only; unknown, other-tenant, disallowed-source and
  malformed ids are dropped and the answer is the same either way.
- The vault (memories, ``events.jsonl``, ``vault.yaml``) never changes.
"""

from __future__ import annotations

import io
import json
import os
import stat
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from memfw.core.repository import Repository
from memfw.index import MemoryIndex
from memfw.mcp import schema
from memfw.mcp.server import Gateway
from memfw.ports import MAX_USAGE_IDS, UsageSignal, UsageSink
from memfw.ports.reference import MemoryUsageSink, NullUsageSink
from memfw.ports.testing import ContractError, check_usage_sink, tree_digest
from memfw.runtime import build_runtime
from memfw.runtime.usage import RuntimeOptions, env_switch, options_from_config
from memfw.usage import FILENAME, SqliteUsageSink, UsageStoreError, projection_path

T0 = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
#: Disposable files a call may create or change (projections and the lock).
PROJECTIONS = (FILENAME, "index.sqlite3", ".memfw.lock", f"{FILENAME}-journal")


def _vault(tmp_path: Path, *, usage: Any = None) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("Homelab backup", "nightly homelab backup", memory_id="m-backup")
    repository.add("Port", "service port 8080", memory_id="m-port", source="nas")
    repository.add("Alpha", "tenant alpha secret plan", memory_id="m-alpha", tenant="alpha")
    MemoryIndex(repository.index_path).rebuild(repository)
    if usage is not None:
        config = repository.read_config()
        config["runtime"] = {"usage": {"enabled": usage}}
        repository.configure(config)
    return repository


def _gateway(root: Path, **kwargs: Any) -> Gateway:
    kwargs.setdefault("tenant", "")
    kwargs.setdefault("entity_enabled", False)
    kwargs.setdefault("allowed_sources", ())
    kwargs.setdefault("app", "codex")
    return Gateway(root, **kwargs)


def _rpc(gateway: Gateway, message: dict[str, Any]) -> dict[str, Any]:
    out = io.StringIO()
    with redirect_stdout(out):
        gateway.handle(message)
    return json.loads(out.getvalue())


def _call(gateway: Gateway, arguments: Any) -> dict[str, Any]:
    return _rpc(
        gateway,
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "memory_used", "arguments": arguments},
        },
    )


def _tools(gateway: Gateway) -> list[dict[str, Any]]:
    return _rpc(gateway, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]


@pytest.fixture(autouse=True)
def _no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEMFW_USAGE_SIGNAL", raising=False)
    monkeypatch.delenv("MEMFW_RECORD_ACCESS", raising=False)


# --- off by default -------------------------------------------------------------


def test_off_by_default_tool_list_is_exactly_tools(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = _gateway(repository.root)
    tools = _tools(gateway)
    assert tools == schema.TOOLS
    assert json.dumps(tools) == json.dumps(schema.TOOLS)
    assert "memory_used" not in {tool["name"] for tool in tools}
    answer = _call(gateway, {"ids": ["m-backup"], "session_ref": "s1"})
    assert answer["error"] == {"code": -32601, "message": "unknown tool: memory_used"}
    assert not projection_path(repository.root).exists()


@pytest.mark.parametrize("value", [False, "yes", 1, None, {"x": 1}])
def test_vault_yaml_not_true_keeps_it_off(tmp_path: Path, value: Any) -> None:
    repository = _vault(tmp_path, usage=value)
    gateway = _gateway(repository.root)
    assert _tools(gateway) == schema.TOOLS
    assert "error" in _call(gateway, {"ids": ["m-backup"], "session_ref": "s1"})


def test_no_vault_at_start_keeps_it_off(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path / "missing")
    assert _tools(gateway) == schema.TOOLS


def test_env_zero_overrides_vault_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository = _vault(tmp_path, usage=True)
    monkeypatch.setenv("MEMFW_USAGE_SIGNAL", "0")
    assert _tools(_gateway(repository.root)) == schema.TOOLS


def test_bad_env_refuses_to_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository = _vault(tmp_path)
    monkeypatch.setenv("MEMFW_USAGE_SIGNAL", "maybe")
    with pytest.raises(ValueError, match="MEMFW_USAGE_SIGNAL"):
        _gateway(repository.root)


def test_env_switch_values() -> None:
    assert env_switch({}, "X") is None
    assert env_switch({"X": " "}, "X") is None
    assert all(env_switch({"X": v}, "X") is True for v in ("1", "true", "YES", "on"))
    assert all(env_switch({"X": v}, "X") is False for v in ("0", "false", "No", "off"))


def test_options_from_config_fail_closed() -> None:
    assert options_from_config({}) == RuntimeOptions(usage_enabled=False, record_access=True)
    assert options_from_config({"runtime": {"usage": {"enabled": True}}}).usage_enabled
    assert options_from_config({"runtime": "on"}) == RuntimeOptions(False, False)
    assert options_from_config({"runtime": {"search": {"record_access": "no"}}}) == (
        RuntimeOptions(False, False)
    )
    assert options_from_config({"runtime": {"search": {"record_access": False}}}) == (
        RuntimeOptions(False, False)
    )


# --- on ----------------------------------------------------------------------------


@pytest.mark.parametrize("how", ["vault", "env"])
def test_enabled_lists_the_tool_and_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    repository = _vault(tmp_path, usage=True if how == "vault" else None)
    if how == "env":
        monkeypatch.setenv("MEMFW_USAGE_SIGNAL", "1")
    gateway = _gateway(repository.root)
    gateway.runtime.usage_clock = lambda: T0
    tools = _tools(gateway)
    assert tools == [*schema.TOOLS, schema.MEMORY_USED_TOOL]
    answer = _call(gateway, {"ids": ["m-backup", "m-port", "m-backup"], "session_ref": "conv:s1"})
    assert answer["result"]["isError"] is False
    assert json.loads(answer["result"]["content"][0]["text"]) == {"ok": True}
    path = projection_path(repository.root)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert SqliteUsageSink(path).rows() == [
        {"memory_id": "m-backup", "session_ref": "conv:s1", "app": "codex", "at": T0.isoformat()},
        {"memory_id": "m-port", "session_ref": "conv:s1", "app": "codex", "at": T0.isoformat()},
    ]
    assert SqliteUsageSink(path).counts(["m-port", "m-none"]) == {"m-port": 1, "m-none": 0}


def test_unknown_foreign_and_malformed_ids_are_indistinguishable(tmp_path: Path) -> None:
    repository = _vault(tmp_path, usage=True)
    gateway = _gateway(repository.root, allowed_sources=("manual",))
    known = _call(gateway, {"ids": ["m-backup"], "session_ref": "s1"})
    hidden = [
        ["m-missing"],
        ["m-alpha"],  # other tenant
        ["m-port"],  # source not allowed
        ["../m-backup", "bad id", ""],  # malformed
    ]
    for ids in hidden:
        assert _call(gateway, {"ids": ids, "session_ref": "s1"}) == known, ids
    rows = SqliteUsageSink(projection_path(repository.root)).rows()
    assert [row["memory_id"] for row in rows] == ["m-backup"]


def test_other_tenant_gateway_records_only_its_own(tmp_path: Path) -> None:
    repository = _vault(tmp_path, usage=True)
    alpha = _gateway(repository.root, tenant="alpha")
    alpha.memory_used(["m-alpha", "m-backup"], "s2")
    rows = SqliteUsageSink(projection_path(repository.root)).rows()
    assert [row["memory_id"] for row in rows] == ["m-alpha"]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"session_ref": "s1"}, "ids must be a list"),
        ({"ids": "m-backup", "session_ref": "s1"}, "ids must be a list"),
        ({"ids": [1], "session_ref": "s1"}, "ids must be a list"),
        ({"ids": [], "session_ref": "s1"}, "ids required"),
        ({"ids": ["m-backup"] * (MAX_USAGE_IDS + 1), "session_ref": "s1"}, "at most"),
        ({"ids": ["m-backup"]}, "session_ref"),
        ({"ids": ["m-backup"], "session_ref": "what was the backup query"}, "session_ref"),
        ({"ids": ["m-backup"], "session_ref": "x" * 201}, "session_ref"),
    ],
)
def test_shape_errors(tmp_path: Path, arguments: dict[str, Any], message: str) -> None:
    repository = _vault(tmp_path, usage=True)
    answer = _call(_gateway(repository.root), arguments)
    assert answer["result"]["isError"] is True
    assert message in answer["result"]["content"][0]["text"]
    assert not projection_path(repository.root).exists()


def test_memory_used_never_changes_the_vault(tmp_path: Path) -> None:
    repository = _vault(tmp_path, usage=True)
    before = tree_digest(repository.root, exclude=PROJECTIONS)
    gateway = _gateway(repository.root)
    for _ in range(3):
        gateway.memory_used(["m-backup", "m-port", "m-missing"], "s1")
    assert projection_path(repository.root).exists()
    assert tree_digest(repository.root, exclude=PROJECTIONS) == before


def test_a_broken_store_does_not_change_the_answer(tmp_path: Path) -> None:
    repository = _vault(tmp_path, usage=True)
    target = tmp_path / "elsewhere.sqlite3"
    projection_path(repository.root).symlink_to(target)
    gateway = _gateway(repository.root)
    assert gateway.memory_used(["m-backup"], "s1") == {"ok": True}
    assert not target.exists()


def test_injected_sink_and_disabled_null_sink(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    sink = MemoryUsageSink()
    runtime = build_runtime(repository.root, usage_signal=True, usage_sink=sink)
    runtime.usage_clock = lambda: T0
    assert runtime.memory_used(["m-backup", "m-missing"], "s1", app="Kimi") == {"ok": True}
    assert sink.signals == (
        UsageSignal(memory_ids=("m-backup",), session_ref="s1", app="kimi", at=T0),
    )
    off = build_runtime(repository.root, usage_signal=False)
    assert isinstance(off.usage_sink(repository), NullUsageSink)
    assert off.memory_used(["m-backup"], "s1", app="kimi") == {"ok": True}
    assert not projection_path(repository.root).exists()


def test_odd_app_name_is_recorded_as_unnamed(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    sink = MemoryUsageSink()
    runtime = build_runtime(repository.root, usage_signal=True, usage_sink=sink)
    runtime.memory_used(["m-backup"], "s1", app="my app!")
    assert sink.signals[0].app == ""


# --- the port ------------------------------------------------------------------------


def _signals() -> list[UsageSignal]:
    return [
        UsageSignal(memory_ids=("m-a", "m-b"), session_ref="s1", app="codex", at=T0),
        UsageSignal(memory_ids=("m-c",), session_ref="conv:kimi:s2@4", app="", at=T0),
    ]


@pytest.mark.parametrize("name", ["null", "memory", "sqlite"])
def test_sinks_pass_the_contract(tmp_path: Path, name: str) -> None:
    vault = _vault(tmp_path).root
    stored = None
    if name == "null":
        sink: UsageSink = NullUsageSink()
    elif name == "memory":
        memory = MemoryUsageSink()
        sink, stored = memory, memory.rows
    else:
        sqlite = SqliteUsageSink(projection_path(vault))
        sink, stored = sqlite, sqlite.rows
    check_usage_sink(
        sink, signals=_signals(), vault=vault, vault_exclude=PROJECTIONS, stored=stored
    )


def test_contract_catches_a_sink_that_keeps_text(tmp_path: Path) -> None:
    class Leaky(MemoryUsageSink):
        def rows(self) -> list[dict[str, Any]]:
            return [{**row, "query": "what was the backup plan"} for row in super().rows()]

    leaky = Leaky()
    with pytest.raises(ContractError, match="not a signal field"):
        check_usage_sink(leaky, signals=_signals(), stored=leaky.rows)


def test_contract_catches_a_sink_that_writes_the_vault(tmp_path: Path) -> None:
    vault = _vault(tmp_path).root

    class Writer(NullUsageSink):
        def record(self, signal: UsageSignal) -> None:
            super().record(signal)
            (vault / "events.jsonl").write_bytes(b"")

    with pytest.raises(ContractError, match="changed the vault"):
        check_usage_sink(Writer(), signals=_signals(), vault=vault, vault_exclude=PROJECTIONS)


@pytest.mark.parametrize(
    "fields",
    [
        {"memory_ids": ()},
        {"memory_ids": ("m-a", "m-a")},
        {"memory_ids": ("bad id",)},
        {"memory_ids": tuple(f"m-{n}" for n in range(MAX_USAGE_IDS + 1))},
        {"session_ref": "has spaces"},
        {"session_ref": ""},
        {"app": "Codex"},
        {"at": datetime(2026, 1, 1)},
    ],
)
def test_signal_validation(fields: dict[str, Any]) -> None:
    base: dict[str, Any] = {"memory_ids": ("m-a",), "session_ref": "s1", "app": "codex", "at": T0}
    with pytest.raises(ValueError):
        UsageSignal(**{**base, **fields})


def test_signal_type_errors() -> None:
    with pytest.raises(TypeError):
        UsageSignal(memory_ids=["m-a"], session_ref="s1", app="", at=T0)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        UsageSignal(memory_ids=("m-a",), session_ref="s1", app="", at="now")  # type: ignore[arg-type]


def test_sqlite_sink_is_private_and_resettable(tmp_path: Path) -> None:
    path = tmp_path / "usage.sqlite3"
    sink = SqliteUsageSink(path)
    assert sink.rows() == [] and sink.counts() == {}
    old = os.umask(0)
    try:
        sink.record(_signals()[0])
    finally:
        os.umask(old)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    sink.record(_signals()[0])
    assert sink.counts() == {"m-a": 2, "m-b": 2}
    sink.reset()
    assert not path.exists() and sink.rows() == []


def test_sqlite_sink_refuses_a_symlink_and_a_non_database(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"")
    link = tmp_path / "usage.sqlite3"
    link.symlink_to(target)
    with pytest.raises(UsageStoreError):
        SqliteUsageSink(link).record(_signals()[0])
    assert target.read_bytes() == b""
    junk = tmp_path / "junk.sqlite3"
    junk.write_bytes(b"not a database" * 100)
    with pytest.raises(UsageStoreError):
        SqliteUsageSink(junk).record(_signals()[0])

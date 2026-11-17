"""C5: ``memory_used`` records a usage signal only, off by default.

- Off (no ``runtime.usage`` in vault.yaml, no ``MEMFW_USAGE_SIGNAL``): the tool
  is not listed (``tools/list`` is exactly ``schema.TOOLS``), calling it is an
  unknown tool, and no usage store appears.
- On: one row per visible id in the usage store (outside the vault, under
  ``~/.local/share/memfw/usage/``, mode 0600) with the id,
  session_ref, app and time only; unknown, other-tenant, disallowed-source and
  malformed ids are dropped and the answer's content is the same either way.
- The vault (memories, ``events.jsonl``, ``vault.yaml``) never changes.
"""

from __future__ import annotations

import io
import json
import os
import stat
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
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
from memfw.stats import measure_physical_disk_bytes
from memfw.usage import SqliteUsageSink, UsageStoreError, projection_path

T0 = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
#: Disposable files a call may create or change (the index and the lock).
PROJECTIONS = ("index.sqlite3", ".memfw.lock")


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
def _no_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("MEMFW_USAGE_SIGNAL", raising=False)
    monkeypatch.delenv("MEMFW_RECORD_ACCESS", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


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


def test_unknown_foreign_and_malformed_ids_give_the_same_answer(tmp_path: Path) -> None:
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


def test_store_lives_outside_the_vault_and_is_keyed_by_it(tmp_path: Path) -> None:
    repository = _vault(tmp_path, usage=True)
    other = Repository(tmp_path / "other")
    other.init()
    path = projection_path(repository.root)
    assert repository.root not in path.parents
    assert path.parent == tmp_path / "home" / ".local" / "share" / "memfw" / "usage"
    assert projection_path(other.root) != path
    _gateway(repository.root).memory_used(["m-backup"], "s1")
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert not any("usage" in p.name for p in repository.root.rglob("*"))


def test_budget_inputs_are_unaffected_by_usage_volume(tmp_path: Path) -> None:
    """Physical size (what budget enforcement acts on) and the fingerprint stay put."""

    from memfw.curation.engine import enforce_budget

    repository = _vault(tmp_path, usage=True)
    config = repository.read_config()
    # A 1 MB physical budget the vault fits in; 2000 signals would have pushed a
    # store inside the vault past it (hard forget = deletion).
    config.update(budget_mode="total", budget_level="1MB", forget_action="hard", budget_scope="all")
    repository.configure(config)
    physical = measure_physical_disk_bytes(repository.root)
    fingerprint = repository._tree_fingerprint()
    gateway = _gateway(repository.root)
    for number in range(2000):
        gateway.memory_used(["m-backup", "m-port"], f"s{number}")
    assert SqliteUsageSink(projection_path(repository.root)).counts()["m-backup"] == 2000
    assert projection_path(repository.root).stat().st_size > 64 * 1024
    assert measure_physical_disk_bytes(repository.root) == physical
    assert repository._tree_fingerprint() == fingerprint
    result = enforce_budget(repository, now=T0)
    assert result.applied_ids == () and result.satisfied_after_apply is True
    assert result.physical_bytes_before == physical
    assert {doc.id for doc in repository.list()} == {"m-backup", "m-port", "m-alpha"}


def test_retention_prunes_by_age_and_count(tmp_path: Path) -> None:
    sink = SqliteUsageSink(tmp_path / "u.sqlite3", max_rows=5, max_age_days=10)

    def signal(day: int, memory_id: str) -> UsageSignal:
        return UsageSignal(
            memory_ids=(memory_id,), session_ref="s1", app="", at=T0 + timedelta(days=day)
        )

    sink.record(signal(0, "m-old"))
    sink.record(signal(11, "m-new"))  # m-old is now 11 days old
    assert [row["memory_id"] for row in sink.rows()] == ["m-new"]
    for n in range(7):
        sink.record(signal(11, f"m-{n}"))
    assert [row["memory_id"] for row in sink.rows()] == [f"m-{n}" for n in range(2, 7)]
    with pytest.raises(ValueError):
        SqliteUsageSink(tmp_path / "x", max_rows=0)
    with pytest.raises(ValueError):
        SqliteUsageSink(tmp_path / "x", max_age_days=0)


def test_count_retention_keeps_the_newest_rows_despite_rowid_gaps(tmp_path: Path) -> None:
    sink = SqliteUsageSink(tmp_path / "u.sqlite3", max_rows=3, max_age_days=10)

    def signal(day: int, memory_id: str) -> UsageSignal:
        return UsageSignal(
            memory_ids=(memory_id,), session_ref="s1", app="", at=T0 + timedelta(days=day)
        )

    # An out-of-order (older) signal pruned by age leaves a hole in the rowids:
    # count retention still keeps the newest max_rows rows, not fewer.
    for day, memory_id in ((11, "m-a"), (0, "m-b"), (11, "m-c"), (11, "m-d")):
        sink.record(signal(day, memory_id))
    assert [row["memory_id"] for row in sink.rows()] == ["m-a", "m-c", "m-d"]
    sink.record(signal(11, "m-e"))
    assert [row["memory_id"] for row in sink.rows()] == ["m-c", "m-d", "m-e"]


def test_existing_store_gains_the_at_epoch_index(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "u.sqlite3"
    with sqlite3.connect(path) as connection:  # a file written before the index existed
        connection.execute(
            "CREATE TABLE used (memory_id TEXT NOT NULL, session_ref TEXT NOT NULL,"
            " app TEXT NOT NULL, at TEXT NOT NULL, at_epoch REAL NOT NULL)"
        )
        connection.execute(
            "INSERT INTO used VALUES ('m-old', 's0', '', ?, ?)", (T0.isoformat(), T0.timestamp())
        )
    os.chmod(path, 0o600)
    sink = SqliteUsageSink(path)
    sink.record(_signals()[0])
    with sqlite3.connect(path) as connection:
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'used'"
            )
        }
    assert {"used_at", "used_memory"} <= indexes
    assert sink.counts() == {"m-old": 1, "m-a": 1, "m-b": 1}


def test_existing_usage_directory_is_tightened(tmp_path: Path) -> None:
    directory = tmp_path / "usage"
    directory.mkdir(mode=0o755)
    os.chmod(directory, 0o755)
    SqliteUsageSink(directory / "u.sqlite3").record(_signals()[0])
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_a_write_at_the_row_cap_does_not_scan_the_table(tmp_path: Path) -> None:
    import sqlite3
    import time

    path = tmp_path / "u.sqlite3"
    sink = SqliteUsageSink(path)
    sink.record(_signals()[0])
    with sqlite3.connect(path) as connection:
        connection.executemany(
            "INSERT INTO used (memory_id, session_ref, app, at, at_epoch) VALUES (?, ?, ?, ?, ?)",
            (
                (f"m-{n:06x}", "conv:kimi:s2@4", "codex", T0.isoformat(), T0.timestamp())
                for n in range(100_000)
            ),
        )
    timings = []
    for _ in range(5):
        start = time.perf_counter()
        sink.record(_signals()[1])
        timings.append(time.perf_counter() - start)
    assert len(sink.rows()) == 100_000
    # The old NOT IN (... ORDER BY rowid DESC LIMIT 100000) delete took ~30-40 ms
    # here; the rowid-range delete is a few ms. The bound is generous for slow CI.
    assert sorted(timings)[2] < 0.020


def test_missing_vault_is_not_cached(tmp_path: Path) -> None:
    root = tmp_path / "late"
    runtime = build_runtime(root)
    assert runtime.options() == RuntimeOptions(usage_enabled=False, record_access=False)
    _vault(tmp_path / "x", usage=True)  # unrelated vault, for the layout
    repository = Repository(root)
    repository.init()
    config = repository.read_config()
    config["runtime"] = {"usage": {"enabled": True}}
    repository.configure(config)
    assert runtime.options() == RuntimeOptions(usage_enabled=True, record_access=True)
    # Cached from now on.
    config["runtime"] = {"usage": {"enabled": False}}
    repository.configure(config)
    assert runtime.usage_enabled() is True


def test_unreadable_vault_yaml_fails_closed_and_is_not_cached(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    good = repository.config_path.read_bytes()
    repository.config_path.write_text("runtime: [unclosed\n", encoding="utf-8")
    runtime = build_runtime(repository.root)
    assert runtime.options() == RuntimeOptions(usage_enabled=False, record_access=False)
    assert runtime.record_access() is False
    repository.config_path.write_bytes(good)
    assert runtime.options() == RuntimeOptions(usage_enabled=False, record_access=True)


def test_explicit_switches_need_no_vault(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path / "missing", usage_signal=True, record_access=True)
    assert runtime.options() == RuntimeOptions(usage_enabled=True, record_access=True)


def test_a_broken_store_does_not_change_the_answer(tmp_path: Path) -> None:
    repository = _vault(tmp_path, usage=True)
    target = tmp_path / "elsewhere.sqlite3"
    link = projection_path(repository.root)
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
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

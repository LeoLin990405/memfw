"""``runtime.recall.adapters``: operator-configured ``module:factory`` recall providers.

Runs on a default install: the adapter is ``tests/recall_adapter_stub.py``
(the stdlib reference ``InMemoryRecallProvider``), not a real adapter package.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from memfw.core.repository import Repository
from memfw.index import MemoryIndex
from memfw.runtime import build_runtime
from memfw.runtime.recall_adapters import (
    AdapterEntry,
    RecallAdapterError,
    load_adapters,
    parse_entries,
)
from tests import recall_adapter_stub as stub

STUB = "tests.recall_adapter_stub:factory"


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


def _vault(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    add = repository.add
    add("Homelab backup", "nightly homelab backup to the NAS", memory_id="m-backup")
    add("HAL9000 rack", "HAL9000 rack in the homelab, backup power", memory_id="m-hal")
    add("Alpha backup", "tenant alpha homelab backup notes", memory_id="m-alpha", tenant="alpha")
    MemoryIndex(repository.index_path).rebuild(repository)
    return repository


def _configure(repository: Repository, adapters: Any) -> None:
    path = repository.root / "vault.yaml"
    path.write_text(
        path.read_text(encoding="utf-8")
        + "runtime:\n  recall:\n    adapters: "
        + json.dumps(adapters)
        + "\n",
        encoding="utf-8",
    )


def _two_stage(runtime: Any, query: str = "homelab backup") -> dict[str, Any]:
    return runtime.search(query, limit=10, mode="two_stage")  # type: ignore[no-any-return]


def test_an_adapter_runs_beside_fts_with_its_weight_and_timeout(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    _configure(repository, [{"factory": STUB, "weight": 0.5, "timeout_seconds": 5}])
    stub.CALLS.clear()
    runtime = build_runtime(repository.root, record_access=False)
    payload = _two_stage(runtime)
    assert payload["recall"]["providers"] == ["fts", "stub"] and not payload["degraded"]
    ids = [row["id"] for row in payload["results"]]
    assert ids[0] == "m-backup" and "m-alpha" not in ids
    assert any(row["providers"] == ["fts", "stub"] for row in payload["results"])
    (loaded,) = runtime.recall_adapters(repository)
    assert (loaded.weight, loaded.timeout_seconds) == (0.5, 5.0)
    assert runtime.recall_adapters(repository) == (loaded,)
    _two_stage(runtime)
    assert stub.CALLS == [frozenset({"vault", "embedder"})]  # built once per Runtime


def test_without_the_setting_two_stage_is_unchanged_and_imports_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _vault(tmp_path)
    monkeypatch.delitem(sys.modules, "tests.recall_adapter_stub", raising=False)
    runtime = build_runtime(repository.root, record_access=False)
    payload = _two_stage(runtime)
    assert payload["recall"]["providers"] == ["fts"]
    assert runtime.recall_adapters(repository) == ()
    assert "tests.recall_adapter_stub" not in sys.modules
    fts = runtime.search("homelab backup", limit=10, mode="fts")
    assert [row["id"] for row in payload["results"]] == [row["id"] for row in fts["results"]]


def test_a_down_adapter_degrades_and_fts_answers(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    _configure(repository, [{"factory": "tests.recall_adapter_stub:down_factory"}])
    payload = _two_stage(build_runtime(repository.root, record_access=False))
    assert payload["degraded"] is True
    assert payload["recall"]["degraded"] == {"down": "unavailable:unreachable"}
    assert payload["recall"]["providers"] == ["fts"]
    assert [row["id"] for row in payload["results"]][0] == "m-backup"


@pytest.mark.parametrize(
    ("adapters", "code"),
    [
        (STUB, "bad_config"),
        ([{"factory": "tests.recall_adapter_stub"}], "bad_factory"),
        ([{"factory": STUB, "extra": 1}], "bad_entry"),
        ([{"factory": STUB, "options": {"vault": "/x"}}], "reserved_option"),
        ([{"factory": STUB, "options": ["x"]}], "bad_options"),
        ([{"factory": STUB, "weight": -1}], "bad_weight"),
        ([{"factory": STUB, "weight": True}], "bad_weight"),
        ([{"factory": STUB, "timeout_seconds": 0}], "bad_timeout"),
        ([{"factory": STUB, "timeout_seconds": 31}], "bad_timeout"),
        ([{"factory": "no_such_module_xyz:factory"}], "not_importable"),
        ([{"factory": "tests.recall_adapter_stub:NAME"}], "not_importable"),
        ([{"factory": STUB, "options": {"colour": 1}}], "factory_error"),
        ([{"factory": "builtins:dict"}], "not_a_provider"),
        ([{"factory": STUB, "options": {"name": "fts"}}], "duplicate_name"),
        ([{"factory": STUB}, {"factory": STUB}], "duplicate_name"),
        ([{"factory": STUB}] * 9, "bad_config"),
    ],
)
def test_bad_settings_fail_two_stage_closed(tmp_path: Path, adapters: Any, code: str) -> None:
    repository = _vault(tmp_path)
    _configure(repository, adapters)
    runtime = build_runtime(repository.root, record_access=False)
    with pytest.raises(RecallAdapterError) as caught:
        _two_stage(runtime)
    assert caught.value.code == code
    # Only two_stage uses the adapters; the other modes are unaffected.
    assert runtime.search("homelab backup", limit=10, mode="fts")["results"]


def test_parse_and_load_directly(tmp_path: Path) -> None:
    assert parse_entries({}) == ()
    assert parse_entries({"runtime": {"recall": {}}}) == ()
    entries = parse_entries({"runtime": {"recall": {"adapters": [{"factory": STUB}]}}})
    assert entries == (AdapterEntry(STUB),)
    repository = _vault(tmp_path)
    loaded = load_adapters(entries, root=repository.root, embedder=None, taken=["fts"])
    assert loaded[0].provider.name == "stub" and loaded[0].weight == 1.0


def _access_ids(repository: Repository) -> list[str]:
    events = (repository.root / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ids = []
    for line in events:
        event = json.loads(line)
        if event.get("kind") == "memory_access":
            ids.append(str(event.get("details", {}).get("memory_id")))
    return ids


def test_hits_the_clearance_withholds_get_no_access_event(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    path = repository.root / "vault.yaml"
    path.write_text(
        path.read_text(encoding="utf-8") + "legacy_privacy_default: public\n", encoding="utf-8"
    )
    repository.add(
        "Private backup",
        "private homelab backup keys",
        memory_id="m-priv",
        tags=["sensitivity:private"],
    )
    MemoryIndex(repository.index_path).rebuild(repository)
    _configure(repository, [{"factory": STUB}])
    before = len(_access_ids(repository))
    cleared = build_runtime(repository.root, clearance=frozenset({"public"}), record_access=True)
    payload = _two_stage(cleared, "private homelab backup keys")
    shown = [row["id"] for row in payload["results"]]
    assert "m-priv" not in shown and shown
    logged = _access_ids(repository)[before:]
    assert "m-priv" not in logged and set(logged) == set(shown)
    # Without a clearance nothing changes: every returned hit is logged, as before.
    open_runtime = build_runtime(repository.root, record_access=True)
    mark = len(_access_ids(repository))
    payload = _two_stage(open_runtime, "private homelab backup keys")
    assert "m-priv" in [row["id"] for row in payload["results"]]
    assert "m-priv" in _access_ids(repository)[mark:]


def test_racing_first_calls_share_one_set_of_adapters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    repository = _vault(tmp_path)
    _configure(repository, [{"factory": STUB}])
    runtime = build_runtime(repository.root, record_access=False)
    gate = threading.Barrier(2)
    real = stub.factory

    def slow(settings: Any) -> Any:
        gate.wait(timeout=5)  # both callers are inside a factory at once: no lock held
        return real(settings)

    monkeypatch.setattr(stub, "factory", slow)
    results: list[Any] = []
    threads = [
        threading.Thread(target=lambda: results.append(runtime.recall_adapters(repository)))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert len(results) == 2 and results[0] is results[1]

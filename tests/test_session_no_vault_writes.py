"""C6: no vault writes mid-session.

During a session an agent calls ``memory_context``, ``memory_search`` and
``memory_used``. With the session-safe setting (``runtime.search.record_access:
false``) none of them changes the vault: the memory files, ``events.jsonl``
(the hash-chained event log) and its ``events.state.json`` head, ``vault.yaml``
and ``_candidates/`` hash the same before and after. Only disposable
projections (``PROJECTIONS``) may appear or change.

The documented exception (``docs/ports-and-recall.md`` section 4.5): with the
default ``record_access: true``, ``memory_search`` ``hybrid``, ``vector`` and
``two_stage`` append one ``memory_access`` event per returned hit and warm a
cold or stub hit, as they always have. ``fts`` and ``multihop``,
``memory_context`` and ``memory_used`` never write, whatever the setting.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import pytest

from memfw.core.models import EventKind, Tier
from memfw.core.repository import Repository
from memfw.index import MemoryIndex
from memfw.mcp.server import Gateway
from memfw.notes import HashEmbeddingProvider
from memfw.ports.testing import tree_digest
from tests.conversation_fixtures import standard_rows, write_store

pytest.importorskip("sqlite_vec")

#: Disposable files at the vault root a read may build or refresh: the FTS and
#: hybrid indexes, the usage projection, SQLite side files and the lock. Never
#: a memory, the event log, its head, vault.yaml or a candidate.
PROJECTIONS = (
    ".memfw.lock",
    "index.sqlite3",
    "index.sqlite3-journal",
    ".memory-hybrid.sqlite",
    ".memory-hybrid.sqlite-journal",
    ".memory-hybrid.manifest.json",
    "usage.sqlite3",
    "usage.sqlite3-journal",
)

SEARCHES: list[dict[str, Any]] = [
    {"query": "zebra vault keys", "mode": mode}
    for mode in ("fts", "hybrid", "vector", "multihop", "two_stage")
] + [
    {"query": "zebra vault keys"},
    {"query": "zebra vault keys", "mode": "two_stage", "policy": "strict"},
    {"query": "zebra vault keys", "mode": "fts", "labels": True},
    {"query": "zebra vault keys", "mode": "hybrid", "labels": True},
    {"query": "zebra", "mode": "two_stage", "include_history": True},
]
CONTEXTS: list[dict[str, Any]] = [
    {},
    {"v": 1, "limit_tokens": 50},
    {"v": 2},
    {"v": 2, "policy": "strict", "slots": ["core", "handoff", "topics"]},
    {"v": 2, "slots": ["topics", "handoff"]},
]
USED: list[dict[str, Any]] = [
    {"ids": ["m-hot", "m-cold", "m-stub", "m-missing"], "session_ref": "conv:codex:s1"},
]


def _vault(tmp_path: Path, runtime: dict[str, Any] | None) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    add = repository.add
    add("Zebra hot", "zebra vault keys in the hot tier", memory_id="m-hot", about=["concept:z"])
    add("Zebra cold", "zebra vault keys cold copy", memory_id="m-cold", tier=Tier.COLD)
    add("Zebra stub", "zebra vault keys archived copy", memory_id="m-stub", tier=Tier.COLD)
    repository.cold_archive("archive-zebra", memory_ids={"m-stub"})
    add("Cite", "Always cite zebra sources.", memory_id="a-cite", anchor="sources")
    add(
        "Zebra private",
        "zebra vault keys private",
        memory_id="m-priv",
        tags=["sensitivity:private"],
    )
    MemoryIndex(repository.index_path).rebuild(repository)
    store = write_store(tmp_path / "data" / "conversations.sqlite", standard_rows())
    config = repository.read_config()
    config["conversation_store"] = {"path": str(store)}
    config["conversation_share"] = {"enabled": True}
    if runtime is not None:
        config["runtime"] = runtime
    repository.configure(config)
    return repository


def _session(root: Path) -> list[dict[str, Any]]:
    """One session's reads over MCP; returns every response."""

    gateway = Gateway(
        root,
        tenant="",
        entity_enabled=False,
        allowed_sources=(),
        app="codex",
        embedder=HashEmbeddingProvider(dimension=64),
    )
    messages: list[dict[str, Any]] = []
    calls = [("memory_context", args) for args in CONTEXTS]
    calls += [("memory_search", args) for args in SEARCHES]
    calls += [("memory_used", args) for args in USED]
    calls += [("memory_search", args) for args in SEARCHES]  # a warm second round
    for number, (name, arguments) in enumerate(calls):
        messages.append(
            {
                "jsonrpc": "2.0",
                "id": number,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
    messages.append(
        {
            "jsonrpc": "2.0",
            "id": "res",
            "method": "resources/read",
            "params": {"uri": "memfw://context/core"},
        }
    )
    out = io.StringIO()
    with redirect_stdout(out):
        for message in messages:
            gateway.handle(message)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def _vault_state(repository: Repository) -> dict[str, str]:
    return tree_digest(repository.root, exclude=PROJECTIONS)


def _accesses(repository: Repository) -> list[str]:
    return [
        event.memory_id for event in repository.events() if event.kind == EventKind.MEMORY_ACCESS
    ]


def _assert_answered(responses: list[dict[str, Any]], *, used: bool) -> None:
    for response in responses:
        assert "result" in response, response
        result = response["result"]
        if "isError" in result:
            assert result["isError"] is False, result
    search = [
        json.loads(r["result"]["content"][0]["text"])
        for r in responses
        if r["id"] == len(CONTEXTS)  # the first search: fts
    ]
    assert search and search[0]["results"], "the fixture search found nothing"
    used_answers = [r for r in responses if r["id"] == len(CONTEXTS) + len(SEARCHES)]
    if used:
        assert json.loads(used_answers[0]["result"]["content"][0]["text"]) == {"ok": True}


SESSION_SAFE = {"search": {"record_access": False}, "usage": {"enabled": True}}


def test_session_safe_reads_leave_the_vault_unchanged(tmp_path: Path) -> None:
    repository = _vault(tmp_path, SESSION_SAFE)
    events_before = (repository.root / "events.jsonl").read_bytes()
    head_before = repository.root.joinpath("events.state.json").read_bytes()
    before = _vault_state(repository)
    responses = _session(repository.root)
    _assert_answered(responses, used=True)
    assert (repository.root / "events.jsonl").read_bytes() == events_before
    assert repository.root.joinpath("events.state.json").read_bytes() == head_before
    after = _vault_state(repository)
    changed = sorted(set(before) ^ set(after) | {p for p in before if before[p] != after.get(p)})
    assert changed == [], changed
    # Cold and stub hits were returned but not warmed.
    assert repository.get("m-cold").tier == Tier.COLD
    assert repository.get("m-stub").is_stub
    # memory_used did record, outside the vault's authority.
    assert (repository.root / "usage.sqlite3").is_file()


def test_session_safe_via_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository = _vault(tmp_path, None)
    monkeypatch.setenv("MEMFW_RECORD_ACCESS", "0")
    monkeypatch.setenv("MEMFW_USAGE_SIGNAL", "1")
    before = _vault_state(repository)
    _assert_answered(_session(repository.root), used=True)
    assert _vault_state(repository) == before


def test_default_exception_is_exactly_access_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default settings: only hybrid, vector and two_stage write, and only access/warm events."""

    monkeypatch.delenv("MEMFW_RECORD_ACCESS", raising=False)
    monkeypatch.delenv("MEMFW_USAGE_SIGNAL", raising=False)
    repository = _vault(tmp_path, None)
    gateway = Gateway(
        repository.root,
        tenant="",
        entity_enabled=False,
        allowed_sources=(),
        app="codex",
        embedder=HashEmbeddingProvider(dimension=64),
    )
    before = _vault_state(repository)
    for arguments in CONTEXTS:
        gateway.context(
            int(arguments.get("limit_tokens") or 2000),
            v=arguments.get("v"),
            policy=arguments.get("policy"),
            slots=arguments.get("slots"),
        )
    for mode in ("fts", "multihop"):
        gateway.search("zebra vault keys", mode=mode)
    assert _vault_state(repository) == before, "context, fts and multihop never write"
    count = len(list(repository.events()))
    kinds_before = {event.event_id for event in repository.events()}
    for mode in ("hybrid", "vector", "two_stage"):
        gateway.search("zebra vault keys", mode=mode)
    new = [event for event in repository.events() if event.event_id not in kinds_before]
    assert len(list(repository.events())) > count
    assert {event.kind for event in new} <= {
        EventKind.MEMORY_ACCESS,
        EventKind.TIER_CHANGE,
        EventKind.MEMORY_RESTORE,
    }
    assert EventKind.MEMORY_ACCESS in {event.kind for event in new}
    assert _accesses(repository)

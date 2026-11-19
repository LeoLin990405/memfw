"""``retrieval_health`` on the real outputs: one new key, nothing else changes.

``memory_search`` (Runtime/MCP), ``memory_context`` v=2 and
``conversations_search`` (default and two_stage) each gain exactly the
``retrieval_health`` key; without it the payload is what it was (pinned by
comparing against the unwired code path, or against a run whose derivation
is stubbed out).
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

import pytest

from memfw.conversations import open_share, two_stage
from memfw.mcp.server import Gateway
from memfw.notes import HashEmbeddingProvider
from memfw.retrieval import health
from memfw.runtime import search as runtime_search
from tests.conversation_fixtures import (
    isolated_home,  # noqa: F401  (fixture)
    turn,
    write_store,
)
from tests.test_conversations_two_stage import DAY, BrokenEmbedder, WordEmbedder, _setup, _share
from tests.test_mcp_two_stage import _gateway, _vault

KEY = health.HEALTH_KEY


@pytest.fixture(autouse=True)
def _home(isolated_home: Path) -> Path:  # noqa: F811
    """Every test here writes worker state under a throwaway HOME, never the real one."""

    return isolated_home


@pytest.fixture(autouse=True)
def _fresh_cache() -> Any:
    two_stage._cache.clear()
    two_stage._cache_bytes = 0
    yield
    two_stage._cache.clear()
    two_stage._cache_bytes = 0


#: Timing fields: differ between two runs whatever the code does.
TIMINGS = ("total_ms", "latency_ms")


def _untimed(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _untimed(v) for k, v in value.items() if k not in TIMINGS}
    if isinstance(value, list):
        return [_untimed(item) for item in value]
    return value


def _without(payload: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(payload)
    assert KEY in out
    out.pop(KEY)
    return out


def _check_shape(found: dict[str, Any]) -> None:
    assert set(found) == {"schema", "mode", "confidence", "reasons", "score_kind", "note"}
    assert found["schema"] == health.SCHEMA
    for code in found["reasons"]:
        health.check_reason(code)


# -- memory_search -----------------------------------------------------------------


def _direct(gateway: Gateway, query: str, mode: str) -> dict[str, Any]:
    """The pre-wiring path: ``memfw.runtime.search.memory_search`` itself."""

    runtime = gateway.runtime
    return runtime_search.memory_search(
        runtime,
        runtime.open_repository(),
        query,
        10,
        mode=mode,
        source="",
        include_history=False,
        as_of=None,
        rounds=None,
        scope="",
        policy="compat_fts",
        labels=False,
        client_message=lambda text, _root: text,
    )


@pytest.mark.parametrize("mode", ["fts", "hybrid", "multihop", "two_stage"])
def test_memory_search_only_adds_the_key(tmp_path: Path, mode: str) -> None:
    repository = _vault(tmp_path)
    gateway = _gateway(repository.root)
    got = gateway.search("backup", 10, mode=mode)
    before = _direct(gateway, "backup", mode)
    assert _untimed(_without(got)) == _untimed(before)
    _check_shape(got[KEY])


def test_memory_search_fts_is_full(tmp_path: Path) -> None:
    gateway = _gateway(_vault(tmp_path).root)
    found = gateway.search("backup", 10, mode="fts")[KEY]
    assert (found["mode"], found["confidence"], found["reasons"]) == ("full", "high", [])
    assert found["score_kind"] == "bm25"


def test_memory_search_hybrid_without_embedder_is_degraded_medium(tmp_path: Path) -> None:
    gateway = _gateway(_vault(tmp_path).root)
    got = gateway.search("backup", 10, mode="hybrid")
    assert got["degraded"] is True and got["mode"] == "fts"  # today's signal
    found = got[KEY]
    assert found["mode"] == "degraded" and found["confidence"] == "medium"
    assert found["reasons"] == ["keyword_only", "semantic_unavailable"]


def test_memory_search_two_stage_with_embedder_is_full(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = _gateway(repository.root, embedder=HashEmbeddingProvider(dimension=64))
    got = gateway.search("backup", 10, mode="two_stage")
    assert got["degraded"] is False
    found = got[KEY]
    assert found["mode"] == "full" and found["score_kind"] == "rank"
    # Policy drops are reported by category, from recall_metrics.dropped only.
    dropped = got["recall_metrics"]["dropped"]
    for code in found["reasons"]:
        assert code.startswith("withheld:")
    total = sum(int(code.rsplit(":", 1)[1]) for code in found["reasons"])
    assert total == sum(v for v in dropped.values() if isinstance(v, int))


def test_memory_search_two_stage_without_embedder_names_it(tmp_path: Path) -> None:
    gateway = _gateway(_vault(tmp_path).root)
    found = gateway.search("backup", 10, mode="two_stage")[KEY]
    assert "semantic_unavailable" in found["reasons"] and "keyword_only" in found["reasons"]
    assert (found["mode"], found["confidence"]) == ("degraded", "medium")


def test_memory_search_no_match_is_empty(tmp_path: Path) -> None:
    gateway = _gateway(_vault(tmp_path).root)
    got = gateway.search("zzzqqqnothing", 10, mode="fts")
    assert got["results"] == []
    assert got[KEY]["mode"] == "empty" and got[KEY]["reasons"] == ["no_match"]


# -- memory_context v=2 ------------------------------------------------------------


def test_memory_context_v2_only_adds_the_key(tmp_path: Path) -> None:
    gateway = _gateway(_vault(tmp_path).root)
    got = gateway.context(2000, v=2)
    found = got[KEY]
    _check_shape(found)
    # No degraded slot: full, or empty (no_match) when the slots carry nothing.
    entries = sum(len(slot["entries"]) for slot in got["slots"])
    assert found["mode"] == ("full" if entries else "empty")
    assert set(found["reasons"]) <= {"no_match"} and found["score_kind"] == "none"
    again = gateway.context(2000, v=2)
    stamp = {"generated_at": None}
    assert {**_without(again), **stamp} == {**_without(got), **stamp}
    assert set(got) - {KEY} == set(_without(got))
    # The short "unchanged" reply and v=1 are not touched.
    short = gateway.context(2000, v=2, if_version=got["pack_version"])
    assert short["unchanged"] is True and KEY not in short
    assert KEY not in gateway.context(2000)


# -- conversations_search ------------------------------------------------------------


def _stubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = lambda payload: {}  # noqa: E731
    monkeypatch.setattr(two_stage, "from_conversation_search", stub)
    from memfw.conversations import share as share_module

    monkeypatch.setattr(share_module, "from_conversation_search", stub)


@pytest.mark.parametrize("mode", ["default", "two_stage"])
def test_conversation_search_only_adds_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    share = _share(tmp_path, embedder=WordEmbedder())
    real = share.search("uploader", mode=mode)
    _check_shape(real[KEY])
    _stubbed(monkeypatch)
    plain = share.search("uploader", mode=mode)
    assert _without(real) == _without(plain)


def test_conversation_default_mode_keys_and_health(tmp_path: Path) -> None:
    share = _share(tmp_path)
    got = share.search("uploader")
    assert set(got) == {"notice", "results", "next_offset", KEY}
    assert got[KEY]["mode"] == "full" and got[KEY]["score_kind"] == "none"
    empty = share.search("zzzqqqnothing")[KEY]
    assert empty["mode"] == "empty" and empty["reasons"] == ["no_match"]


def test_conversation_two_stage_no_embedder(tmp_path: Path) -> None:
    got = _share(tmp_path).search("uploader", mode="two_stage")
    assert got["degraded"] == ["no_embedder"]  # today's signal, unchanged
    found = got[KEY]
    assert found["mode"] == "degraded" and found["confidence"] == "medium"
    assert found["reasons"] == ["keyword_only", "semantic_unavailable"]
    assert found["score_kind"] == "rank"


def test_conversation_two_stage_embedder_error(tmp_path: Path) -> None:
    got = _share(tmp_path, embedder=BrokenEmbedder()).search("uploader", mode="two_stage")
    assert got["degraded"] == ["embedder_error"]
    assert "source_error:embedder" in got[KEY]["reasons"]


def test_conversation_two_stage_healthy_is_full(tmp_path: Path) -> None:
    got = _share(tmp_path, embedder=WordEmbedder()).search("uploader", mode="two_stage")
    assert got["degraded"] == []
    assert got[KEY]["mode"] == "full" and got[KEY]["confidence"] == "high"


@pytest.mark.parametrize("cap", [4096, 8192, 16384])
def test_conversation_two_stage_cap_holds_with_health(tmp_path: Path, cap: int) -> None:
    """A page that really truncates still fits the cap with retrieval_health in it."""

    vault, store = _setup(tmp_path / str(cap))
    many = [
        turn("codex", f"s-many-{index}", position, "user", "uploader " + "x" * 3000, DAY + "5:00Z")
        for index in range(30)
        for position in range(4)
    ]
    write_store(store, many)
    share = open_share(vault, "claude", store=store, embedder=WordEmbedder())
    got = share.search(
        "uploader", mode="two_stage", limit=50, windows=5, window_turns=3, max_bytes=cap
    )
    assert got["truncated"] is True and got["sessions"]
    assert len(json.dumps(got, ensure_ascii=False).encode()) <= cap
    assert got[KEY]["mode"] != "empty"


def test_conversation_two_stage_page_beyond_range(tmp_path: Path) -> None:
    share = _share(tmp_path, embedder=WordEmbedder())
    first = share.search("uploader", mode="two_stage")
    assert first["sessions"] and first["candidates"]["total"] > 0
    far = share.search("uploader", mode="two_stage", offset=first["candidates"]["total"] + 5)
    assert far["sessions"] == []
    assert far[KEY]["mode"] == "empty" and "page_empty" in far[KEY]["reasons"]
    assert "no_match" not in far[KEY]["reasons"]


def test_conversation_two_stage_empty_while_degraded(tmp_path: Path) -> None:
    got = _share(tmp_path).search("zzzqqqnothing", mode="two_stage")
    assert got["sessions"] == [] and got["degraded"] == ["no_embedder"]
    assert "empty_degraded" in got[KEY]["reasons"] and "no_match" not in got[KEY]["reasons"]


# -- R1: an access-event failure is informational -------------------------------------


def _plain_vault(tmp_path: Path) -> Any:
    from memfw.core.repository import Repository
    from memfw.index import MemoryIndex

    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("Homelab backup", "nightly homelab backup", memory_id="m-backup")
    repository.add("Backup notes", "backup notes and restore drills", memory_id="m-notes")
    MemoryIndex(repository.index_path).rebuild(repository)
    return repository


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores modes")
def test_two_stage_access_write_failure_stays_full(tmp_path: Path) -> None:
    repository = _plain_vault(tmp_path)
    gateway = _gateway(repository.root, embedder=HashEmbeddingProvider(dimension=64))
    log = repository.root / "events.jsonl"
    log.chmod(0o444)
    try:
        payload = gateway.search("backup", 10, mode="two_stage")
    finally:
        log.chmod(0o644)
    assert payload["results"]
    # Today's signals: the flag is set and the error names the cause.
    assert payload["degraded"] is True and "access_not_recorded" in payload["errors"]
    assert "degraded_sources" not in payload  # no provider page was degraded
    found = payload[KEY]
    assert (found["mode"], found["confidence"]) == ("full", "high")
    assert found["reasons"] == ["access_not_recorded"]


def test_two_stage_healthy_reply_has_no_degraded_sources(tmp_path: Path) -> None:
    repository = _plain_vault(tmp_path)
    gateway = _gateway(repository.root, embedder=HashEmbeddingProvider(dimension=64))
    payload = gateway.search("backup", 10, mode="two_stage")
    assert "degraded_sources" not in payload and payload["degraded"] is False
    assert payload[KEY]["mode"] == "full" and payload[KEY]["reasons"] == []

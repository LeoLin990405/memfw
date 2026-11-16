"""MCP ``memory_search`` ``mode=two_stage``: the coordinator behind the MCP tool."""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from memfw.core.repository import Repository
from memfw.index import MemoryIndex
from memfw.mcp.server import TOOLS, Gateway
from memfw.notes import HashEmbeddingProvider
from memfw.ports import Availability, Candidate, RecallBudget
from memfw.runtime import Runtime

QUERIES = (
    "homelab backup",
    "HAL9000",
    "cron pause before disk migration",
    "backup",
    "notes",
    "迁移 备份",
    "release v0.2.0",
)

CORE_FIELDS = ("id", "title", "source", "layer", "tier", "type")
#: Fields only ``two_stage`` rows carry, or whose meaning differs (fused score).
TWO_STAGE_ONLY = ("score", "providers", "recall_labels")


def _vault(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    add = repository.add
    add("Homelab backup", "nightly homelab backup to the NAS", memory_id="m-backup")
    add("HAL9000 rack", "HAL9000 rack in the homelab, backup power", memory_id="m-hal")
    add("NAS1 migration", "pause cron before the disk migration on NAS1", memory_id="m-nas1")
    add("Backup notes", "backup notes and restore drills", memory_id="m-notes", source="nas")
    add("Ops backup", "ops backup runbook notes", memory_id="m-ops", source="ops")
    add("Release", "release v0.2.0 notes for memfw", memory_id="m-release")
    add("迁移计划", "迁移之前先暂停定时任务，再做备份", memory_id="m-zh")
    add(
        "Private backup",
        "private backup keys location",
        memory_id="m-priv",
        tags=["sensitivity:private"],
    )
    add("Alpha backup", "tenant alpha backup notes", memory_id="m-alpha", tenant="alpha")
    add("Old backup plan", "old homelab backup plan notes", memory_id="m-old")
    add("Expired backup", "expired backup window notes", memory_id="m-exp")
    repository.update("m-old", superseded_by="m-backup")
    repository.update("m-exp", valid_to=datetime(2026, 1, 1, tzinfo=UTC))
    add("Future backup", "future backup rotation notes", memory_id="m-future")
    repository.update("m-future", valid_from=datetime(2099, 1, 1, tzinfo=UTC))
    for index in range(20):
        add(
            f"Filler {index}",
            f"backup filler entry number {index} notes",
            memory_id=f"m-f{index:02d}",
        )
    MemoryIndex(repository.index_path).rebuild(repository)
    return repository


def _core(payload: dict[str, Any]) -> list[tuple[Any, ...]]:
    return [tuple(row[key] for key in CORE_FIELDS) for row in payload["results"]]


def _rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Every row field except the two_stage-only ones: freshness included."""

    return [
        {key: value for key, value in row.items() if key not in TWO_STAGE_ONLY}
        for row in payload["results"]
    ]


def _ids(payload: dict[str, Any]) -> list[str]:
    return [row["id"] for row in payload["results"]]


def _gateway(root: Path, **kwargs: Any) -> Gateway:
    kwargs.setdefault("tenant", "")
    kwargs.setdefault("entity_enabled", False)
    kwargs.setdefault("allowed_sources", ())
    return Gateway(root, **kwargs)


PARITY_CASES: list[dict[str, Any]] = [
    {},
    {"allowed": ("manual", "nas")},
    {"source": "nas"},
    {"include_history": True},
    {"as_of": "2025-06-01T00:00:00+00:00"},
    {"as_of": "2030-01-01T00:00:00+00:00"},
    {"as_of": "2030-01-01T00:00:00"},
    {"as_of": "2030-01-01T00:00:00Z"},
    {"source": "nas", "allowed": ("nas", "manual")},
]


@pytest.mark.parametrize("case", PARITY_CASES, ids=lambda case: json.dumps(case))
def test_two_stage_compat_matches_fts_without_embedder(
    tmp_path: Path, case: dict[str, Any]
) -> None:
    repository = _vault(tmp_path)
    gateway = _gateway(repository.root, allowed_sources=tuple(case.get("allowed", ())))
    assert not gateway.embedder
    kwargs = {
        "source": case.get("source", ""),
        "include_history": case.get("include_history", False),
        "as_of": case.get("as_of"),
    }
    seen_any = False
    for query in QUERIES:
        for limit in (3, 10, 50):
            fts = gateway.search(query, limit=limit, mode="fts", **kwargs)
            two = gateway.search(
                query, limit=limit, mode="two_stage", policy="compat_fts", **kwargs
            )
            assert _core(two) == _core(fts), (case, query, limit)
            # Freshness and labels too: stale, index_version, sot_version,
            # content_hash, epistemic, causal, why, recorded_at ...
            assert _rows(two) == _rows(fts), (case, query, limit)
            assert two["errors"] == fts["errors"]
            assert two.get("ambiguity") == fts.get("ambiguity")
            assert two["mode"] == "two_stage"
            assert two["degraded"] is False
            seen_any = seen_any or bool(fts["results"])
    if "as_of" not in case or case["as_of"].startswith("2030"):
        assert seen_any, case


def test_two_stage_rows_carry_providers_labels_and_recall(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = _gateway(repository.root)
    payload = gateway.search("homelab backup", limit=5, mode="two_stage")
    assert payload["policy"] == "compat_fts"
    assert payload["results"]
    for row in payload["results"]:
        assert row["providers"] == ["fts"]
        assert isinstance(row["recall_labels"], list)
        assert row["why"] == "lexical"
        assert isinstance(row["score"], float)
        assert "epistemic" in row and "causal" in row
        assert "body" not in row
    recall = payload["recall"]
    assert recall["providers"] == ["fts"]
    assert recall["fail_closed"] is False
    metrics = payload["recall_metrics"]
    assert metrics["returned"] == len(payload["results"])
    # Plain JSON, and neither the query nor any body text appears.
    text = json.dumps(payload, ensure_ascii=False)
    assert "homelab backup" not in json.dumps(
        {"recall": recall, "recall_metrics": metrics}, ensure_ascii=False
    )
    assert "nightly homelab backup to the NAS" not in text


def test_two_stage_through_the_tool_handler(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = _gateway(repository.root)
    handler = gateway.handlers()["memory_search"]
    fts = handler({"query": "backup", "limit": 10, "mode": "fts"})
    two = handler({"query": "backup", "limit": 10, "mode": "two_stage"})
    assert _core(two) == _core(fts)
    strict = handler({"query": "private backup keys", "mode": "two_stage", "policy": "strict"})
    assert "m-priv" not in [row["id"] for row in strict["results"]]


def test_strict_policy_drops_a_private_memory_compat_returns(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = _gateway(repository.root)
    compat = gateway.search("private backup keys", limit=10, mode="two_stage")
    strict = gateway.search("private backup keys", limit=10, mode="two_stage", policy="strict")
    assert "m-priv" in [row["id"] for row in compat["results"]]
    assert "m-priv" not in [row["id"] for row in strict["results"]]
    assert strict["recall_metrics"]["dropped"].get("privacy") == 1
    assert strict["policy"] == "strict"


@pytest.mark.parametrize("policy", ["compat_fts", "strict"])
def test_two_stage_never_crosses_tenants(tmp_path: Path, policy: str) -> None:
    repository = _vault(tmp_path)
    default = _gateway(repository.root)
    alpha = _gateway(repository.root, tenant="alpha")
    for query in ("backup", "tenant alpha backup notes", "alpha"):
        ids = [
            row["id"]
            for row in default.search(query, limit=50, mode="two_stage", policy=policy)["results"]
        ]
        assert "m-alpha" not in ids
        alpha_ids = [
            row["id"]
            for row in alpha.search(query, limit=50, mode="two_stage", policy=policy)["results"]
        ]
        assert set(alpha_ids) <= {"m-alpha"}
    assert [
        row["id"]
        for row in alpha.search("tenant alpha backup notes", limit=5, mode="two_stage")["results"]
    ] == ["m-alpha"]


def test_two_stage_respects_allowed_sources(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = _gateway(repository.root, allowed_sources=("nas",))
    ids = [row["id"] for row in gateway.search("backup", limit=50, mode="two_stage")["results"]]
    assert ids == ["m-notes"]
    with pytest.raises(ValueError, match="source is not allowed"):
        gateway.search("backup", mode="two_stage", source="ops")


def test_policy_is_rejected_outside_two_stage(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = _gateway(repository.root)
    for mode in ("fts", "hybrid", "vector", "multihop"):
        with pytest.raises(ValueError, match="policy only applies to mode=two_stage"):
            gateway.search("backup", mode=mode, policy="strict")
    # The default value is accepted everywhere, so existing callers are unaffected.
    assert gateway.search("backup", mode="fts", policy="compat_fts")["mode"] == "fts"


def test_unknown_policy_and_as_of_handling(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = _gateway(repository.root)
    with pytest.raises(ValueError, match="unknown policy"):
        gateway.search("backup", mode="two_stage", policy="loose")
    with pytest.raises(ValueError, match="unknown policy"):
        gateway.search("backup", mode="fts", policy="loose")
    # Like mode=fts: a naive timestamp is read as UTC; a malformed one errors the same way.
    naive = gateway.search("backup", mode="two_stage", as_of="2030-01-01T00:00:00")
    aware = gateway.search("backup", mode="two_stage", as_of="2030-01-01T00:00:00+00:00")
    assert _ids(naive) == _ids(aware)
    with pytest.raises(Exception) as fts_error:
        gateway.search("backup", mode="fts", as_of="yesterday")
    with pytest.raises(type(fts_error.value)) as two_error:
        gateway.search("backup", mode="two_stage", as_of="yesterday")
    assert str(two_error.value) == str(fts_error.value)


def test_two_stage_limit_is_capped(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = _gateway(repository.root)
    assert len(gateway.search("backup", limit=500, mode="two_stage")["results"]) <= 50
    assert len(gateway.search("backup", limit=0, mode="two_stage")["results"]) == 1


def test_two_stage_adds_hybrid_only_with_an_embedder(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = _gateway(repository.root, embedder=HashEmbeddingProvider(dimension=64))
    payload = gateway.search("nightly backup to the storage box", limit=5, mode="two_stage")
    reports = {item["name"] for item in payload["recall_metrics"]["providers"]}
    assert reports == {"fts", "hybrid"}
    json.dumps(payload)  # plain JSON whether or not hybrid answered


def test_schema_lists_two_stage_and_keeps_the_default() -> None:
    tool = next(tool for tool in TOOLS if tool["name"] == "memory_search")
    properties = tool["inputSchema"]["properties"]
    assert properties["mode"]["enum"] == ["fts", "hybrid", "vector", "multihop", "two_stage"]
    assert properties["mode"]["default"] == "hybrid"
    assert properties["policy"]["enum"] == ["compat_fts", "strict"]
    assert properties["policy"]["default"] == "compat_fts"


def test_default_mode_is_still_hybrid(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = _gateway(repository.root)
    payload = gateway.search("backup", limit=5)
    assert payload["mode"] != "two_stage"
    assert "recall" not in payload
    assert all("providers" not in row for row in payload["results"])


def test_freshness_matches_fts_after_an_edit(tmp_path: Path) -> None:
    """A file changed after indexing is ``stale`` in both modes, fresh rows in neither."""

    repository = _vault(tmp_path)
    gateway = _gateway(repository.root)
    before = gateway.search("homelab backup", limit=25, mode="two_stage")
    assert before["results"]
    assert not any(row.get("stale") for row in before["results"])
    assert all(row["index_version"] for row in before["results"])
    path = repository.memories_dir / "m-backup.md"
    path.write_text(path.read_text(encoding="utf-8") + "\nedited\n", encoding="utf-8")
    # Keep the index's mtime newer so it is not rebuilt: the file is now stale.
    import os

    stamp = path.stat().st_mtime - 10
    os.utime(path, (stamp, stamp))
    fts = gateway.search("homelab backup", limit=25, mode="fts")
    two = gateway.search("homelab backup", limit=25, mode="two_stage")
    assert _rows(two) == _rows(fts)


@pytest.mark.parametrize(
    ("query", "as_of"),
    [
        ("x" * 5000, None),
        ("x" * 513, None),
        ("nul\x00byte", None),
        ("lone \ud800 surrogate", None),
        ("backup", "yesterday"),
        ("backup", "2026-13-01T00:00:00+00:00"),
        ("backup", ""),
    ],
    ids=["5000", "513", "nul", "surrogate", "as_of-word", "as_of-month", "as_of-empty"],
)
def test_input_errors_match_fts_exactly(tmp_path: Path, query: str, as_of: str | None) -> None:
    """Pins error type and message parity with mode=fts, so a drift in the
    shared service's rules shows up here."""

    repository = _vault(tmp_path)
    gateway = _gateway(repository.root)
    with pytest.raises(Exception) as fts_error:
        gateway.search(query, mode="fts", as_of=as_of)
    with pytest.raises(type(fts_error.value)) as two_error:
        gateway.search(query, mode="two_stage", as_of=as_of)
    assert type(two_error.value) is type(fts_error.value)
    assert str(two_error.value) == str(fts_error.value)
    assert getattr(two_error.value, "status", None) == getattr(fts_error.value, "status", None)


def test_a_512_character_query_is_accepted_like_fts(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = _gateway(repository.root)
    query = "backup " + "x" * 505
    assert len(query) == 512
    assert _ids(gateway.search(query, mode="two_stage")) == _ids(gateway.search(query, mode="fts"))


def test_tool_handler_rejects_a_non_string_policy(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    handler = _gateway(repository.root).handlers()["memory_search"]
    for value in (False, 0, 1, [], {}, ["strict"]):
        with pytest.raises(ValueError, match="policy must be a string"):
            handler({"query": "backup", "mode": "two_stage", "policy": value})
    assert handler({"query": "backup", "mode": "two_stage", "policy": None})["policy"] == (
        "compat_fts"
    )


def _stable(payload: dict[str, Any]) -> str:
    """The payload without timings (``recall_metrics`` differs per call)."""

    return json.dumps({k: v for k, v in payload.items() if k != "recall_metrics"}, sort_keys=True)


def test_empty_or_null_policy_is_the_default_in_every_mode(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = _gateway(repository.root)
    handler = gateway.handlers()["memory_search"]
    for mode in (None, "fts", "hybrid", "vector", "multihop", "two_stage"):
        base: dict[str, Any] = {"query": "backup", "limit": 5}
        if mode:
            base["mode"] = mode
        expected = _stable(handler(base))
        for value in ("", None):
            assert _stable(handler({**base, "policy": value})) == expected
        direct = {"mode": mode} if mode else {}
        assert _stable(gateway.search("backup", 5, policy="", **direct)) == _stable(
            gateway.search("backup", 5, **direct)
        )


# -- the coordinator filter, driven through the MCP path -----------------------


class _StubProvider:
    """Returns fixed ids, whatever the query: the filter has to do the work."""

    lexical = True
    projection_version = ""

    def __init__(self, ids: Sequence[str], *, name: str = "stub") -> None:
        self.name = name
        self.ids = list(ids)

    def available(self) -> Availability:
        return Availability.ok()

    def search(
        self,
        query: str,
        *,
        limit: int,
        budget: RecallBudget,
        tenant: str,
        as_of: datetime | None,
    ) -> Sequence[Candidate]:
        return [
            Candidate(
                ref=memory_id,
                ref_kind="memory_id",
                score=1.0,
                provider=self.name,
                rank=rank,
                extra={"why": "semantic"},
            )
            for rank, memory_id in enumerate(self.ids[:limit], start=1)
        ]


def _with_stub(monkeypatch: pytest.MonkeyPatch, stub: Any, *, only: bool = False) -> None:
    original = Runtime._two_stage_providers

    def providers(self: Runtime, *args: Any, **kwargs: Any) -> tuple[list[Any], dict[str, Any]]:
        found, configs = original(self, *args, **kwargs)
        if only:
            return [stub], {}
        return [*found, stub], configs

    monkeypatch.setattr(Runtime, "_two_stage_providers", providers)


STUB_IDS = ("m-alpha", "m-ops", "m-exp", "m-old", "m-future", "m-priv", "m-backup", "m-missing")


@pytest.mark.parametrize("policy", ["compat_fts", "strict"])
def test_filter_drops_what_a_provider_must_not_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: str
) -> None:
    repository = _vault(tmp_path)
    _with_stub(monkeypatch, _StubProvider(STUB_IDS), only=True)
    gateway = _gateway(repository.root, allowed_sources=("manual",))
    payload = gateway.search("anything at all here", limit=10, mode="two_stage", policy=policy)
    ids = _ids(payload)
    dropped = payload["recall_metrics"]["dropped"]
    # Cross-tenant, disallowed source, expired, superseded, unknown id.
    for memory_id in ("m-alpha", "m-ops", "m-exp", "m-old", "m-missing"):
        assert memory_id not in ids, memory_id
    assert dropped.get("expired") == 1
    assert dropped.get("superseded") == 1
    assert dropped.get("tenant") == 1
    assert dropped.get("source") == 1
    assert dropped.get("unresolved") == 1
    if policy == "strict":
        # Not yet valid and private are strict-only drops.
        assert ids == ["m-backup"]
        assert dropped.get("privacy") == 1
        assert dropped.get("not_yet_valid") == 1
    else:
        # compat_fts applies the index clause only, which (like mode=fts) does
        # not look at valid_from without as_of.
        assert ids == ["m-future", "m-priv", "m-backup"]
    assert all(row["providers"] == ["stub"] for row in payload["results"])
    assert all(row["why"] == "semantic" for row in payload["results"])


def test_filter_with_as_of_and_history_through_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _vault(tmp_path)
    _with_stub(monkeypatch, _StubProvider(("m-old", "m-exp", "m-future", "m-backup")), only=True)
    gateway = _gateway(repository.root)
    query = "anything at all here"
    # Before m-exp's valid_to: the expired memory was valid then; m-future was not yet.
    then = gateway.search(query, mode="two_stage", as_of="2025-12-31T00:00:00+00:00")
    assert "m-future" not in _ids(then)
    # After 2099: m-future is valid, m-exp has expired.
    later = gateway.search(query, mode="two_stage", as_of="2099-06-01T00:00:00+00:00")
    assert "m-future" in _ids(later) and "m-exp" not in _ids(later)
    # include_history keeps superseded and expired rows.
    history = gateway.search(query, mode="two_stage", include_history=True)
    assert {"m-old", "m-exp"} <= set(_ids(history))
    # Default: only the current memory (the index clause drops m-old and m-exp;
    # m-future's valid_from is beyond the index clause, so it is kept as by fts).
    now = gateway.search(query, mode="two_stage")
    assert "m-old" not in _ids(now) and "m-exp" not in _ids(now)
    strict = gateway.search(query, mode="two_stage", policy="strict")
    assert _ids(strict) == ["m-backup"]


def test_embedder_path_keeps_tenant_and_allowed_sources(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    embedder = HashEmbeddingProvider(dimension=64)
    query = "what is the nightly plan for the storage box"
    for tenant, allowed in (("", ("nas",)), ("", ("manual", "nas")), ("alpha", ())):
        gateway = _gateway(
            repository.root, embedder=embedder, tenant=tenant, allowed_sources=allowed
        )
        payload = gateway.search(query, limit=20, mode="two_stage")
        reports = {item["name"]: item for item in payload["recall_metrics"]["providers"]}
        assert reports["hybrid"]["status"] == "ok", reports
        for row in payload["results"]:
            document = repository.get(row["id"])
            assert document.tenant == tenant
            assert not allowed or document.source in allowed
        if tenant == "alpha":
            assert set(_ids(payload)) <= {"m-alpha"}
    hybrid_only = [
        row
        for row in _gateway(repository.root, embedder=embedder).search(
            query, limit=20, mode="two_stage"
        )["results"]
        if row["providers"] == ["hybrid"]
    ]
    assert hybrid_only and all(row["why"] == "semantic" for row in hybrid_only)


class _HangingProvider(_StubProvider):
    def __init__(self, release: threading.Event) -> None:
        super().__init__(("m-backup",), name="hang")
        self.release = release
        self.calls = 0

    def search(self, query: str, **kwargs: Any) -> Sequence[Candidate]:
        self.calls += 1
        self.release.wait(30)
        return []


def test_a_hung_provider_holds_one_thread_across_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _vault(tmp_path)
    release = threading.Event()
    hanging = _HangingProvider(release)
    _with_stub(monkeypatch, hanging)
    gateway = _gateway(repository.root)
    try:
        for _ in range(5):
            payload = gateway.search("homelab backup", limit=5, mode="two_stage")
            assert payload["degraded"] is True
            assert payload["recall"]["degraded"]["hang"] in {"timeout", "busy"}
            assert _ids(payload)  # FTS still answers
            workers = [t for t in threading.enumerate() if t.name == "memfw-recall-provider:hang"]
            assert len(workers) <= 1
        assert hanging.calls == 1
    finally:
        release.set()

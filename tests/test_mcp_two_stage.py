"""MCP ``memory_search`` ``mode=two_stage``: the coordinator behind the MCP tool."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from memfw.core.repository import Repository
from memfw.index import MemoryIndex
from memfw.mcp.server import TOOLS, Gateway
from memfw.notes import HashEmbeddingProvider

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


def test_unknown_policy_and_naive_as_of_are_rejected(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = _gateway(repository.root)
    with pytest.raises(ValueError, match="unknown policy"):
        gateway.search("backup", mode="two_stage", policy="loose")
    with pytest.raises(ValueError, match="unknown policy"):
        gateway.search("backup", mode="fts", policy="loose")
    with pytest.raises(ValueError, match="as_of must include a timezone"):
        gateway.search("backup", mode="two_stage", as_of="2026-01-01T00:00:00")


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

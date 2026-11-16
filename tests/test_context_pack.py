"""C1: the ``ContextPack`` contract and MCP ``memory_context`` v2."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from memfw.context import build_core_pack, context_request
from memfw.core.models import Layer, Tier
from memfw.core.repository import Repository
from memfw.mcp.server import TOOLS, Gateway
from memfw.ports import (
    ContextEntry,
    ContextPack,
    ContextSlot,
    compute_pack_version,
    pack_version_payload,
)
from memfw.ports.testing import ContractError, check_context_pack

HASH = "sha256:" + "0" * 64
NOW = datetime(2026, 11, 16, tzinfo=UTC)
PRIVATE_BODY = "the private body text that must never leak"


def _entry(memory_id: str = "m-1", text: str = "hello world", **changes: object) -> ContextEntry:
    values: dict[str, object] = {
        "id": memory_id,
        "title": "Title",
        "type": None,
        "layer": "anchor",
        "anchor": None,
        "epistemic": None,
        "causal": "none",
        "text": text,
        "version": 1,
        "content_hash": HASH,
    }
    values.update(changes)
    return ContextEntry(**values)  # type: ignore[arg-type]


def _vault(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("Anchor B", "Always cite sources.", memory_id="a-b", layer=Layer.ANCHOR)
    repository.add("Anchor A", "Prefer local tools.", memory_id="a-a", anchor="tools")
    repository.add("Proc", "1. Search 2. Read 3. Cite", memory_id="p-1", memory_type="procedural")
    repository.add("Semantic", "not resident", memory_id="s-1", memory_type="semantic")
    repository.add(
        "Private anchor",
        PRIVATE_BODY,
        memory_id="a-priv",
        layer=Layer.ANCHOR,
        tags=["privacy:local-private"],
    )
    repository.add(
        "Odd privacy",
        "odd privacy body",
        memory_id="a-odd",
        layer=Layer.ANCHOR,
        tags=["privacy:secret-ish"],
    )
    repository.add(
        "Cold proc", "cold body", memory_id="p-cold", memory_type="procedural", tier=Tier.COLD
    )
    repository.add(
        "Other tenant",
        "other tenant body",
        memory_id="a-other",
        layer=Layer.ANCHOR,
        tenant="tenant-b",
    )
    return repository


def _core(payload: dict) -> dict:
    assert [slot["name"] for slot in payload["slots"]] == ["core"]
    return payload["slots"][0]


# --- v1 stays as it was ------------------------------------------------------


def test_v1_default_payload_is_unchanged(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("Anchor", "Always cite.", memory_id="a-1", layer=Layer.ANCHOR, anchor="ctx")
    repository.add("Proc", "Search, read.", memory_id="p-1", memory_type="procedural")
    gateway = Gateway(repository.root)
    expected = {
        "limit_tokens": 2000,
        "used_chars": 25,
        "entries": [
            {
                "id": "a-1",
                "title": "Anchor",
                "type": None,
                "layer": "anchor",
                "anchor": "ctx",
                "epistemic": None,
                "causal": "none",
                "text": "Always cite.",
            },
            {
                "id": "p-1",
                "title": "Proc",
                "type": "procedural",
                "layer": "current",
                "anchor": None,
                "epistemic": None,
                "causal": "none",
                "text": "Search, read.",
            },
        ],
    }
    handler = gateway.handlers()["memory_context"]
    for payload in (gateway.context(), handler({}), handler({"v": 1}), handler({"v": None})):
        assert json.dumps(payload) == json.dumps(expected)
    assert handler({"v": 1, "policy": ""}) == expected


def test_v1_truncation_matches_the_character_budget(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("A", "a" * 60, memory_id="a-1", layer=Layer.ANCHOR)
    repository.add("B", "b" * 60, memory_id="a-2", layer=Layer.ANCHOR)
    repository.add("C", "c" * 60, memory_id="a-3", layer=Layer.ANCHOR)
    payload = Gateway(repository.root).context(limit_tokens=1)  # 100 chars minimum
    assert payload["used_chars"] == 100
    assert [entry["text"] for entry in payload["entries"]] == ["a" * 60, "b" * 40]
    assert "overflow" not in payload and "pack_version" not in payload


# --- v2 shape and contract ---------------------------------------------------


def test_v2_returns_a_valid_pack_with_the_v1_selection(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = Gateway(repository.root)
    v1 = gateway.context()
    payload = gateway.handlers()["memory_context"]({"v": 2})
    assert payload["v"] == 2
    assert payload["policy"] == "compat_fts"
    assert payload["limit_tokens"] == 2000
    assert len(payload["pack_version"]) == 64
    assert datetime.fromisoformat(payload["generated_at"]).tzinfo is not None
    core = _core(payload)
    # compat_fts: the same entries, order and text as v1.
    strip = [
        {key: value for key, value in entry.items() if key not in ("version", "content_hash")}
        for entry in core["entries"]
    ]
    assert strip == v1["entries"]
    assert core["overflow"] == 0 and core["truncated_ids"] == []
    assert core["used_tokens_est"] == (v1["used_chars"] + 3) // 4
    for entry in core["entries"]:
        assert entry["version"] >= 1
        assert entry["content_hash"].startswith("sha256:")
    pack = build_core_pack(repository, tenant="", allowed_sources=(), limit_tokens=2000, now=NOW)
    check_context_pack(pack, slots=["core"], max_tokens=2000, forbidden=[PRIVATE_BODY])
    assert pack.pack_version == payload["pack_version"]


def test_content_hash_matches_memory_search(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = Gateway(repository.root)
    found = gateway.search("Always cite sources", limit=5, mode="fts")
    by_id = {row["id"]: row["content_hash"] for row in found["results"]}
    core = _core(gateway.context(v=2))
    hashes = {entry["id"]: entry["content_hash"] for entry in core["entries"]}
    assert hashes["a-b"] == by_id["a-b"]


# --- pack_version -------------------------------------------------------------


def _version(repository: Repository, **kwargs: object) -> str:
    return build_core_pack(
        repository, tenant="", allowed_sources=(), limit_tokens=2000, now=NOW, **kwargs
    ).pack_version  # type: ignore[arg-type]


def test_pack_version_is_stable_across_runs_and_time(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    first = _version(repository)
    assert _version(repository) == first
    later = build_core_pack(
        repository,
        tenant="",
        allowed_sources=(),
        limit_tokens=2000,
        now=datetime(2030, 1, 1, tzinfo=UTC),
    )
    assert later.pack_version == first  # generated_at is not part of the key
    assert Gateway(repository.root).context(v=2)["pack_version"] == first


def test_pack_version_changes_with_an_entry_body(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    before = _version(repository)
    repository.update("p-1", body="1. Search 2. Read 3. Cite 4. Check")
    after = _version(repository)
    assert after != before
    assert repository.get("p-1").version == 2


def test_pack_version_ignores_unrelated_memories(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    before = _version(repository)
    repository.add("New semantic", "unrelated", memory_id="s-2", memory_type="semantic")
    repository.update("s-1", body="still not resident")
    repository.add("Other tenant", "x", memory_id="a-other-2", layer=Layer.ANCHOR, tenant="t-x")
    assert _version(repository) == before


def test_pack_version_depends_on_policy_and_cut(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    assert _version(repository) != _version(repository, policy="strict")
    # 100 vs 112 characters: the same memories are cut (p-1 part-way, p-cold
    # left out), only the length carried differs.
    short = build_core_pack(repository, tenant="", allowed_sources=(), limit_tokens=25, now=NOW)
    longer = build_core_pack(repository, tenant="", allowed_sources=(), limit_tokens=28, now=NOW)
    assert short.slots[0].truncated_ids == longer.slots[0].truncated_ids == ("p-1", "p-cold")
    assert short.pack_version != longer.pack_version


def test_pack_version_input_has_no_body_text() -> None:
    body = "a body that is long enough to notice"
    slot = ContextSlot("core", (_entry(text=body, title="A secret title"),), used_tokens_est=9)
    payload = pack_version_payload((slot,), "strict").decode("ascii")
    assert body not in payload and "secret title" not in payload
    assert json.loads(payload)["slots"][0]["entries"] == [["m-1", 1, HASH, len(body)]]
    other = ContextSlot("core", (_entry(text="b" * len(body), title="Other"),), used_tokens_est=9)
    # Same id, version, hash and length: the same key whatever the text.
    assert compute_pack_version((slot,), "strict") == compute_pack_version((other,), "strict")


# --- filtering ------------------------------------------------------------------


def test_strict_drops_local_private_unknown_privacy_and_archived(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = Gateway(repository.root)
    compat = {entry["id"] for entry in _core(gateway.context(v=2))["entries"]}
    assert {"a-priv", "a-odd", "p-cold"} <= compat  # compat_fts: as v1
    strict_payload = gateway.context(v=2, policy="strict")
    assert strict_payload["policy"] == "strict"
    strict = {entry["id"] for entry in _core(strict_payload)["entries"]}
    assert strict == {"a-a", "a-b", "p-1"}
    assert PRIVATE_BODY not in json.dumps(strict_payload)
    # The dropped memories are filtered out, not reported as overflow.
    assert _core(strict_payload)["truncated_ids"] == []


def test_strict_drops_superseded_and_lifecycle_tagged(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    repository.add(
        "Retired",
        "retired body",
        memory_id="p-ret",
        memory_type="procedural",
        tags=["lifecycle:retired"],
    )
    repository.supersede("p-1", "Proc v2", "new procedure", memory_id_new="p-2")
    gateway = Gateway(repository.root)
    compat = {entry["id"] for entry in _core(gateway.context(v=2))["entries"]}
    strict = {entry["id"] for entry in _core(gateway.context(v=2, policy="strict"))["entries"]}
    assert "p-1" not in compat and "p-2" in compat and "p-ret" in compat
    assert "p-ret" not in strict and "p-2" in strict


def test_tenant_and_source_isolation(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    repository.add(
        "NAS proc", "nas body", memory_id="p-nas", memory_type="procedural", source="nas"
    )
    for policy in ("compat_fts", "strict"):
        mine = Gateway(repository.root).context(v=2, policy=policy)
        assert "a-other" not in {entry["id"] for entry in _core(mine)["entries"]}
        assert "other tenant body" not in json.dumps(mine)
        theirs = Gateway(repository.root, tenant="tenant-b").context(v=2, policy=policy)
        assert [entry["id"] for entry in _core(theirs)["entries"]] == ["a-other"]
        scoped = Gateway(repository.root, allowed_sources=("nas",)).context(v=2, policy=policy)
        assert [entry["id"] for entry in _core(scoped)["entries"]] == ["p-nas"]


# --- truncation -----------------------------------------------------------------


def test_truncation_reports_overflow_and_truncated_ids(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("A", "a" * 60, memory_id="a-1", layer=Layer.ANCHOR)
    repository.add("B", "b" * 60, memory_id="a-2", layer=Layer.ANCHOR)
    repository.add("C", "c" * 60, memory_id="a-3", layer=Layer.ANCHOR)
    repository.add("D", "d" * 10, memory_id="p-1", memory_type="procedural")
    gateway = Gateway(repository.root)
    core = _core(gateway.context(limit_tokens=1, v=2))
    assert [entry["id"] for entry in core["entries"]] == ["a-1", "a-2"]
    assert core["entries"][1]["text"] == "b" * 40  # cut part-way
    assert core["truncated_ids"] == ["a-2", "a-3", "p-1"]
    assert core["overflow"] == 3
    assert core["used_tokens_est"] == 25
    # Exactly full: nothing cut, the rest overflows.
    exact = _core(gateway.context(limit_tokens=30, v=2))  # 120 chars
    assert [entry["id"] for entry in exact["entries"]] == ["a-1", "a-2"]
    assert exact["truncated_ids"] == ["a-3", "p-1"]
    # Room for everything: no overflow.
    roomy = _core(gateway.context(limit_tokens=100, v=2))
    assert roomy["overflow"] == 0 and roomy["truncated_ids"] == []
    pack = build_core_pack(repository, tenant="", allowed_sources=(), limit_tokens=1, now=NOW)
    check_context_pack(pack, slots=["core"], max_tokens=25)


# --- argument validation ----------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"v": 3}, "unknown context version"),
        ({"v": "2"}, "v must be an integer"),
        ({"v": True}, "v must be an integer"),
        ({"v": 2, "policy": "loose"}, "unknown policy"),
        ({"v": 2, "policy": 1}, "policy must be a string"),
        ({"v": 2, "policy": ["strict"]}, "policy must be a string"),
        ({"policy": "strict"}, "policy only applies to v=2"),
        ({"v": 1, "policy": "strict"}, "policy only applies to v=2"),
    ],
)
def test_argument_validation(tmp_path: Path, args: dict, message: str) -> None:
    gateway = Gateway(_vault(tmp_path).root)
    with pytest.raises(ValueError, match=message):
        gateway.handlers()["memory_context"](args)


def test_policy_defaults() -> None:
    assert context_request(None, None) == (1, "compat_fts")
    assert context_request(2, "") == (2, "compat_fts")
    assert context_request(2, None) == (2, "compat_fts")
    assert context_request(2, "strict") == (2, "strict")


def test_tool_schema_lists_the_new_arguments() -> None:
    tool = next(tool for tool in TOOLS if tool["name"] == "memory_context")
    properties = tool["inputSchema"]["properties"]
    assert properties["v"]["enum"] == [1, 2]
    assert properties["policy"]["enum"] == ["compat_fts", "strict"]
    assert properties["limit_tokens"] == {"type": "integer", "default": 2000}


# --- contract types and kit -------------------------------------------------------


def test_entry_validation() -> None:
    with pytest.raises(ValueError):
        _entry(memory_id="bad id")
    with pytest.raises(ValueError):
        _entry(content_hash="0" * 64)
    with pytest.raises(ValueError):
        _entry(epistemic="belief")
    with pytest.raises(ValueError):
        _entry(causal="proven")
    with pytest.raises(ValueError):
        _entry(version=True)


def test_slot_and_pack_validation() -> None:
    entry = _entry()
    with pytest.raises(ValueError):
        ContextSlot("core", (entry, entry), used_tokens_est=1)
    with pytest.raises(ValueError):
        ContextSlot("core", (entry,), used_tokens_est=1, overflow=1)
    with pytest.raises(ValueError):
        ContextSlot("Core!", (entry,), used_tokens_est=1)
    slot = ContextSlot("core", (entry,), used_tokens_est=3)
    pack = ContextPack.build((slot,), policy="strict", generated_at=NOW)
    with pytest.raises(ValueError):
        ContextPack((slot,), "f" * 64, "strict", NOW)
    with pytest.raises(ValueError):
        ContextPack.build((slot,), policy="strict", generated_at=datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        ContextPack.build((slot, slot), policy="strict", generated_at=NOW)
    assert check_context_pack(pack, slots=["core"]) is pack
    assert pack.slot("core") is slot


def test_contract_kit_catches_broken_packs() -> None:
    long_text = "this text is long enough to be checked"
    slot = ContextSlot("core", (_entry(text=long_text),), used_tokens_est=10)
    pack = ContextPack.build((slot,), policy="compat_fts", generated_at=NOW)
    check_context_pack(pack)
    with pytest.raises(ContractError, match="slots are"):
        check_context_pack(pack, slots=["core", "handoff"])
    with pytest.raises(ContractError, match="exceeds"):
        check_context_pack(pack, max_tokens=5)
    # A field changed after construction no longer matches the version.
    tampered = replace(pack, generated_at=NOW)
    object.__setattr__(tampered, "policy", "strict")
    with pytest.raises(ContractError, match="pack_version"):
        check_context_pack(tampered)
    with pytest.raises(ContractError, match="exactly"):
        check_context_pack({"pack_version": pack.pack_version})
    # A partial entry that is not the last carried one.
    first, second = _entry("m-1"), _entry("m-2")
    bad_slot = ContextSlot(
        "core", (first, second), used_tokens_est=1, overflow=1, truncated_ids=("m-1",)
    )
    bad = ContextPack.build((bad_slot,), policy="strict", generated_at=NOW)
    with pytest.raises(ContractError, match="partial entry"):
        check_context_pack(bad)


def test_contract_kit_rejects_a_version_that_carries_content() -> None:
    query = "the query text a caller asked about"
    slot = ContextSlot("core", (_entry(memory_id="m-1"),), used_tokens_est=1)
    pack = ContextPack.build((slot,), policy="strict", generated_at=NOW)
    check_context_pack(pack, forbidden=[query])
    # An id that happens to equal a forbidden string is content in the key.
    leaky_slot = ContextSlot("core", (_entry(memory_id="the-query-text"),), used_tokens_est=1)
    leaky = ContextPack.build((leaky_slot,), policy="strict", generated_at=NOW)
    with pytest.raises(ContractError, match="content"):
        check_context_pack(leaky, forbidden=["the-query-text"])


def test_context_capability_does_not_import_interface_layers() -> None:
    """L3 ``memfw.context`` may use core, recall and ports, never MCP/HTTP/CLI."""

    import ast

    package = Path(__file__).resolve().parents[1] / "src" / "memfw" / "context"
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                if name.split(".")[0] != "memfw":
                    continue
                assert name.startswith(
                    ("memfw.core", "memfw.recall", "memfw.ports", "memfw.context")
                ), f"{path.name} imports {name}"


# --- review follow-ups -------------------------------------------------------------


def _hand_placed(repository: Repository, source_id: str, new_id: str) -> None:
    path = repository.memories_dir / f"{source_id}.md"
    text = path.read_text(encoding="utf-8").replace(f"id: {source_id}", f"id: {new_id}")
    (repository.memories_dir / f"{new_id}.md").write_text(text, encoding="utf-8")
    path.unlink()


def test_v2_skips_an_out_of_pattern_id_that_v1_returns(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    repository.add("Dotted", "dotted body", memory_id="a-dot", layer=Layer.ANCHOR)
    _hand_placed(repository, "a-dot", "a.dot")
    gateway = Gateway(repository.root)
    assert "a.dot" in {entry["id"] for entry in gateway.context()["entries"]}
    core = _core(gateway.context(v=2))
    assert "a.dot" not in {entry["id"] for entry in core["entries"]}
    assert core["skipped"] == 1
    assert "a.dot" not in core["truncated_ids"]
    assert _core(gateway.context(v=2, policy="strict"))["skipped"] == 1


def test_v2_entry_text_version_and_hash_come_from_one_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _vault(tmp_path)
    stale = repository.list()  # p-1 at version 1
    repository.update("p-1", body="the newer procedure body")
    monkeypatch.setattr(Repository, "list", lambda self: stale)
    pack = build_core_pack(repository, tenant="", allowed_sources=(), limit_tokens=2000, now=NOW)
    entry = next(entry for entry in pack.slots[0].entries if entry.id == "p-1")
    raw = (repository.memories_dir / "p-1.md").read_bytes()
    import hashlib

    assert entry.text == "the newer procedure body"
    assert entry.version == 2
    assert entry.content_hash == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert pack.slots[0].skipped == 0


def test_v2_skips_a_memory_that_no_longer_qualifies_when_re_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _vault(tmp_path)
    stale = repository.list()
    repository.update("p-1", type="semantic")
    monkeypatch.setattr(Repository, "list", lambda self: stale)
    pack = build_core_pack(repository, tenant="", allowed_sources=(), limit_tokens=2000, now=NOW)
    assert "p-1" not in {entry.id for entry in pack.slots[0].entries}
    assert pack.slots[0].skipped == 1


def test_contract_kit_accepts_any_truncated_order_with_the_partial_last_carried() -> None:
    first, second = _entry("m-1"), _entry("m-2")
    slot = ContextSlot(
        "core", (first, second), used_tokens_est=1, overflow=2, truncated_ids=("m-9", "m-2")
    )
    check_context_pack(ContextPack.build((slot,), policy="strict", generated_at=NOW))


def test_contract_kit_usage_and_false_positives() -> None:
    slot = ContextSlot(
        "core",
        (_entry(memory_id="memory-0001", title="memory-0001", text="compat_fts"),),
        used_tokens_est=1,
    )
    pack = ContextPack.build((slot,), policy="compat_fts", generated_at=NOW)
    check_context_pack(pack)  # a title equal to its id is not content in the key
    with pytest.raises(ValueError, match="at least 8"):
        check_context_pack(pack, forbidden=["short"])
    broken = _entry()
    object.__setattr__(broken, "title", {1, 2})
    bad = ContextPack.build(
        (ContextSlot("core", (_entry("m-2"),), used_tokens_est=1),),
        policy="strict",
        generated_at=NOW,
    )
    object.__setattr__(bad.slots[0], "entries", (broken,))
    object.__setattr__(bad, "pack_version", compute_pack_version(bad.slots, bad.policy))
    with pytest.raises(ContractError, match="JSON"):
        check_context_pack(bad)

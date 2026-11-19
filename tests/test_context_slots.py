"""C2: several context slots (core, handoff, topics), degrade, and MCP resources."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from memfw.context import (
    SLOT_NAMES,
    CoreSlotProvider,
    HandoffSlotProvider,
    SessionSummary,
    SlotInflight,
    TopicCatalogue,
    TopicsSlotProvider,
    assemble_pack,
    cut_items,
    slots_request,
)
from memfw.core.models import Layer
from memfw.core.repository import Repository
from memfw.mcp import schema
from memfw.mcp.server import TOOLS, Gateway
from memfw.ports import (
    ContextItem,
    ContextPack,
    ContextSlot,
    ContextSlotProvider,
    SlotRequest,
    compute_pack_version,
    pack_version_payload,
)
from memfw.ports.testing import (
    ContractError,
    check_context_pack,
    check_context_slot_provider,
    tree_digest,
)
from tests.conversation_fixtures import (
    EMAIL,
    SECRET,
    isolated_home,  # noqa: F401  (fixture)
    standard_rows,
    write_store,
)

# Every test runs with an empty HOME: no real data directory is ever read.
pytestmark = pytest.mark.usefixtures("isolated_home")

NOW = datetime(2026, 11, 16, tzinfo=UTC)
BODY = "the body text of a concept memory that must never be in topics"
REQUEST = SlotRequest(limit_tokens=2000, policy="compat_fts", now=NOW)


def _concept(root: Path, oid: str, label: str, **extra: str) -> None:
    slug = oid.split(":")[-1]
    lines = [f"oid: {oid}", "class: Concept", f"pref_label: {label}"]
    lines += [f"{key}: {value}" for key, value in extra.items()]
    path = root / "concepts" / "Concept" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\n" + "\n".join(lines) + "\n---\n\nA definition.\n", encoding="utf-8")


def _vault(tmp_path: Path, share: dict[str, Any] | None = None) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("Anchor", "Always cite sources.", memory_id="a-1", layer=Layer.ANCHOR)
    repository.add("Proc", "1. Search 2. Read", memory_id="p-1", memory_type="procedural")
    repository.add("Port A", BODY, memory_id="m-a", about=["concept:service-port"])
    repository.add("Port B", BODY, memory_id="m-b", about=["concept:service-port"])
    repository.add(
        "Backup", BODY, memory_id="m-c", about=["concept:backup"], applies_to=["concept:old"]
    )
    repository.add("Other tenant", BODY, memory_id="m-t", about=["concept:backup"], tenant="beta")
    repository.add(
        "Private",
        BODY,
        memory_id="m-p",
        about=["concept:backup", "concept:secret-topic"],
        tags=["privacy:local-private"],
    )
    repository.add("Nas", BODY, memory_id="m-n", about=["concept:service-port"], source="nas")
    _concept(repository.root, "concept:service-port", "Service port", status="active")
    _concept(repository.root, "concept:backup", "Backup", status="active")
    _concept(
        repository.root, "concept:old", "Old backup", status="active", merged_into="concept:backup"
    )
    _concept(repository.root, "concept:secret-topic", "Secret topic", status="active")
    _concept(repository.root, "concept:draft", "Draft", status="candidate")
    store = write_store(tmp_path / "data" / "conversations.sqlite", standard_rows())
    config = repository.read_config()
    config["conversation_store"] = {"path": str(store)}
    if share is not None:
        config["conversation_share"] = share
    repository.configure(config)
    return repository


SHARE = {
    "enabled": True,
    "apps": {"grok": {"can_read": False}, "cursor": {"readable": False}},
}


def _call(gateway: Gateway, arguments: dict[str, Any]) -> dict[str, Any]:
    sent: list[Any] = []
    gateway.respond = sent.append  # type: ignore[method-assign]
    gateway.handle(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "memory_context", "arguments": arguments},
        }
    )
    return sent[0]["result"]


def _ok(gateway: Gateway, arguments: dict[str, Any]) -> dict[str, Any]:
    result = _call(gateway, arguments)
    assert not result["isError"], result
    payload: dict[str, Any] = json.loads(result["content"][0]["text"])
    return payload


def _rpc(gateway: Gateway, method: str, params: Any = None) -> dict[str, Any]:
    sent: list[Any] = []
    gateway.respond = sent.append  # type: ignore[method-assign]
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": 5, "method": method}
    if params is not None:
        message["params"] = params
    gateway.handle(message)
    assert len(sent) == 1
    response: dict[str, Any] = sent[0]
    return response


def _slot(payload: dict[str, Any], name: str) -> dict[str, Any]:
    slot: dict[str, Any] = next(slot for slot in payload["slots"] if slot["name"] == name)
    return slot


# --- slot selection ------------------------------------------------------------------


def test_slots_come_back_in_the_requested_order(tmp_path: Path) -> None:
    repository = _vault(tmp_path, SHARE)
    gateway = Gateway(repository.root, app="claude")
    for order in (["topics", "core"], ["core", "handoff", "topics"], ["handoff"]):
        payload = _ok(gateway, {"v": 2, "slots": order})
        assert [slot["name"] for slot in payload["slots"]] == order
        assert all("degraded" not in slot for slot in payload["slots"])


def test_default_slots_match_the_explicit_core_slot(tmp_path: Path) -> None:
    repository = _vault(tmp_path, SHARE)
    gateway = Gateway(repository.root, app="claude")
    default = _ok(gateway, {"v": 2})
    explicit = _ok(gateway, {"v": 2, "slots": ["core"]})
    assert [slot["name"] for slot in default["slots"]] == ["core"]
    del default["generated_at"], explicit["generated_at"]
    assert default == explicit
    # retrieval_health (retrieval-health/1) is the one key added since C2.
    assert set(default) == {
        "v",
        "limit_tokens",
        "pack_version",
        "policy",
        "slots",
        "retrieval_health",
    }
    assert set(default["slots"][0]) == {
        "name",
        "entries",
        "used_tokens_est",
        "overflow",
        "truncated_ids",
        "skipped",
    }


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"v": 2, "slots": ["core", "nope"]}, "unknown slot"),
        ({"v": 2, "slots": []}, "non-empty list"),
        ({"v": 2, "slots": "core"}, "non-empty list"),
        ({"v": 2, "slots": [1]}, "non-empty list"),
        ({"v": 2, "slots": ["core", "core"]}, "repeat"),
        ({"v": 1, "slots": ["core"]}, "only applies to v=2"),
        ({"slots": ["core"]}, "only applies to v=2"),
    ],
)
def test_slot_argument_errors(tmp_path: Path, arguments: dict[str, Any], message: str) -> None:
    repository = _vault(tmp_path, SHARE)
    result = _call(Gateway(repository.root, app="claude"), arguments)
    assert result["isError"]
    assert message in result["content"][0]["text"]


def test_unknown_slot_error_does_not_echo_the_input() -> None:
    with pytest.raises(ValueError) as caught:
        slots_request(2, ["core", "x" * 40])
    assert "x" * 40 not in str(caught.value)
    assert str(caught.value) == "unknown slot (known: core, handoff, topics)"


def test_schema_and_resource_names_match_the_capability() -> None:
    tool = next(tool for tool in TOOLS if tool["name"] == "memory_context")
    assert tool["inputSchema"]["properties"]["slots"]["items"]["enum"] == list(SLOT_NAMES)
    assert schema.CONTEXT_SLOTS == SLOT_NAMES
    assert [item["uri"] for item in schema.RESOURCES] == [
        f"memfw://context/{name}" for name in SLOT_NAMES
    ]


# --- handoff ---------------------------------------------------------------------------


def test_handoff_lists_visible_sessions_with_refs_and_redacted_text(tmp_path: Path) -> None:
    repository = _vault(tmp_path, SHARE)
    payload = _ok(Gateway(repository.root, app="claude"), {"v": 2, "slots": ["handoff"]})
    slot = _slot(payload, "handoff")
    ids = [entry["id"] for entry in slot["entries"]]
    assert set(ids) == {"conversation:codex/s-alpha", "conversation:claude/s-beta"}
    alpha = next(entry for entry in slot["entries"] if entry["id"].endswith("s-alpha"))
    assert alpha["kind"] == "session"
    assert alpha["text"] == "Add a retry wrapper to the flaky uploader."
    assert alpha["count"] == 4
    assert alpha["refs"] and all(ref.startswith("codex:s-alpha@") for ref in alpha["refs"])
    text = json.dumps(payload, ensure_ascii=False)
    assert EMAIL not in text and SECRET not in text
    assert "cursor" not in text and "s-gamma" not in text  # hidden app never appears
    assert "/work/demo" not in text  # no cwd


def test_handoff_obeys_can_read(tmp_path: Path) -> None:
    repository = _vault(
        tmp_path,
        {**SHARE, "apps": {**SHARE["apps"], "kimi": {"can_read": ["codex"]}}},
    )
    # grok may not read anything: an empty slot, not an error and not degraded.
    grok = _slot(
        _ok(Gateway(repository.root, app="grok"), {"v": 2, "slots": ["handoff"]}), "handoff"
    )
    assert grok["entries"] == [] and grok["overflow"] == 0 and "degraded" not in grok
    kimi = _slot(
        _ok(Gateway(repository.root, app="kimi"), {"v": 2, "slots": ["handoff"]}), "handoff"
    )
    assert [entry["id"] for entry in kimi["entries"]] == ["conversation:codex/s-alpha"]


@pytest.mark.parametrize("app", ["", "operator"])
def test_handoff_without_a_named_app_is_empty(tmp_path: Path, app: str) -> None:
    repository = _vault(tmp_path, SHARE)
    slot = _slot(_ok(Gateway(repository.root, app=app), {"v": 2, "slots": ["handoff"]}), "handoff")
    assert slot["entries"] == [] and "degraded" not in slot


def test_handoff_with_sharing_off_is_empty(tmp_path: Path) -> None:
    repository = _vault(tmp_path, None)
    slot = _slot(
        _ok(Gateway(repository.root, app="claude"), {"v": 2, "slots": ["handoff"]}), "handoff"
    )
    assert slot["entries"] == [] and "degraded" not in slot


def test_handoff_with_a_missing_store_is_degraded(tmp_path: Path) -> None:
    repository = _vault(tmp_path, SHARE)
    (tmp_path / "data" / "conversations.sqlite").unlink()
    payload = _ok(Gateway(repository.root, app="claude"), {"v": 2, "slots": ["core", "handoff"]})
    assert _slot(payload, "handoff")["degraded"] == "unavailable"
    assert _slot(payload, "core")["entries"]


# --- topics ----------------------------------------------------------------------------


def test_topics_carry_labels_and_counts_only(tmp_path: Path) -> None:
    repository = _vault(tmp_path, SHARE)
    payload = _ok(Gateway(repository.root), {"v": 2, "slots": ["topics"]})
    slot = _slot(payload, "topics")
    found = {entry["id"]: (entry["label"], entry["count"]) for entry in slot["entries"]}
    # Tenant "" sees m-a, m-b, m-n (service port) and m-c + m-p (backup; m-c via the
    # merged concept:old counts once). Candidates and other tenants are not counted.
    assert found == {
        "concept:service-port": ("Service port", 3),
        "concept:backup": ("Backup", 2),
        "concept:secret-topic": ("Secret topic", 1),
    }
    assert [entry["id"] for entry in slot["entries"]][0] == "concept:service-port"
    assert all(entry["text"] == "" and entry["kind"] == "topic" for entry in slot["entries"])
    text = json.dumps(payload)
    assert BODY not in text and "concept:draft" not in text and "concept:old" not in text


def test_topic_counts_follow_tenant_sources_and_policy(tmp_path: Path) -> None:
    repository = _vault(tmp_path, SHARE)

    def topics(**kwargs: Any) -> dict[str, int]:
        policy = kwargs.pop("policy", "compat_fts")
        gateway = Gateway(repository.root, **kwargs)
        payload = _ok(gateway, {"v": 2, "slots": ["topics"], "policy": policy})
        return {entry["id"]: entry["count"] for entry in _slot(payload, "topics")["entries"]}

    assert topics(tenant="beta") == {"concept:backup": 1}
    assert topics(allowed_sources=("manual",)) == {
        "concept:service-port": 2,
        "concept:backup": 2,
        "concept:secret-topic": 1,
    }
    # strict drops local-private: its only topic disappears, backup loses one.
    assert topics(policy="strict") == {"concept:service-port": 3, "concept:backup": 1}


def test_topics_with_an_invalid_concept_file_are_degraded(tmp_path: Path) -> None:
    repository = _vault(tmp_path, SHARE)
    (repository.root / "concepts" / "Concept" / "bad.md").write_bytes(b"---\npref_label: x\n---\n")
    payload = _ok(Gateway(repository.root), {"v": 2, "slots": ["core", "topics"]})
    assert _slot(payload, "topics") == {
        "name": "topics",
        "entries": [],
        "used_tokens_est": 0,
        "overflow": 0,
        "truncated_ids": [],
        "skipped": 0,
        "degraded": "error",
    }
    assert "concepts" not in json.dumps(payload)  # no path, no message
    assert _slot(payload, "core")["entries"]


def test_topics_without_concepts_are_empty(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("Port", BODY, memory_id="m-a", about=["concept:service-port"])
    slot = _slot(_ok(Gateway(repository.root), {"v": 2, "slots": ["topics"]}), "topics")
    assert slot["entries"] == [] and "degraded" not in slot


def test_item_slots_overflow_whole_items() -> None:
    items = [ContextItem.build("topic", f"concept:t{index}", label="x" * 40) for index in range(5)]
    slot = cut_items("topics", items, limit_tokens=20)  # 100 characters: two items fit
    assert [entry.id for entry in slot.entries] == ["concept:t0", "concept:t1"]
    assert slot.truncated_ids == ("concept:t2", "concept:t3", "concept:t4")
    assert slot.overflow == 3 and slot.used_tokens_est == 20
    check_context_pack(ContextPack.build((slot,), policy="compat_fts", generated_at=NOW))


# --- degrade ---------------------------------------------------------------------------


class _Raising:
    name = "handoff"

    def build(self, request: SlotRequest) -> ContextSlot:
        raise RuntimeError("/private/path/secret failure")


class _Slow:
    def __init__(self, name: str, release: threading.Event) -> None:
        self.name = name
        self.release = release

    def build(self, request: SlotRequest) -> ContextSlot:
        self.release.wait(5)
        return ContextSlot(self.name, (), 0)


class _WrongName:
    name = "topics"

    def build(self, request: SlotRequest) -> ContextSlot:
        return ContextSlot("core", (), 0)


def test_a_raising_provider_degrades_only_its_slot(tmp_path: Path) -> None:
    repository = _vault(tmp_path, SHARE)
    gateway = Gateway(repository.root, app="claude")
    original = gateway.runtime.context_slot_provider

    def provider(name: str, repo: Repository, **kwargs: Any) -> ContextSlotProvider:
        if name == "handoff":
            return _Raising()
        return original(name, repo, **kwargs)

    gateway.runtime.context_slot_provider = provider  # type: ignore[method-assign]
    payload = _ok(gateway, {"v": 2, "slots": ["core", "handoff", "topics"]})
    assert [slot["name"] for slot in payload["slots"]] == ["core", "handoff", "topics"]
    assert _slot(payload, "handoff")["degraded"] == "error"
    assert "secret failure" not in json.dumps(payload)
    assert _slot(payload, "core")["entries"] and _slot(payload, "topics")["entries"]
    pack = ContextPack.build(
        [ContextSlot(**_slot_args(slot)) for slot in payload["slots"]],
        policy="compat_fts",
        generated_at=NOW,
    )
    assert pack.pack_version == payload["pack_version"]


def _slot_args(slot: dict[str, Any]) -> dict[str, Any]:
    entries: list[Any] = []
    for entry in slot["entries"]:
        if "kind" in entry:
            entries.append(ContextItem(**{**entry, "refs": tuple(entry["refs"])}))
        else:
            from memfw.ports import ContextEntry

            entries.append(ContextEntry(**entry))
    return {
        "name": slot["name"],
        "entries": tuple(entries),
        "used_tokens_est": slot["used_tokens_est"],
        "overflow": slot["overflow"],
        "truncated_ids": tuple(slot["truncated_ids"]),
        "skipped": slot["skipped"],
        "degraded": slot.get("degraded"),
    }


def test_a_slow_provider_times_out_then_reports_busy(tmp_path: Path) -> None:
    release = threading.Event()
    inflight = SlotInflight(limit=1)
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("Anchor", "Always cite sources.", memory_id="a-1", layer=Layer.ANCHOR)
    core = CoreSlotProvider(repository, lambda document: True)
    try:
        first = assemble_pack(
            [_Slow("topics", release), core],
            REQUEST,
            timeouts={"topics": 0.05},
            default_timeout=5.0,
            inflight=inflight,
        )
        assert first.slot("topics").degraded == "timeout"
        assert [entry.id for entry in first.slot("core").entries] == ["a-1"]
        second = assemble_pack(
            [_Slow("topics", release)],
            REQUEST,
            timeouts={"topics": 0.05},
            default_timeout=5.0,
            inflight=inflight,
        )
        assert second.slot("topics").degraded == "busy"
    finally:
        release.set()
    for _ in range(100):
        if inflight.running("topics") == 0:
            break
        threading.Event().wait(0.01)
    assert inflight.running("topics") == 0


def test_a_provider_returning_another_slot_is_invalid() -> None:
    pack = assemble_pack([_WrongName()], REQUEST, timeouts={}, default_timeout=1.0)
    assert pack.slot("topics").degraded == "invalid"


def test_degraded_slots_change_the_version_and_carry_nothing() -> None:
    healthy = ContextSlot("handoff", (), 0)
    degraded = ContextSlot.degraded_slot("handoff", "timeout")
    assert compute_pack_version((healthy,), "compat_fts") != compute_pack_version(
        (degraded,), "compat_fts"
    )
    assert b'"degraded":"timeout"' in pack_version_payload((degraded,), "compat_fts")
    assert b"degraded" not in pack_version_payload((healthy,), "compat_fts")
    with pytest.raises(ValueError):
        ContextSlot("handoff", (), 1, degraded="timeout")
    with pytest.raises(ValueError):
        ContextSlot("handoff", (), 0, degraded="Bad Reason")


# --- pack_version ------------------------------------------------------------------------


def test_pack_version_is_stable_and_covers_each_slot(tmp_path: Path) -> None:
    repository = _vault(tmp_path, SHARE)
    gateway = Gateway(repository.root, app="claude")
    slots = ["core", "handoff", "topics"]
    first = _ok(gateway, {"v": 2, "slots": slots})
    again = _ok(gateway, {"v": 2, "slots": slots})
    assert first["pack_version"] == again["pack_version"]
    reordered = _ok(gateway, {"v": 2, "slots": ["topics", "handoff", "core"]})
    assert reordered["pack_version"] != first["pack_version"]
    core_only = _ok(gateway, {"v": 2})
    assert core_only["pack_version"] != first["pack_version"]
    # A new memory about a topic changes the topic count and so the version.
    repository.add("Port C", BODY, memory_id="m-z", about=["concept:service-port"])
    changed = _ok(gateway, {"v": 2, "slots": slots})
    assert changed["pack_version"] != first["pack_version"]
    assert _ok(gateway, {"v": 2})["pack_version"] == core_only["pack_version"]


def test_runtime_pack_passes_the_contract_kit(tmp_path: Path) -> None:
    repository = _vault(tmp_path, SHARE)
    runtime = Gateway(repository.root, app="claude").runtime
    pack = runtime.context_pack(
        repository,
        policy_name="compat_fts",
        limit_tokens=2000,
        now=NOW,
        slots=SLOT_NAMES,
        app="claude",
    )
    check_context_pack(pack, slots=list(SLOT_NAMES), forbidden=[BODY, SECRET, EMAIL])
    with pytest.raises(ValueError, match="unknown slot"):
        runtime.context_pack(
            repository, policy_name="compat_fts", limit_tokens=10, slots=("bogus",)
        )


# --- provider contract kit -----------------------------------------------------------------


def test_every_builtin_provider_passes_the_contract_kit(tmp_path: Path) -> None:
    repository = _vault(tmp_path, SHARE)
    runtime = Gateway(repository.root, app="claude").runtime
    keep = runtime.context_keep("compat_fts", now=NOW)
    for name in SLOT_NAMES:
        provider = runtime.context_slot_provider(name, repository, keep=keep, app="claude")
        slot = check_context_slot_provider(
            provider, request=REQUEST, forbidden=[BODY, SECRET], vault=repository.root
        )
        assert slot.entries, name


def test_contract_kit_rejects_broken_providers() -> None:
    with pytest.raises(ContractError, match="ContextSlotProvider"):
        check_context_slot_provider(object(), request=REQUEST)
    with pytest.raises(ContractError, match="named"):
        check_context_slot_provider(_WrongName(), request=REQUEST)

    class Unstable:
        name = "topics"
        calls = 0

        def build(self, request: SlotRequest) -> ContextSlot:
            self.calls += 1
            item = ContextItem.build("topic", f"concept:t{self.calls}", label="T", count=1)
            return ContextSlot("topics", (item,), 1)

    with pytest.raises(ContractError, match="different version"):
        check_context_slot_provider(Unstable(), request=REQUEST)

    class Degraded:
        name = "topics"

        def build(self, request: SlotRequest) -> ContextSlot:
            return ContextSlot.degraded_slot("topics", "error")

    with pytest.raises(ContractError, match="degraded"):
        check_context_slot_provider(Degraded(), request=REQUEST)
    assert check_context_slot_provider(Degraded(), request=REQUEST, allow_degraded=True)


def test_injected_providers_are_pure_over_their_sources(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("Port", BODY, memory_id="m-a", about=["concept:x"])
    before = tree_digest(repository.root)
    feed_calls: list[int] = []

    def feed(limit: int) -> list[SessionSummary]:
        feed_calls.append(limit)
        return [
            SessionSummary("codex", "s-1", 3, "2026-01-01", "goal " * 200, ("codex:s-1@0",)),
            SessionSummary("codex", "s-1", 3, "2026-01-01", "duplicate", ()),
            SessionSummary("codex", "bad id", 1, "", "skipped: whitespace in the id", ()),
        ]

    handoff = HandoffSlotProvider(feed).build(REQUEST)
    assert feed_calls == [3]
    assert [entry.id for entry in handoff.entries] == ["conversation:codex/s-1"]
    assert len(handoff.entries[0].text) <= 280
    topics = TopicsSlotProvider(
        repository,
        lambda document: True,
        lambda: TopicCatalogue(labels={"concept:x": "X"}, canonical={"concept:x": "concept:x"}),
    ).build(REQUEST)
    assert [(entry.id, entry.count) for entry in topics.entries] == [("concept:x", 1)]
    assert tree_digest(repository.root) == before


def test_item_validation() -> None:
    item = ContextItem.build("topic", "concept:x", label="X", count=2)
    with pytest.raises(ValueError, match="content_hash"):
        ContextItem("topic", "concept:x", "Y", "", 2, (), item.content_hash)
    with pytest.raises(ValueError):
        ContextItem.build("topic", "has space", label="X")
    with pytest.raises(ValueError):
        ContextItem.build("topic", "concept:x", label="x" * 201)
    with pytest.raises(ValueError):
        ContextItem.build("topic", "concept:x", label="X", count=-1)
    with pytest.raises(ValueError, match="all ContextEntry or all ContextItem"):
        from memfw.ports import ContextEntry

        entry = ContextEntry(
            "m-1", "T", None, "anchor", None, None, "none", "t", 1, "sha256:" + "0" * 64
        )
        ContextSlot("mixed", (entry, item), 1)


# --- MCP resources -----------------------------------------------------------------------


def test_initialize_advertises_resources(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    result = _rpc(Gateway(repository.root), "initialize", {})["result"]
    assert result["capabilities"] == {
        "tools": {"listChanged": False},
        "resources": {"listChanged": False},
    }


def test_resources_list_and_read(tmp_path: Path) -> None:
    repository = _vault(tmp_path, SHARE)
    gateway = Gateway(repository.root, app="claude")
    listed = _rpc(gateway, "resources/list")["result"]["resources"]
    assert [item["uri"] for item in listed] == [
        "memfw://context/core",
        "memfw://context/handoff",
        "memfw://context/topics",
    ]
    assert all(item["mimeType"] == "application/json" for item in listed)
    for name in SLOT_NAMES:
        uri = f"memfw://context/{name}"
        contents = _rpc(gateway, "resources/read", {"uri": uri})["result"]["contents"]
        assert len(contents) == 1 and contents[0]["uri"] == uri
        body = json.loads(contents[0]["text"])
        policy = "strict" if name == "topics" else "compat_fts"
        tool = _ok(gateway, {"v": 2, "slots": [name], "policy": policy})
        del body["generated_at"], tool["generated_at"]
        assert body == tool  # the same pack as the tool, pack_version included
        assert body["policy"] == policy and len(body["pack_version"]) == 64


def test_resources_honour_the_caller(tmp_path: Path) -> None:
    repository = _vault(tmp_path, SHARE)
    grok = _rpc(
        Gateway(repository.root, app="grok"), "resources/read", {"uri": "memfw://context/handoff"}
    )
    assert json.loads(grok["result"]["contents"][0]["text"])["slots"][0]["entries"] == []
    beta = _rpc(
        Gateway(repository.root, tenant="beta"), "resources/read", {"uri": "memfw://context/topics"}
    )
    entries = json.loads(beta["result"]["contents"][0]["text"])["slots"][0]["entries"]
    assert {entry["id"]: entry["count"] for entry in entries} == {"concept:backup": 1}


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"uri": "memfw://context/nope"}, -32002),
        ({"uri": "memfw://other/core"}, -32002),
        ({"uri": "memfw://context/"}, -32002),
        ({}, -32602),
        ({"uri": 5}, -32602),
    ],
)
def test_resource_read_errors(tmp_path: Path, params: dict[str, Any], code: int) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    response = _rpc(Gateway(repository.root), "resources/read", params)
    assert response["error"]["code"] == code


def test_resource_read_of_a_missing_vault_is_an_error(tmp_path: Path) -> None:
    response = _rpc(
        Gateway(tmp_path / "missing"), "resources/read", {"uri": "memfw://context/core"}
    )
    assert response["error"] == {"code": -32603, "message": "vault root does not exist"}


def test_tools_list_is_unchanged_but_for_slots() -> None:
    tool = next(tool for tool in TOOLS if tool["name"] == "memory_context")
    # C3 adds the optional ``if_version`` (see tests/test_context_cache.py).
    assert sorted(tool["inputSchema"]["properties"]) == [
        "if_version",
        "limit_tokens",
        "policy",
        "slots",
        "v",
    ]
    assert tool["inputSchema"]["properties"]["slots"]["default"] == ["core"]


# --- review follow-ups (C2) ----------------------------------------------------------------

PENDING = "A pending transaction needs explicit `memfw recover`"


def _pending(repository: Repository) -> None:
    repository.pending_path.write_text("{}", encoding="utf-8")


def test_default_request_keeps_the_c1_error_on_a_pending_transaction(tmp_path: Path) -> None:
    repository = _vault(tmp_path, SHARE)
    _pending(repository)
    gateway = Gateway(repository.root, app="claude")
    for arguments in ({"v": 2}, {"v": 2, "slots": ["core"]}, {"v": 2, "policy": "strict"}, {}):
        result = _call(gateway, arguments)
        assert result["isError"], arguments
        assert result["content"][0]["text"] == PENDING
    response = _rpc(gateway, "resources/read", {"uri": "memfw://context/core"})
    assert response["error"] == {"code": -32603, "message": PENDING}


def test_multi_slot_request_names_the_vault_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    repository = _vault(tmp_path, SHARE)
    _pending(repository)
    with caplog.at_level("WARNING", logger="memfw.context.slots"):
        payload = _ok(
            Gateway(repository.root, app="claude"), {"v": 2, "slots": ["core", "handoff", "topics"]}
        )
    assert _slot(payload, "core")["degraded"] == "pending_recovery"
    assert _slot(payload, "topics")["degraded"] == "pending_recovery"
    assert "degraded" not in _slot(payload, "handoff") and _slot(payload, "handoff")["entries"]
    logged = " | ".join(record.getMessage() for record in caplog.records)
    assert "context slot core degraded: pending_recovery (SlotDegraded)" in logged
    assert "explicit" not in logged and str(repository.root) not in logged


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        ("IntegrityError", "integrity"),
        ("NotFoundError", "not_found"),
        ("DeletedContentUnavailable", "vault_error"),
        ("RuntimeError", "error"),
    ],
)
def test_failures_map_to_reason_codes_and_log_the_class_only(
    error: str, reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    from memfw.core import errors

    exc_type = getattr(errors, error, RuntimeError)

    class Failing:
        name = "topics"

        def build(self, request: SlotRequest) -> ContextSlot:
            raise exc_type("/private/vault/path secret detail")

    with caplog.at_level("WARNING", logger="memfw.context.slots"):
        pack = assemble_pack([Failing()], REQUEST, timeouts={}, default_timeout=1.0)
    assert pack.slot("topics").degraded == reason
    logged = " | ".join(record.getMessage() for record in caplog.records)
    assert f"context slot topics degraded: {reason} ({exc_type.__name__})" in logged
    assert "secret detail" not in logged and "/private" not in logged


def test_a_thread_that_cannot_start_degrades_and_frees_its_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inflight = SlotInflight(limit=1)

    def refuse(self: threading.Thread) -> None:
        raise RuntimeError("can't start new thread")

    class Fine:
        name = "topics"

        def build(self, request: SlotRequest) -> ContextSlot:
            return ContextSlot("topics", (), 0)

    monkeypatch.setattr(threading.Thread, "start", refuse)
    pack = assemble_pack([Fine()], REQUEST, timeouts={}, default_timeout=1.0, inflight=inflight)
    monkeypatch.undo()
    assert pack.slot("topics").degraded == "error"
    assert inflight.running("topics") == 0
    again = assemble_pack([Fine()], REQUEST, timeouts={}, default_timeout=1.0, inflight=inflight)
    assert again.slot("topics").degraded is None


def test_topics_resource_defaults_to_strict(tmp_path: Path) -> None:
    repository = _vault(tmp_path, SHARE)
    gateway = Gateway(repository.root)

    def read(uri: str) -> dict[str, Any]:
        response = _rpc(gateway, "resources/read", {"uri": uri})
        body: dict[str, Any] = json.loads(response["result"]["contents"][0]["text"])
        return body

    def counts(body: dict[str, Any]) -> dict[str, int]:
        return {entry["id"]: entry["count"] for entry in body["slots"][0]["entries"]}

    default = read("memfw://context/topics")
    assert default["policy"] == "strict"
    # The local-private memory is not counted: its only topic is absent.
    assert counts(default) == {"concept:service-port": 3, "concept:backup": 1}
    compat = read("memfw://context/topics?policy=compat_fts")
    assert compat["policy"] == "compat_fts" and counts(compat)["concept:secret-topic"] == 1
    assert read("memfw://context/core")["policy"] == "compat_fts"
    assert read("memfw://context/core?policy=strict")["policy"] == "strict"
    assert read("memfw://context/handoff?policy=strict")["policy"] == "strict"


@pytest.mark.parametrize(
    "query",
    [
        "?policy=loose",
        "?policy=",
        "?policy",
        "?",
        "?policy=strict&policy=compat_fts",
        "?limit_tokens=10",
        "?Policy=strict",
        "?policy=strict#x",
    ],
)
def test_resource_query_fails_closed(tmp_path: Path, query: str) -> None:
    repository = _vault(tmp_path, SHARE)
    response = _rpc(
        Gateway(repository.root), "resources/read", {"uri": "memfw://context/topics" + query}
    )
    assert response["error"] == {"code": -32602, "message": "invalid params"}

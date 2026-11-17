"""U6: the optional ``current`` context slot (short-lived context with a lifetime)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from memfw.context import (
    CLOCK_SKEW,
    CURRENT_SLOT,
    DEFAULT_CURRENT_TTL_DAYS,
    SLOT_NAMES,
    CurrentSlotProvider,
    is_current,
    select_current,
)
from memfw.core.models import Layer, MemoryDocument, Tier
from memfw.core.repository import Repository
from memfw.mcp import schema
from memfw.mcp.server import TOOLS, Gateway
from memfw.ports import ContextPack, SlotRequest
from memfw.ports.testing import check_context_pack, check_context_slot_provider, tree_digest
from memfw.runtime import build_runtime
from memfw.runtime.usage import RuntimeOptions, options_from_config
from tests.conversation_fixtures import isolated_home  # noqa: F401  (fixture)
from tests.harness.m3c_allowance import with_m3c_tools_list

pytestmark = pytest.mark.usefixtures("isolated_home")

T0 = datetime(2026, 11, 1, tzinfo=UTC)
NOW = T0 + timedelta(days=1)
BODY = "the body text of a current memory that never goes in the version key"


class _Clock:
    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


def _vault(tmp_path: Path, *, enabled: bool | None = True, ttl: Any = None) -> Repository:
    clock = _Clock(T0)
    repository = Repository(tmp_path / "vault", clock=clock)
    repository.init()
    repository.add("Anchor", "Always cite sources.", memory_id="a-1", layer=Layer.ANCHOR)
    repository.add("Ticket", "Working on ticket ABC-1", memory_id="c-old", tags=["current"])
    clock.moment = T0 + timedelta(hours=1)
    repository.add("Meeting", "Design review at 3pm", memory_id="c-new", tags=[" Current "])
    repository.add("Not current", BODY, memory_id="m-plain")
    config = repository.read_config()
    context: dict[str, Any] = {}
    if enabled is not None:
        context["current"] = enabled
    if ttl is not None:
        context["current_ttl_days"] = ttl
    if context:
        config["runtime"] = {"context": context}
    repository.configure(config)
    return repository


def _ids(slot: Any) -> list[str]:
    return [entry.id for entry in slot.entries]


def _pack(repository: Repository, *, now: datetime = NOW, policy: str = "compat_fts", **kw: Any):
    runtime = build_runtime(repository.root, **kw)
    return runtime.context_pack(
        repository, policy_name=policy, limit_tokens=2000, now=now, slots=[CURRENT_SLOT]
    ).slot(CURRENT_SLOT)


def _call(gateway: Gateway, method: str, params: Any = None) -> dict[str, Any]:
    sent: list[Any] = []
    gateway.respond = sent.append  # type: ignore[method-assign]
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        message["params"] = params
    gateway.handle(message)
    assert len(sent) == 1
    response: dict[str, Any] = sent[0]
    return response


def _context(gateway: Gateway, arguments: dict[str, Any]) -> dict[str, Any]:
    response = _call(gateway, "tools/call", {"name": "memory_context", "arguments": arguments})
    result: dict[str, Any] = response["result"]
    return result


# --- selection -------------------------------------------------------------------------


def test_newest_first_and_only_tagged_memories(tmp_path: Path) -> None:
    slot = _pack(_vault(tmp_path))
    assert _ids(slot) == ["c-new", "c-old"]
    assert slot.overflow == 0 and slot.skipped == 0


def _doc(memory_id: str, created: datetime, **extra: Any) -> MemoryDocument:
    return MemoryDocument(
        id=memory_id,
        title=memory_id,
        tags=extra.pop("tags", ["current"]),
        created_at=created,
        updated_at=extra.pop("updated_at", created),
        body="b",
        **extra,
    )


def test_ordering_ties_break_by_id() -> None:
    documents = [_doc("c-b", T0), _doc("c-a", T0), _doc("c-c", T0 + timedelta(minutes=1))]
    ordered = select_current(documents, lambda d: True, now=NOW, ttl_days=7)
    assert [d.id for d in ordered] == ["c-c", "c-a", "c-b"]


def test_validity_window_and_ttl() -> None:
    def current(now: datetime, ttl: int = 7, **extra: Any) -> bool:
        return is_current(_doc("c-1", T0, **extra), now=now, ttl_days=ttl)

    assert current(T0)
    assert current(T0 + timedelta(days=7) - timedelta(seconds=1))
    assert not current(T0 + timedelta(days=7))  # TTL expired (now < created_at + ttl)
    # updated_at is not the anchor: a later write never extends the lifetime.
    assert not current(T0 + timedelta(days=8), updated_at=T0 + timedelta(days=7, hours=1))
    assert not current(T0 + timedelta(days=2), ttl=1)
    assert not current(T0, ttl=0)  # no implicit lifetime
    # valid_to governs when set, past the TTL too; it is exclusive.
    until = T0 + timedelta(days=30)
    assert current(T0 + timedelta(days=20), valid_to=until)
    assert not current(until, valid_to=until)
    assert current(T0, ttl=0, valid_to=until)
    # valid_from opens the window (inclusive).
    start = T0 + timedelta(hours=2)
    assert not current(T0, valid_from=start)
    assert current(start, valid_from=start)
    # Superseded or untagged memories are never current.
    assert not current(T0, superseded_by="c-2")
    assert not current(T0, tags=["currently", "lifecycle:current"])


def test_ttl_expiry_through_the_runtime(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    assert _ids(_pack(repository, now=T0 + timedelta(days=7, minutes=30))) == ["c-new"]
    assert _ids(_pack(repository, now=T0 + timedelta(days=8))) == []
    short = _vault(tmp_path / "short", ttl=1)
    assert _ids(_pack(short, now=T0 + timedelta(days=1, minutes=30))) == ["c-new"]


def test_valid_to_and_valid_from_through_the_runtime(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    repository.update("c-old", valid_to=T0 + timedelta(days=30))
    repository.update("c-new", valid_from=T0 + timedelta(days=3))
    assert _ids(_pack(repository, now=NOW)) == ["c-old"]
    late = T0 + timedelta(days=20)
    # c-old outlives the TTL through valid_to; c-new's TTL has run out.
    assert _ids(_pack(repository, now=late)) == ["c-old"]
    assert _ids(_pack(repository, now=T0 + timedelta(days=31))) == []


def _clocked(tmp_path: Path) -> tuple[Repository, _Clock]:
    clock = _Clock(T0)
    repository = Repository(tmp_path / "vault", clock=clock)
    repository.init()
    repository.add("Ticket", "Working on ticket ABC-1", memory_id="c-1", tags=["current"])
    config = repository.read_config()
    config["runtime"] = {"context": {"current": True}}
    repository.configure(config)
    return repository, clock


@pytest.mark.parametrize(
    "change",
    [
        {"tier": Tier.COLD},
        {"strength": 0.9},
        {"reference_count": 3},
        {"privacy": "shared"},
    ],
)
def test_expired_stays_expired_after_a_mechanical_write(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    repository, clock = _clocked(tmp_path)
    assert _ids(_pack(repository, now=T0 + timedelta(days=6))) == ["c-1"]
    clock.moment = T0 + timedelta(days=30)
    actor = "human" if "privacy" in change else "mechanism"
    repository.update("c-1", actor=actor, **change)
    assert repository.get("c-1").updated_at == clock.moment  # the write did refresh it
    assert _ids(_pack(repository, now=T0 + timedelta(days=31))) == []


def test_a_human_body_edit_does_not_extend_but_valid_to_does(tmp_path: Path) -> None:
    repository, clock = _clocked(tmp_path)
    clock.moment = T0 + timedelta(days=6)
    repository.update("c-1", body="Still on ABC-1, now in review")
    assert _ids(_pack(repository, now=T0 + timedelta(days=8))) == []
    repository.update("c-1", valid_to=T0 + timedelta(days=14))
    assert _ids(_pack(repository, now=T0 + timedelta(days=8))) == ["c-1"]


def test_a_future_anchor_is_not_current() -> None:
    def current(created: datetime) -> bool:
        return is_current(_doc("c-1", created), now=NOW, ttl_days=7)

    assert not current(datetime(2099, 1, 1, tzinfo=UTC))
    assert not current(NOW + CLOCK_SKEW + timedelta(seconds=1))
    assert current(NOW + CLOCK_SKEW)  # small skew is tolerated
    # An explicit valid_to still governs (the anchor is not used).
    assert is_current(
        _doc("c-1", datetime(2099, 1, 1, tzinfo=UTC), valid_to=NOW + timedelta(days=1)),
        now=NOW,
        ttl_days=7,
    )


@pytest.mark.parametrize(
    ("ttl", "expected"),
    [(None, DEFAULT_CURRENT_TTL_DAYS), (3, 3), (0, 0), (-1, 0), (366, 0), ("7", 0), (True, 0)],
)
def test_ttl_option_fails_closed_to_zero(ttl: Any, expected: int) -> None:
    context: dict[str, Any] = {"current": True}
    if ttl is not None:
        context["current_ttl_days"] = ttl
    options = options_from_config({"runtime": {"context": context}})
    assert options.context_current is True and options.current_ttl_days == expected


def test_switch_parsing() -> None:
    assert options_from_config({}) == RuntimeOptions()
    assert options_from_config({}).context_current is False
    assert (
        options_from_config({"runtime": {"context": {"current": "yes"}}}).context_current is False
    )
    assert options_from_config({"runtime": {"context": "on"}}).context_current is False
    assert build_runtime(Path("/nonexistent-u6"), context_current=True).context_current_enabled()


# --- budget, privacy, contract -----------------------------------------------------------


def test_budget_cuts_like_core(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    repository.update("c-new", body="x" * 300)
    runtime = build_runtime(repository.root)
    pack = runtime.context_pack(
        repository, policy_name="compat_fts", limit_tokens=30, now=NOW, slots=[CURRENT_SLOT]
    )
    slot = pack.slot(CURRENT_SLOT)
    assert _ids(slot) == ["c-new"] and len(slot.entries[0].text) == 120
    assert slot.truncated_ids == ("c-new", "c-old") and slot.overflow == 2
    check_context_pack(pack, slots=[CURRENT_SLOT], forbidden=[BODY])


def test_privacy_tenant_and_strict(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    repository.add("Private", BODY, memory_id="c-priv", tags=["current", "privacy:local-private"])
    repository.add("Beta", BODY, memory_id="c-beta", tags=["current"], tenant="beta")
    repository.add("Nas", BODY, memory_id="c-nas", tags=["current"], source="nas")
    assert set(_ids(_pack(repository))) == {"c-new", "c-old", "c-priv", "c-nas"}
    assert "c-priv" not in _ids(_pack(repository, policy="strict"))
    assert _ids(_pack(repository, tenant="beta")) == ["c-beta"]
    assert "c-nas" not in _ids(_pack(repository, allowed_sources=("manual",)))
    # MM0 clearance applies exactly as for core.
    assert "c-priv" not in _ids(_pack(repository, clearance=frozenset({"public", "shared"})))
    public = _vault(tmp_path / "public")
    public.add("Pub", "public text", memory_id="c-pub", tags=["current"], privacy="public")
    assert _ids(_pack(public, clearance=frozenset({"public"}))) == ["c-pub"]


def test_provider_passes_the_contract_kit_and_never_writes(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    provider = CurrentSlotProvider(repository, lambda document: True)
    request = SlotRequest(limit_tokens=2000, policy="compat_fts", now=NOW)
    before = tree_digest(repository.root)
    slot = check_context_slot_provider(
        provider, request=request, forbidden=[BODY], vault=repository.root
    )
    assert _ids(slot) == ["c-new", "c-old"]
    assert tree_digest(repository.root) == before


def test_not_cached_and_expiry_changes_the_version(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    runtime = build_runtime(repository.root)
    assert runtime.context_cache is not None
    stats = runtime.context_cache.stats()

    def build(now: datetime) -> ContextPack:
        return runtime.context_pack(
            repository, policy_name="compat_fts", limit_tokens=2000, now=now, slots=[CURRENT_SLOT]
        )

    first = build(NOW)
    again = build(NOW)
    expired = build(T0 + timedelta(days=7, minutes=30))
    assert first.pack_version == again.pack_version
    assert _ids(expired.slot(CURRENT_SLOT)) == ["c-new"]
    assert expired.pack_version != first.pack_version
    assert runtime.context_cache.stats() == stats  # current never touches the cache


# --- MCP gating --------------------------------------------------------------------------


def test_off_by_default_everything_is_unchanged(tmp_path: Path) -> None:
    repository = _vault(tmp_path, enabled=None)
    gateway = Gateway(repository.root, app="claude")
    assert _call(gateway, "tools/list")["result"]["tools"] == TOOLS
    assert _call(gateway, "resources/list")["result"]["resources"] == schema.RESOURCES
    result = _context(gateway, {"v": 2, "slots": ["current"]})
    assert result["isError"]
    assert result["content"][0]["text"].endswith("unknown slot (known: core, handoff, topics)")
    read = _call(gateway, "resources/read", {"uri": "memfw://context/current"})
    assert read["error"]["code"] == -32002
    with pytest.raises(ValueError, match="unknown slot"):
        gateway.runtime.context_pack(
            repository, policy_name="compat_fts", limit_tokens=10, slots=(CURRENT_SLOT,)
        )


def test_enabled_lists_and_serves_current(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    gateway = Gateway(repository.root, app="claude")
    tools = _call(gateway, "tools/list")["result"]["tools"]
    assert [tool["name"] for tool in tools] == [tool["name"] for tool in TOOLS]
    for listed, base in zip(tools, TOOLS, strict=True):
        if base["name"] != "memory_context":
            assert listed == base
            continue
        enum = listed["inputSchema"]["properties"]["slots"]["items"]["enum"]
        assert enum == [*SLOT_NAMES, CURRENT_SLOT]
        assert listed["inputSchema"]["properties"]["slots"]["default"] == ["core"]
    # TOOLS itself is never mutated.
    base = next(tool for tool in TOOLS if tool["name"] == "memory_context")
    assert base["inputSchema"]["properties"]["slots"]["items"]["enum"] == list(SLOT_NAMES)
    listed_uris = [item["uri"] for item in _call(gateway, "resources/list")["result"]["resources"]]
    assert listed_uris == [*(item["uri"] for item in schema.RESOURCES), "memfw://context/current"]
    for query, policy in (("", "compat_fts"), ("?policy=strict", "strict")):
        uri = "memfw://context/current" + query
        contents = _call(gateway, "resources/read", {"uri": uri})["result"]["contents"]
        body = json.loads(contents[0]["text"])
        tool = json.loads(
            _context(gateway, {"v": 2, "slots": ["current"], "policy": policy})["content"][0][
                "text"
            ]
        )
        del body["generated_at"], tool["generated_at"]
        assert body == tool and body["policy"] == policy
        # The real clock is past the fixture's TTL: nothing is current any more.
        assert body["slots"][0]["name"] == "current"
    error = _context(gateway, {"v": 2, "slots": ["nope"]})
    assert error["content"][0]["text"].endswith(
        "unknown slot (known: core, handoff, topics, current)"
    )


def test_enabled_does_not_change_default_packs(tmp_path: Path) -> None:
    off = Gateway(_vault(tmp_path / "off", enabled=None).root, app="claude")
    on = Gateway(_vault(tmp_path / "on").root, app="claude")
    for arguments in (
        {},
        {"limit_tokens": 10},
        {"v": 2},
        {"v": 2, "policy": "strict"},
        {"v": 2, "slots": ["core", "handoff", "topics"]},
    ):
        replies = []
        for gateway in (off, on):
            payload = json.loads(_context(gateway, arguments)["content"][0]["text"])
            payload.pop("generated_at", None)
            replies.append(payload)
        assert replies[0] == replies[1], arguments


def test_live_current_memory_through_mcp(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("Ticket", "Working on ABC-2", memory_id="c-live", tags=["current"])
    repository.add("Ended", "old", memory_id="c-ended", tags=["current"])
    repository.update("c-ended", valid_to=datetime.now(UTC) - timedelta(minutes=1))
    config = repository.read_config()
    config["runtime"] = {"context": {"current": True}}
    repository.configure(config)
    payload = json.loads(
        _context(Gateway(repository.root), {"v": 2, "slots": ["current", "core"]})["content"][0][
            "text"
        ]
    )
    assert [slot["name"] for slot in payload["slots"]] == ["current", "core"]
    assert [entry["id"] for entry in payload["slots"][0]["entries"]] == ["c-live"]


# --- byte-identical against the base ref -------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("MEMFW_ARCH_BASE_REF"),
    reason="opt-in (slow): set MEMFW_ARCH_BASE_REF to compare against that ref",
)
def test_default_context_is_byte_identical_to_base_ref() -> None:
    from tests.harness import byte_identical

    if not byte_identical.git_available():
        pytest.skip("git is not available")
    results = byte_identical.compare(
        "tests.harness.context_current_probe:default_context",
        [{}, {"tenant": "beta"}, {"clearance": "public"}],
        old_ref=os.environ["MEMFW_ARCH_BASE_REF"],
    )
    for result in results:
        # M3c changes only the conversations_search entry of tools/list (checked
        # narrowly by with_m3c_tools_list); every other reply must be identical.
        expected = byte_identical.Comparison(
            payload=result.payload, old=with_m3c_tools_list(result.old), new=result.new
        )
        assert expected.identical, f"{result.payload}:\n{expected.diff[:4000]}"
    assert '"memfw://context/topics"' in results[0].new

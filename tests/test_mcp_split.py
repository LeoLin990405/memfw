"""The A4a split of ``memfw.mcp.server``: re-exports and the all-tools comparison."""

from __future__ import annotations

import copy
import json
import os
import re
from collections.abc import Callable
from typing import Any

import pytest

from memfw.mcp import protocol, schema, server, tools_dream, tools_search
from memfw.runtime import search as runtime_search
from tests.harness.m3c_allowance import expect_m3c_search

#: Constants re-exported by ``memfw.mcp.server`` and the module that defines them.
CONSTANTS = {
    "DREAM_LIST_MAX": tools_dream,
    "DREAM_MAX_CANDIDATES": tools_dream,
    "DREAM_MAX_LIMIT": tools_dream,
    "DREAM_MAX_SESSIONS": tools_dream,
    "DREAM_MAX_STAGED": tools_dream,
    "DREAM_STAGE_INTERVAL_SECONDS": tools_dream,
    "INSTRUCTIONS": schema,
    "PROTOCOL": schema,
    "SEARCH_POLICIES": schema,
    "SERVER_INFO": schema,
    "TOOLS": schema,
    # Deprecated aliases of ``memfw.runtime.search`` public names (A3b); see below.
    "_FRESHNESS_READ_ATTEMPTS": tools_search,
    "_RAW_HASH_CACHE": tools_search,
}
#: The deprecated alias in ``tools_search``/``server`` and its public name (A3b).
RUNTIME_ALIASES = {
    "_FRESHNESS_READ_ATTEMPTS": "FRESHNESS_READ_ATTEMPTS",
    "_RAW_HASH_CACHE": "RAW_HASH_CACHE",
    "_RawHashCache": "RawHashCache",
    "_frontmatter_version": "frontmatter_version",
}
#: Defined in ``server.py`` itself.
OWN = {"Gateway", "main", "serve"}


def test_server_reexports_are_the_defining_modules_objects() -> None:
    """Every re-export is the very object its defining module reads (read-only alias)."""

    assert set(server.__all__) == set(CONSTANTS) | OWN | {
        "_RawHashCache",
        "_frontmatter_version",
        "client_endpoint_fields",
    }
    for name, module in CONSTANTS.items():
        assert getattr(server, name) is getattr(module, name), name
    for alias, public in RUNTIME_ALIASES.items():
        assert getattr(server, alias) is getattr(runtime_search, public), alias
        assert getattr(tools_search, alias) is getattr(runtime_search, public), alias
        assert public in runtime_search.__all__, public
    assert server.client_endpoint_fields is protocol.client_endpoint_fields
    for name in OWN:
        assert getattr(server, name).__module__ == "memfw.mcp.server", name


def _messages(text: str) -> list[Any]:
    """The probe's output: JSON documents joined by newlines."""

    decoder = json.JSONDecoder()
    found: list[Any] = []
    position = 0
    while position < len(text):
        while position < len(text) and text[position] in " \n":
            position += 1
        if position >= len(text):
            break
        value, position = decoder.raw_decode(text, position)
        found.append(value)
    return found


def _render(messages: list[Any]) -> str:
    return "\n".join(
        json.dumps(item, ensure_ascii=False, sort_keys=True, indent=1) for item in messages
    )


def with_expected_c2_changes(old: str) -> str:
    """``old`` (the base ref's probe output) with exactly the changes C2 and C3 make.

    1. ``initialize`` advertises ``capabilities.resources = {"listChanged": false}``;
    2. ``tools/list``: ``memory_context`` gains the optional ``slots`` property
       (C2) and the optional ``if_version`` property (C3), the current
       schema's; ``memory_remember`` gains the optional ``privacy``
       property (MM0); ``conversations_search`` gains the M3c filter and
       ``mode`` properties and their description (the current schema's).
       Nothing else may differ.

    Idempotent: a base ref that already has C2 (or C3) is left as it is.
    """

    messages = _messages(old)
    assert _render(messages) == old, "the probe output does not round-trip"
    slots = next(tool for tool in schema.TOOLS if tool["name"] == "memory_context")
    slots_property = slots["inputSchema"]["properties"]["slots"]
    if_version_property = slots["inputSchema"]["properties"]["if_version"]
    remember_tool = next(tool for tool in schema.TOOLS if tool["name"] == "memory_remember")
    privacy_property = remember_tool["inputSchema"]["properties"]["privacy"]
    search_tool = next(tool for tool in schema.TOOLS if tool["name"] == "conversations_search")
    changed = copy.deepcopy(messages)
    seen = {"initialize": 0, "tools/list": 0}
    for item in changed:
        if not isinstance(item, dict) or not isinstance(item.get("result"), dict):
            continue
        result = item["result"]
        if item.get("id") == 1 and "capabilities" in result:
            result["capabilities"]["resources"] = {"listChanged": False}
            seen["initialize"] += 1
        if item.get("id") == 3 and "tools" in result:
            tool = next(tool for tool in result["tools"] if tool["name"] == "memory_context")
            tool["inputSchema"]["properties"]["slots"] = slots_property
            tool["inputSchema"]["properties"]["if_version"] = if_version_property
            # MM0: ``memory_remember`` gains the optional ``privacy`` property.
            remember = next(tool for tool in result["tools"] if tool["name"] == "memory_remember")
            remember["inputSchema"]["properties"]["privacy"] = privacy_property
            # M3c: ``conversations_search`` gains exactly the M3c properties, a longer
            # description and a ``query`` description; nothing else may change.
            search = next(
                tool for tool in result["tools"] if tool["name"] == "conversations_search"
            )
            expect_m3c_search(search, search_tool)
            seen["tools/list"] += 1
    assert seen == {"initialize": 1, "tools/list": 1}, seen
    return _render(changed)


#: ``memory_search`` ``labels`` description before U5 added ``corroboration``.
_U2_LABELS_DESCRIPTION = (
    "Add recall_labels (open_question:<id>, conflicted) to every row of the fts, hybrid, "
    "vector and multihop modes. mode=two_stage always has them."
)


COUNTS = ("count", "apps", "sessions", "first_seen", "last_seen")


def without_u5_changes(
    new: str, *, labelled: Callable[[dict[str, Any]], bool], required: bool = True
) -> tuple[str, list[dict[str, Any]]]:
    """``new`` (a probe output) with exactly the changes U5 makes undone, and what was removed.

    1. ``tools/list``: the ``memory_search`` ``labels`` description mentions
       ``corroboration`` (put back to the U2 text);
    2. the rows of a ``memory_search`` response that ``labelled(response)``
       says answered a ``labels: true`` call carry ``corroboration``: removed
       (with ``required``, the default for this tree's output, each row must
       have it; the base ref's rows may lack it). ``corroboration`` anywhere
       else fails.

    Applied to both trees' output: a base ref that already has U5 compares
    unchanged.
    """

    messages = _messages(new)
    assert _render(messages) == new, "the probe output does not round-trip"
    changed = copy.deepcopy(messages)
    dropped: list[dict[str, Any]] = []
    for item in changed:
        result = item.get("result") if isinstance(item, dict) else None
        if not isinstance(result, dict):
            assert "corroboration" not in json.dumps(item), item
            continue
        for tool in result.get("tools") or []:
            if tool.get("name") == "memory_search":
                tool["inputSchema"]["properties"]["labels"]["description"] = _U2_LABELS_DESCRIPTION
        if labelled(item):
            for block in result.get("content") or []:
                text = block.get("text")
                if isinstance(text, dict) and isinstance(text.get("results"), list):
                    for row in text["results"]:
                        if not required and "corroboration" not in row:
                            continue
                        value = row.pop("corroboration")
                        assert set(value) in ({"degraded"}, set(COUNTS)), value
                        dropped.append(value)
        assert '"corroboration"' not in json.dumps(item), item
    return _render(changed), dropped


#: The release bump changes ``serverInfo.version`` (memfw's own version) on purpose; it is the
#: only handshake field masked before the comparison.
_SERVER_VERSION = re.compile(r'("serverInfo": \{\s*"name": "memfw",\s*"version": )"[^"]*"')


def _without_server_version(rendered: str) -> str:
    return _SERVER_VERSION.sub(r'\1"<memfw version>"', rendered)


def _all_tools_labelled(item: dict[str, Any]) -> bool:
    from tests.harness.probes import LABELS_ID

    return item.get("id") == LABELS_ID


@pytest.mark.skipif(
    not os.environ.get("MEMFW_ARCH_BASE_REF"),
    reason="opt-in (slow): set MEMFW_ARCH_BASE_REF to compare every MCP tool against that ref",
)
def test_all_tools_byte_identical_to_base_ref() -> None:
    """``mcp_all_tools`` on the base ref and on this tree; only timing fields are masked.

    The base ref predates C2, so its output gets C2's two expected changes
    (``with_expected_c2_changes``) before the comparison; any other byte differs.
    """

    from tests.harness import byte_identical

    if not byte_identical.git_available():
        pytest.skip("git is not available")
    variants = [
        {},
        {"history": True},
        {"app": "kimi", "share": True, "history": True},
        {"app": "kimi", "share": False, "history": True},
        {"app": "kimi", "share": True, "history": True, "embedder": True},
        {"app": "claude", "share": True, "history": True, "optout": ["claude"]},
        {"app": "codex", "share": True, "history": True, "optout": ["logs"]},
        {"app": "operator", "share": True, "history": True},
        {"app": "kimi", "share": True, "history": True, "tenant": "alpha"},
        {"app": "kimi", "share": True, "history": True, "allowed_sources": ["manual"]},
        {"history": True, "embedder": True, "allowed_sources": ["nas", "manual"]},
    ]
    results = byte_identical.compare(
        "tests.harness.probes:mcp_all_tools",
        variants,
        old_ref=os.environ["MEMFW_ARCH_BASE_REF"],
    )
    removed = 0
    for result in results:
        old, _ = without_u5_changes(
            with_expected_c2_changes(result.old), labelled=_all_tools_labelled, required=False
        )
        new, dropped = without_u5_changes(result.new, labelled=_all_tools_labelled)
        removed += len(dropped)
        old, new = _without_server_version(old), _without_server_version(new)
        if result.payload.get("tenant") or result.payload.get("allowed_sources"):
            old, new = _with_remember_visibility_fix(
                old, new, empty=bool(result.payload.get("tenant"))
            )
        expected = byte_identical.Comparison(payload=result.payload, old=old, new=new)
        assert expected.identical, f"{result.payload}:\n{expected.diff[:4000]}"
    assert removed, "labels=true calls must carry corroboration"


#: Values that follow from the number of events but cannot be recomputed here
#: (the log head hash and ids drawn from the probe's counter). The event count
#: itself is checked exactly (``_EVENT_COUNT``).
_COUNTED = (
    (re.compile(r"dream-20260101T000000Z-[0-9a-f]{8}"), "dream-<id>"),
    (re.compile(r"t-mcp-[0-9a-f]{32}"), "t-mcp-<n>"),
    (re.compile(r'"head": "[0-9a-f]{64}"'), '"head": "<head>"'),
)
_EVENT_COUNT = re.compile(r'"event_count": ([0-9]+)')


def _with_remember_visibility_fix(old: str, new: str, *, empty: bool) -> tuple[str, str]:
    """The one intended change for a tenant- or source-bound server (#104 review, HIGH 3).

    ``memory_remember`` used to suggest (and access-log) memories outside the
    caller's tenant or allowed sources. Now it suggests only what the caller
    may read: the new suggestions must be a subset of the old ones (empty for
    the tenant-bound variant, whose tenant holds no memory in the probe
    vault). The suggestions and the values that follow from the fewer events
    (event count, log head, counter-drawn ids) are then masked in both
    outputs. Nothing else may differ.
    """

    def suggestions(text: str) -> list[list[str]]:
        found: list[list[str]] = []
        for item in _messages(text):
            content = (item.get("result") or {}).get("content") if isinstance(item, dict) else None
            for block in content or []:
                payload = block.get("text") if isinstance(block, dict) else None
                if isinstance(payload, dict) and "staged_candidate" in payload:
                    found.append([row["memory_id"] for row in payload.get("suggestions") or []])
        return found

    before, after = suggestions(old), suggestions(new)
    assert len(before) == len(after)
    dropped = 0
    for was, now in zip(before, after, strict=True):
        assert set(now) <= set(was), (was, now)
        if empty:
            assert not now, now
        dropped += len(set(was) - set(now))
    # Exactly one ``memory_access`` event fewer per dropped suggestion (the probe
    # vault's memories are hot: no warm events), from the first remember on.
    old_counts = [int(value) for value in _EVENT_COUNT.findall(old)]
    new_counts = [int(value) for value in _EVENT_COUNT.findall(new)]
    deltas = [was - now for was, now in zip(old_counts, new_counts, strict=True)]
    assert all(delta in (0, dropped) for delta in deltas), (deltas, dropped)
    assert deltas == sorted(deltas) and (dropped == 0 or deltas[-1] == dropped), deltas
    replacement = iter(new_counts)
    old = _EVENT_COUNT.sub(lambda _m: f'"event_count": {next(replacement)}', old)

    def masked(text: str) -> str:
        messages = _messages(text)
        for item in messages:
            content = (item.get("result") or {}).get("content") if isinstance(item, dict) else None
            for block in content or []:
                payload = block.get("text") if isinstance(block, dict) else None
                if isinstance(payload, dict) and "staged_candidate" in payload:
                    payload["suggestions"] = "<masked>"
        rendered = _render(messages)
        for pattern, replacement in _COUNTED:
            rendered = pattern.sub(replacement, rendered)
        return rendered

    return masked(old), masked(new)


def test_mcp_search_policies_are_the_runtime_ones() -> None:
    """``Runtime.search`` checks ``policy`` against ``RECALL_POLICIES`` (A3b)."""

    from memfw.runtime import RECALL_POLICIES

    assert schema.SEARCH_POLICIES == RECALL_POLICIES


def _stable_search_payloads() -> list[dict[str, Any]]:
    """Every mode, a few queries, with/without an embedder and an open question."""

    payloads: list[dict[str, Any]] = []
    for mode in ("fts", "hybrid", "vector", "multihop", "two_stage"):
        for query in ("service port", "backup", "port"):
            for embedder in (False, True):
                for open_question in (False, True):
                    arguments: dict[str, Any] = {"query": query, "mode": mode, "limit": 5}
                    if open_question and mode != "two_stage":
                        arguments["labels"] = True
                    payloads.append(
                        {
                            "arguments": arguments,
                            "embedder": embedder,
                            "open_question": open_question,
                        }
                    )
    payloads += [
        {
            "arguments": {"query": "service port", "mode": mode, "labels": True},
            "corroboration": True,
        }
        for mode in ("fts", "hybrid", "multihop", "two_stage")
    ]
    payloads += [
        {"arguments": {"query": "service port", "mode": "two_stage", "labels": True}},
        {
            "arguments": {"query": "service port", "mode": "two_stage", "labels": True},
            "open_question": True,
            "corroboration": True,
            "embedder": True,
        },
        {"arguments": {"query": "service port", "mode": "fts"}, "corroboration": True},
        {"arguments": {"query": "port", "mode": "two_stage", "policy": "strict"}},
        {
            "arguments": {"query": "port", "mode": "two_stage", "policy": "strict"},
            "open_question": True,
            "embedder": True,
        },
        {"arguments": {"query": "port", "mode": "fts", "as_of": "2020-01-01"}},
        {"arguments": {"query": "port", "mode": "multihop", "as_of": "2020-01-01"}},
        {"arguments": {"query": "port", "mode": "two_stage", "as_of": "2020-01-01"}},
        {"arguments": {"query": "port", "mode": "fts", "scope": " x "}},
        {"arguments": {"query": "port", "mode": "fts", "scope": "x" * 513}},
        {"arguments": {"query": "port", "mode": "fts", "scope": 3}},
        {"arguments": {"query": "port", "mode": "fts", "policy": "strict"}},
        {"arguments": {"query": "port", "mode": "two_stage", "policy": "bogus"}},
        {"arguments": {"query": "port", "mode": "fts", "source": "nas"}},
        {"arguments": {"query": "", "mode": "fts"}},
        {"arguments": {"query": "   ", "mode": "two_stage"}},
    ]
    return payloads


@pytest.mark.skipif(
    not os.environ.get("MEMFW_ARCH_BASE_REF"),
    reason="opt-in (slow): set MEMFW_ARCH_BASE_REF to compare memory_search against that ref",
)
def test_memory_search_stable_byte_identical_to_base_ref() -> None:
    """``mcp_memory_search_stable`` on the base ref and on this tree (A3b moved every mode)."""

    from tests.harness import byte_identical

    if not byte_identical.git_available():
        pytest.skip("git is not available")
    results = byte_identical.compare(
        "tests.harness.probes:mcp_memory_search_stable",
        _stable_search_payloads(),
        old_ref=os.environ["MEMFW_ARCH_BASE_REF"],
    )
    for result in results:
        # U5: ``labels=true`` rows gain ``corroboration``; nothing else may differ.
        labels = result.payload["arguments"].get("labels") is True
        old, _ = without_u5_changes(
            result.old, labelled=lambda _item, flag=labels: flag, required=False
        )
        new, dropped = without_u5_changes(result.new, labelled=lambda _item, flag=labels: flag)
        expected = byte_identical.Comparison(payload=result.payload, old=old, new=new)
        assert expected.identical, f"{result.payload}:\n{expected.diff[:4000]}"
        if not labels:
            assert result.identical, f"{result.payload}:\n{result.diff[:4000]}"
        if labels and result.payload.get("corroboration"):
            # The non-degraded shape is covered: the projection answered.
            assert dropped and all("count" in value for value in dropped), dropped
            assert any(value["count"] > 0 for value in dropped), dropped

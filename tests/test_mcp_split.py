"""The A4a split of ``memfw.mcp.server``: re-exports and the all-tools comparison."""

from __future__ import annotations

import copy
import json
import os
from typing import Any

import pytest

from memfw.mcp import protocol, schema, server, tools_dream, tools_search
from memfw.runtime import search as runtime_search

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
       schema's. Nothing else may differ.

    Idempotent: a base ref that already has C2 (or C3) is left as it is.
    """

    messages = _messages(old)
    assert _render(messages) == old, "the probe output does not round-trip"
    slots = next(tool for tool in schema.TOOLS if tool["name"] == "memory_context")
    slots_property = slots["inputSchema"]["properties"]["slots"]
    if_version_property = slots["inputSchema"]["properties"]["if_version"]
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
            seen["tools/list"] += 1
    assert seen == {"initialize": 1, "tools/list": 1}, seen
    return _render(changed)


#: ``memory_search`` ``labels`` description before U5 added ``corroboration``.
_U2_LABELS_DESCRIPTION = (
    "Add recall_labels (open_question:<id>, conflicted) to every row of the fts, hybrid, "
    "vector and multihop modes. mode=two_stage always has them."
)


COUNTS = ("count", "apps", "sessions", "first_seen", "last_seen")


def without_u5_changes(new: str) -> str:
    """``new`` (this tree's probe output) with exactly the changes U5 makes undone.

    1. ``tools/list``: the ``memory_search`` ``labels`` description mentions
       ``corroboration`` (put back to the U2 text);
    2. ``memory_search`` rows carry ``corroboration`` (only with
       ``labels=true``, which the tests that call this check): removed. The
       probe vaults have no projection, so the values are degraded ones.

    A base ref that already has U5 compares unchanged, since the probe output
    of both trees goes through the same undo.
    """

    messages = _messages(new)
    assert _render(messages) == new, "the probe output does not round-trip"
    changed = copy.deepcopy(messages)

    def strip(value: Any) -> None:
        if isinstance(value, dict):
            rows = value.get("results")
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict) and "corroboration" in row:
                        dropped = row.pop("corroboration")
                        assert set(dropped) in ({"degraded"}, set(COUNTS)), dropped
            for item in value.values():
                strip(item)
        elif isinstance(value, list):
            for item in value:
                strip(item)

    for item in changed:
        result = item.get("result") if isinstance(item, dict) else None
        if not isinstance(result, dict):
            continue
        for tool in result.get("tools") or []:
            if tool.get("name") == "memory_search":
                tool["inputSchema"]["properties"]["labels"]["description"] = _U2_LABELS_DESCRIPTION
        strip(result)
    return _render(changed)


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
    for result in results:
        expected = byte_identical.Comparison(
            payload=result.payload,
            old=without_u5_changes(with_expected_c2_changes(result.old)),
            new=without_u5_changes(result.new),
        )
        assert expected.identical, f"{result.payload}:\n{expected.diff[:4000]}"


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
        expected = byte_identical.Comparison(
            payload=result.payload,
            old=without_u5_changes(result.old),
            new=without_u5_changes(result.new),
        )
        assert expected.identical, f"{result.payload}:\n{expected.diff[:4000]}"
        if not result.payload["arguments"].get("labels"):
            assert result.identical, f"{result.payload}:\n{result.diff[:4000]}"

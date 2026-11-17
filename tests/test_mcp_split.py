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
    # Moved below the interface in A3b; ``tools_search`` keeps aliases too.
    "_FRESHNESS_READ_ATTEMPTS": runtime_search,
    "_RAW_HASH_CACHE": runtime_search,
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
    assert server._RawHashCache is runtime_search._RawHashCache
    assert server._frontmatter_version is runtime_search._frontmatter_version
    for name in ("_FRESHNESS_READ_ATTEMPTS", "_RAW_HASH_CACHE", "_RawHashCache"):
        assert getattr(tools_search, name) is getattr(runtime_search, name), name
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
    """``old`` (the base ref's probe output) with exactly the changes C2 makes.

    1. ``initialize`` advertises ``capabilities.resources = {"listChanged": false}``;
    2. ``tools/list``: ``memory_context`` gains the optional ``slots`` property
       (the current schema's). Nothing else may differ.

    Idempotent: a base ref that already has C2 is left as it is.
    """

    messages = _messages(old)
    assert _render(messages) == old, "the probe output does not round-trip"
    slots = next(tool for tool in schema.TOOLS if tool["name"] == "memory_context")
    slots_property = slots["inputSchema"]["properties"]["slots"]
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
            seen["tools/list"] += 1
    assert seen == {"initialize": 1, "tools/list": 1}, seen
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
            payload=result.payload, old=with_expected_c2_changes(result.old), new=result.new
        )
        assert expected.identical, f"{result.payload}:\n{expected.diff[:4000]}"

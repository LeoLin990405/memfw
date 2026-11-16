"""The A4a split of ``memfw.mcp.server``: re-exports and the all-tools comparison."""

from __future__ import annotations

import os

import pytest

from memfw.mcp import protocol, schema, server, tools_dream, tools_search

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
    "_FRESHNESS_READ_ATTEMPTS": tools_search,
    "_RAW_HASH_CACHE": tools_search,
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
    assert server._RawHashCache is tools_search._RawHashCache
    assert server._frontmatter_version is tools_search._frontmatter_version
    assert server.client_endpoint_fields is protocol.client_endpoint_fields
    for name in OWN:
        assert getattr(server, name).__module__ == "memfw.mcp.server", name


@pytest.mark.skipif(
    not os.environ.get("MEMFW_ARCH_BASE_REF"),
    reason="opt-in (slow): set MEMFW_ARCH_BASE_REF to compare every MCP tool against that ref",
)
def test_all_tools_byte_identical_to_base_ref() -> None:
    """``mcp_all_tools`` on the base ref and on this tree; only timing fields are masked."""

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
        assert result.identical, f"{result.payload}:\n{result.diff[:4000]}"

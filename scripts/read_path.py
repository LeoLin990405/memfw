"""Shared read-path fields for HTTP ``/search`` and MCP ``memory_search``.

This module is stdlib only. The readonly service must not import ``memfw``
to use it, including on ``--help``.
"""

from __future__ import annotations

from typing import Any

PROVENANCE_TYPES = ("user_said", "agent_inferred", "imported", "derived")
WHY_VALUES = ("lexical", "semantic", "expansion", "related")


def _version_text(value: Any) -> str:
    if value is None or value == "":
        return ""
    return str(value)


def provenance_type(provenance: str, source: str) -> str:
    """Map a stored provenance string onto the four discrete types."""

    raw = (provenance or "").strip().lower().replace("-", "_")
    source_name = (source or "").strip().lower()
    if raw in PROVENANCE_TYPES:
        return raw
    if "agent_inferred" in raw or source_name in {"stage", "agent"}:
        return "agent_inferred"
    if "imported" in raw or source_name in {"notes", "import", "nas"}:
        return "imported"
    if "derived" in raw or source_name == "derived":
        return "derived"
    return "user_said"


def annotate(
    result: dict[str, Any],
    *,
    provenance: str,
    source: str,
    index_version: Any,
    sot_version: Any,
    why: str = "lexical",
) -> dict[str, Any]:
    """Add the read-path evidence fields. A version mismatch sets ``stale``."""

    index_text = _version_text(index_version)
    sot_text = _version_text(sot_version)
    result["provenance_type"] = provenance_type(provenance, source)
    result["why"] = why if why in WHY_VALUES else "lexical"
    result["index_version"] = index_text
    result["sot_version"] = sot_text
    result["stale"] = index_text != sot_text
    return result

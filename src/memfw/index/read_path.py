"""Shared read-path fields for HTTP ``/search`` and MCP ``memory_search``.

This module is stdlib only. The readonly service must not import ``memfw``
to use it, including on ``--help``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

PROVENANCE_TYPES = ("user_said", "agent_inferred", "imported", "derived")
WHY_VALUES = ("lexical", "semantic", "expansion", "related")


def _accepted_why(why: str) -> str:
    """Keep a known channel, or an ``alias(<label>→<oid>)`` expansion."""

    if why in WHY_VALUES:
        return why
    if (
        why.startswith("alias(")
        and why.endswith(")")
        and "→" in why[6:-1]
        and "\n" not in why
        and len(why) <= 400
    ):
        return why
    return "lexical"


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
    result["why"] = _accepted_why(why)
    result["index_version"] = index_text
    result["sot_version"] = sot_text
    result["stale"] = index_text != sot_text
    return result


# ---------------------------------------------------------------------------
# Privacy clearance (MM0)
# ---------------------------------------------------------------------------
#
# Standard-library copy of ``memfw.ports.privacy`` for the HTTP service, which
# runs without ``memfw``; ``tests/test_mm0_privacy_clearance.py`` checks that
# the two give the same answers. See that module for the rules.

PRIVACY_LEVELS = ("public", "shared", "local-private")
_PRIVACY_RANK = {"public": 0, "shared": 1, "local-private": 2, "unknown": 3}
LEGACY_PRIVACY_FALLBACK = "local-private"
LEGACY_PRIVACY_KEY = "legacy_privacy_default"
MALFORMED_CLEARANCE = frozenset({"public"})
_TAG_NO_RESTRICTION = frozenset({"", "none", "null"})
_TAG_SHARED = frozenset({"shared", "fleet"})


def field_level(raw: Any) -> str:
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value in PRIVACY_LEVELS:
            return value
    return "unknown"


def legacy_default(raw: Any) -> str:
    if isinstance(raw, str) and raw.strip().lower() in PRIVACY_LEVELS:
        return raw.strip().lower()
    return LEGACY_PRIVACY_FALLBACK


def legacy_default_from_config(config: Mapping[str, Any] | None) -> str:
    if not isinstance(config, Mapping):
        return LEGACY_PRIVACY_FALLBACK
    return legacy_default(config.get(LEGACY_PRIVACY_KEY))


def tag_levels(tags: Iterable[Any]) -> list[str]:
    levels: list[str] = []
    for tag in tags:
        lowered = str(tag).lower()
        if lowered.startswith("privacy:"):
            value = lowered.split(":", 1)[1].strip()
            if value in _TAG_NO_RESTRICTION:
                continue
            if value == "public":
                levels.append("public")
            elif value in _TAG_SHARED:
                levels.append("shared")
            elif value == "local-private":
                levels.append("local-private")
            else:
                levels.append("unknown")
        elif lowered.startswith("sensitivity:"):
            value = lowered.split(":", 1)[1].strip()
            levels.append("local-private" if value == "private" else "unknown")
    return levels


SENSITIVITY_KEY = "sensitivity"


def _sensitivity_item(value: Any) -> str:
    if isinstance(value, str) and value.strip().lower() == "private":
        return "local-private"
    return "unknown"


def sensitivity_key_levels(raw: Any) -> list[str]:
    if raw is None or (isinstance(raw, (str, list, dict)) and len(raw) == 0):
        return []
    if isinstance(raw, list):
        return [_sensitivity_item(item) for item in raw]
    return [_sensitivity_item(raw)]


def strictest(levels: Iterable[str]) -> str:
    best = "public"
    for level in levels:
        current = level if level in _PRIVACY_RANK else "unknown"
        if _PRIVACY_RANK[current] > _PRIVACY_RANK[best]:
            best = current
    return best


def effective_level(
    privacy: Any,
    tags: Iterable[Any] = (),
    *,
    present: bool | None = None,
    legacy: str = LEGACY_PRIVACY_FALLBACK,
    sensitivity: Any = None,
) -> str:
    has_field = (privacy is not None) if present is None else present
    base = field_level(privacy) if has_field else legacy_default(legacy)
    if isinstance(tags, (str, bytes)) or not isinstance(tags, Iterable):
        tags = ()
    return strictest([base, *tag_levels(tags), *sensitivity_key_levels(sensitivity)])


def parse_clearance(value: Any) -> frozenset[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        if not value.strip():
            return None
        tokens = value.split(",")
    else:
        tokens = list(value)
    cleaned = {str(token).strip().lower() for token in tokens if str(token).strip()}
    if not cleaned or not cleaned <= set(PRIVACY_LEVELS):
        return MALFORMED_CLEARANCE
    return frozenset(cleaned)


def clearance_is_valid(value: Any) -> bool:
    if value is None or (isinstance(value, str) and not value.strip()):
        return True
    tokens = value.split(",") if isinstance(value, str) else list(value)
    cleaned = {str(token).strip().lower() for token in tokens if str(token).strip()}
    return bool(cleaned) and cleaned <= set(PRIVACY_LEVELS)


def admits(clearance: frozenset[str] | None, level: str) -> bool:
    if clearance is None:
        return True
    return level != "unknown" and level in clearance


def metadata_level(metadata: Mapping[str, Any], legacy: str = LEGACY_PRIVACY_FALLBACK) -> str:
    """Level of one parsed frontmatter mapping (``privacy``, ``tags``, ``sensitivity``)."""

    tags = metadata.get("tags")
    return effective_level(
        metadata.get("privacy"),
        tags if isinstance(tags, list) else (),
        legacy=legacy,
        sensitivity=metadata.get(SENSITIVITY_KEY),
    )

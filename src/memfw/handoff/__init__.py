"""Handoff packs: continue another agent's session from the conversation store.

Native sessions cannot be resumed across CLIs, so "continuing" is always a
new session plus a pack: :func:`build_pack` (Python) or ``memfw handoff``
(CLI). The JSON contract is :mod:`memfw.handoff.schema` (``memfw.handoff/v1``).
See ``docs/handoff.md``.
"""

from __future__ import annotations

from .pack import HandoffError, build_pack, render_json, render_markdown
from .schema import PACK_SCHEMA, SCHEMA, validate_pack

__all__ = [
    "PACK_SCHEMA",
    "SCHEMA",
    "HandoffError",
    "build_pack",
    "render_json",
    "render_markdown",
    "validate_pack",
]

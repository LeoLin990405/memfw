"""Human-facing projections of a vault (L1): rebuilt from the vault, never written back.

``obsidian`` renders a read-only Obsidian board (``memfw project obsidian``).
"""

from __future__ import annotations

from .obsidian import (
    DEFAULT_CLEARANCE,
    GENERATED,
    MANIFEST_PATH,
    ObsidianProjector,
    ObsidianResult,
    parse_clearance,
)

__all__ = [
    "DEFAULT_CLEARANCE",
    "GENERATED",
    "MANIFEST_PATH",
    "ObsidianProjector",
    "ObsidianResult",
    "parse_clearance",
]

"""Local-first Markdown memory framework.

The root re-exports below are lazy: importing any ``memfw.<module>`` loads only
that module and what it imports (``memfw.retrieval.health`` loads nothing
else), while ``from memfw import RelatedIndex`` works as before.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__version__ = "0.4.0"

if TYPE_CHECKING:
    from .related import RelatedEdge, RelatedIndex, RelatedItem

__all__ = ["RelatedEdge", "RelatedIndex", "RelatedItem"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from . import related

        return getattr(related, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

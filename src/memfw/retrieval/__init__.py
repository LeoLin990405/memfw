"""Read paths over the projections: vault retrieval, multihop, judges, health.

The names below are re-exported lazily, so importing a side-effect-free
submodule (``memfw.retrieval.health``) does not load the vault, the index or
their dependencies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .vault import Retriever, VaultRetriever

__all__ = ["Retriever", "VaultRetriever"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from . import vault

        return getattr(vault, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

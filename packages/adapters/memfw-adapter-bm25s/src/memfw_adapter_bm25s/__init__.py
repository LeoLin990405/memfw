"""memfw-adapter-bm25s: an in-process BM25 ``RecallProvider`` over a vault's memories.

Enable it in ``vault.yaml`` (it is off otherwise)::

    runtime:
      recall:
        adapters:
          - factory: "memfw_adapter_bm25s:factory"
            options: {clearance: "public,shared"}

See :mod:`memfw_adapter_bm25s.provider` for the settings and the filtering
rules, and :mod:`memfw_adapter_bm25s.store` for where the index lives.
"""

from __future__ import annotations

from .provider import (
    BM25S_VERSION,
    DEFAULT_CLEARANCE,
    NAME,
    SPEC,
    Bm25sProvider,
    Settings,
    __version__,
    factory,
)

__all__ = [
    "BM25S_VERSION",
    "DEFAULT_CLEARANCE",
    "NAME",
    "SPEC",
    "Bm25sProvider",
    "Settings",
    "__version__",
    "factory",
]

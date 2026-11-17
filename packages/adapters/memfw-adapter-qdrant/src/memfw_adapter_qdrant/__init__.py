"""memfw-adapter-qdrant: a semantic ``RecallProvider`` over a Qdrant service (REST, loopback).

Enable it in ``vault.yaml`` (it is off otherwise) and fill the collection with
``memfw-adapter-qdrant sync``::

    runtime:
      recall:
        adapters:
          - factory: "memfw_adapter_qdrant:factory"
            options: {url: "http://127.0.0.1:6333", collection: memfw}

See :mod:`memfw_adapter_qdrant.provider` for the settings and the rules,
:mod:`memfw_adapter_qdrant.sync` for what is sent, and
:mod:`memfw_adapter_qdrant.fake` for the loopback test double.
"""

from __future__ import annotations

from .provider import (
    DEFAULT_CLEARANCE,
    DEFAULT_COLLECTION,
    DEFAULT_URL,
    NAME,
    SERVER_SERIES,
    SERVER_VERSION,
    SPEC,
    QdrantProvider,
    Settings,
    __version__,
    factory,
)

__all__ = [
    "DEFAULT_CLEARANCE",
    "DEFAULT_COLLECTION",
    "DEFAULT_URL",
    "NAME",
    "SERVER_SERIES",
    "SERVER_VERSION",
    "SPEC",
    "QdrantProvider",
    "Settings",
    "__version__",
    "factory",
]

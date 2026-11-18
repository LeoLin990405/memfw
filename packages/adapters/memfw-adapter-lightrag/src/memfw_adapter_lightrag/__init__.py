"""memfw-adapter-lightrag: graph and answer ``Deriver`` views from a loopback LightRAG server.

Off until the operator enables it, runs ``lightrag-server`` on ``127.0.0.1``
with an API key, and exports that key under the variable the settings name::

    adapters:
      enabled: [lightrag]
      lightrag:
        base_url: "http://127.0.0.1:9621"
        api_key_env: LIGHTRAG_API_KEY

See :mod:`memfw_adapter_lightrag.deriver` for what is sent, the source
mapping and the network rules.
"""

from __future__ import annotations

from .deriver import (
    DEFAULT_BASE_URL,
    DEFAULT_KEY_ENV,
    EXTERNAL_VERSION,
    KINDS,
    NAME,
    SPEC,
    VIEW_FORMAT,
    LightRagDeriver,
    LightRagError,
    Settings,
    SyncResult,
    __version__,
    candidates,
    factory,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_KEY_ENV",
    "EXTERNAL_VERSION",
    "KINDS",
    "NAME",
    "SPEC",
    "VIEW_FORMAT",
    "LightRagDeriver",
    "LightRagError",
    "Settings",
    "SyncResult",
    "__version__",
    "candidates",
    "factory",
]

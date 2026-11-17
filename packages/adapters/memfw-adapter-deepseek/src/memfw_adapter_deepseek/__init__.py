"""memfw-adapter-deepseek: a ``conv-v2`` conversation-tagging backend over DeepSeek.

Enable it in ``vault.yaml`` (and export ``DEEPSEEK_API_KEY`` for the worker)::

    conversations:
      backend: "memfw_adapter_deepseek:factory"
      cloud: true

See :mod:`memfw_adapter_deepseek.backend` for what is sent, the key rules
and the per-run budget.
"""

from __future__ import annotations

from .backend import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    KEY_ENV,
    DeepSeekBackend,
    DeepSeekError,
    Settings,
    __version__,
    factory,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "KEY_ENV",
    "DeepSeekBackend",
    "DeepSeekError",
    "Settings",
    "__version__",
    "factory",
]

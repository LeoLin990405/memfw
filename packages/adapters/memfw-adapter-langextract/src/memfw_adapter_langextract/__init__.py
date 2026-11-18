"""memfw-adapter-langextract: a ``Deriver`` of grounded entities and relations.

Off until the operator enables it (and installs ``langextract==1.7.2``)::

    adapters:
      enabled: [langextract]
      langextract:
        backend: "memfw_adapter_langextract.ollama:factory"
        backend_options: {model: "qwen2.5:7b-instruct"}

Importing this package never imports LangExtract. See
:mod:`memfw_adapter_langextract.deriver` for what is sent, the privacy rule,
the gate for non-local backends and the per-run token budget.
"""

from __future__ import annotations

from .deriver import (
    EXTERNAL_VERSION,
    KINDS,
    NAME,
    SPEC,
    TASK,
    VIEW_FORMAT,
    LangExtractDeriver,
    RunBudget,
    Settings,
    __version__,
    candidates,
    factory,
)

__all__ = [
    "EXTERNAL_VERSION",
    "KINDS",
    "NAME",
    "SPEC",
    "TASK",
    "VIEW_FORMAT",
    "LangExtractDeriver",
    "RunBudget",
    "Settings",
    "__version__",
    "candidates",
    "factory",
]

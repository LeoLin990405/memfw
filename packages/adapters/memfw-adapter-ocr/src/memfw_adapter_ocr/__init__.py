"""memfw-adapter-ocr (MM4): attachment originals → text proxy import files with locators.

Engines sit behind the adapter's own :class:`~.backend.OcrBackend` interface:

* ``mcp`` (primary): an OCR server the operator names in the settings,
  run as a child process and spoken to over the MCP stdio protocol;
* ``pdftotext`` (fallback): poppler's ``pdftotext -bbox-layout`` for PDFs
  with a text layer;
* ``"module:factory"``: any other implementation of the interface.

The two shipped backends run locally and upload nothing; a custom
``module:factory`` backend is outside that guarantee (the operator's
responsibility). Off by default: enable it in the
adapter registry (``adapters: {enabled: [ocr], ocr: {...}}``) or run
``memfw-ocr-proxy`` by hand. The output is a proxy import file in MM2a's
``memfw.proxy`` v1 format (:data:`~.proxy.PROXY_FORMAT`), validated with
``memfw.ports.proxies`` (MM2a) whenever the installed ports have it.
"""

from __future__ import annotations

from typing import Any

__version__ = "0.1.2"

from .backend import OcrBackend, OcrBudget, OcrError, OcrLine, OcrPage, OcrResult
from .deriver import NAME, VIEW_KIND, OcrDeriver, Settings, build_backend, factory
from .mcp import McpOcrBackend
from .pdftotext import PdftotextBackend
from .proxy import PROXY_FORMAT, generate_proxy, to_proxy_candidate, write_proxy


def adapter_spec() -> Any:
    """The registry entry (``memfw.adapters`` entry point ``ocr``)."""

    from ._ports import AdapterSpec

    return AdapterSpec(name=NAME, kind="deriver", factory=factory, version=__version__)


__all__ = [
    "NAME",
    "PROXY_FORMAT",
    "VIEW_KIND",
    "McpOcrBackend",
    "OcrBackend",
    "OcrBudget",
    "OcrDeriver",
    "OcrError",
    "OcrLine",
    "OcrPage",
    "OcrResult",
    "PdftotextBackend",
    "Settings",
    "__version__",
    "adapter_spec",
    "build_backend",
    "factory",
    "generate_proxy",
    "to_proxy_candidate",
    "write_proxy",
]

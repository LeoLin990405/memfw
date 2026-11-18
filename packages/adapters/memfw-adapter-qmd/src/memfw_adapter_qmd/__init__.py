"""memfw-adapter-qmd: QMD as a memfw ``RecallProvider`` (memory projections and notes).

Two adapters, both off until enabled in ``AdaptersConfig``::

    {"enabled": ["qmd", "qmd-notes"],
     "qmd": {"endpoint": "http://127.0.0.1:8181/mcp", "collection": "memfw-memory",
             "manifest": "/srv/projection/current"},
     "qmd-notes": {"endpoint": "http://127.0.0.1:8181/mcp", "collection": "knowledge-hub",
                   "notes_root": "/srv/knowledge-hub", "legacy_privacy": "shared"}}

Both are registered under the ``memfw.adapters`` entry-point group
(``memfw_adapter_qmd:MEMORY_SPEC`` / ``NOTES_SPEC``) and can be built
directly with :func:`memory_factory` / :func:`notes_factory`. See
:mod:`memfw_adapter_qmd.provider` for what each returns and drops, and
:mod:`memfw_adapter_qmd.mcp` for the network rules.
"""

from __future__ import annotations

__version__ = "0.1.1"

from ._ports import AdapterSpec  # noqa: E402
from .mcp import McpHttpClient, QmdResponseError  # noqa: E402
from .paths import handelize, note_ref, parse_note_ref  # noqa: E402
from .provider import (  # noqa: E402
    DEFAULT_ENDPOINT,
    MEMORY_NAME,
    NOTES_NAME,
    QMD_VERSION,
    MemorySettings,
    NotesSettings,
    QmdMemoryProvider,
    QmdNotesProvider,
    memory_factory,
    notes_factory,
)

SOURCE = "https://github.com/tobi/qmd"
LICENSE = "MIT"

MEMORY_SPEC = AdapterSpec(
    name=MEMORY_NAME,
    kind="recall",
    factory=memory_factory,
    version=QMD_VERSION,
    source=SOURCE,
    license=LICENSE,
)
NOTES_SPEC = AdapterSpec(
    name=NOTES_NAME,
    kind="recall",
    factory=notes_factory,
    version=QMD_VERSION,
    source=SOURCE,
    license=LICENSE,
)

__all__ = [
    "DEFAULT_ENDPOINT",
    "LICENSE",
    "MEMORY_NAME",
    "MEMORY_SPEC",
    "NOTES_NAME",
    "NOTES_SPEC",
    "QMD_VERSION",
    "SOURCE",
    "McpHttpClient",
    "MemorySettings",
    "NotesSettings",
    "QmdMemoryProvider",
    "QmdNotesProvider",
    "QmdResponseError",
    "__version__",
    "handelize",
    "memory_factory",
    "note_ref",
    "notes_factory",
    "parse_note_ref",
]

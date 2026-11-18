"""memfw-adapter-pageindex: PageIndex-style tree search as a memfw ``RecallProvider``.

Enable it through the adapter registry (it is off until listed)::

    adapters:
      enabled: [pageindex]
      pageindex:
        index_dir: /absolute/path/to/pageindex-index
        mode: reasoning                    # or: titles (no model)
        backend: "some_module:factory"     # a memfw ModelBackend
        cloud: false                       # true only for a non-local backend

Trees are built offline with ``memfw-pageindex build-md`` (Markdown notes) or
imported with ``memfw-pageindex import-tree`` (PageIndex's PDF output). See
the package README for the gates, the budget and the ref format.
"""

from __future__ import annotations

from typing import Any

__version__ = "0.1.1"

from .prompt import PROMPT_VERSION, TASK, parse_tree_search_reply, render_tree_search_prompt
from .provider import NAME, PageIndexProvider, Settings, SettingsError, factory
from .tree import (
    TREE_SCHEMA,
    TreeError,
    build_markdown_tree,
    import_pageindex_tree,
    load_index,
    write_tree,
)

#: The PageIndex release whose tree format this adapter was checked against.
PAGEINDEX_VERSION = "0.7.0"
PAGEINDEX_SOURCE = "https://github.com/VectifyAI/PageIndex"
PAGEINDEX_LICENSE = "MIT"


def adapter_spec() -> Any:
    """The registry entry (``memfw.adapters`` entry point ``pageindex``)."""

    from ._ports import AdapterSpec

    return AdapterSpec(
        name=NAME,
        kind="recall",
        factory=factory,
        version=__version__,
        source=PAGEINDEX_SOURCE,
        license=PAGEINDEX_LICENSE,
    )


__all__ = [
    "NAME",
    "PAGEINDEX_LICENSE",
    "PAGEINDEX_SOURCE",
    "PAGEINDEX_VERSION",
    "PROMPT_VERSION",
    "TASK",
    "TREE_SCHEMA",
    "PageIndexProvider",
    "Settings",
    "SettingsError",
    "TreeError",
    "__version__",
    "adapter_spec",
    "build_markdown_tree",
    "factory",
    "import_pageindex_tree",
    "load_index",
    "parse_tree_search_reply",
    "render_tree_search_prompt",
    "write_tree",
]

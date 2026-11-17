"""memfw-adapter-graphrag: GraphRAG as an offline batch ``Deriver``.

Off until the operator enables it and points it at a GraphRAG project::

    adapters:
      enabled: [graphrag]
      graphrag:
        root: /srv/graphrag/memfw      # holds settings.yaml (table_provider: csv)

``stage_input`` copies shareable documents from a ``memfw projection export``
generation, ``run_index`` runs ``graphrag index`` as a subprocess (never on
its own), and ``derive`` reads the CSV output back into a ``DerivedView``.
See :mod:`memfw_adapter_graphrag.deriver`.
"""

from __future__ import annotations

from .deriver import (
    EXTERNAL_VERSION,
    KINDS,
    NAME,
    SPEC,
    VIEW_FORMAT,
    GraphRagDeriver,
    GraphRagError,
    Settings,
    StageResult,
    __version__,
    candidates,
    factory,
)

__all__ = [
    "EXTERNAL_VERSION",
    "KINDS",
    "NAME",
    "SPEC",
    "VIEW_FORMAT",
    "GraphRagDeriver",
    "GraphRagError",
    "Settings",
    "StageResult",
    "__version__",
    "candidates",
    "factory",
]

"""The layer map: every ``memfw`` package and top-level module, placed in one layer.

``docs/architecture.md`` explains the rules; ``allowlist.py`` lists today's
known violations. A new top-level module or subpackage must be added here, or
``tests/test_architecture_layers.py`` fails.

Layers are ordered bottom (0) to top. A unit may import units in lower layers.
It may import a unit in its own layer only when the pair is in
``SAME_LAYER_ALLOWED`` (a reviewed, acyclic, design-level edge; everything else
is meant to go through an ``memfw.ports`` protocol). Upward imports are
violations.

Units are dotted module prefixes; a module belongs to the longest key that
prefixes it. Most keys are whole packages. A few modules get their own key
because they sit in a different layer from their package:

* ``memfw.core.repository`` is the vault facade: ``Repository`` is composed
  from mixins in ``storage``, ``log`` and ``knowledge``, so it sits above them,
  while the rest of ``memfw.core`` (models, errors, epistemic, context) sits
  below them. ``memfw/core`` has no ``__init__.py``, so importing
  ``memfw.core.models`` does not load the facade.
* ``memfw.core.policies`` sits next to ``permissions`` (it imports it).
* Every ``cli.py`` (and ``curation/review_cli.py``) is a Typer sub-app: layer
  L5. ``notes/mcp.py`` (the notes MCP server) is L5 too. (``notes/http.py`` is
  an outbound HTTP helper the notes library uses, so it stays in ``notes``.)
* ``memfw`` itself (``src/memfw/__init__.py``) runs before any submodule, so
  it is placed at the bottom: its re-export of ``memfw.related`` is an upward
  import (allowlisted until A5 makes the root lazy).
* ``memfw.recall.builtin`` is the one recall module that may reach memfw
  internals; the rest of ``memfw.recall`` may import only ``memfw.ports``.
* ``memfw.handoff.schema`` is the stdlib-only response contract
  (``redaction_policy``, ``omitted_fields``, pack schema). ``conversations``
  and the rest of ``handoff`` both import it, so it sits below L3.

Relation to ``memfw-architecture`` section 2 (L0 ports, L1 index/projections,
L2 core, L3 capabilities, L4 runtime, L5 interfaces): every projection is
rebuilt *from* the vault and imports ``memfw.core``, so the kernel layers sit
below the projection layers (spec v1.1). The spec's L-number is kept in each
layer name.
"""

from __future__ import annotations

from typing import Final

#: Ordered bottom to top; the index is the rank.
LAYERS: Final[tuple[str, ...]] = (
    "L0-ports",
    "L2-kernel-model",
    "L2-kernel-parts",
    "L2-vault",
    "L1-projection",
    "L1-read",
    "L3-capability",
    "L4-runtime",
    "L5-interface",
)

RANK: Final[dict[str, int]] = {name: rank for rank, name in enumerate(LAYERS)}

UNITS: Final[dict[str, str]] = {
    # --- L0. The package root loads first for every ``import memfw.x``.
    "memfw": "L0-ports",
    # Contracts. Stdlib only (also checked by test_ports_isolation).
    "memfw.ports": "L0-ports",
    # --- L2 kernel model: the data model every layer shares. Pure code.
    # models / errors / epistemic / context / common / markdown_parse.
    "memfw.core": "L2-kernel-model",
    # Byte/record budget accounting; no memfw imports.
    "memfw.budget": "L2-kernel-model",
    # Pure response contract. Below L3 so conversations and handoff can both
    # import it without a same-layer capability edge.
    "memfw.handoff.schema": "L2-kernel-model",
    # Pure budget selectors over the budget model.
    "memfw.policies": "L2-kernel-model",
    # --- L2 kernel parts: pieces the vault facade is assembled from.
    "memfw.core.policies": "L2-kernel-parts",
    # Permission workflow policy read from vault.yaml.
    "memfw.permissions": "L2-kernel-parts",
    # Hash-chained event log; EventLogMixin is part of Repository.
    "memfw.log": "L2-kernel-parts",
    # Markdown storage, transactions, cold archive; mixins of Repository.
    "memfw.storage": "L2-kernel-parts",
    # Knowledge cards: KnowledgeStoreMixin is a mixin of Repository, so the
    # vault facade cannot exist without it (kernel, not L3). The K iteration
    # may lift it to L3 behind a protocol.
    "memfw.knowledge": "L2-kernel-parts",
    # Attachments (MM1a): AttachmentStoreMixin is a mixin of Repository (records
    # in the vault, blobs outside it), so it sits with the knowledge cards.
    "memfw.attachments": "L2-kernel-parts",
    # Proposal schemas the vault signs and verifies (agents.models); the
    # package __init__ (deterministic mock judges) loads with it.
    "memfw.agents": "L2-kernel-parts",
    # Layer classification and permission-checked transitions.
    "memfw.layers": "L2-kernel-parts",
    # Ontology model + alias expansion (spec: part of the kernel).
    "memfw.ontology": "L2-kernel-parts",
    # Hot/cold placement decisions (budget + policies, no vault I/O).
    "memfw.tiers": "L2-kernel-parts",
    # --- L2 vault facade.
    "memfw.core.repository": "L2-vault",
    # --- L1 projections: rebuildable data derived from the vault or sources.
    # SQLite FTS5 memory index.
    "memfw.index": "L1-projection",
    # BM25 / keyness algorithms and usage statistics.
    "memfw.stats": "L1-projection",
    # Rebuildable entity projection.
    "memfw.entities": "L1-projection",
    # Rebuildable relatedness edges.
    "memfw.related": "L1-projection",
    "memfw.related_annotation": "L1-projection",
    # Plain-file projection for external recall backends.
    "memfw.projection": "L1-projection",
    # Notes library index/retrieval (a projection over a note vault).
    "memfw.notes": "L1-projection",
    # Read-only index over agent transcripts.
    "memfw.history": "L1-projection",
    # Rebuildable corroboration counts (U4); the share policy is injected.
    "memfw.corroboration": "L1-projection",
    # Rebuildable typed-edge projection (K3) behind the ProjectionStore port.
    "memfw.edges": "L1-projection",
    # Disposable usage signals (C5 ``memory_used``); never written to the vault.
    "memfw.usage": "L1-projection",
    # Human-facing board projections (O1a Obsidian); read-only on the vault.
    "memfw.projections": "L1-projection",
    # --- L1 read paths over the projections.
    "memfw.retrieval": "L1-read",
    "memfw.memory_hybrid": "L1-read",
    # --- L3 capabilities.
    "memfw.recall": "L3-capability",
    "memfw.recall.builtin": "L3-capability",
    "memfw.capture": "L3-capability",
    "memfw.conversations": "L3-capability",
    "memfw.dream": "L3-capability",
    "memfw.curation": "L3-capability",
    "memfw.handoff": "L3-capability",
    "memfw.okf": "L3-capability",
    "memfw.experience": "L3-capability",
    "memfw.export": "L3-capability",
    "memfw.code_retrieval": "L3-capability",
    "memfw.memory_eval": "L3-capability",
    # Session-start context packs (C1); filters are injected, no capability imports.
    "memfw.context": "L3-capability",
    # --- L4 runtime: the composition root (A3a: recall wiring; A3b: every MCP tool).
    "memfw.runtime": "L4-runtime",
    # --- L5 interfaces: protocol translation only.
    "memfw.cli": "L5-interface",
    "memfw.mcp": "L5-interface",
    "memfw.capture.cli": "L5-interface",
    "memfw.conversations.cli": "L5-interface",
    "memfw.dream.cli": "L5-interface",
    "memfw.curation.review_cli": "L5-interface",
    "memfw.experience.cli": "L5-interface",
    "memfw.handoff.cli": "L5-interface",
    "memfw.history.cli": "L5-interface",
    "memfw.attachments.cli": "L5-interface",
    "memfw.attachments.proxy_cli": "L5-interface",
    "memfw.ontology.cli": "L5-interface",
    "memfw.notes.mcp": "L5-interface",
}

#: Same-layer edges that are part of the design (source unit, target unit).
#: Must stay acyclic; an entry that no longer occurs fails the test (stale).
#: Capability-to-capability (L3) edges are never listed here: the spec routes
#: them through a protocol, so today's ones are in ``allowlist.py``.
SAME_LAYER_ALLOWED: Final[dict[tuple[str, str], str]] = {
    # Kernel building blocks.
    ("memfw.policies", "memfw.budget"): "selectors over the budget model",
    ("memfw.core.policies", "memfw.permissions"): "core policy reads PermissionPolicy",
    ("memfw.knowledge", "memfw.storage"): "knowledge cards share the vault write lock",
    ("memfw.attachments", "memfw.storage"): "attachment records share the vault write lock",
    # Projections built on the FTS index and the stats algorithms.
    ("memfw.index", "memfw.stats"): "BM25/keyness scoring",
    ("memfw.history", "memfw.index"): "transcript index reuses FTS term building",
    ("memfw.notes", "memfw.index"): "notes index reuses FTS term building",
    ("memfw.related", "memfw.entities"): "entity overlap is one relatedness signal",
    ("memfw.related", "memfw.notes"): "relatedness edges over notes",
    # Read paths.
    ("memfw.memory_hybrid", "memfw.retrieval"): "hybrid route falls back to retrieval",
    # The recall adapter module is the designed bridge into recall's core.
    ("memfw.recall.builtin", "memfw.recall"): "builtin providers implement recall's ports",
    # The root Typer app mounts the sub-apps and serves the notes MCP server.
    ("memfw.cli", "memfw.capture.cli"): "mounts sub-app",
    ("memfw.cli", "memfw.attachments.cli"): "mounts sub-app",
    ("memfw.attachments.cli", "memfw.attachments.proxy_cli"): "mounts `attach proxy`",
    ("memfw.cli", "memfw.conversations.cli"): "mounts sub-app",
    ("memfw.cli", "memfw.curation.review_cli"): "mounts sub-app",
    ("memfw.cli", "memfw.dream.cli"): "mounts sub-app",
    ("memfw.cli", "memfw.experience.cli"): "mounts sub-app",
    ("memfw.cli", "memfw.handoff.cli"): "mounts sub-app",
    ("memfw.cli", "memfw.history.cli"): "mounts sub-app",
    ("memfw.cli", "memfw.ontology.cli"): "mounts sub-app",
    ("memfw.cli", "memfw.notes.mcp"): "`memfw notes mcp` serves it",
}

#: Units that may import only these memfw units (beyond themselves). Imports
#: of any other memfw unit are violations even when they point downward.
RESTRICTED: Final[dict[str, frozenset[str]]] = {
    # Stdlib only.
    "memfw.ports": frozenset(),
    # Recall's core reaches memfw internals only through recall/builtin.py.
    "memfw.recall": frozenset({"memfw.ports"}),
    # Spec section 9: the MCP server depends on the Runtime and protocols only
    # (A3 builds memfw.runtime, A4 splits the server).
    "memfw.mcp": frozenset({"memfw.ports", "memfw.runtime"}),
}

#: Modules allowed to call a loader on a name only known at run time, with the
#: exact number of such call sites. Most load an operator-configured
#: ``module:function`` plug-in (off unless configured). A new dynamic loader
#: elsewhere is a violation; a changed count fails until this is updated.
DYNAMIC_IMPORT_ALLOWED: Final[dict[str, tuple[str, int]]] = {
    "memfw.conversations.backends": ("operator-configured summarizer `module:factory`", 1),
    "memfw.dream.run": ("operator-configured judge `module:function`", 1),
    "memfw.dream.skills.draft": ("operator-configured drafter `module:function`", 1),
    "memfw.index.readonly": ("standalone script loads sibling read_path.py", 1),
    "memfw.runtime.recall_adapters": ("operator-configured recall provider `module:factory`", 1),
}

"""Known architecture violations: the A0 baseline. This list only shrinks.

Keys are unit-level edges ``(source unit, target unit)`` as defined in
``layers.py``. Each value is ``(reason, sites)``: the reason starts with the
step that removes it (memfw-architecture section 8) and ``sites`` maps each
importing module to its number of import lines on that edge. A new module or a
new line on an edge fails; so does a site count that dropped until it is
lowered here. Plain Python literals only: the merge-base check reads this file
from git history with ``ast.literal_eval``.

Steps:

* A1 (done, #38): the readonly service moved into ``memfw.index.readonly``;
  no library code loads ``scripts/``.
* A2: cycles and upward/same-layer edges are cut with protocols or by moving
  shared helpers down.
* A3: ``memfw.runtime`` (the composition root) builds providers, indexes and
  repositories; interfaces get them from the ``Runtime``.
* A4: ``mcp/server.py`` is split by tool group; each group depends on the
  ``Runtime`` and capability protocols only.
* A5: package split; the ``memfw`` root ``__init__`` stops importing
  submodules eagerly.

``tests/test_architecture_layers.py`` fails when a violation is missing here,
when an entry or site here no longer occurs, and (with git history) when this
list has more entries or sites than at the merge-base. Delete an entry in the
PR that fixes it and lower ``ALLOWLIST_SIZE`` in that test.
"""

from __future__ import annotations

from typing import Final

Sites = dict[str, int]

ALLOWLIST: Final[dict[tuple[str, str], tuple[str, Sites]]] = {
    # --- package root ----------------------------------------------------------
    ("memfw", "memfw.related"): (
        "A5: root __init__ re-exports RelatedIndex, so every `import memfw.x` loads"
        " the vault (lazy __getattr__ re-export)",
        {"memfw": 1},
    ),
    # --- cycles (spec section 1) -----------------------------------------------
    ("memfw.conversations", "memfw.dream"): (
        "A2: conversations.worker uses dream.verify/dream.model (Summarizer protocol)",
        {"memfw.conversations.worker": 3},
    ),
    ("memfw.dream", "memfw.conversations"): (
        "A2: dream.skills reads the conversation store (TranscriptReader protocol)",
        {"memfw.dream.skills.queue": 5},
    ),
    ("memfw.stats", "memfw.curation"): (
        "A2: stats calls curation.engine._with_archived_body (StatsSource protocol)",
        {"memfw.stats": 1},
    ),
    # --- library -> CLI (spec section 1: capture -> cli, curation -> cli) ------
    ("memfw.capture", "memfw.cli"): (
        "A2: capture.confirm uses memfw.cli._repo (move the vault opener to core)",
        {"memfw.capture.confirm": 1},
    ),
    ("memfw.capture.cli", "memfw.cli"): (
        "A2: capture sub-app uses memfw.cli internals",
        {"memfw.capture.cli": 1},
    ),
    ("memfw.curation.review_cli", "memfw.cli"): (
        "A2: review sub-app uses memfw.cli._repo (move the vault opener to core)",
        {"memfw.curation.review_cli": 1},
    ),
    # --- found by A0, not in spec section 1 ------------------------------------
    ("memfw.storage", "memfw.capture"): (
        "A2: kernel calls capture._recover_stage_intent (move recovery into storage)",
        {"memfw.storage.transactions": 1},
    ),
    ("memfw.storage", "memfw.curation"): (
        "A2: kernel calls curation.engine._with_archived_body (move it into storage)",
        {"memfw.storage.operations": 2},
    ),
    ("memfw.handoff", "memfw.conversations"): (
        "A2: handoff reads the conversation store (TranscriptReader protocol)",
        {
            "memfw.handoff.gitinfo": 1,
            "memfw.handoff.ledger": 1,
            "memfw.handoff.pack": 2,
            "memfw.handoff.source": 1,
            "memfw.handoff.summarize": 1,
        },
    ),
    ("memfw.okf", "memfw.capture"): (
        "A2: OKF import stages through capture.review/staging (memfw.ports staging contract)",
        {"memfw.okf.importer": 2},
    ),
    # --- MCP server depends on implementations (spec sections 1 and 9) ---------
    ("memfw.mcp", "memfw.core.repository"): (
        "A3: Repository comes from the Runtime",
        {"memfw.mcp.server": 1},
    ),
    ("memfw.mcp", "memfw.index"): (
        "A3: MemoryIndex comes from the Runtime",
        {"memfw.mcp.server": 1},
    ),
    ("memfw.mcp", "memfw.memory_hybrid"): (
        "A3: retriever and readonly service from the Runtime",
        {"memfw.mcp.server": 1},
    ),
    ("memfw.mcp", "memfw.notes"): (
        "A3: embedder is built by the Runtime",
        {"memfw.mcp.server": 1},
    ),
    ("memfw.mcp", "memfw.recall"): (
        "A3: Coordinator/InflightRegistry come from the Runtime",
        {"memfw.mcp.server": 3},
    ),
    ("memfw.mcp", "memfw.recall.builtin"): (
        "A3: two_stage providers are assembled by the Runtime",
        {"memfw.mcp.server": 2},
    ),
    ("memfw.mcp", "memfw.related"): (
        "A3: RelatedIndex comes from the Runtime",
        {"memfw.mcp.server": 1},
    ),
    ("memfw.mcp", "memfw.retrieval"): (
        "A3: multihop search comes from the Runtime",
        {"memfw.mcp.server": 1},
    ),
    ("memfw.mcp", "memfw.core"): (
        "A4: tool groups use port dataclasses, not core models/errors",
        {"memfw.mcp.server": 2},
    ),
    ("memfw.mcp", "memfw.capture"): (
        "A4: capture tool group behind a protocol",
        {"memfw.mcp.server": 2},
    ),
    ("memfw.mcp", "memfw.conversations"): (
        "A4: conversations tool group behind a protocol",
        {"memfw.mcp.server": 1},
    ),
    ("memfw.mcp", "memfw.dream"): (
        "A4: dream tool group behind a protocol",
        {"memfw.mcp.server": 2},
    ),
    ("memfw.mcp", "memfw.history"): (
        "A4: history tool group behind a protocol",
        {"memfw.mcp.server": 2},
    ),
    ("memfw.mcp", "memfw.ontology"): (
        "A4: ontology expansion behind a protocol",
        {"memfw.mcp.server": 3},
    ),
}

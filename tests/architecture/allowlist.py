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
* A2 (done): cycles and upward/same-layer edges are cut with protocols or by
  moving shared helpers down. A2a: the conversations/dream and stats/curation
  cycles and the kernel/handoff edges (cold-archive read and capture stage
  recovery moved into storage, transcript reader into history, claim ports).
  A2b: the library-to-CLI edges (vault opener into ``memfw.core``) and
  ``okf -> capture`` (candidate files and staging writer into storage).
* A3: ``memfw.runtime`` (the composition root) builds providers, indexes and
  repositories; interfaces get them from the ``Runtime``.
* A4: ``mcp/server.py`` is split by tool group; each group depends on the
  ``Runtime`` and capability protocols only.
* A5: package split; the ``memfw`` root ``__init__`` stops importing
  submodules eagerly.

``tests/test_architecture_layers.py`` fails when a violation is missing here,
when an entry or site here no longer occurs, and (with git history) when this
list has more entries or sites than at the merge-base. Debt is counted per edge
per unit; refactors may redistribute it inside the unit: against the
merge-base, an edge's sites may move between modules of its source unit (A4a
split ``memfw.mcp.server``) as long as the edge's total does not grow, while a
new key, a larger total or an importing module outside the source unit fails.
Delete an entry in the PR that fixes it and lower ``ALLOWLIST_SIZE`` in that
test.
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
}

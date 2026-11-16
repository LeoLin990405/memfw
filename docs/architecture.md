# Architecture guards

memfw is split into layers that may only depend downward. Contracts live in `memfw.ports`
(stdlib only); implementations depend on contracts; interfaces (CLI, MCP) translate protocols
and hold no business logic. Tests enforce this in CI, so a new coupling fails the build
instead of waiting for review. Ports, the recall coordinator and the contract kit are
described in [`ports-and-recall.md`](ports-and-recall.md).

## Layers

The layer map is [`tests/architecture/layers.py`](../tests/architecture/layers.py). Every
`memfw` subpackage and top-level module is a key there (a *unit*). A few modules get their
own key because they sit in another layer than their package: `memfw.core.repository` (the
vault facade, assembled from `storage`, `log` and `knowledge` mixins), `memfw.core.policies`,
`memfw.recall.builtin`, every `cli.py` and `notes/mcp.py`.

| Rank | Layer | Units (summary) |
|---|---|---|
| 0 | `L0-ports` | `memfw.ports`; the `memfw` root `__init__` (it runs before every submodule) |
| 1 | `L2-kernel-model` | `memfw.core` (models, errors, epistemic, context), `budget`, `policies` |
| 2 | `L2-kernel-parts` | `storage`, `log`, `knowledge`, `permissions`, `core.policies`, `agents`, `layers`, `ontology`, `tiers` |
| 3 | `L2-vault` | `memfw.core.repository` |
| 4 | `L1-projection` | `index`, `stats`, `entities`, `related`, `related_annotation`, `projection`, `notes`, `history` |
| 5 | `L1-read` | `retrieval`, `memory_hybrid` |
| 6 | `L3-capability` | `recall`, `recall.builtin`, `capture`, `conversations`, `dream`, `curation`, `handoff`, `okf`, `experience`, `export`, `code_retrieval`, `memory_eval` |
| 7 | `L4-runtime` | reserved for `memfw.runtime` (A3) |
| 8 | `L5-interface` | `memfw.cli`, `memfw.mcp`, every `*/cli.py`, `curation.review_cli`, `notes.mcp` |

The spec's L-number is kept in each name. The spec draws projections (L1) under the kernel
(L2); in code every projection is rebuilt from the vault and imports `memfw.core`, so here
the kernel ranks lower.

## Rules (`tests/test_architecture_layers.py`)

The test parses every file (AST; imports inside functions and under `TYPE_CHECKING` count,
relative imports are resolved) and checks, per unit-level edge:

1. **No upward imports.**
2. **Same-layer imports only when listed** in `SAME_LAYER_ALLOWED` (a reviewed, acyclic
   list; never for L3 capabilities, which talk through a port).
3. **Restricted units:** `memfw.ports` imports only the standard library; `memfw.recall`
   imports only `memfw.ports` (internals go through `recall/builtin.py`); `memfw.mcp`
   imports only `memfw.ports` (and `memfw.runtime` once it exists).
4. **No library code reaches into `scripts/`**, by import or by file path.
5. **No cycles** between units once allowlisted edges are removed.
6. **Every module is classified:** a new subpackage or top-level module that is not in the
   map fails, as does a map key that no longer exists; every `cli.py` must be in L5.

## The allowlist only shrinks

Violations that existed when the guard landed are in
[`tests/architecture/allowlist.py`](../tests/architecture/allowlist.py), each with the step
that removes it (A1 readonly service into `memfw.index`; A2 cycle breaking; A3 composition
root; A4 MCP split; A5 package split). The test fails when:

- a violation is not in the allowlist (fix it; do not add it), or
- an allowlist entry no longer occurs (delete it, and lower `ALLOWLIST_SIZE` in the test).

So the list can only get shorter, and it is empty when the A series is done.

## Byte-identical harness

[`tests/harness/byte_identical.py`](../tests/harness/byte_identical.py) runs one probe against
two source trees and diffs the output. The old tree is `src/` and `scripts/` at a git ref,
unpacked with `git archive`; the new one is another ref or the working tree. Each tree runs in
its own subprocess with an empty `HOME` and no `MEMFW_*` variables.

```python
from tests.harness import byte_identical

results = byte_identical.compare(
    "tests.harness.probes:mcp_memory_search", [{"query": "backup"}], old_ref="origin/main"
)
assert all(result.identical for result in results), results[0].diff
```

Probes live in `tests/harness/probes.py` and may use only APIs present in both trees.
`tests/test_byte_identical_harness.py` is the self-test (HEAD against HEAD); it skips when
git is unavailable.

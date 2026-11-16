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

The spec's L-number is kept in each name. Projections (L1) rank above the kernel (L2)
because every projection is rebuilt from the vault and imports `memfw.core` (spec v1.1).
`memfw.knowledge` is in the kernel because `KnowledgeStoreMixin` is a mixin of `Repository`:
the vault facade cannot exist without it.

## Rules (`tests/test_architecture_layers.py`)

The scanner (`tests/architecture/scan.py`) parses every file; nothing is imported. A module
depends on another when it imports it (at any depth, including inside functions and under
`TYPE_CHECKING`; relative imports are resolved), when it imports a name a package
`__init__` re-exports (`from memfw import RelatedIndex` counts as `memfw.related`, so the
root cannot launder an upward import), or when it holds a string constant naming a memfw
module (`"memfw.cli"`, `"memfw.cli:app"`: the input of `importlib.import_module`,
`__import__` or `pkgutil.resolve_name`). Checked per unit-level edge:

1. **No upward imports.**
2. **Same-layer imports only when listed** in `SAME_LAYER_ALLOWED` (a reviewed, acyclic
   list; never for L3 capabilities, which talk through a port).
3. **Restricted units:** `memfw.ports` imports only the standard library; `memfw.recall`
   imports only `memfw.ports` (internals go through `recall/builtin.py`); `memfw.mcp`
   imports only `memfw.ports` (and `memfw.runtime` once it exists).
4. **No library code reaches into `scripts/`.** This is a heuristic: an import of a
   `scripts` module, or a non-docstring string constant with `scripts` as a path component
   (`"scripts"`, `"scripts/x.py"`, `"../../scripts"`) or naming a script module
   (`memfw_readonly_service`).
5. **No unreadable loader calls.** `import_module`, `__import__`, `spec_from_file_location`
   and `resolve_name` on a name that is not a string constant (or a module-level name bound
   to one) is a violation, except in the modules of `DYNAMIC_IMPORT_ALLOWED`
   (operator-configured `module:function` plug-ins), with an exact site count. Bare
   `import memfw` is a violation too, because attribute access on it is not resolved.
6. **No cycles** between units once allowlisted edges are removed.
7. **Every top-level module is classified:** a new subpackage or top-level module of
   `memfw` that is not in the map fails, as does a map key that no longer exists. Nested
   modules inherit their package's unit (a new `memfw.capture.x` is `memfw.capture`),
   except that every `cli.py` must map to L5.

Not seen by the scanner: module names assembled at run time from pieces, entry points loaded
through `importlib.metadata`, and attribute access on a `memfw` binding.

## The allowlist only shrinks

Violations that existed when the guard landed are in
[`tests/architecture/allowlist.py`](../tests/architecture/allowlist.py). Each entry names the
step that removes it (A1 readonly service into `memfw.index`; A2 cycle breaking; A3
composition root; A4 MCP split; A5 package split) and records every importing module with its
number of import lines on that edge. The test fails when:

- a violation is not in the allowlist (fix it; do not add it);
- an allowlisted edge gains an importing module or an import line (fix it);
- an entry or a site count no longer occurs (delete or lower it, and lower `ALLOWLIST_SIZE`
  in the test);
- the list has more entries, or more sites in total, than at the merge-base with
  `origin/main` (override the ref with `MEMFW_ARCH_BASE_REF`). This check skips, with the
  reason, when the ref or the merge-base is not available or the file did not exist there.

So the list can only get shorter, and it is empty when the A series is done. The allowlist
is plain Python literals because the merge-base check reads the old version from git with
`ast.literal_eval`.

**CI history.** `actions/checkout` fetches one commit by default, so the merge-base check
and any harness comparison against `origin/main` skip or fail for lack of history. A refactor
PR that relies on them sets `fetch-depth: 0` on the checkout step (or runs
`git fetch origin main` first).

## Byte-identical harness

[`tests/harness/byte_identical.py`](../tests/harness/byte_identical.py) runs one probe against
two source trees and diffs the output. The old tree is `src/` and `scripts/` at a git ref,
unpacked with `git archive`; the new one is another ref or the working tree. Each tree runs in
its own subprocess with an empty `HOME` and no `MEMFW_*` variables. Both trees share the
current interpreter and venv (third-party packages and the probe module); only memfw's source
differs. An unknown ref, or one without `src/` and `scripts/`, is a hard error
(`UnknownRef`); only a missing git makes the self-test skip.

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

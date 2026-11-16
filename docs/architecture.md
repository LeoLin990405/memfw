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
| 7 | `L4-runtime` | `memfw.runtime` (the composition root; A3a: recall wiring) |
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
`__import__` or `pkgutil.resolve_name`), or names one through a loader call
(`import_module(".x", "memfw")`, `__import__("memfw", fromlist=["x"])`,
`spec_from_file_location(name, "src/memfw/x.py")`). The entry-point group name
`"memfw.adapters"` in `ports/registry.py` is exempt (`NOT_MODULE_NAMES`). Checked per
unit-level edge:

1. **No upward imports.**
2. **Same-layer imports only when listed** in `SAME_LAYER_ALLOWED` (a reviewed, acyclic
   list; never for L3 capabilities, which talk through a port).
3. **Restricted units:** `memfw.ports` imports only the standard library; `memfw.recall`
   imports only `memfw.ports` (internals go through `recall/builtin.py`); `memfw.mcp`
   imports only `memfw.ports` and `memfw.runtime`.
4. **No library code reaches into `scripts/`.** This is a heuristic: an import of a
   `scripts` module, or a non-docstring string constant with `scripts` as a path component
   (`"scripts"`, `"scripts/x.py"`, `"../../scripts"`) or naming a script module
   (`memfw_readonly_service`). `tests/test_no_scripts_import.py` (from A1) guards the same
   rule independently and more strictly (it also flags `exec`, `sys.path` mutation and
   similar); the two overlap on purpose and both stay.
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
step that removes it (A1, done in #38: readonly service into `memfw.index.readonly`; A2 cycle breaking; A3
composition root; A4 MCP split; A5 package split) and records every importing module with its
number of import lines on that edge. The test fails when:

- a violation is not in the allowlist (fix it; do not add it);
- an allowlisted edge gains an importing module or an import line (fix it);
- an entry or a site count no longer occurs (delete or lower it, and lower `ALLOWLIST_SIZE`
  in the test);
- compared with the allowlist at the merge-base with `origin/main` (override the ref with
  `MEMFW_ARCH_BASE_REF`), it has a key the base lacks (a renamed or swapped entry counts),
  a module with more sites than at the base, more entries, or more sites in total. The check
  skips when the file did not exist at the merge-base. When the ref or the merge-base is not
  available it skips locally and fails in CI (`CI` set).

So the list can only get shorter, and it is empty when the A series is done. The allowlist
is plain Python literals because the merge-base check reads the old version from git with
`ast.literal_eval`.

**CI history.** `actions/checkout` fetches one commit by default; `.github/workflows/ci.yml`
sets `fetch-depth: 0` so `origin/main` and the merge-base exist for this check and for
harness comparisons against `origin/main`. Another CI setup needs the same (or
`git fetch origin main`).

## A3: the composition root

`memfw.runtime` (L4) builds capability objects for the interfaces: one Runtime per Gateway.
`build_runtime(root, *, tenant, allowed_sources, embedder, entity_enabled, provider_timeout=3.0,
total_timeout=4.0)` resolves `root` once (as `Repository` does) and takes the other values the
interface already resolved; there is no `runtime:` section in `vault.yaml` yet, so the default
assembly is today's behaviour, byte-identical except timing fields.

**A3a (done).** `Runtime.recall(query, *, limit, policy_name, source, include_history, as_of,
scope) -> RecallOutcome` owns the `memory_search mode=two_stage` wiring: the providers
(`FtsProvider`, plus `HybridProvider` with an embedder; the `Runtime._two_stage_providers`
test seam), `default_resolvers`, the `compat_fts`/`strict` `FilterPolicy` (any other
`policy_name` raises `ValueError`: fail closed), the `InflightRegistry` (one per Runtime) and
the coordinator call. `RecallOutcome.observed` carries the raw index rows of the providers
that answered, from which the MCP server builds rows as before. `Runtime.ensure_index()`
(`memfw.runtime.ensure_search_index`) is the index staleness check the MCP server runs before
every FTS-backed search, before the coordinator's deadline starts. In-flight slots are counted
per provider name and shared by every caller of one Runtime: a hung provider shows as `busy`
across tools until it returns. The MCP server no longer imports `memfw.recall` or
`memfw.recall.builtin` (two allowlist entries removed).

**Still to do (A3b and later).**

- Resolve settings (tenant, allowed sources, entity flag, embedder from
  `MEMFW_EMBEDDING_*`) in `build_runtime` instead of `Gateway.__init__`; that removes
  `memfw.mcp -> memfw.notes`.
- `Repository` opening from the runtime (`memfw.mcp -> memfw.core.repository`); the
  `memfw.index` edge goes once `readonly` is reached through the runtime too.
- The other `memory_search` modes: `MemoryHybridRetriever` (`memfw.memory_hybrid`),
  `search_multihop` (`memfw.retrieval`), `RelatedIndex` (`memfw.related`).
- The two_stage input checks (`check_query_text`, `as_of` parsing) and row building move
  below L5 so the server only translates.
- The CLI gets its capability objects from the `Runtime` too; a `runtime:` config section
  (fail closed on unknown names) and `build_runtime(test_config)` for tests.
- A4 entries (`core`, `capture`, `conversations`, `dream`, `history`, `ontology`) go with
  the MCP split, not A3.

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

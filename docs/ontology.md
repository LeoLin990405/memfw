# Concept taxonomy, relation types, edges and path patterns (K1–K4)

Concept files live at `concepts/<class>/<slug>.md` (see `memfw concepts --help`).
This page covers four additive pieces on top of them: an optional parent
hierarchy (`broader`), a versioned table of relation types, a rebuildable
projection of typed edges (K3) and bounded path patterns over it (K4). None of
them changes search, `memory_context`, topics, OKF export or the lint of a
vault that does not use them.

## K1: `broader` and `narrower`

A concept file may carry `broader`, the oids of its parents:

```yaml
---
oid: concept:fruit
class: Concept
pref_label: fruit
alt_labels: []
same_as: []
status: active
merged_into: null
broader:
- concept:food
---
```

A single oid string is accepted too. `narrower` is never written: it is
derived from `broader` (`memfw.ontology.narrower_index`, and the `narrower` key
of `memfw concepts show`). A parent that was merged counts for the concept it
was merged into. A hand-written `narrower` key is reported as
`unknown_relation`, as before.

`memfw concepts show` adds `broader` and `narrower` only for a concept that has
them; other concepts print the same keys as before.

**No query expansion.** Nothing on the read path follows `broader`: a query
for `food` does not return memories about `fruit`. Expansion along `broader`
would change default results and needs separate approval and an evaluation.

### Lint

These review kinds are reported by `memfw concepts list` (and
`lint-relations`) only for concepts that carry `broader`:

| kind | meaning |
|---|---|
| `broader_invalid` | the value is not an oid or a list of oids |
| `broader_dangling` | a parent oid has no concept file |
| `broader_merged` | a parent was merged; `resolved` names the oid to use instead |
| `broader_cycle` | following `broader` (through merges) loops back; `oids` lists the loop |
| `broader_on_merged` | a merged concept still carries `broader`; its parents count for `resolved` |

A merged concept's own `broader` counts for the concept it was merged into, so
`broader_cycle` also finds a loop that runs through it.

`broader` is no longer reported as `unknown_relation`, and its targets are
checked by `broader_dangling` instead of `dangling_oid`.

## K2: relation-type table

`src/memfw/ontology/relations.yaml` (version 1, read by
`memfw.ontology.builtin_relation_table()`, printed by
`memfw concepts relations`) names every typed relation memfw already stores,
and where it is stored:

| relation | from → to | written in |
|---|---|---|
| `about`, `applies_to` | memory or concept → concept | `about`, `applies_to` |
| `superseded_by` / `supersedes` | memory or concept → memory or concept | `superseded_by` (`supersedes` on concept files) |
| `evidence` | memory or concept → memory, concept, `att:`, `kh:`, external | `evidence` |
| `cites` | memory → `kh:` note | `kh:` refs in evidence or body |
| `observed_in` | memory → `att:` attachment | `att:` refs in evidence |
| `treatment_of`, `outcome_of` | memory → concept | `causal_record.treatment` / `.outcome` |
| `experience_of` | memory → task or memory | `experience.task`, `experience.evidence` |
| `conflicts` | open question → memory | `open_question.conflicts` |
| `resolved_by` | open question → memory | `open_question.resolved_by` |
| `related` | any → any | `related`; derived and untyped |
| `broader` / `narrower` | concept → concept | `broader`; `narrower` derived |
| `decided_by`, `authored_by`, `contradicts`, `derived_from`, `part_of`, `runs_on` | concept → concept | the key of the same name |

**Version 1 is additive.** New rows (such as `conflicts` and `resolved_by`)
may be added to `relations.yaml` without changing `version: 1`; a row is
never removed, renamed or narrowed (fewer node kinds, fields or a weaker
filter) under the same version. A change of that kind bumps `version`, which
the loader then refuses until the code knows it.

Each row states which node kinds each end allows, whether cycles are allowed,
the strongest `epistemic` type an edge of that kind can carry (`about` is
`inference`), and that it is filtered by privacy and tenant. Every row must
set both filters to `true`; the loader refuses a table that does not, or one
with an unknown key, node kind, epistemic type, inverse, or version (`version`
must be the integer `1`; `true` is refused), or with an end or field kind that
is not a plain string. Rows are immutable and hashable (`fields` is a
read-only `FieldMap`), so the cached built-in table can be shared.

**The `filter` flags are a declaration.** Every row says it is filtered by
privacy and tenant, but the table itself filters nothing. The code that reads
edges must apply the filters: the K3 projection does (below, with contract
tests), lint only reports.

The concept-file relations in the table are exactly the keys
`memfw concepts list` already accepted plus `broader` (a test pins this), so
the existing `unknown_relation` check and the table agree.

The table is read by lint and by the edge projection (K3), which builds an
edge only for a relation the table lists, between node kinds the row allows.

### `memfw concepts lint-relations`

An opt-in, read-only check of the vault against the table. Nothing else calls
it.

| kind | meaning |
|---|---|
| everything in the K1 lint | |
| `relation_unknown_type` | a concept-file key that is neither a core key nor a concept relation in the table |
| `relation_dangling` | a memory's `about`/`applies_to` names no concept (checked only when `concepts/` exists), or its `superseded_by` names no memory |
| `relation_cross_tenant` | a memory's `superseded_by`, `evidence`, `experience.evidence`, `related`, `open_question.conflicts` or `open_question.resolved_by` names a memory of another tenant (a `memory:`/`mem:` prefix is read as the projection reads it). The K3 projection never builds such an edge; other exits apply their own tenant rule (the plain-file projection admits `""` and `fleet` together by default), so lint reports it rather than assuming it is hidden |

## K3: the typed-edge projection

`memfw graph rebuild` reads the vault and writes the edge projection
**outside the vault**, in memfw's local data directory and keyed by the vault:
`~/.local/share/memfw/edges/<key>.sqlite3`, where `<key>` is the first 24 hex
digits of the sha256 of the vault's resolved path (the rule the usage
projection follows, `memfw.core.local_data`; the home directory is `$HOME`).
The file is mode 0600 (a directory it creates is 0700), written to a
temporary file and renamed into place, and refused inside the vault and
through a symlink. Outside the vault it never counts toward the physical-size
budget (which total-mode budget enforcement acts on; a hard forget deletes
memories) or the transaction fingerprint, however large it grows. It can be
deleted at any time; nothing else reads it, and building it never writes the
vault. The same vault gives the same file bytes and the same `version`.

```console
$ memfw graph rebuild --root ~/vault
{"path": "~/.local/share/memfw/edges/<key>.sqlite3", "edges": 412, "version": "edges-v1:3f…"}
```

(`path` is printed in full.) An `edges.sqlite3` at the vault root, left by a
pre-release build, is ignored: `graph rebuild` adds `"legacy_file":
"edges.sqlite3"` to its answer and prints a one-line note on stderr
suggesting you delete it. memfw never deletes it.

**Which vault built it.** The file's meta table records the vault's
identity (`memfw.core.local_data.vault_identity`): its resolved path, its
`vault_id` from `vault.yaml` when it has one (written on the first attachment
and never changed), and the hash of its first event (read from the segment
catalog once the log is compacted, else from the first line of
`events.jsonl`; the hash covers that event's random id and its timestamp).
`graph match` refuses a file whose identity does not match the vault — paths
must be equal, then the `vault_id`s when both sides have one, else the
first-event hashes — with `edge projection belongs to another vault: rebuild
it (memfw graph rebuild)`. So deleting a vault and running `memfw init` at
the same path never lets the new vault read the old vault's edges, even
before a rebuild. A vault that gets its `vault_id` after a build still
matches (through its first event); a vault whose event log was empty at
build time stops matching at its first write, like any stale projection.
A projection from the release before (schema 1, no identity) is refused as
"another schema version: rebuild it".

**Moved, symlinked and `$HOME` vaults.** The file is keyed by the vault's
*resolved* path:

- a vault opened through a symlink uses the same projection as its target
  (same key, same identity);
- a moved (or copied) vault has a new key, so it starts with no projection:
  run `memfw graph rebuild` at the new path. The old file stays where it
  was, unused, until you delete it;
- a vault rooted at `$HOME` itself cannot have an edge projection: the file
  would be inside the vault, and `graph rebuild` refuses that. The same holds
  for the corroboration projection.

**Edges** (`memfw.edges.collect_edges`). One edge per `(relation, source,
target)`, each with the vault field it came from:

| relation | from → to | read from |
|---|---|---|
| `about`, `applies_to` | memory or concept → concept | the field; an oid (through `merged_into`), else a label/alias with one owner, else a concept node named by the value |
| `superseded_by` | memory or concept → memory or concept | `superseded_by`; a concept's `supersedes` is stored as `superseded_by` read backwards |
| `evidence` | memory or concept → memory, concept, `att:`, `kh:`, external | `evidence` |
| `observed_in`, `cites` | memory → `att:` / `kh:` | `att:`/`kh:` refs in `evidence`; `kh:` refs in the body |
| `treatment_of`, `outcome_of` | memory → concept | `causal_record.treatment` / `.outcome`, only when they name a concept |
| `experience_of` | memory → task or memory | `experience.task`, `experience.evidence` |
| `related` | memory → memory or concept | `related` |
| `conflicts`, `resolved_by` | open question → memory | `open_question.conflicts`, `.resolved_by` |
| `broader` | concept → concept | `broader` |
| other concept relations | concept → concept | `decided_by`, `authored_by`, `contradicts`, `derived_from`, `part_of`, `runs_on` |

Inverse names are never stored: `supersedes` is `superseded_by` walked
backwards, `narrower` is `broader` walked backwards. A merged concept's
relations count for the concept it was merged into. Self loops, edges to a
memory that does not exist, ids memfw cannot store and relations or node
kinds the table does not allow are dropped. **An edge between memories of two
tenants is never built.**

**Visibility.** Each edge carries the tenant and privacy level of both ends
and the validity window of its memory ends (latest `valid_from`, earliest
`valid_to`):

- a memory's level is the rule every exit uses (`memfw.ports.privacy`, MM0):
  the `privacy` field, else the vault's `legacy_privacy_default`
  (`local-private` when unset), then `privacy:*` and `sensitivity:*` tags and a
  top-level `sensitivity` key; the strictest wins and anything unrecognised is
  `unknown`;
- a concept is shared vocabulary: no tenant, and a level only when its file
  states `privacy`, privacy tags or `sensitivity` (a merged concept carries
  the strictest of itself and its target);
- `att:`, `kh:`, external and task nodes have neither; they are reached only
  through the memory that names them.

An edge is shown only when every end's tenant that is set equals the caller's
tenant and every end's level that is set is in the caller's clearance
(`unknown` never is). So **a private end hides the edge**, and the private
node's id never appears. The rule lives in the port
(`memfw.ports.projection_store.edge_visible`) and every store must apply it on
`query`; `memfw.ports.testing.check_projection_store` checks both stores
memfw ships (SQLite and the in-memory reference) against it.

**The port.** `ProjectionStore` (architecture section 4): `rebuild(edges)`,
`version()`, `query(scope, node=, direction=, relations=, limit=)`. See
`docs/ports-and-recall.md` section 2.8.

## K4: bounded path patterns

`memfw graph match` follows a short typed path from one start node. It uses
Cypher's semantics for the parts it borrows, with no Cypher in memfw:

```console
$ memfw graph match --root ~/vault --from mem-apple --path "about>broader*1..2><about"
{"from": "mem-apple", "path": "about>broader*1..2><about", "paths": [{"nodes": [...], "edges": [...]}], "truncated": false}
```

**Syntax.** A path is a chain of steps (spaces between them are ignored):

| step | meaning |
|---|---|
| `NAME>` | one `NAME` edge forward (source → target) |
| `<NAME` | one `NAME` edge backward (target → source) |
| `NAME*N>` / `<NAME*N` | exactly `N` hops |
| `NAME*M..N>` / `<NAME*M..N` | `M` to `N` hops (`1 <= M <= N`) |

`NAME` is a relation of the table; `supersedes` and `narrower` are accepted and
rewritten to `<superseded_by` and `<broader` (the answer's `path` shows the
canonical spelling). Examples: `about><about` (memories that share a concept),
`supersedes*1..3>` (what a memory replaced, up to three versions back),
`<about` from a concept (memories about it), `broader*1..4>` (ancestors).

**Semantics.**

- An edge is used at most once per path (Cypher's `DIFFERENT
  RELATIONSHIPS`); nodes may repeat, unless `--acyclic` (Cypher's `ACYCLIC`).
- Every step is bounded: no unbounded `*`, no predicates, and the steps' upper
  bounds add up to at most **4 hops**.
- `--fanout` (default 50, at most 200) neighbours per node and step, sorted
  by edge key; at most 2000 store queries and 10 000 collected paths per
  match; `--limit` (default 20, at most 100) paths returned. A node's
  neighbours are read page by page (the port's `after` cursor) until
  fan-out + 1 visible ones are found, so hidden edges never crowd out visible
  ones. `truncated` means a visible result was cut (more than `--fanout`
  visible neighbours, more than `--limit` paths) or a query/collection cap
  ran out; hidden edges never set it.
- A path is its sequence of edges: when two ways of splitting the hops
  between steps walk the same edges (`related*1..2>related*1..2>` over
  a→b→c→d), it is returned once.
- Paths are sorted by length, then node ids, then edge keys: the same edges
  and path give the same answer from every store.
- `--from` is a memory id (`memory:`/`mem:` prefix allowed), a concept oid or
  a concept label/alias with one owner, or any other node id (`kh:`…).

**Filtering, like every exit.** `--tenant` (default `""`, exact match),
`--clearance` (default `public,shared`; there is no "no filter" value) and
`--source` (repeatable allowed sources). Besides the store's filter, every
memory on a path is re-read from the live vault and checked against these
rules with the current `legacy_privacy_default`, and a concept file that
states a privacy level is re-read too, so a memory deleted or made private
after the last rebuild is hidden before the next one. A concept node must
still exist in the vault: a concept file (or a label/alias of one, through
the alias index), else a value that a memory the caller may see names in
`about` or `applies_to` (the vault's memories are read once per match, only
when such a node is reached). Any other concept node — one only a deleted,
edited or hidden memory named, or one whose file was removed and that no
visible memory names — is hidden. A hidden node is absent:
no path runs through it, it does not use up the fan-out, and a hidden start
answers exactly like a start that does not exist (no paths, not truncated).

*Known limit:* the store reads a node's neighbours a page at a time, and
every page after a node's first costs one of the 2000 store queries, also
when the live check then hides all its rows. A match over a node with many
thousands of hidden neighbours can therefore run out of queries and report
`truncated: true` sooner than the visible result alone would explain. It
reveals no hidden id, and needs that scale to notice.

**Scope.** CLI and runtime (`memfw.runtime.graph`) only. There is no MCP tool:
the default `tools/list` is unchanged. `multihop` recall does not use these
semantics yet (moving it needs an evaluation).

## Compatibility

- Files without `broader` keep their bytes; no file is rewritten by loading or
  linting. `memfw concepts merge` renders `broader` where it did before (sorted
  with the other extra keys).
- memfw 0.2.0 reads a concept with `broader` (it keeps the key as an extra and
  writes it back on merge), but its `memfw concepts list` reports
  `unknown_relation: broader` and `dangling_oid` for a missing parent, and it
  has no `narrower`, no `broader_*` lint, no relation table, and no
  `concepts relations` / `lint-relations` commands.
- `memfw concepts import-seed` now copies a seed entry's `broader` when it is
  an oid or a list of oids (a malformed value such as `null` or `{}` is left
  out); 0.2.0 dropped it.
- Neither version expands queries along `broader`.
- K3/K4 add `memfw graph rebuild` / `memfw graph match` and the edge
  projection file (outside the vault); no existing command reads it or changes its
  output when it exists. `memfw concepts relations` prints the two new rows
  (`conflicts`, `resolved_by`); `memfw concepts list` adds
  `broader_on_merged` (and a cycle through a merged concept) only for a merged
  concept that carries `broader`.

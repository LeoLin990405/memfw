# Concept taxonomy and relation types (K1, K2)

Concept files live at `concepts/<class>/<slug>.md` (see `memfw concepts --help`).
This page covers two additive pieces on top of them: an optional parent
hierarchy (`broader`) and a versioned table of relation types. Neither changes
search, `memory_context`, topics, OKF export or the lint of a vault that does
not use them.

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
| `related` | any → any | `related`; derived and untyped |
| `broader` / `narrower` | concept → concept | `broader`; `narrower` derived |
| `decided_by`, `authored_by`, `contradicts`, `derived_from`, `part_of`, `runs_on` | concept → concept | the key of the same name |

Each row states which node kinds each end allows, whether cycles are allowed,
the strongest `epistemic` type an edge of that kind can carry (`about` is
`inference`), and that it is filtered by privacy and tenant. Every row must
set both filters to `true`; the loader refuses a table that does not, or one
with an unknown key, node kind, epistemic type, inverse, or version.

The concept-file relations in the table are exactly the keys
`memfw concepts list` already accepted plus `broader` (a test pins this), so
the existing `unknown_relation` check and the table agree.

The table is read only by lint today. The edge projection (K3) will build
typed edges from it.

### `memfw concepts lint-relations`

An opt-in, read-only check of the vault against the table. Nothing else calls
it.

| kind | meaning |
|---|---|
| everything in the K1 lint | |
| `relation_unknown_type` | a concept-file key that is neither a core key nor a concept relation in the table |
| `relation_dangling` | a memory's `about`/`applies_to` names no concept (checked only when `concepts/` exists), or its `superseded_by` names no memory |
| `relation_cross_tenant` | a memory's `superseded_by`, `evidence` or `experience.evidence` names a memory of another tenant; such an edge is always filtered out |

## Compatibility

- Files without `broader` keep their bytes; no file is rewritten by loading or
  linting. `memfw concepts merge` renders `broader` where it did before (sorted
  with the other extra keys).
- memfw 0.2.0 reads a concept with `broader` (it keeps the key as an extra and
  writes it back on merge), but its `memfw concepts list` reports
  `unknown_relation: broader` and `dangling_oid` for a missing parent, and it
  has no `narrower`, no `broader_*` lint, no relation table, and no
  `concepts relations` / `lint-relations` commands.
- `memfw concepts import-seed` now copies a seed entry's `broader`; 0.2.0
  dropped it.
- Neither version expands queries along `broader`.

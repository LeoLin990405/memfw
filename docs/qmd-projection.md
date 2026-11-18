# Recall projection and resolve (QMD and other recall backends)

`memfw.projection` lets an external recall backend index memfw memories and
hand its hits back to memfw for the authoritative answer. QMD is the first
backend that uses it.

## Boundary

- **QMD is an independent, native product. memfw contains no QMD code**: it
  does not import, vendor, spawn or call QMD.
- memfw couples to a backend in only two ways:
  - **files out**: `memfw projection export` writes plain Markdown files and a
    manifest. The backend indexes them with its own tooling.
  - **ids back**: `memfw resolve` takes the paths or ids the backend returned
    and answers with memfw ids plus authoritative metadata (lifecycle,
    validity, supersession, privacy, tenant) and the body read from the vault.
- The contract is backend-neutral. Nothing in the files, the manifest or the
  resolve payload is specific to QMD, so another recall backend can reuse it.
- A backend is trusted only for *which* documents matched and how well. It is
  never trusted for a document's body or its metadata.
- Every command here is read-only on the vault. The export writes only under
  `--out`, which may not be inside the vault.
- `src/memfw/projection.py` depends only on the memfw-core document model
  (`memfw.core.models`, `memfw.core.errors`) and the narrow parser
  `memfw.core.markdown_parse.parse_memory_markdown(text, expected_id=...)`,
  never on the repository, storage or index, so it can move into its own
  package.

> **Privacy warning.** By default the export projects **every** privacy
> class, including `local-private` and `unknown` bodies. That is safe only
> while the backend's index is reachable solely through memfw (`resolve` +
> the filter matrix): the backend's own endpoints must not be exposed
> (in the R6 rollout that is step D-3b, closing the tailnet listeners).
> Until then, export with `--skip-privacy unknown,local-private` (policy key
> `skip_privacy`), which leaves those documents out of the projection
> entirely (`counts.skipped.privacy`).

**Two-stage recall (PR-2a).** The coordinator side lives in `memfw.recall`
(`docs/ports-and-recall.md`); the QMD side is the separate adapter package
`packages/adapters/memfw-adapter-qmd` ([below](#qmd-recall-adapter)), which
talks to QMD only through QMD's own MCP JSON-RPC. memfw still contains no QMD
code and never imports the adapter.

## Commands

```bash
memfw projection export --root /srv/vault --out /srv/projection \
  [--policy policy.json] [--policy-version 1|2] \
  [--lifecycle-source /srv/status/memory-meta-index.json] \
  [--previous /srv/projection/current] [--json]

printf 'f0123456789ab\nqmd://memories/memories/f0aaaaaaaaaaa.md\n' | \
  memfw resolve --root /srv/vault --lifecycle-source /srv/status/memory-meta-index.json \
  [--policy policy.json] [--policy-version N] --ids-stdin [--max-lines 200] [--json]

printf 'cron migration' | memfw index search --query-stdin --dry-run --root /srv/vault
```

- Each of `projection export` and `resolve` prints one JSON line on stdout.
  Any error exits with status 2 and prints one `error: …` line on stderr.
- The global `memfw --now ISO` pins the export's `generated_at_utc` and the
  generation directory's timestamp (for reproducible tests).
- `index search --query-stdin` reads the query from stdin, so it never appears
  in argv, `ps` output or sudo logs. Default (`--apply`) output and the
  `--dry-run` JSON are unchanged.
- `resolve` takes ids on stdin for the same reason.

## Fields: source and destination

The vault is keyed by memory id; the lifecycle source
(`status/memory-meta-index.json`, `{generated_at, root, stats, items[]}`) is
keyed by the `files/`-relative `path`. [`join_meta`](#join-rule) connects them.

| Field | Read from | Projection file | Manifest `meta` / resolve |
|---|---|---|---|
| `id` | vault `MemoryDocument.id` (imported: `f` + `sha256(rel)[:12]`) | path `memories/<id>.md`, frontmatter | yes |
| `fleet_path` | vault tag `fleet_path:<rel>` | frontmatter | yes (join key) |
| `title` | vault `title` | frontmatter, H1 | resolve only |
| `epistemic`, `causal` | vault (R3 fields) | policy v2 only: frontmatter + label line | v2 only; `null` under v1 |
| `status` | vault frontmatter | frontmatter | yes |
| `lifecycle` | lifecycle source `items[].lifecycle` | never | yes |
| `superseded_by` | vault frontmatter, else lifecycle source | frontmatter (vault value) | yes (either source) |
| `valid_to` | vault frontmatter | frontmatter | yes |
| `valid_until` | lifecycle source `items[].valid_until` | never | yes |
| `privacy` | lifecycle source `items[].privacy` + vault tag `sensitivity:private` | never | raw value + `privacy_class` |
| `tenant` | vault `MemoryDocument.tenant` (never derived from `scope`) | frontmatter | yes |
| `scope` | lifecycle source `items[].scope` | never | yes (not filtered on) |
| `layer`, `tier`, `is_stub` | vault | frontmatter (`layer`, `tier`) | yes |
| `type`, `tags`, `about`, `applies_to`, `anchor` | vault; `tags` minus `fleet_path:*` / `sensitivity:*` | label lines (+ `type` in frontmatter) | `type`, `tags` |

Lifecycle, privacy and `valid_until` live only in the manifest and the resolve
answer, so a lifecycle-source change does not change any projection file and
does not force a re-embed.

## Projection file

`files/memories/<id>.md` (synthetic, policy v1):

```markdown
---
id: f0123456789ab
fleet_path: example_cron_pause.md
title: 'Example: pause cron before a disk migration'
status: unconfirmed
layer: current
tier: hot
valid_to: null
superseded_by: null
tenant: ''
type: fact
projection_policy_version: 1
---

<!-- memfw-stable-labels v1; 供 BM25 召回，不参与过滤 -->
[memfw 认知=未声明 epistemic=unspecified | 因果=none causal=none | type=fact]
[memfw tags=cron,migration | about=disk-migration | applies_to=host-a | anchor=none]

# Example: pause cron before a disk migration

## Summary
Pausing cron avoids interrupted jobs during the copy.

## Body
Synthetic body.
```

- Frontmatter is `yaml.safe_dump` in the order shown; under **policy v2**
  `epistemic` and `causal` follow `title`. The frontmatter is informational:
  backends are not expected to filter on it, and memfw never reads it back.
- Exactly two label lines, fixed order, `[memfw …]` prefix, for lexical
  recall only. Values are whitespace-collapsed, `]` → `)`, `|` → `/`,
  comma-joined; an empty list is `none`.
- Line one under v1 is always `epistemic=unspecified` / `causal=none`. Under
  v2 it carries the document's values (`经验/事实/判断/推断`,
  `相关/假设/已验证`); a missing `epistemic` stays `unspecified`.
- A change event (when included) is written with `type` = the policy's
  `change_event_marker.type`.
- `## Summary` only when the summary is non-empty; `## Body` always.
  `## Causal claim` (treatment, outcome, estimand, identification,
  robustness) only under v2 when `causal` is `hypothesis` or `verified` and
  a causal record exists.
- Lifecycle, privacy, validity, tenant and verification state are never in
  the body text.

## Policy file

JSON, all keys optional; the defaults are the built-in v1 policy:

```json
{"schema_version": 1, "projection_policy_version": 1,
 "include_change_events": false,
 "change_event_marker": {"tag_prefix": "fleet_path:feedback-change-event-", "type": "change-event"},
 "exclude_tiers": ["cold"], "exclude_stubbed": true, "exclude_lifecycle": ["retired"],
 "lifecycle_source": "status/memory-meta-index.json", "lifecycle_max_lag_seconds": 600,
 "join_key": "fleet_path", "tenant_map": "identity", "label_line_version": 1,
 "authority": "files", "skip_privacy": []}
```

Validation (any failure → exit 2): unknown keys are rejected;
`schema_version` must be `1`; `projection_policy_version` `1` or `2`;
`join_key` only `fleet_path`; `tenant_map` only `identity`;
`label_line_version` only `1`; `change_event_marker` only `{tag_prefix, type}`;
`lifecycle_max_lag_seconds` a non-negative integer; the two booleans must be
booleans; `exclude_tiers` / `exclude_lifecycle` lists of strings;
`skip_privacy` a list of `none` / `local-private` / `unknown` (also set by
`projection export --skip-privacy a,b`; default empty = project all).
`--policy-version` overrides `projection_policy_version`. `policy_sha256` is
the sha256 of the canonical (sorted-key, compact) JSON of the *normalized*
policy, so formatting and omitted defaults do not change it.
`lifecycle_source` and `authority` are labels recorded in the manifest; the
file actually read is `--lifecycle-source`.

## Join rule

`join_meta(id, tags, meta)` is the one implementation used by both export and
resolve:

1. exactly one `fleet_path:<rel>` tag, else `unjoined`;
2. if the id looks imported (`^f[0-9a-f]{12}$`) it must equal
   `"f" + sha256(rel.encode()).hexdigest()[:12]`, else `id_mismatch`; other
   id shapes are not checked;
3. `rel` must match an `items[].path` exactly (no normalization) and only
   once; a missing lifecycle source, a missing path or a duplicated path is
   `unjoined`.

`join_status` is `ok`, `unjoined` or `id_mismatch`. Only `ok` carries
lifecycle-source fields.

## Privacy classification

- Lifecycle-source value: `null`, `""`, `none`, `public`, `fleet`, `shared`
  (case-insensitive) → `none`; `local-private` → `local-private`; anything
  else, including non-strings → `unknown`.
- Vault `sensitivity:*` tags (prefix matched case-insensitively): none →
  `none`; only `sensitivity:private` (any case) → `local-private`; any other
  value (`secret`, `local-private`, …) → `unknown` (fail closed).
- The stricter of the two wins (`none` < `local-private` < `unknown`).
  `privacy_source_mismatch` is true when exactly one source says `none`.

## Lifecycle source status

| Status | When |
|---|---|
| `unavailable` | no `--lifecycle-source` given |
| `stale` | `generated_at` missing or unparsable, or any vault file fails to parse (lag `null`), or the newest parsed vault `updated_at` is more than `lifecycle_max_lag_seconds` after `generated_at` |
| `ok` | otherwise; lag = `max(0, newest updated_at − generated_at)` seconds, `0` for an empty vault |

A `--lifecycle-source` that is given but unreadable or malformed (not an
object with an `items` list of objects with string `path`) is an error.

## Export

1. Check the vault (a directory with `events.jsonl` or `vault.yaml`, no
   pending transaction) and that `--out` is not inside it.
2. Read every `memories/*.md`, compute `authority_content_head` and the
   lifecycle status.
3. **Fast path**: if the previous manifest (`--previous` may be a generation
   directory or a `manifest.json`; a missing one counts as none) has
   `schema_version` 1 and the same `authority_content_head`, `policy_sha256`
   and `lifecycle_source_status`, return `status: no_change_fast_path` with
   the previous `counts`, `join` and `manifest_sha256`, and write nothing.
4. Otherwise skip each document by its **first** matching reason, in order:
   `change_event` (unless included) → `stub` → `cold` (any tier in
   `exclude_tiers`) → `unjoined` / `id_mismatch` → excluded lifecycle
   (`retired`). Unparsable files count as `invalid`.
5. Write a new generation through a staging directory and one rename:

```text
<out>/tree-<YYYYMMDDTHHMMSSZ>-<generation_sha8>/   (0750)
  files/memories/<id>.md      (0600)
  manifest.json               (0600)
  delta.json                  (0600)  {schema_version, previous_manifest_sha256, added, changed, deleted}
  export-failures.jsonl       (0600)  one line per invalid / unjoined / id_mismatch document
```

memfw does not flip a `current` symlink, prune old generations or keep a delta
queue: the caller (an ops wrapper) owns those. A generation name that already
exists is an error. `delta.json` compares document ids and projection-file
hashes against the previous manifest.

Exported summary line: `schema` (`memfw-projection-export-v1`), `status`
(`exported`), `authority`, `lifecycle_source_status`,
`projection_policy_version`, `counts`, `join {unjoined, id_mismatch}`,
`authority_content_head`, `manifest_sha256`, `tree`, `manifest_path`,
`delta_path`, `failures_path`, `elapsed_ms`.

### `authority_content_head`

`sha256` over, in order:

```text
memfw-authority-content-v1\n
<id>\t<sha256(file bytes)>\n          one per vault memories/*.md, sorted by id
meta\t<sha256(canonical JSON of items sorted by (path, canonical item))>\n   or meta\tunavailable
```

It covers every input of the joined metadata. It excludes the lifecycle
source's `generated_at` (a rebuild with the same items keeps the head) and
the event log (reads append access events). `resolve` computes the same value
over the same inputs, so a consumer can check a cached manifest against the
live vault.

### Manifest

| Key | Value |
|---|---|
| `schema_version` | `1` |
| `projection_policy_version`, `policy_sha256`, `authority` | from the policy |
| `authority_head` | event-log head hash; observational only |
| `authority_content_head` | as above |
| `authority_sync_lag_seconds` | always `null` (see below) |
| `lifecycle_source`, `lifecycle_source_generated_at`, `lifecycle_source_status`, `lifecycle_source_lag_seconds` | as above |
| `join` | `{key: "fleet_path", id_rule: "f+sha256(rel)[:12]", unjoined, id_mismatch}` |
| `generated_at_utc` | export time, or `--now` |
| `config` | `include_change_events`, `exclude_tiers`, `exclude_stubbed`, `exclude_lifecycle`, `tenant_map` |
| `counts` | `vault_documents`, `projected`, `skipped {change_event, cold, stub, unjoined, id_mismatch, invalid, <each excluded lifecycle>}`, `privacy {class: n}`, `privacy_source_mismatch`, `deleted_since_previous` |
| `projection_sha256` | sha256 of canonical `{policy: policy_sha256, files: [[path, content_sha256], …]}` sorted by id |
| `tree` | generation directory name |
| `documents[]` | `{id, path, fleet_path, content_sha256, meta}` sorted by id; `meta` = `status, layer, tier, valid_to, valid_until, superseded_by, tenant, privacy, privacy_class, scope, type, lifecycle, epistemic, causal` |
| `previous_manifest_sha256` | sha256 of the previous manifest's bytes, or `null` |

## Resolve

- stdin lines are stripped, blanks dropped, duplicates removed in order; at
  most **50** per call (more → exit 2). Total stdin is capped at 1 MiB
  (more → exit 2); a line over 4096 characters is never resolved and goes to
  `unresolved`; every `unresolved` entry is cut to 200 characters.
- A line may be a bare id, a `memories/<id>.md` path with any directory or
  `scheme://` prefix (query and fragment ignored), or an `index search` line
  `<id>\t<title>`. Anything else goes to `unresolved`. A `files/<rel>` path
  is not an id: the caller must check that the file exists and map it with
  `stable_id(rel)` first.
- A well-formed id with no vault file goes to `missing`; a file that does not
  parse goes to `invalid`. Two hits for the same id produce one document.

```json
{"schema": "memfw-resolve-v1", "authority_head": "…", "authority_content_head": "…",
 "authority_sync_lag_seconds": null, "lifecycle_source_generated_at": "2026-01-01T00:00:00Z",
 "lifecycle_source_status": "ok", "lifecycle_source_lag_seconds": 0,
 "projection_policy_version": 1,
 "documents": [{"id": "f0123456789ab", "path": "memories/f0123456789ab.md",
   "fleet_path": "example_cron_pause.md", "join_status": "ok", "title": "…",
   "lifecycle": "active", "superseded_by": null, "valid_to": null, "valid_until": null,
   "privacy_class": "none", "privacy_raw": null, "privacy_source_mismatch": false,
   "tenant": "", "scope": "fleet", "status": "unconfirmed", "layer": "current",
   "tier": "hot", "is_stub": false, "type": "fact", "epistemic": null, "causal": null,
   "updated_at": "…", "tags": ["cron"], "content_sha256": "…", "body": "…",
   "body_truncated": false, "hit": "f0123456789ab"}],
 "missing": [], "unresolved": [], "invalid": [], "elapsed_ms": 12}
```

- `documents` keep input order. `body` is cut to `--max-lines` lines
  (`body_truncated: true`); `--max-lines 0` returns `body: null`.
- When `join_status` is not `ok` the document carries only `id`, `path`,
  `join_status`, `join_reason` and `hit`; every other key is present but
  `null` (`tags: []`, booleans `false`) — no title, tags, timestamps or body.
  Callers drop the document.
- Additive to the spec's payload: `unresolved`, `invalid`,
  `lifecycle_source_lag_seconds`, `privacy_source_mismatch`, `join_reason`,
  `body_truncated`, `hit`.

## Filter matrix

`filter_resolved(payload, FilterSettings)` is a library function for the
two-stage consumer (PR-2a). Its settings: `clearance` (default `{none}`;
`parse_clearance` turns an unset, empty or malformed comma list into
`{none}`), `tenants` (default `{"", "fleet"}`; `parse_tenants`, an empty item
allows `""`), `include_retired` (default off), `now`.

If `lifecycle_source_status` is not `ok`, it **fails closed**: no documents,
`fail_closed: true`, `fallback_reason: lifecycle_source_<status>`.

Otherwise `filter_decision` checks each document in this order; the first hit
drops it and increments one counter:

| # | Check | Counter | `include_retired` keeps it? |
|---|---|---|---|
| 1 | `join_status != ok` | `filtered_unjoined` | no |
| 2 | `is_stub` or `tier == cold` | `filtered_archived` | no |
| 3 | `privacy_class` not in clearance (`unknown` unless listed) | `filtered_privacy` | no |
| 4 | `tenant` not in tenants | `filtered_tenant` | no |
| 5 | `lifecycle` not `active` / `retired` / `superseded` (incl. missing) | `filtered_lifecycle_unknown` | no |
| 6 | `valid_to` or `valid_until` set but unparsable | `filtered_expiry_unparsable` | no |
| 7 | `lifecycle == retired` | `filtered_retired` | yes, label `retired` |
| 8 | `lifecycle == superseded` or `superseded_by` set | `filtered_superseded` | yes, label `superseded` |
| 9 | `now > min(valid_to, valid_until)` (either may be empty) | `filtered_expired` | yes, label `expired` |

`filtered_missing` = `len(missing)`; `filtered_unresolved` =
`len(unresolved) + len(invalid)`. Kept documents get `labels` (`disputed`,
`anchor` / `consolidated`, `epistemic:<v>`, `causal:<v>`, plus any of the
three above) and a `rank_adjust`: `disputed` −0.2, `anchor` +0.05. The rank
hints are **provisional** and will be tuned by evaluation.

## Resolved ambiguities

Decisions the implementation took where the spec left a choice open:

- `authority_sync_lag_seconds` is always `null`: memfw sees only the vault,
  not the upstream `files/` tree, so it cannot measure sync lag. The sync job
  that can see both should report it.
- `stale` compares the lifecycle source's `generated_at` with the newest
  vault `updated_at` (not the last `files/` import, which memfw cannot see).
  Export and resolve both use the parsed documents; a file that does not
  parse makes the status `stale` rather than being skipped.
- Export without `--lifecycle-source` projects nothing: every document is
  `unjoined` and the status is `unavailable`.
- `skipped` counts only the first reason, in the order change_event → stub →
  cold → join → lifecycle. `join.unjoined` / `join.id_mismatch` and
  `export-failures.jsonl` cover every document, including ones skipped
  earlier (for example a change event without a lifecycle item).
- `counts.privacy` and `privacy_source_mismatch` cover joined (`ok`)
  documents only, projected or not.
- The `cold` counter name stands for every tier in `exclude_tiers`.
- Resolve withholds everything but the id and join outcome for non-`ok`
  joins, so no unfiltered title, tag or body leaves memfw.
- Export writes a generation through `.staging-*` and removes it on any
  failure; missing parents of `--out` are created `0700`, the generation is
  `0750`, files `0600`.
- `content_sha256` in resolve is the sha256 of the **vault file bytes**; in
  the manifest it is the sha256 of the **projection file bytes**.
- Generation layout: `tree-<UTC stamp>-<generation_sha8>` (`generation_sha8` covers the
  projection files, policy, content head, lifecycle status and counts), written via
  `.staging-…` + rename; symlink, retention and delta queue belong to the
  caller.
- A date-only `valid_to` / `valid_until` means 00:00 UTC that day; a
  timezone-less timestamp is read as UTC.
- An unknown or missing `lifecycle` is dropped as
  `filtered_lifecycle_unknown`, never treated as `active`.
- `include_retired` releases expired documents too (labelled `expired`),
  following the spec's `include_retired` note; it never bypasses privacy,
  tenant, join, unknown lifecycle or unparsable expiry.
- A duplicated `items[].path` makes that path `unjoined` instead of picking
  one item.
- A missing `--previous` manifest is not an error (full export, `delta`
  marks everything `added`); an unreadable one is.

## QMD recall adapter

`memfw-adapter-qmd` (0.1.1, QMD 2.5.3, default off) implements two
`RecallProvider`s over a running `qmd mcp --http` server:

- **`qmd`** searches the collection that indexes `<generation>/files` of a
  projection export and maps each hit back to a memory id **through the
  manifest** (`documents[].path`). QMD stores *handelized* paths (every run of
  characters other than letters, digits and `$` becomes `-`), so the adapter
  handelizes the manifest's `memories/<id>.md` paths and looks hits up in that
  table; a hit not in the table, or one two paths share, is dropped. A
  document whose manifest `meta.privacy_class` is not `none`, whose
  `meta.tenant` is not the call's tenant, or that is not current (`lifecycle`
  set and not `active`, `superseded_by` or `valid_to` set, `valid_until` past
  or unparsable) is dropped too, so `local-private`, `unknown`, expired and
  superseded memories never leave the adapter. The coordinator then resolves the id
  against the vault and filters it again. The manifest is re-read when the file
  behind the configured path changes (a flipped `current` symlink), and the
  provider is unavailable while it is missing, malformed, older than
  `max_manifest_age_seconds` or not `lifecycle_source_status: ok`.
- **`qmd-notes`** searches a Knowledge-Hub collection whose tree is readable on
  this host (`notes_root`) and returns `kh:<path>#<heading>` refs (the heading
  of the hit's best line, as memfw's notes index names sections; characters a
  recall ref cannot hold are percent-encoded; `parse_kh_ref` decodes them, so
  `NotesSegmentResolver` resolves these refs when it is registered, which the Runtime does
  when `vault.yaml` sets `runtime.recall.notes`; see
  [ports-and-recall §4.1c](ports-and-recall.md#41c-lifecycle-source-and-notes-index-runtimerecall)). A note is
  returned only when its privacy level, read like memfw's notes reader
  (`yaml.safe_load` + `metadata_level`; anything unreadable is `unknown`), is
  `public`/`shared` and in the configured clearance; `legacy_privacy` (default
  `local-private`) is the level of notes without a `privacy` field.

Both send one `tools/call` `query` per search (`searches` lex + vec,
`rerank: false`, one collection, `candidateLimit: max(20, 4N)`), read only
`file`, `score` and `line` from the answer, and return the reference, QMD's
score and `extra={"kind": "qmd-memory"|"qmd-note"}`. Titles, snippets and
bodies from QMD are never returned. When QMD is unreachable, slow or answers
off-protocol, `search` raises and the coordinator reports the provider in
`labels.degraded` while FTS answers (compat framework T7).

The adapter checks the meta-index-only fields (`privacy`, `lifecycle`,
`valid_until`) from the manifest itself; QMD hits therefore never include
history. The vault resolver checks them again from the lifecycle meta-index
(`runtime.recall.lifecycle_source`, else the vault's default file).

The contract fixtures (`tests/fixtures/qmd_contract/`) were recorded from a
live QMD 2.5.3 `qmd mcp --http` on loopback, over a throwaway index of two
synthetic collections (the projection of `tests/projection_fixtures.py` and the
fixture notes tree). No embeddings were built, so the recorded answers are
lexical only; text fields are redacted. `handelize_cases` are outputs of QMD's
own function. Re-record with
`python -m memfw_adapter_qmd.record` before upgrading QMD.
`scripts/qmd_notes_consistency.py` is the T6 report: overlap between memfw's
notes index and the QMD notes adapter (not a gate).

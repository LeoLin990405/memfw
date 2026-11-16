# Changelog

All notable changes to memfw. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). Entries name the pull request that
carried the change; `#N` links to `LeoLin990405/memfw` pull request `N`.

The vault format did not change between `0.1.1` and `0.2.0`: `vault.yaml`
stays at `schema_version: 1`, and files written by an earlier version keep
their bytes (see [Compatibility and upgrade](#compatibility-and-upgrade)).

## [Unreleased]

### Fixed — budget enforcement keeps live supersede chains

- Budget/policy enforcement with `forget_action: hard` never deletes a live
  supersede chain. A record is a chain member when its `superseded_by` names a
  memory that still exists, or when such a record names it; for
  `a-old -> z-new` both are kept. A chain whose successor has been cooled to a
  cold stub is still live and still protected. Before, `fifo`, `lru`, `decay` and
  `salience` could delete `a-old` (losing it for `as_of` queries and
  `.history` reconstruction), or delete the head `z-new` and leave `a-old` as
  an orphan. Chain members are retained with the decision reason
  `protected_history` and still count toward usage, so the policy moves on to
  other records. This applies in body and total budget modes and to
  `memfw forget run --action hard`.
- Plain expiry is not protected: a record with `valid_to` set (past or
  future) and no live successor stays an ordinary eviction candidate.
- When only chain members (and anchors) are left in scope and the budget is
  still over, the result is unmet with the new reason code `history_floor`, a
  `budget_unsatisfiable` event, and the new `blocking_history_ids` field
  (filled only for `history_floor`). In body mode, a run blocked by anchors
  still reports `anchor_floor`, even when chain members also block; in total
  mode, `history_floor` is reported once every remaining in-scope non-anchor
  record is a chain member. Dry runs report the same floor from the plan.
  Other reasons are chosen exactly as before (in total mode with
  `budget_scope: hot`, only the `history_floor` check looks at in-scope
  records).
  `proposal_stats` gains `history_retained` and `history_floor_unmet`.
- Still allowed: an explicit `memfw delete` / `Repository.delete`, an
  approved merge and an approved forget proposal can remove a chain member.
  Soft forgetting is unchanged (the cold archive keeps the original bytes).
  Plans for vaults without live chains are byte-identical to before.
  `Policy.plan` takes a new keyword `protected_history_ids` (default empty);
  `memfw.policies.live_chain_ids` computes it.

### Internal — composition root, recall slice (A3a)

- New `memfw.runtime` (layer L4): `build_runtime(...) -> Runtime`. `Runtime.recall` now owns
  the `memory_search mode=two_stage` wiring the MCP server built inline (built-in providers,
  resolvers, `compat_fts`/`strict` filter policy, the per-gateway `InflightRegistry`, the
  coordinator call); the server gets recall only from its `Gateway.runtime`. The test seam
  `_two_stage_providers` moved from `Gateway` to `Runtime`. `memory_search` output is
  byte-identical for every mode.
- Architecture guard: `memfw.runtime` is mapped to `L4-runtime`, `memfw.mcp` may import
  `memfw.ports` and `memfw.runtime`, and the allowlist drops `memfw.mcp -> memfw.recall` and
  `memfw.mcp -> memfw.recall.builtin` (25 to 23 entries). Remaining A3 work is listed in
  `docs/architecture.md`.

### Fixed — recall privacy tags fail closed (security)

- `memfw.recall.builtin.meta_from_document` recognised only the exact, lower-case `sensitivity:private` tag, so under `policy=strict` (`memory_search mode=two_stage`) a memory tagged `Sensitivity:Private`, `sensitivity:local-private` or `sensitivity:secret` was kept. It now classes `sensitivity:*` tags exactly like the projection (`tag_privacy_class`): any case, only `sensitivity:private` is `local-private`, any other value is `unknown`, and both are dropped without clearance. `privacy:` and `lifecycle:` tag prefixes are matched in any case too, and with several such tags the strictest wins instead of the first (privacy: unknown > local-private > none; lifecycle: retired > superseded > other non-active > active), so tag order or case can never widen access. Before, `privacy:none` followed by `privacy:local-private` was kept under `strict`. `MemoryMeta` gains an optional `sensitivity_class`; callers that set only `sensitivity_private` behave as before. `policy=compat_fts` does not check privacy, so its output is unchanged.


### Fixed — body lookup parses only hash matches (performance)

- `_current_body_by_hash` and the archive lookup compared the body hash only after YAML-parsing each file, so every write parsed up to the whole vault and the cost depended on the filesystem's directory order (a perf guard passed on macOS and failed on Linux CI with 109 parses). They now hash first and parse only a matching file; the result is the same first file in glob order that both matches and parses. A non-matching file with invalid UTF-8 no longer raises; a matching one is skipped like any file that fails to parse. A delete in a 200-memory vault now parses 6 files in any directory order (was 6–204).

### Added — architecture guards (A0)

- `tests/test_architecture_layers.py`: every `memfw` module is placed in a layer
  (`tests/architecture/layers.py`); upward imports (including through root
  re-exports and memfw module names held in strings), unlisted same-layer
  imports, library references to `scripts/`, unreadable loader calls and new
  cycles fail the build. A new top-level module that is not in the map fails too.
- `tests/architecture/allowlist.py`: the 25 violations present today with their
  exact import sites, each naming the step (A1–A5) that removes it. The list can
  only shrink: new sites, stale entries or sites, and growth against the
  merge-base (new keys, per-module sites, totals) fail. CI checks out full
  history (`fetch-depth: 0`) so that comparison runs.
- `memfw.ports`: `Clock` and `IdGen` protocols with `SystemClock` (wall clock,
  never returns an earlier time), `FixedClock`,
  `UuidIdGen` and `SeededIdGen`; `memfw.ports.testing.check_clock` and
  `check_id_gen`. Core does not use them yet (A3).
- `tests/harness/byte_identical.py`: run a probe on a git ref and on the working
  tree (or another ref) and diff the output; self-tested with MCP `memory_search`.
- `docs/architecture.md`.

### Fixed — tenant isolation on supersede (security)

- `supersede` (API and CLI) writes the replacement in the predecessor's
  tenant. Before, it was always written in the default tenant `""`: visible to
  untenanted callers and invisible to its owner. The replacement now also keeps
  `layer`, `tier`, `type`, `related`, `about`, `applies_to`, `anchor` and
  `manually_marked`; `status` resets to `unconfirmed` and access statistics,
  `summary`, validity and archive fields start fresh (the full table is in the
  `Repository.supersede` docstring). `group_key` is deliberately not inherited:
  budget consolidation would group the replacement with its superseded
  predecessor, keep the predecessor and plan the current replacement for
  forgetting. A new `overrides` argument sets inherited fields (and
  `group_key`), never `tenant`.
- Because `tier` is inherited, superseding a cold-archived stub gives a
  replacement with `tier: cold` that is not a stub (it carries the new body).
  Soft forget, which cools hot memories, therefore no longer picks it.
- A reviewed merge refuses duplicates from a tenant other than the
  representative's, since it copies their bodies into the representative.
- Vaults may already hold replacements written in tenant `""`. `fsck` reports
  each one as `supersede_scope_mismatch` (the replacement's tenant differs from
  its predecessor's) and does not rewrite it. Any later replacement of such a
  memory inherited the wrong tenant too, so the message lists every downstream
  id of the chain: fix them all, for example by setting `tenant` with
  `Repository.update`. A deliberate tenant move with `update` on a replacement
  is reported the same way; there is no acknowledgement for it yet.

### Fixed — vault integrity

- Frontmatter ends only at a closing `---` line, so a tag, title or summary
  ending in `---` no longer truncates the frontmatter on read (and an update no
  longer persists the damage). 0.1.x files still parse → render byte-identical.
- `rollback` restores every document field of the target version except `id`,
  `created_at`, `recorded_at`, `version` and `updated_at` (previously `type`,
  `tenant`, `related`, validity, `superseded_by`, `status`, `evidence`,
  `epistemic`, `causal`, `causal_record`, `limitations` and `experience` were
  dropped). A human rollback counts as a human decision at the epistemic gate.
- `delete` prunes `.history/bodies/` objects, so forgotten text no longer stays
  on disk.
- A body object shared with another memory stays recoverable after that memory
  is cold-archived; archived-body lookup also searches other archive members
  by hash.
- `superseded_by` must name an existing memory other than itself and must not
  form a cycle; `delete` clears back-references to the deleted memory; `fsck`
  reports `dangling_superseded_by` and `cyclic_superseded_by`.
- A missing `events.jsonl` while committed state still expects events raises
  `IntegrityError` on read and append instead of reading as empty /
  `FileNotFoundError`.
- `events()` enforces `catalog_hash` from `events.state.json`; the append
  cursor no longer reseeds over a mismatched catalog. New archived summary rows
  carry the original `event_hash` and a full-event digest and are re-verified
  on read; older rows without the digest still load but must match
  `catalog_hash`.
- Archived event summaries keep `storage`, `after_config_hash`,
  `before_config_hash` and `target_snapshot_hash_mode`, which recovery and
  proposal staleness checks read.
- Rollback onto a version whose `superseded_by` names a since-deleted memory
  fails with an error that says so and suggests clearing `superseded_by`.
- The cleanup `delete` runs after its DELETE event commits (body-object
  pruning, clearing `superseded_by` back-references) contains failures per
  step and per reference; `fsck` re-runs it and reports
  `deleted_superseded_by_cleared`.

### Compatibility

- **No downgrade after compaction.** `summary_hash` on archived event
  summaries now also covers `original_event_digest` and `lossless`. Once this
  version compacts a vault's event log, memfw ≤ 0.2.0 can no longer read that
  log. Vaults that were never compacted by this version stay readable by 0.2.0.
- **`open_question` is new frontmatter.** memfw 0.2.0 reads an open-question
  file without error, keeps `type: open-question`, and lists `open_question` in
  `ignored_keys`; a write by 0.2.0 drops the block, after which this version
  refuses gated writes to that memory and `fsck` reports
  `open_question_missing_block`. Files without the block keep their bytes.
### Breaking for memfw-http/2 clients

- `/related` without a `tenant` now only sees empty-tenant memories: asking
  about a tenanted memory returns 404, and neighbours from other tenants are
  dropped. Clients must send the tenant they read (S1).
- `/stage` with an existing `proposal_id` but different content (title, tags,
  related, tenant or source) returns 409 `conflict` instead of
  `duplicate: true`; an identical resubmission is still a duplicate (S8).
- Conductor's federation client already handles both: a 404 from `/related`
  is an empty neighbour set and a 409 from `/stage` is a conflict.

### Security

- Readonly service `/related` compares tenants exactly, the empty tenant
  included, and always scopes edge neighbours by tenant, so an untenanted
  request no longer learns other tenants' ids, titles or existence (S1).
- MCP `memory_get`, `memory_related` and `memory_context` enforce the bound
  tenant and `MEMFW_ALLOWED_SOURCES`; FTS search filters sources in SQL
  before the page limit; `memory_status` counts only visible memories (S2).
- MCP history tools (`history_list`, `history_search`, `history_read`) honour
  `conversation_optout`, `readable: false` and the caller's `can_read`;
  `history_list` stats count only the sources the caller may see (S9).
- Concept errors name a vault-relative path; the service and MCP send clients
  a fixed message and log details to stderr (S4).
- The service bind guard resolves the host with `AI_PASSIVE` and refuses any
  unspecified address (`""`, `0`, `0.0`, `000.0.0.0`, `::`) (S5).
- `/stage` applies the bound `MEMFW_TENANT` (default bound tenant, foreign
  tenant → 403) (S6), and a resubmitted `proposal_id` with different
  title/tags/related/tenant/source is a 409 conflict instead of a silent
  duplicate (S8).
- Incremental transcript refresh resumes from the parser state saved before a
  partial trailing line, so redaction context (a password after
  `Enter password:`), line numbers and windows match a full rebuild (I1).

### Fixed — budget consolidation scope

- `consolidate_then_forget` groups memories only within one tenant: a memory
  is no longer consolidated away or forgotten because another tenant holds the
  same body or `group_key`.
- Historical records (`valid_to` or `superseded_by` set) join no consolidation
  group. Before, a supersede that kept the body grouped old and new versions,
  the old one won as representative, and the plan listed the current version
  as forgotten. `run_forget` never applies consolidation `forgotten_ids`
  directly, so the harm was a misleading plan plus a pending merge review
  proposal with `disposition: delete` against the current version, which an
  approval would have applied. Budget-triggered merge review now skips any
  pair that names a historical record; the judge still sees every document,
  so existing review proposal ids do not change. The other policies (`fifo`,
  `lru`, `decay`, `salience`) rank each record on its own and keep their
  behaviour, including cooling or deleting historical records under budget
  pressure.
- Agent merge proposals (`find_near_duplicates`, mock, live and local MLX)
  stay within one tenant; a provider pair that spans tenants is dropped (review
  refuses cross-tenant merges) and counted in the judge's
  `dropped_cross_tenant_pairs`, with a debug log line that carries no memory
  text.
- Plans for single-tenant vaults without historical duplicates or a shared
  `group_key` with a historical record are unchanged.

### Fixed — service, MCP and index

- A stale memory index is reported as `degraded: true` with
  `errors: ["index_stale"]` instead of a silent empty or unfiltered result (S3).
- MCP `memory_search` rejects a non-boolean `include_history` such as
  `"false"` (S7).
- Queries made only of terms under three characters (`AI ML`, `数据 迁移`)
  match on trigram indexes (memory index, service, notes, history) through an
  `instr` clause merged after FTS hits; lowercase English stopwords such as
  `is` and `of` are skipped there so they add no noise (I3).
- The MCP index reindexes when memory files are added with an older mtime or
  deleted outside memfw; multihop drops hits whose file is gone (I4).
- Multihop, hybrid and `MemoryIndex.search`/`search_many`/`ranked_search`
  treat `superseded_by` without `valid_to` as history (I5).
- Notes: deleting a note without vector tables no longer raises (I2); a
  changed embedder or dimension rebuilds vectors (I6); `path_prefix`/`since`
  filters apply before the candidate cap (I7); the build-time FTS tokenizer is
  recorded and used at query time (I8); tags with spaces survive (new
  `tags_json` column) and a reranked result is no longer marked degraded (I10).
- Code retrieval matches the ignore list against repo-relative paths,
  updates embeddings by row, and resolves recent files against the repo
  root (I9).
### Fixed — CLI, OKF export and candidates

- `okf export --force` refuses an output that is the vault, contains it or
  sits inside it, comparing by file identity (`st_dev`/`st_ino`) so symlinks
  and a case-insensitive file system cannot disguise the vault; the same check
  covers the `.tmp` scratch and backup paths. It replaces only a directory whose
  `manifest.json` was written by memfw-okf-export. Back-to-back exports in the
  same second get unique backup names, a failed export leaves no `.tmp`,
  `okf export .` works, and the refusal names `--force`; the CLI reports these
  as usage errors instead of tracebacks. The reported bundle path keeps the
  spelling you gave.
- `conversations schedule --print` fills plist placeholders in one pass, so a
  value containing a placeholder name is not expanded again; `--bin` defaults
  to the absolute path of the running `memfw` (failing clearly when none is
  found); the command reminds you to create the log directory, which launchd
  does not do.
- Candidates: a blank `proposal_id` is refused as `missing_proposal_id` (with
  the `/stage` advice) and a non-string one (YAML `false`) as
  `invalid_proposal_id`, instead of being applied; an unknown related id is a
  clean error.
- `memfw candidates` skips tampered, unreadable or unparseable candidate
  files and any path that resolves outside the vault (a symlinked file or a
  symlinked `_candidates/remember` directory), and lists them under `skipped`,
  instead of failing the whole listing; `candidates-expire` never writes
  through such a directory.
- `approve-proposal --dry-run` runs the same check as the approval
  (`Repository.check_proposal_approval`): the proposal exists, is undecided,
  matches the payload hash, is not stale and its changes would apply, instead
  of always reporting `payload_valid`. `record-proposal` refuses an invalid
  `layer`/`tier` or changes that do not make a valid memory, so a human
  decision is never written for a proposal that cannot apply, and its
  `--dry-run` runs exactly the real run's checks (`Repository.check_proposal`).
- `supersede`, `layer-set`, `cold-archive --apply` with no cold memories,
  `record-proposal` / `approve-proposal` with non-object or unsupported
  payloads, and `concepts import-seed` with a missing or malformed file exit
  with a usage error instead of a traceback.
- `record-proposal` refuses a proposal for an unknown memory before appending
  an event, and `rollback --dry-run` fails on an unknown event like the real
  run.
- `memfw eval causal` works from an installed wheel: its fixtures ship as
  package data under `memfw/eval_data/causal_eval/`.
### Conversation store and readers (fixes)

- Ingest no longer rolls back a session holding a lone UTF-16 surrogate:
  every text field is scrubbed to U+FFFD before it is written, so the session
  stores and the labels re-label pass completes.
- The same session under two roots (a Worker home mirroring it) is no longer
  lost when one copy shrinks: the new `conversation_turn_locators` table
  records which locators hold each turn, and a turn is dropped only when no
  locator holds it. Schema 2; an existing store is migrated on open (ownership
  backfilled, cursors cleared, so every session is re-read once).
- The schema 1 -> 2 migration runs in one `BEGIN IMMEDIATE` transaction and is
  decided by `schema_version`, so a crash mid-migration rolls back and the next
  open retries. Every open also reconciles ownership rows with turns (stores
  touched by older code): missing rows are backfilled, orphans removed.
- Upgrade note: the conversation worker re-processes every session once after
  upgrading (the session fingerprint includes `updated_at`, now UTC-normalised).
- Upgrade note: readers now skip a store whose data directory is a symlink that
  resolves outside the store root (previously followed); a warning is logged
  naming the path. Point the root at the real directory to read it.
- Goal extraction also strips `<ide_opened_file>`, `<ide_selection>` and
  `<user-prompt-submit-hook>` blocks; sort keys accept nanosecond and
  seconds-less `Z` timestamps.
- A session that re-reads empty although rows exist (emptied file, rewrite)
  is retried at most 3 times for the same fingerprint, then kept as stored
  (`sessions_stale`) instead of staying busy forever. A session file over the
  reader's size limit raises `SessionTooLarge` and keeps its rows
  (`sessions_too_large`).
- Timestamps with a UTC offset are stored as UTC `...Z`; the adapter's
  `since` filter and ordering compare in UTC, including rows stored before.
- Readers survive one malformed field: unhashable `type` values (DeepSeek,
  Kimi, Grok, StepFun), out-of-range numeric timestamps, a bad `file://` URI
  (Antigravity), non-list Gemini `toolCalls`/`$patch` values. JSONL reading
  strips a UTF-8 BOM and is linear in the line count. Symlinked store
  directories that resolve outside the root (Cursor IDE, GLM, OpenCode) are
  skipped. Antigravity offsets no longer collide (parser version 2).
- Non-UTF-8 session file names hash via `os.fsencode`.
- Handoff and share packs: recent turns come from the session's tail, the goal
  is the first user turn with content after harness-injected blocks are
  stripped, and a redacted cwd skips git with a note in `system.notes`.
- The conversation worker no longer treats a session without timestamps as
  idle on first sight; it waits `idle_minutes` after it last saw it change.
### Added

- `type: open-question` memories with an `open_question` block (`question`,
  `conflicts`, `missing`, `resolved_by`): what is unknown and which memories
  conflict on it. The shared write gate refuses `epistemic`, a causal grade,
  `causal_record` and `experience` on them, and refuses `conflicts` that are
  missing, in another tenant, or the question itself. Creating one, changing
  `type` to or from it, and resolving or reopening it need a human or an
  approved proposal. `Repository.resolve_open_question` sets `resolved_by` and
  `valid_to` in place and keeps the file. `fsck` reports `open_question_*`
  findings. `Repository.add` accepts `about` and `open_question`; agent
  `update` proposals accept `open_question` (and `valid_to` only with it). OKF
  export carries the type and block in the `memfw:` envelope. See
  `docs/epistemic-and-experience.md`.
- `memfw okf import <bundle> --root <vault> [--dry-run/--apply] [--json]`: an
  OKF Ingestor that path-checks and validates the bundle, then stages each
  concept as a candidate under `_candidates/remember/` (never the vault) with a
  deterministic `proposal_id` and `source: okf:<bundle-name>`. Re-import is
  idempotent; concepts already in the vault are reported as duplicates.
  The bundle is read once through `O_NOFOLLOW` directory descriptors;
  symlinks leaving it, non-regular files, non-UTF-8 Markdown, concepts over
  64 KiB and YAML anchors/aliases refuse the whole bundle. Staging is
  all-or-nothing. See `docs/okf.md`.

### Changed

- The candidate staging writer moved from `memfw.mcp.server` to
  `memfw.capture.staging` so `memory_remember` and `okf import` share it; the
  candidate file `memory_remember` writes is unchanged.
R6 PR-1: the backend-neutral recall projection, documented in
`docs/qmd-projection.md`. memfw contains no QMD code; it couples to a recall
backend only through exported files and the `resolve` interface.

### Recall projection

- `memfw projection export` writes one Markdown file per projectable memory,
  `manifest.json`, `delta.json` and `export-failures.jsonl` into a new
  `tree-<UTC stamp>-<sha8>` generation (staging directory + rename, files
  `0600`). With an unchanged content head, policy and lifecycle-source status
  it returns `no_change_fast_path` and writes nothing. The caller owns the
  `current` symlink and any delta queue.
- Projection policy file (v1 default; v2 adds `epistemic`/`causal`), validated
  strictly: unknown keys are rejected, and `tenant_map`, `join_key` and
  `label_line_version` accept only `identity`, `fleet_path` and `1`.
- `memfw resolve --ids-stdin` maps backend hits (ids, `memories/<id>.md` paths
  with any prefix, `<id>\t<title>` lines; at most 50 per call) to the
  authoritative memfw metadata and the vault body (`memfw-resolve-v1`).
- One shared `join_meta()` (`fleet_path` tag ↔ lifecycle-source `path`, with
  the `f`+12-hex id check), stricter-wins privacy classification, and the
  fail-closed `filter_resolved()` filter matrix for the two-stage consumer
  that comes next (PR-2a).
- `memfw index search --query-stdin` reads the query from stdin, so it stays
  out of argv and sudo logs.
- Fail-closed hardening: `sensitivity:*` tags other than `sensitivity:private`
  classify as `unknown`; resolve staleness uses parsed `updated_at` (an
  unparseable file → `stale`); unjoined resolve entries carry only id and join
  outcome; resolve stdin capped (1 MiB, 4 KiB lines); export cleans up its
  staging directory on failure and creates new `--out` parents `0700`.
- Opt-in `projection export --skip-privacy unknown,local-private` (policy
  `skip_privacy`); by default every privacy class is projected, which is safe
  only while the backend is reachable solely through memfw.
- `memfw.core.markdown_parse.parse_memory_markdown()`: the storage-free parser
  the projection module depends on (the storage mixin now delegates to it).
- No change to existing command output: `index search` default and
  `--dry-run` output are unchanged, and every new command is read-only on the
  vault.
### Compat framework: ports and recall coordinator

- New `memfw.ports`: the four port protocols (`RecallProvider`, `Projector`,
  `Ingestor`, `Deriver`), their frozen value types, reference staging under
  `_candidates/`, an adapter registry (`memfw.adapters` entry points plus
  `register()`; discovery imports nothing, an adapter is imported only once
  enabled in config, and a missing dependency or failing import reports it
  unavailable instead of raising), atomic content-addressed staging,
  and a contract-test kit, `memfw.ports.testing`. The package imports only
  the standard library, so it can ship as a standalone distribution.
- New `memfw.recall`: a coordinator that runs route → parallel recall
  (bounded workers for `available()`, `search()` and resolvers under one
  deadline, a per-provider in-flight limit so a hung adapter holds one
  thread, and a failing provider is marked degraded) →
  resolve → fail-closed policy filter (tenant, privacy clearance defaulting
  to `none`, `unknown` always dropped, validity, supersession, lifecycle,
  status, sources) → weighted
  RRF → labels → metrics. Metrics never contain the query.
  `memfw.recall.builtin` holds the vault resolver and the FTS, hybrid and
  multihop providers. It wraps the existing code without changing it.
- MCP `memory_search` gains the opt-in `mode=two_stage`: FTS (plus hybrid when
  an embedder is configured) through the coordinator, with a `policy` of
  `compat_fts` (default) or `strict` (adds privacy, status, lifecycle, archive
  and strict validity checks). Without an embedder, `compat_fts` returns the
  same rows as `mode=fts` (ids, order, freshness fields; the score is the fused
  one). Rows add `providers` and `recall_labels`; the payload adds `recall` and
  `recall_metrics` (no query or body text). Query and `as_of` rules and errors
  match `mode=fts`; 3 s per provider, 4 s in total; one in-flight registry per
  gateway, so a hung provider holds one thread. No ontology alias expansion in
  this mode yet. Not wired into HTTP or the CLI. See
  `docs/ports-and-recall.md` §4.1.
- **Behaviour change:** `memory_search` with `policy` other than `compat_fts`
  and a mode other than `two_stage` is now an error, as is a non-string
  `policy`. Without `policy` (or with `null` or `""`, both read as the
  default), every other mode and the default are byte-identical to before.
- `Coordinator(..., inflight=InflightRegistry())` shares in-flight counts
  between coordinators; `FtsProvider` and `HybridProvider` take an optional
  `observe(page_items, payloads)` hook for in-process callers.

### Changed — readonly read path moves into the library (A1)

Search results are unchanged: HTTP `/search` and `/related`, MCP
`memory_search` in every mode and the recall `FtsProvider` return the same
JSON as the previous commit (checked side by side, timing fields aside).
What changes is where the code lives, how the HTTP script finds it, and the
names `shared_readonly()` exposes.

- The read side of `scripts/memfw_readonly_service.py` (query and `as_of`
  rules, `ServiceError`, the FTS `search_vault` path, document helpers) is now
  the public module `memfw.index.readonly`; `read_path.py` moved to
  `memfw.index.read_path`. Library code no longer loads the script by file
  path. MCP `mode=two_stage` calls the public `check_query_text` and
  `normalise_as_of` instead of the script's private helpers.
- `scripts/memfw_readonly_service.py` keeps the HTTP layer, `/related`,
  `/stage` and the CLI, and re-exports every name it exported before. It loads
  the read path by file, first match wins: `readonly.py` beside the script,
  then the checkout's `src/memfw/index/readonly.py`, then the installed
  `memfw` package. Loading imports neither `memfw.index` nor jieba (`memfw` is
  still imported lazily for a unicode61 index or alias expansion). The service
  logs the chosen file at startup.
- **Deploy:** a standalone service directory needs three files from one
  revision: the script, `readonly.py` and `read_path.py` (see
  `docs/READONLY-SERVICE-UPGRADE.md`). With only the script and no installed
  `memfw` it exits with an `ImportError` naming the missing files instead of
  `No module named 'memfw'`.
- **Behaviour change:** `memfw.memory_hybrid.shared_readonly()` remains but
  returns `memfw.index.readonly`, a module whose helpers have public names
  (`bad_request`, `internal_error`, `unsupported_query`, `normalise_as_of`,
  `read_metadata`, `iso_string`, `optional_text`, `recorded_at_text`,
  `document_payload`, `has_table`, `fts_query_terms`, `quoted_or`,
  `fts_query`). The old private names (`_bad_request`, `_metadata`,
  `_optional`, `_recorded_at`, `_iso_string`, ...) stay as deprecated aliases
  for one release and will then be removed.
- The wheel no longer force-includes `memfw/read_path.py` (it is
  `memfw/index/read_path.py` now); it still ships
  `memfw/memfw_readonly_service.py`.
- `tests/test_no_scripts_import.py` fails if anything under `src/memfw`
  imports `scripts`, names a `scripts/` path or the service module, edits
  `sys.path` (aliases included), or calls `exec`, `site.addsitedir`,
  `SourceFileLoader` or `runpy.run_path`.

## [0.2.0] — 2026-11-15

The first release since the standalone core: notes, concepts, the transcript
index, the conversation store, handoff packs, dream, OKF export and the
upgrade fixes found by running this branch against a copy of a real vault.

### Storage and vault format

- `recorded_at` on memory entries, plus TTL-aware freshness; the stamp is
  written only when set, so files that predate it keep their bytes (#28).
- Verification state on entries: `status` (`unconfirmed` by default) and
  `evidence`, again written only when they differ from the default (#27).
- Unknown frontmatter keys from a newer writer are tolerated instead of
  rejecting the memory, and `list_warnings` names them; a rewrite by the
  current version drops them (#27).
- Default values of `type`, `tenant`, `related`, `valid_from`, `valid_to` and
  `superseded_by` no longer reach the file, so parse → render of a legacy
  memory is byte-identical. A file written on or after 2026-11-06 that already
  spelled them out keeps them: `parse` records which the source had and
  `render` writes them back, so a write does not change that file's content
  hash either (#33).

### Index, search and retrieval

- Per-term matching instead of one quoted phrase, for FTS5 and the
  `unicode61` fallback (#20).
- R0 evaluation harness, one shared read path, the MCP multi-hop pilot, and
  short-text expansion measured on the baseline with hybrid FTS restored (#18).
- Alias-expanded search: a concept alias reaches the memories that mention
  it, inside the same read-path gates (#24, #25).
- Retrieval traces keep full candidate hashes (#5); opt-in query-coverage
  abstention (#9); retrieval feedback and shadow tooling (#4).
- A stale index is detected and rebuilt before a search instead of failing
  with `no such table`; `--dry-run` reports `would_rebuild`. Detection reads
  the schema generation (`PRAGMA user_version`), so a later column change is
  rebuilt too (#33).
- `supersede` hands over at a single instant: the old record's `valid_to` is
  the replacement's `created_at`, so an `as_of` query never reports both
  generations as current (#33).

### Concepts and ontology

- Concepts, aliases and alias-expanded search (O1+O3) (#24).
- Scoped aliases resolve only inside their scope; alias and memory-reference
  projections are cached; unicode61 query terms are shared between the
  library and the HTTP service (#25).
- The Traditional-to-Simplified map used by alias matching comes from OpenCC
  (Apache-2.0, see `NOTICE`).

### Notes

- Section-level Markdown notes retrieval (#1).
- Notes MCP server with checkpointed indexing (#2), covered by tests and
  documented (#3).
- Notes index, code retrieval and the relatedness projection are documented
  in `docs/COMPONENTS.md`.

### History and the conversation store

- Read-only transcript layer: `memfw history list|read|search`, with a
  secret scan, safe opens and hardening against hostile nesting and huge
  files (#26).
- Conversation store with 14 app readers, per-session lineage and forks
  (#28), desktop-agent readers for M2a-2 (#29), and reader fixes for
  automated/subagent labels, model/project fill, cursor timestamps and
  forks (#31).
- Sharing rules and the worker are documented in
  `docs/conversations-share.md`.

### Handoff

- `memfw.handoff/v1` packs: build a redacted work record for the next agent,
  with size and privacy rules, exposed as a CLI command and an MCP tool
  (#30).

### Dream

- `memfw dream run`: verify-first, proposal-only consolidation, with a
  configurable judge that `vault.yaml` cannot override (#27); write-free dry
  runs and openat-style staging (#27).
- Curate and distill, including merge/supersede decisions that respect
  subjects and negation, and MCP exposure (#30); skill candidates and their
  quality filtering (#30).
- Synthetic dream evaluation cases (#21) and the evaluation data behind
  `docs/dream.md` (#22).

### OKF

- OKF v0.2 export and validation, documented in `docs/okf.md`.

### MCP and the read-only HTTP service

- The `memfw-http/2` contract v2: tenant and temporal filters in SQL before
  ranking, history/superseded inclusion, related edges, and staging that
  never touches the vault.
- `valid_until` / `superseded` accepted as input aliases (#16).
- Tenant-scoped related results, safe outbound HTTP, symlink-safe notes and a
  rerank allow-list from the memory test findings (#15).
- The read token is bound to its tenant; the judge and the stage ledger stay
  off the read path (#18).

### CLI and budget

- `memfw --version` prints the installed version, so a release smoke test can
  check what is on `PATH` without importing the package.
- `memfw budget configure|show`, `stats`, `forget run --dry-run` and
  `log verify` work on a copied vault end to end (verified in `0.2.0`).
- `index rebuild --dry-run` reports the count it would index.
- The budget keeps its `10MB` profile semantics: body-only accounting, with
  physical usage reported separately.

### Compatibility and upgrade

- Running `0.2.0` against a vault written by the deployed older release:
  parse → render was byte-identical for **every** file after #33 (before it,
  every file gained six default lines on rewrite), in both directions — files
  written after 2026-11-06 keep the default lines they already carry.
- A read-only sync script's full command surface (`import`, `index rebuild`,
  `budget configure --apply`, `budget show`, `stats`, `forget run --dry-run`
  for decay and consolidate, `log verify`) runs on a copied vault with
  `0.2.0`; `budget configure --apply` produces the same `vault.yaml` bytes.
- A full import of the whole vault succeeded with zero failures, and a second
  incremental run skipped every file (no rewrite churn).
- **After upgrading, run `memfw index rebuild` once** (or let the first search
  do it): the index is a disposable projection and is not migrated in place.

### Evaluation and documentation

- Evaluation evidence for the external-judge work: cross-LLM judging,
  MiniMax as external judge with the StepFun blocker, judge consensus and
  StepFun diagnostics (`docs/evaluation.md`, #10–#13).
- Golden-query annotation tooling: voice-assisted annotation, Typeless voice
  aliases, batch packets and import (#6–#8).
- README rewritten for newcomers (#23) and split into
  `docs/COMPONENTS.md`; per-term search and dream evaluation data (#22);
  retrieval read path and multi-hop pilot (#19); hardening summary (#17); the
  Conductor v1 federation note (#14).
- Personal paths and maintainer host names removed from the public docs
  (#23, and `6656381`).

## [0.1.1] — 2026-11-04

Fixes after the first standalone core: the portable reproducibility test now
passes in CI, and filesystem-allocation metrics are excluded from the
experiment output. Tagged as `0.1.1`; `pyproject.toml` and `__version__`
still said `0.1.0`, so this was never published as a numbered package.

## [0.1.0] — 2026-11-04

First standalone `memfw` core: the Markdown vault and hash-linked event log,
retention, the disposable FTS5 index, the CLI and the local MCP server, with
the repository extracted from the earlier Conductor tree.

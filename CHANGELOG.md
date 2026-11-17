# Changelog

All notable changes to memfw. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). Entries name the pull request that
carried the change; `#N` links to `LeoLin990405/memfw` pull request `N`.

The vault format did not change between `0.1.1` and `0.2.0`: `vault.yaml`
stays at `schema_version: 1`, and files written by an earlier version keep
their bytes (see [Compatibility and upgrade](#compatibility-and-upgrade)).

## [Unreleased]

### Added — privacy levels and clearance filtering on the exits (MM0)

- `MemoryDocument.privacy`: `public`, `shared` or `local-private` (read
  case-insensitively). Absent in older files: they parse as `None`, render
  without the key and dump without it, so their bytes and snapshot hashes do
  not change. `Repository.add(privacy=...)`, `update(privacy=...)` and
  `memfw add --privacy` store the lowercase value and refuse anything else;
  only a human (or an approved proposal) may loosen or clear it.
- New `memfw.ports.privacy` (stdlib): the level a reader acts on is the
  strictest of the field (missing → the vault's `legacy_privacy_default`,
  `local-private` when unset), `privacy:*` tags and `sensitivity:*` tags;
  unrecognised values are `unknown` and never admitted. A clearance is a set
  of levels; a malformed one narrows to `public`. `memfw/index/read_path.py`
  carries a stdlib copy for the HTTP service (parity test-enforced).
- New `vault.yaml` key `legacy_privacy_default` (validated) and
  `memfw privacy legacy-default [LEVEL]` to show or set it (one
  `config_change` event).
- Opt-in clearance on every exit; **unset means no filtering, exactly as
  before**: HTTP read-only service `--clearance` / `MEMFW_READONLY_CLEARANCE`
  (`/search`, `/related`; a withheld source memory answers 404, note
  neighbours are dropped), `memfw okf export --clearance` (withheld memories
  are left out; the manifest gains `privacy: {clearance, withheld_count}` only
  then; a concept carries `memfw.privacy` only when the memory sets it), the
  notes MCP server `--clearance` / `MEMFW_NOTES_CLEARANCE` (plus
  `--legacy-privacy-default`; search, get, related and traces, read from the
  note file), and the memfw MCP server `MEMFW_MCP_CLEARANCE` (search in every
  mode, get, related, context, status; a withheld memory raises the same
  not-found error as a missing one). HTTP `/search` rows list only `related`
  ids the clearance admits; a memory file whose frontmatter does not parse to
  a mapping (CRLF, YAML error, none at all) is `unknown`. Leak tests:
  `tests/test_mm0_privacy_clearance.py`.
- Writes keep the level: `supersede` inherits `privacy` (a human may
  override it), `rollback` restores an unset field, and without a human
  decision an update may not loosen the effective level (field and
  `privacy:`/`sensitivity:` tags together). The recall strict policy reads the
  field too (strictest of field and tags).
- New `vault.yaml` key `new_privacy_default` and
  `memfw privacy new-default [LEVEL]`: when set, new memories that set no
  level get it; unset, nothing is stamped (as before).
- Privacy travels through staged candidates: `memory_remember` (new optional
  `privacy` argument), HTTP `/stage` (optional `privacy`), `candidate-apply`,
  and `memfw okf import`, which honours `memfw.privacy` (an unrecognised
  value becomes `local-private`). Export → import keeps `local-private`.
- notes MCP under a clearance: abstention is judged on visible notes and
  `notes_status` counts only visible notes.
- Dream open questions (U3): a memory whose `privacy` field is not
  `public`/`shared` counts as private and is never named; when either memory
  sets the field, the candidate carries the stricter level and
  `accept-question` writes it. Conversation-derived dream claims propose
  `privacy: local-private`, and confirmed session captures are written
  `local-private`. A non-human, unapproved write may not clear an explicit
  `privacy` field.
- Under `MEMFW_MCP_CLEARANCE`, recall labels (`labels: true` in every mode and
  `two_stage`) count only open questions the clearance admits: a withheld
  question gives neither `open_question:<id>` nor `conflicted`. The U5
  review queue (`memfw corroboration queue`) reads the field through the
  strict policy, and under a clearance lists only admitted questions and
  conflict ids. Corroboration is computed on rows that passed the clearance.
- The O1a Obsidian projection classifies with the same rule: memories without
  a privacy field follow `legacy_privacy_default` (unset → `local-private`,
  so they are not exported under the default `public,shared` until the vault
  sets it); a `privacy:public` tag alone no longer makes a memory public.
- `docs/READONLY-SERVICE-UPGRADE.md`: deploy the three service files together
  and pair `MEMFW_READONLY_CLEARANCE` with the vault's privacy defaults.

### Fixed — wave 3 wrap-up guards

- New test `tests/test_no_conflict_markers.py`: every git-tracked text file
  (walked tree without git) must be free of merge-conflict markers (lines
  starting `<<<<<<< `, `||||||| `, `>>>>>>> `, or exactly `=======`; Markdown
  setext headings and binary files excepted).
- Usage projection (`SqliteUsageSink.record`): an index on `at_epoch` (added to
  existing files with `CREATE INDEX IF NOT EXISTS`), and the count retention
  deletes below the `max_rows`-th newest rowid instead of a sorted
  `NOT IN (... LIMIT 100000)`; the same rows are kept, and a write at the
  100000-row cap drops from ~30 ms to ~2 ms. An existing usage directory the
  user owns is tightened to 0700, not only a new one. Docstring size note
  corrected (about 7.4 MB at the cap, plus the index).
- Settled history ingest: a forced re-read marker is cleared only after the
  cursor is saved, so a locator that reads zero rows (busy) keeps its owed
  re-read; a file whose mtime is in the future and that has no usable
  timestamps falls back to its ctime instead of staying unsettled until the
  clock catches up.
- `DreamOptions`: the `allow_config_judge` docstring is back under its field.

### Added — settled ingest and archive state (M3e-a)

- `memfw history ingest --settled-minutes N` skips transcripts whose last
  activity (the later of newest turn timestamp and file mtime; timestamp first
  for sessions in a shared database; future stamps ignored) is within `N`
  minutes, and
  archives the settled sessions: new store table `conversation_archive`
  (`archived_at`, `archived_fingerprint`) and cursor column
  `settled_fingerprint`, both added on open without a cursor reset. A session
  that becomes active again, or gets new turns, is no longer archived until the
  next settled ingest. `memfw conversations worker --settled-minutes N` runs its
  ingest the same way; the archive fingerprint is the worker's job key, so a
  session is processed once per final shape.
- `memfw conversations list/search/get` accept `--with-archive-state`
  (`archived: true/false`) and `--archived-only`. Without the new flags every
  command's output is byte-identical (`tests/harness/settled_probe.py`).

### Added — read-only Obsidian projection (O1a, #64)

- New `memfw project obsidian --out <dir> [--root <vault>] [--clearance
  public,shared] [--tenant <t>]` writes an Obsidian board: `memories/<id>.md`,
  `questions/<id>.md` (open questions), `concepts/<name>.md` (from `about`),
  wikilinks for `about`, supersede (both directions), open-question
  `conflicts`/`resolved_by`, K1 `broader`/`narrower` between concept pages,
  Bases views in `memfw-views/*.base`, and
  `.memfw-obsidian/memfw-projection.json` (sha256 per file). Its marker
  (`generated: memfw-projection`), manifest, temp folder and page folders are
  disjoint from any other generator writing into the same folder. See
  [docs/obsidian-projection.md](docs/obsidian-projection.md).
- `memfw.projections.ObsidianProjector` (new unit `memfw.projections`, layer
  `L1-projection`) implements the L0 `Projector` port and passes
  `check_projector`. Default clearance `public,shared`; `unknown` privacy is
  never exported; hidden memories leave no page, link, backlink, count or
  concept page. One tenant per run (default `""`).
- Deterministic: no run timestamps in any file, stable names and order, so the
  output can be committed to git and diffs show only memory changes; unchanged
  files are not rewritten. A rebuild deletes or overwrites only files listed in
  the previous manifest, reports hand-edited ones, and refuses an unlisted file
  at a generated path. Temp files live in `.memfw-obsidian/tmp-projection/`
  and stale ones are removed at start. The vault is only read; the output may
  not be inside, equal to or contain the vault, be a symlink, or be a
  protected path (`/`, `$HOME`, `~/Documents`, `~/Library/Mobile Documents`,
  any `knowledge-hub` path, `MEMFW_OBSIDIAN_PROTECTED`). A question answered
  by a hidden memory is shown as open.
- Off by default: nothing runs unless the command is invoked; no other
  command's output changes.

### Added — corroboration display and the open-question review order (U5, part 1)

- MCP `memory_search` with `labels: true` adds `corroboration`
  (`{count, apps, sessions, first_seen, last_seen}`) to every row in every mode,
  `two_stage` included, trimmed to the server app's `can_read` (computed in
  `memfw.runtime.corroboration`). It is `{"degraded": CODE}` when it cannot be shown
  (no caller app, invalid share policy, missing or unreadable projection). Without
  `labels` the output is byte-identical.
- `memfw corroboration queue` lists unresolved open questions in review order: questions
  whose `conflicts` include a memory corroborated by at least 2 visible apps come first
  (rule in `memfw.recall.priority`, shipped in the `memfw-recall` wheel). Read-only and
  fail-closed: `--policy strict` (default) filters both the questions and the conflicts
  that add support (no private, rejected or archived memory is listed or counted).
- Deferred: promotion candidates and ranking weights (need evaluation and approval).

### Added — conflicts become open-question candidates (U3)

- `memfw dream run`: every pair curate proposes a `supersede` for (two current
  memories about one concept that disagree) also gets an `open-question`
  candidate (operation `curate`). `changes.new_memory` is a `type:
  open-question` memory naming both ids in `conflicts`, `about` the concept,
  with `missing` saying what evidence would settle it. No winner is picked and
  neither memory changes; the candidate passes the open-question gate before it
  is staged. Never a vault write.
- Dedupe per unordered pair: none while an unresolved open question lists both
  ids, while a resolved one does and neither memory changed since it closed,
  while a recorded rejection matches both memories' versions, or while a staged
  candidate for the pair was not accepted. Same tenant only; a pair with a
  private memory (`sensitivity:*`, a `privacy:` tag other than `none`, or the
  lifecycle meta-index's privacy; fail closed) is skipped. New
  `--lifecycle-source` on `dream run` and `dream accept-question`.
- New `memfw dream accept-question DREAM_ID MEM_A MEM_B [--apply]`: the human
  writes the question through the U1 gate, rebuilt from the two memories (an
  edited or outdated staged candidate is refused; the dry run prints the full
  memory), under the vault lock, with `evidence: [dream:DREAM_ID]`.
- New `memfw dream reject-question DREAM_ID MEM_A MEM_B [--apply]`: removes
  only that candidate and records a rejection under
  `_candidates/dream-rejections/`.
- Default output: `report.open_questions` and the candidate appear only when a
  conflict was found; a dream without conflicts is byte-identical to before
  (harness probe `tests.harness.probes:dream_run`).

### Added — `memory_context` cache and `if_version` (C3)

- `memory_context(v=2)` takes an optional `if_version` (also
  `memfw://context/<slot>?if_version=`): when the freshly built pack has that
  `pack_version` the reply is the pack header with `unchanged: true` and no
  slots; otherwise the full pack with `unchanged: false`. Without it the output
  is byte-identical to before.
- New `memfw.runtime.context_cache.ContextCache` (bounded LRU, per process,
  thread-safe; on by default, `build_runtime(context_cache=False)` disables
  it, an instance can be injected). It caches only the ids and versions of the
  `core` slot's pool per `(tenant, policy, slot, scope)`, valid while a stat
  fingerprint of the memory files, `events.jsonl` and `vault.yaml` is
  unchanged; on a hit the memories are re-read from the vault and filtered
  again, so the pack and `pack_version` equal a fresh build. `handoff` and
  `topics` are not cached. `memfw.context` gains the `CoreCandidates` seam
  (`CoreSlotProvider(candidates=...)`), `is_core_shaped`, `select_core` and
  `if_version_request`.
### Added — DeepSeek tagging adapter and conv-v2 tags (M3a)

- New L0 port `memfw.ports.models`: `ModelBackend`, `ModelReply` (moved from
  `memfw.conversations.backends`, which re-exports them unchanged),
  `BackendUnavailable`, `BackendBudgetExhausted` and the `conv-v2` tag schema
  (summary plus `project`, `task_type`, `components`, `intent`, `outcome`).
  Contract check `memfw.ports.testing.check_model_backend`.
  It ships in the same ports API release as `UsageSink` (`PORTS_API_VERSION`
  stays `0.2.0`); `ModelBackend` is registered in `PORT_VERSIONS`.
- New workspace package `packages/adapters/memfw-adapter-deepseek`
  (`memfw_adapter_deepseek:factory`): a standard-library `conv-v2` backend over
  DeepSeek's chat completions API with JSON mode, retries with backoff on
  429/5xx (an outage stops the run without failing jobs), and a per-run token
  and cost budget (price defaults are placeholders). The key comes only from
  `DEEPSEEK_API_KEY` and is sent only to `https://api.deepseek.com`. Built and
  smoke-tested as a standalone wheel in CI.
- The conversation worker may take its backend from `vault.yaml`
  (`conversations: {backend, cloud, options}`), gated on `cloud: true` and the
  adapter being available; otherwise the run is `degraded` (no fallback). A
  non-local `--backend` (and `memfw handoff --summarize`) also needs
  `conversations.cloud: true`; `--allow-cloud` can only narrow it. A new prompt
  version re-plans only new, changed or last-30-days sessions, and at most the
  newest 1000 already-processed ones (wider only with
  `conversations.backfill_days` / `--backfill-days` or
  `conversations.replan_limit`; held-back sessions are reported), newest first.
  `memfw conversations status` shows the configured backend's state.
  `memory_claims` stays extractive. Worker state gains `summaries.tags`,
  `jobs.prompt_version` and `jobs.session_ts` (additive migration; existing
  rows untouched). With no
  `conversations` backend configured the worker's output is unchanged.
  See `docs/conversations-share.md`.

### Added — concept taxonomy and relation-type table (K1, K2)

- Concept files may carry an optional `broader: [<oid>, ...]` (parents).
  `narrower` is derived, never written (`memfw.ontology.narrower_index`;
  `memfw concepts show` adds `broader`/`narrower` only for a concept that has
  them). New lint kinds `broader_invalid`, `broader_dangling`,
  `broader_merged` and `broader_cycle` fire only on concepts with `broader`;
  `broader` is no longer an `unknown_relation`. `import-seed` copies a seed's
  `broader`. Nothing follows `broader` on the read path (no query expansion).
- New versioned relation-type table `src/memfw/ontology/relations.yaml`
  (version 1, `memfw.ontology.builtin_relation_table`, `memfw concepts
  relations`): only relations that already live in vault fields, each with its
  allowed node kinds, cycles, `epistemic` ceiling and privacy/tenant filtering
  (required; the loader fails closed). New opt-in, read-only
  `memfw concepts lint-relations` reports `relation_unknown_type`,
  `relation_dangling` and `relation_cross_tenant`.
- Search, context, topics, OKF export, and lint on a vault without `broader`
  are unchanged (byte-identical probe against `MEMFW_ARCH_BASE_REF`). memfw
  0.2.0 keeps `broader` but lints it as `unknown_relation`; see
  [docs/ontology.md](docs/ontology.md#compatibility).

### Added — `memory_used` usage signal (C5) and no vault writes mid-session (C6)

- New MCP tool `memory_used(ids, session_ref)`, off by default: listed and callable only
  with `vault.yaml` `runtime: {usage: {enabled: true}}` or `MEMFW_USAGE_SIGNAL=1`. Off,
  `tools/list` is exactly the previous list. It records one row per visible id (id,
  session_ref, calling app, time; no query text) in a disposable store outside the vault,
  `~/.local/share/memfw/usage/<vault key>.sqlite3` (0600; so it never counts toward the
  physical-size budget), pruned on every write to 90 days and 100000 rows. It never writes
  the vault or its event chain. Unknown, other-tenant, disallowed-source and malformed ids
  are dropped; the answer is always `{"ok": true}` (its content does not say which ids were
  kept; timing can differ).
- New port `memfw.ports.UsageSink` / `UsageSignal` (`memfw.ports.usage`) with
  `check_usage_sink` in the contract kit; implementations `memfw.usage.SqliteUsageSink`,
  `NullUsageSink` and `MemoryUsageSink` (`memfw.ports.reference`). Wiring in
  `memfw.runtime` (`Runtime.memory_used`, `build_runtime(usage_signal=, usage_sink=)`).
  `PORTS_API_VERSION` and both wheels move to `0.2.0`. New `memfw.ports.PORT_VERSIONS`
  (every port's contract version) and a package ↔ `PORT_VERSION` compatibility table in
  docs/ports-and-recall.md section 1.1, checked against the package metadata by a test.
- New session-safe switch `runtime: {search: {record_access: false}}` /
  `MEMFW_RECORD_ACCESS=0`: `memory_search` `hybrid`, `vector` and `two_stage` then append
  no `memory_access` event and warm no cold or stub hit. Default unchanged (access
  recorded). A missing vault or unreadable `vault.yaml` fails closed on both switches and
  is not cached. Documented the "no vault writes mid-session" constraint and its one default
  exception (docs/ports-and-recall.md section 4.5);
  `tests/test_session_no_vault_writes.py` proves `memory_context`, every `memory_search`
  mode and `memory_used` leave the vault byte-identical under the session-safe setting.

### Added — standalone `memfw-ports` and `memfw-recall` wheels (A5b)

- New uv workspace members `packages/memfw-ports` and `packages/memfw-recall`
  build wheels importable as `memfw_ports` and `memfw_recall` from memfw's own
  `src/memfw/ports` and `src/memfw/recall` (hatch `force-include`, no copied
  sources; `recall/builtin.py` is not shipped). An adapter author can install
  them and run `memfw_ports.testing` without installing memfw.
- `memfw.recall` now takes its port names through `memfw.recall._ports`: a
  relative import of the sibling `ports` package when recall has a parent
  package (inside memfw: `memfw.ports`, the same objects; no behaviour change),
  `memfw_ports` when it is the top-level `memfw_recall`. Inside a memfw process
  use `memfw.ports`; `memfw_ports` classes are distinct copies.
- New `memfw.ports.PORTS_API_VERSION` (`"0.1.0"`); both wheel versions follow
  it (test-enforced). The members pin `hatchling==1.33.0` and ship memfw's
  `LICENSE` and `NOTICE`. CI pins `actions/*` and `setup-uv` to commit SHAs,
  pins uv, and runs with `permissions: contents: read`.
- `tests/standalone/wheel_smoke.py` runs every reference implementation through
  its contract check and a coordinator search in a venv without memfw; new CI
  job `standalone-wheels` builds both wheels and runs it.
  `tests/test_standalone_ports_wheel.py` checks the wheel file lists and the
  seam, and with `MEMFW_STANDALONE_WHEELS=1` (and `uv`) does the build, clean
  install and smoke itself.
- memfw does not depend on the new distributions; `uv.lock` gains the two
  workspace members only.

### Fixed — `two_stage` access events follow the filter

- `memory_search` `mode=two_stage` with an embedder logged one `memory_access`
  event per hybrid-provider hit before the coordinator's `FilterPolicy` ran, so
  hits the policy later dropped (privacy under `strict`, another tenant) were
  still access-logged and fed strength/tier signals. Recall providers are now
  read-only: `HybridProvider` builds `MemoryHybridRetriever` with the new
  `record_access=False` (also covering its internal FTS fallback and
  `VaultRetriever`'s new `record_access` knob), and `Runtime.recall` appends
  one `memory_access` event (`operation: two_stage_retrieve`) per hit it
  returns, after filtering.
- Warming followed the same pattern: the hybrid loop warmed a cold or stub hit
  (`tier_change`, and for a stub `memory_restore` plus the un-archived body)
  before its tenant, source and visibility checks, and `VaultRetriever` warmed
  every id before any check, so a `two_stage` search could warm or un-archive
  another tenant's or a private memory it never returned. Both retrievers take
  a new `autowarm` knob (default on); `HybridProvider` passes false, and
  `Runtime.recall` warms only the hits it returns (then logs them). The hybrid
  loop now warms only after the checks, and the retriever's legacy FTS fallback
  passes `VaultRetriever` an `admit` check, so it warms and logs only what it
  can return.
- Security change on the direct paths: `mode=hybrid`/`vector` (and the legacy
  FTS fallback) no longer warm a cold or stub memory of another tenant, or one
  the source/visibility checks drop. For the hits they return, events
  (`memory_access`, `tier_change`, `memory_restore`) are identical to before.
- Warming and access logging after the filter are best effort: a failure (for
  example a read-only `events.jsonl`) keeps the hits, sets `degraded` and adds
  the error code `access_not_recorded`; before this, such a log made
  `two_stage` raise.
- `two_stage` now logs access for its returned hits whether or not an
  embedder is configured (before, only the hybrid provider's hits were logged,
  unfiltered). `mode=fts` still logs no access; aligning it is a follow-up.
- A cold or stub hit that `two_stage` warms is marked `stale` on that call (its
  row comes from the pre-warm index row) and fresh from the next call on, as
  direct `mode=hybrid` already marks a warmed hit.
- Swap drills: the hybrid recall provider no longer skips the kit's
  byte-identical vault check; only its disposable index files are excluded, so
  `events.jsonl` and `events.state.json` are covered.

### Internal — MCP gets everything from the Runtime (A3b, A4b)

- `memfw.mcp` now imports only `memfw.ports`, `memfw.runtime` and its own
  modules: all 12 `memfw.mcp` allowlist entries are gone (`ALLOWLIST_SIZE`
  13 → 1; the root `RelatedIndex` re-export, A5, is the one left).
- Settings: `memfw.runtime.resolve_settings` (explicit values, else
  `MEMFW_APP`, `MEMFW_TENANT`, `MEMFW_ENTITY_ENABLED`,
  `MEMFW_ALLOWED_SOURCES`, and the `HttpEmbeddingProvider` from
  `MEMFW_EMBEDDING_*`) and `history_refresh_seconds` replace the parsing in
  `Gateway.__init__`; the values are the same.
- `Runtime.open_repository()` is the vault opener (refuses a missing vault,
  opens per call from the unresolved root, never keeps one); `Gateway._repo`
  and `Runtime.ensure_index` use it.
- `Runtime.search` checks the `memory_search` arguments (blank query,
  `policy`, 512-character `scope`, allowed `source`; same messages, before the
  vault is opened), then runs every mode (fts, hybrid/vector, multihop,
  two_stage, alias expansion, U2 labels) and builds the rows; that code moved
  from `memfw/mcp/tools_search.py` to `memfw/runtime/search.py` (the MCP
  server passes its `_client_message` for error texts). The MCP tool only
  parses the arguments.
- `Gateway.tenant`, `allowed_sources`, `embedder` and `entity_enabled` are
  read-only properties returning the Runtime's values.
- `Runtime.remember`, `related`, `status`, `conversation_share`,
  `history_scoped`, `history_ref_source`, `dream_run` and `staged_dreams`
  (modules `memfw/runtime/{memories,sharing,dreams}.py`) back the other tool
  groups; the capability modules load on first use, so `import memfw.runtime`
  stays light.
- Byte-identical: `mcp_all_tools` (11 variants) and `mcp_memory_search_stable`
  (73 payloads, every mode with and without an embedder, error paths included)
  match `origin/main`; the second is a new opt-in test next to the first
  (`MEMFW_ARCH_BASE_REF`).
- Tests that patch `search_multihop` now patch `memfw.runtime.search`.
  `RawHashCache`, `RAW_HASH_CACHE`, `FRESHNESS_READ_ATTEMPTS` and
  `frontmatter_version` are public in `memfw.runtime.search`; the underscore
  names in `memfw.mcp.tools_search` and `memfw.mcp.server` are deprecated
  aliases.

### Internal — swap drills: every port has two implementations (A5a)

- New `memfw.ports.reference` (stdlib only, imports only `memfw.ports`): `InMemoryRecallProvider`
  (BM25-lite), `JsonlProjector` and `MirrorProjector`, `JsonIngestor` and `MarkdownIngestor`,
  `ConcatDeriver` and `TimelineDeriver`, `KeywordClaimVerifier`, `FileClaimStager`. Not imported
  by `memfw.ports`; `register_reference()` adds the adapter specs on request, each off until
  enabled. No entry points, no change to any default output.
- The reference projectors and derivers apply core's privacy classes, never looser
  (cross-checked against `meta_from_document`): `local-private` only with
  `include_private`, `unknown` (`sensitivity:secret`, `privacy:restricted`, ...) never. Their
  front matter reader fails closed: a flow, indented or block form, or a duplicate key, under
  `tags`/`privacy`/`sensitivity` makes the file unreadable (skipped), as does a list item there
  that is empty, starts with a YAML indicator, contains `": "` or ends with `:`, a tab, a
  line break other than `\n`, or front matter that core would cut differently (it is cut
  exactly as `memfw.core.markdown_parse` does). A 46-variant evasion corpus checks it is
  never looser than core and skips every file core rejects. `MirrorProjector` removes
  stale copies; writes go through a temporary file and never follow a symlink.
- `tests/test_swap_drills.py` runs every implementation of every port (`RecallProvider`,
  `Projector`, `Ingestor`, `Deriver`, `Clock`, `IdGen`, `ClaimVerifier`, `ClaimStager`) through
  the same `memfw.ports.testing` check, and runs the coordinator with the reference recall
  provider in place of FTS (filter policy, fusion, labels and tenant isolation hold).
- `memfw.ports.staging` exposes `check_ingest_args` and `create_exclusive`, which
  `stage_candidates` now uses (same behaviour).
- The ports isolation test walks `memfw/ports` recursively and covers the new package.

### Added — context slots and MCP resources (C2)

- `memory_context(v=2, slots=[...])`: slots `core` (as before), `handoff` and `topics`,
  returned in the requested order. Default `["core"]`: the v1 and v2 outputs are
  byte-identical to before. An unknown or repeated slot, an empty or non-list `slots`, or
  `slots` with `v=1` is an error.
- `handoff`: the latest shared sessions the calling app may read (`conversation_share`
  applies, through `ConversationShare.list_sessions`/`handoff`): one item per session with
  its id, the redacted goal (at most 280 characters), the turn count and the latest turn
  refs. No app, the operator name, sharing off or `can_read: false` give an empty slot;
  hidden apps never appear. `topics`: active concepts with the number of memories the
  policy keeps that are `about`/`applies_to` them (merged concepts count for their
  target); labels and counts only, no body text.
- Slots are built in parallel, each with its own deadline (`core` 10 s, `handoff` and
  `topics` 3 s). A slot that raises, times out, is still running from an earlier call or
  returns another slot is returned empty with `degraded` (`error`, `timeout`, `busy`,
  `invalid`, `unavailable`; vault errors: `pending_recovery`, `integrity`, `not_found`,
  `vault_error`); the others are not affected. `degraded` is in `as_dict()` and
  `pack_version` only when set. Degrades are logged with the exception class only. The
  default request (`["core"]`) runs inline and keeps C1's errors byte for byte (a pending
  transaction is still the tool error, not a degraded slot).
- MCP resources `memfw://context/<slot>` (`resources/list`, `resources/read`): the JSON
  body is that slot's v=2 pack, `pack_version` included, for the same caller (app, tenant,
  sources). `?policy=compat_fts|strict` selects the policy (any other query fails closed);
  without it `topics` uses `strict`, `core` and `handoff` `compat_fts`. `initialize` now advertises `resources`. `tools/list`
  changes only by the optional `slots` property of `memory_context`; the all-tools probe
  comparison applies exactly these two changes to the base output and must then match.
- Ports: `ContextSlotProvider` (name, `build(SlotRequest) -> ContextSlot`), `SlotRequest`,
  `ContextItem` (non-memory entries) and `check_context_slot_provider` in
  `memfw.ports.testing`. `memfw.context` composes the slots and imports only core and
  ports; the Runtime adapts the conversation share and the concept catalogue
  (`memfw.runtime.context_sources`). No new allowlist entry.
- Swap drills: `ContextSlotProvider` joins the port matrix (`core`, `topics` with a fixed
  catalogue, `handoff` with a fixed session feed, all through `check_context_slot_provider`).

### Internal — MCP server split by tool group (A4a)

- `memfw/mcp/server.py` (about 1900 lines) is split into `base`, `schema`,
  `protocol` and one `tools_*` module per tool group (search, memory, context,
  history, conversations, dream). `Gateway` in `server.py` composes them; its
  methods and attributes are unchanged. A pure move: every MCP tool's output,
  `tools/list` included, is byte-identical to before (timing fields masked;
  new harness probe `mcp_all_tools`: every MCP method and all 15 tools).
- Allowlist: the `memfw.mcp` entries name the group modules that now hold each
  import; keys and site totals are unchanged. The merge-base check now counts debt per
  edge per unit: an edge's sites may move between modules of its source unit
  if its total does not grow; a new key, a larger total or an importing module
  outside the source unit still fails.
- Module-level names stay importable from `memfw.mcp.server`; tests that
  monkeypatch `DREAM_*` or `search_multihop` now patch `memfw.mcp.tools_dream`
  and `memfw.mcp.tools_search`, where the code reads them.

### Added — corroboration projection (U4)

- `memfw corroboration rebuild` writes a rebuildable `corroboration.sqlite3` at the vault
  root: per memory, the independent (app, session) pairs that mention it, from its own
  `conv:` evidence, staged conversation-worker claims (default tenant only, matched on a
  unique key of at least 4 tokens and 20 characters) and accepted experience points. Ids,
  names, counts and times only; the file is `0600`; rebuilds are byte-identical; the
  vault is only read.
- Independence rules: one count per app and session; carriers that cite the memory id,
  dream candidates naming it, mentions after a session-scoped injection event, generated
  provenance, and apps hidden or opted out under `conversation_share` do not count.
- `memfw corroboration show ID --as-app APP` (and `memfw.corroboration.corroboration_for`)
  trim the answer to the apps `APP` may read; hidden apps never appear. Default off:
  nothing else reads the projection and no existing output changes. Not on MCP yet.
  See `docs/epistemic-and-experience.md`.

### Added — recall shows "unknown" (U2)

- MCP `memory_search` marks hits that an unresolved open question is about:
  `open_question:<id>` when a visible open question shares an `about` concept
  with the hit, and `conflicted` when one lists the hit in its `conflicts`.
  Only questions with no `resolved_by`, no `valid_to`, not superseded and kept
  by the call's filter policy count, so a question in another tenant or one the
  policy drops (for example a private one under `policy=strict`) never appears
  in a label.
- `mode=two_stage` appends them to `recall_labels`. The other modes take a new
  `labels` argument (default `false`); with `labels: true` every row has
  `recall_labels`. Default output of every mode is unchanged, and `two_stage`
  is unchanged when no visible open question exists.
- The `memory_search` description tells agents that a hit of type
  `open-question`, or one labelled `open_question:*` or `conflicted`, means the
  answer is unknown: say so rather than guess.
- A question with `valid_to` set is never shown, even for an `as_of` before
  that instant (conservative). `scope` changes no label: as in `mode=fts` it
  only selects scoped alias labels, and labels match `about` concept ids. The
  labels are judged at the same instant as the coordinator's filter.
- Cost: candidate questions come from the search index (`memory_access`, type
  `open-question`, the caller's tenant), and only those files are read and
  judged from the vault document; a scan of `memories/` is the fallback when
  the index cannot be read. A page with no rows reads nothing.
- Deferred: attaching an anchor's open questions to the `memory_context` pack
  (iteration C, default off) is not part of this change.
- `memfw.recall.open_question_labels` / `OpenQuestionIndex` (pure),
  `memfw.recall.builtin.open_question_index` (the per-call vault builder) and
  `Runtime.open_question_labels`.

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

### Internal — library-to-CLI edges and `okf -> capture` removed (A2b)

CLI output is unchanged: `memfw okf import` (dry run, `--apply`, re-import,
rollback when a publish fails, a refused bundle, a missing vault),
`memfw capture simulate` (seeded and unseeded), `memfw capture confirm`
(list, dry run, keep/discard apply, bad choice, missing vault, pending
transaction) and `memfw review` plus the review sub-app (`list`, `decide`,
`apply`, `batch`) give the same output and vault bytes as the previous commit
(checked side by side with injected `--now`/`--id-seed`). The last four A2
allowlist entries are gone (17 -> 13), so A2 is done:

- `capture -> cli`, `curation.review_cli -> cli`: the vault opener moved from
  `memfw.cli._repo` into `memfw.core.repository.open_vault` (same checks and
  messages). It takes `clock`, `id_factory` and the exception type as
  parameters and reads no process-wide state; the CLI passes
  `typer.BadParameter` and the `--now` / `--id-seed` overrides.
  `memfw capture confirm` and `memfw review` use it directly;
  `memfw.cli._repo` stays as an alias.
- `capture.cli -> cli`: the `--now` / `--id-seed` overrides moved from the
  `memfw.cli._NOW` / `_ID_SEED` globals into `memfw.core.invocation`
  (`set_invocation`, `injected_now`, `injected_id_seed`, `id_factory`; stdlib
  only; `clock()` wraps `--now`). The overrides are CLI-only: the root
  callback records them there and CLI commands pass them explicitly; library
  code takes a clock and id factory instead. `memfw.cli._id_factory` stays as
  an alias. The `memfw.cli._NOW` / `_ID_SEED` globals are removed.
- `okf -> capture`: the candidate staging writer moved from
  `memfw.capture.staging` to `memfw.storage.candidate_staging`, and the
  candidate file reader (`read_candidate`, `candidate_is_expired`,
  `staged_candidate_paths`, ...) from `memfw.capture.review` to
  `memfw.storage.candidate_files`. The OKF importer imports them from storage.
  `memfw.capture.staging` and `memfw.capture.review` re-export the same
  objects; `list_staged_candidates` (and its logger name) stays in
  `memfw.capture.review`. The importer does not use
  `memfw.ports.staging.stage_candidates`: that contract writes JSON under
  `_candidates/<ingestor>/`, while OKF candidates are Markdown under
  `_candidates/remember/` that `memfw candidate-apply` reads.
- Unchanged, noted here: `memfw okf export` and `memfw okf import` ignore
  `--now`; the bundle's generated stamp and a staged candidate's `created_at`
  use the wall clock, as before.

### Internal — composition root, recall slice (A3a)

- New `memfw.runtime` (layer L4): `build_runtime(...) -> Runtime`, one Runtime per Gateway.
  `Runtime.recall` now owns the `memory_search mode=two_stage` wiring the MCP server built
  inline (built-in providers, resolvers, `compat_fts`/`strict` filter policy, the
  `InflightRegistry` (one per Runtime), the coordinator call); the server gets recall only
  from `Gateway.runtime`. `Runtime.recall` accepts exactly `compat_fts` or `strict` and
  raises `ValueError` on any other policy (the MCP checks and messages are unchanged). `Runtime.root`
  is the configured (unresolved) path; `recall` and `ensure_index` take the caller's open
  `Repository`, so a symlinked or relative vault root is followed per call by every mode, as
  before. `Runtime.ensure_index()` without a repository refuses a missing vault (never creates
  one). The two_stage timeouts are `Runtime` parameters
  (defaults 3 s per provider, 4 s total). The index staleness check moved to
  `memfw.runtime.ensure_search_index` / `Runtime.ensure_index`, still run before the
  coordinator's deadline. Callers sharing one Runtime share its in-flight slots per provider
  name, so a hung provider shows as `busy` across tools. The test seam
  `_two_stage_providers` moved from `Gateway` to `Runtime`. `memory_search` output is
  byte-identical for every mode except timing fields.
- Architecture guard: `memfw.runtime` is mapped to `L4-runtime`, `memfw.mcp` may import
  `memfw.ports` and `memfw.runtime`, and the allowlist drops `memfw.mcp -> memfw.recall` and
  `memfw.mcp -> memfw.recall.builtin` (25 to 23 entries). Remaining A3 work is listed in
  `docs/architecture.md`.

### Fixed — recall privacy tags fail closed (security)

- `memfw.recall.builtin.meta_from_document` recognised only the exact, lower-case `sensitivity:private` tag, so under `policy=strict` (`memory_search mode=two_stage`) a memory tagged `Sensitivity:Private`, `sensitivity:local-private` or `sensitivity:secret` was kept. It now classes `sensitivity:*` tags exactly like the projection (`tag_privacy_class`): any case, only `sensitivity:private` is `local-private`, any other value is `unknown`, and both are dropped without clearance. `privacy:` and `lifecycle:` tag prefixes are matched in any case too, and with several such tags the strictest wins instead of the first (privacy: unknown > local-private > none; lifecycle: retired > superseded > other non-active > active), so tag order or case can never widen access. Before, `privacy:none` followed by `privacy:local-private` was kept under `strict`. `MemoryMeta` gains an optional `sensitivity_class`; callers that set only `sensitivity_private` behave as before. `policy=compat_fts` does not check privacy, so its output is unchanged.


### Fixed — body lookup parses only hash matches (performance)

- `_current_body_by_hash` and the archive lookup compared the body hash only after YAML-parsing each file, so every write parsed up to the whole vault and the cost depended on the filesystem's directory order (a perf guard passed on macOS and failed on Linux CI with 109 parses). They now hash first and parse only a matching file; the result is the same first file in glob order that both matches and parses. A non-matching file with invalid UTF-8 no longer raises; a matching one is skipped like any file that fails to parse. A delete in a 200-memory vault now parses 6 files in any directory order (was 6–204).

### Internal — cycles broken in the kernel and between capabilities (A2a)

CLI output is unchanged: `memfw stats`, `memfw cold-archive` / `cold-restore`,
capture stage crash recovery (`Repository.recover_pending`), `memfw handoff`
(JSON and Markdown, with and without `--summarize`), `memfw conversations
worker` (report and staged dream candidates) and `memfw dream skills
--from-queue` give the same bytes as the previous commit (checked side by
side; wall-clock stamps and run ids aside). Six allowlist entries are gone
(23 -> 17):

- `storage -> curation`, `stats -> curation`: the cold-stub body read
  (`_with_archived_body`) moved from `memfw.curation.engine` into
  `memfw.storage.with_archived_body` (next to the cold archive writer);
  `curation.engine._with_archived_body` stays as an alias.
- `storage -> capture`: the capture candidate WAL (`CaptureCandidate`, the
  record reader, pack decompression and the pending-append recovery) moved into
  `memfw.storage.capture_stage`, so `recover_pending` no longer imports the
  capture package. `memfw.capture` re-exports them under the old names.
- `handoff -> conversations`: the transcript read interface and its text
  guards (`ConversationSource`, `SessionInfo`, `Turn`, `safe_text`, the
  timestamp keys, `goal_text`, the private-location guard) moved from
  `memfw.conversations.store` into `memfw.history.transcripts`, next to the
  store it reads; `memfw.conversations.store` re-exports them unchanged. The
  handoff summarizer gets its backend loader injected: `memfw handoff` passes
  `memfw.conversations.backends.load_backend`; calling
  `memfw.handoff.build_pack(..., summarize_spec=...)` from Python now needs
  `loader=` (it raises `SummarizeError` without one).
- `conversations -> dream`: new stdlib-only ports `memfw.ports.claims`
  (`ClaimVerifier`, `ClaimStager`, `StagedClaim`, `ClaimEvidence`) with
  contract checks `memfw.ports.testing.check_claim_verifier` /
  `check_claim_stager`. The worker verifies and stages claims through them;
  `memfw.dream.claims` (`DreamClaimVerifier`, `DreamClaimStager`) implements
  them and `memfw conversations worker` injects them. `ConversationWorker` /
  `open_worker` with a vault now refuse to start without a stager and a
  verifier (or judge).
- `dream -> conversations`: `memfw.dream.skills.queue` reads the worker's
  episode queue through an `EpisodeFeed` protocol;
  `memfw.conversations.feed.WorkerEpisodeFeed` implements it and
  `memfw dream skills --from-queue` injects it. `run_skills(...,
  options=SkillOptions(from_queue=True))` from Python needs `episode_feed=`.

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

### Added — `ContextPack` contract and `memory_context` v2 (C1)

- `memfw.ports.context` (stdlib only): `ContextEntry`, `ContextSlot` and `ContextPack`,
  frozen and validated like the other port types. `pack_version` is a sha256 over the
  policy name and each entry's id, version, content hash and carried length (never body
  text, titles or queries); `pack_version_payload` returns the exact bytes. The contract
  kit gains `check_context_pack`.
- New capability package `memfw.context` (L3, in the A0 layer map) builds the `core`
  slot; it imports only core and ports, and the visibility filter is injected.
  `memfw.runtime` composes it (`Runtime.context_keep`, `context_v1`, `context_pack`;
  strict uses the recall `FilterPolicy`, fail closed on an unknown policy). MCP
  `memory_context` only parses, asks the Runtime and serialises; no allowlist entry.
- MCP `memory_context` takes optional `v` (1 default, or 2) and `policy` (`compat_fts`
  default, or `strict`; v=2 only). Without them the payload is byte-identical to before
  (checked against the previous release on fixture vaults). `v=2` returns a `ContextPack`
  with one `core` slot, each entry's `version` and `content_hash`, and `overflow` /
  `truncated_ids` naming the memories that did not fit. `strict` filters through the recall
  `FilterPolicy`: no `local-private` or unrecognised-privacy memories, no retired,
  superseded, expired or archived ones. See `docs/ports-and-recall.md` §4.2.
- v2 builds each entry from a single read of its file (body, version and hash agree) and
  reports `skipped` for memories it cannot represent (e.g. a hand-placed file whose id is
  outside the memory id pattern) instead of failing.
- New errors for v1 callers: `policy` other than `compat_fts`, or `v` not the integer 1 or 2,
  used to be ignored and now fail.

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

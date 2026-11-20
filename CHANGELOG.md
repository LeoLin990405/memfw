# Changelog

All notable changes to memfw. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). Entries name the pull request that
carried the change; `#N` links to `LeoLin990405/memfw` pull request `N`.

The vault format did not change in `0.4.0` or `0.5.0` either: `vault.yaml`
stays at `schema_version: 1`, and files written by an earlier version keep their
bytes. `0.5.0` changes no memfw data format; MCP replies gain one key (see the
[0.5.0 upgrade notes](#upgrade-notes-040--050)). `0.4.0` adds attachments (a
vault with an `attach` event cannot go back to 0.3.0) and moves two derived
projections out of the vault; see the 0.4.0
[Upgrade notes](#upgrade-notes), the 0.3.0
[Compatibility / upgrade notes](#compatibility--upgrade-notes) and, for
`0.1.x` → `0.2.0`, [Compatibility and upgrade](#compatibility-and-upgrade).

## [Unreleased]

### Fixed

- Redaction (`memfw.history.redact`): an IPv4 address inside a host name
  (`git.<ip>.sslip.io`) or at the end of a sentence is now masked; the dash
  and hex IP forms of `sslip.io` / `nip.io` / `xip.io` (any case), the tailnet
  label of `*.ts.net` names (also dash-encoded in project directories) and
  phone numbers (`[REDACTED:phone]`: Chinese mobiles, `+`-prefixed
  international numbers with a valid country code; see docs/history.md for
  the rules and the false-positive guards; glued letters, extensions,
  space runs and slashes are handled) are new, and leading-zero octets
  (`192.0.2.007`) count as addresses. Masking repeats until the text is
  stable, so redaction stays idempotent; hitting the pass limit is counted
  (`PASS_LIMIT_HITS`) and logged without text. Host names stay visible;
  `redact(..., redact_hostnames=True, hostnames=[...])` is an opt-in hook,
  default off.
- History search results and session lists apply the full redaction to the
  transcript path (was user names only) and redact stored snippets again on
  read; the handoff tool map and dream distillation check for secrets before
  redacting (a secret-shaped value is dropped, not kept masked).

### Added

- `memfw conversations reredact`: re-redacts text already at rest in the
  conversation store under the current rules. Dry run by default (read-only,
  counts only: rows and columns that would change, new `[REDACTED:kind]`
  masks, newly withheld turns); `--apply` takes the store's writer lock and the
  worker's run lock without waiting (exit 3 when either is held), writes a
  verified `0600` backup with the SQLite online backup API, rewrites changed
  rows in batches (one `BEGIN IMMEDIATE` transaction each, guarded UPDATEs,
  `skipped_concurrent` counted) with `secure_delete` on, then re-scans the
  rewritten rows (must find nothing) and runs `integrity_check` (plus
  `wal_checkpoint(TRUNCATE)` in WAL mode); `--vacuum` also rewrites free pages;
  `--worker-state` covers the worker's summaries and episodes; `--json` prints
  a machine-readable summary. Exit codes: 0 ok, 2 refused before writing, 3
  locked, 4 verification failed, 5 failed after the backup (counts so far and
  the backup path reported). Keys (`session_id`,
  `offset`, `locator`) are never rewritten, the row digest is recomputed, so a
  later ingest neither duplicates turns nor re-processes sessions. See
  docs/history.md.
- The conversation store has a writer lock (`<store>.lock`): an ingest waits up
  to 60 seconds for it, then fails that agent's ingest for the round (no data
  lost; the next round catches up). While `reredact --apply` runs, one ingest
  cycle can take about 13 minutes to give up: stop the ingest/worker cycle, or
  deploy the locked ingest, before running it.

### Upgrade notes

- Text stored before this release was redacted under the old rules. Every
  output is redacted again on the way out, but the stored rows still hold the
  old values. **The conversation store is the data, not a cache**: do not
  delete it (many raw transcripts no longer exist to ingest again). Instead,
  stop the ingest and worker jobs, run `memfw conversations reredact` (dry
  run), then `memfw conversations reredact --apply --worker-state --vacuum`,
  check the result, and delete the `*.bak-reredact-*` backup once satisfied
  (it still holds the old values).
- The history index (`<vault>/.memfw-history.sqlite`) is separate derived data
  rebuilt from the transcripts on disk: if that file exists, delete
  `<vault>/.memfw-history.sqlite*` and run `memfw history index`; if it does
  not exist there is nothing to do. Derived files already written to the vault
  (distilled memories, candidates, skill drafts) are not rewritten; review
  them with a scan if needed.

## [0.5.0] — 2026-11-20

The fourth numbered release: the contracts and the first packages for the
Stage pipeline and the LLM Wiki (generator and gate), a Tailscale host
inventory ingestor, a Qdrant sync that survives a slow embedder, and one
trust signal, `retrieval_health`, on every search and context reply. Two
changes reach memfw itself: `memfw.ports` (ports 0.4.0, additive) and the
`retrieval_health` key on three MCP replies (additive; results, order and
scores unchanged). Every new package is a separate wheel that memfw never
imports and that is off until an operator installs and runs it. `PORTS_API_VERSION` is 0.4.0 (thirteen ports;
`EntailmentChecker` is new); `memfw-http/2` and `memfw-http/3`, the MCP tools,
the vault format, the event log and the index schemas are unchanged. The
entries after the upgrade notes give the details.

### Summary

- **Ports 0.4.0: LLM Wiki contracts (W1-1).** `Citation.ref` (`mem:`, `kh:`,
  `att:`, `wiki:`), the `memfw-wiki-page/1` and `memfw-wiki-job/1` formats,
  the `EntailmentChecker` port and the `check_wiki_page` /
  `check_entailment_checker` contract checks (#113).
- **Stage pipeline (P1).** `memfw-pipeline` 0.1.0, the Stage contract and
  runner (locking, atomic publish, shrink guard, freshness, status, unit
  templates); only an example stage ships (#111).
- **Wiki page generator (W1-4).** `memfw-adapter-wikigen` 0.1.0, a `Deriver`
  that turns one wiki job into one cited page candidate (#114).
- **Wiki gate (W1-2).** `memfw-wiki-gate` 0.1.0, the deterministic evidence
  checks a generated page passes before anyone sees it (#115).
- **Tailnet host inventory.** `memfw-adapter-tailnet` 0.1.0, an `Ingestor` of
  identity-only `host-fact` candidates (#112).
- **Qdrant slow-embedder fixes.** `memfw-adapter-qdrant` 0.1.3: configurable
  embedding timeout and batch size, incremental sync that resumes, a lock per
  vault and collection (#110).
- **Retrieval health.** `retrieval_health` (`retrieval-health/1`, 29 fixed
  reason codes, `docs/retrieval-health-codes.json`) on `memory_search`,
  `memory_context` v=2 and `conversations_search`; lazy `memfw` /
  `memfw.retrieval` re-exports; clearer MCP tool descriptions; a test guard
  against touching the real memfw state directories (#116).
- **Package versions.** memfw 0.5.0; `memfw-ports` and `memfw-recall` 0.4.0;
  every adapter gets a patch release for its `memfw-ports==0.4.0` pin.

### Upgrade notes (0.4.0 → 0.5.0)

- **No memfw migration.** The vault format (`schema_version: 1`), the event
  log, the FTS5 and notes indexes, `READ_PATH_VERSION` (2), the projections,
  the set of MCP tools and resources and both HTTP contracts are unchanged,
  so nothing needs a rebuild and a 0.4.0 host can keep reading a vault
  written by 0.5.0. The third-party runtime dependencies in `uv.lock` are
  identical to 0.4.0. MCP `serverInfo.version` (`memfw`, `memfw-notes`) reads
  `0.5.0`.
- **MCP output shape: one added key.** `memory_search` (every mode, also the
  `Runtime.search` payload), `memory_context` v=2 and `conversations_search`
  (default and `two_stage`) gain a top-level `retrieval_health` object; with
  no degradation it is the only difference from 0.4.0. `memory_search`
  `two_stage` adds `degraded_sources` when a provider answered degraded.
  `conversations_search` `two_stage` counts `retrieval_health` toward
  `max_bytes`, so near the cap one session or turn window fewer may fit
  (`truncated` / `next_offset` as usual). `memory_context` v1, its
  `unchanged: true` reply, the `memfw://context/<slot>` resources and every
  HTTP answer (`memfw-http/2`, `memfw-http/3` including `POST /context`) are
  byte-identical. Some `tools/list` descriptions are reworded and a few input
  properties gain a `description` (types, defaults and required fields
  unchanged). A strict client that rejects unknown keys must allow
  `retrieval_health` and `degraded_sources`.
- **Imports.** `memfw` and `memfw.retrieval` re-export lazily; `from memfw
  import RelatedIndex` and `from memfw.retrieval import VaultRetriever` still
  work (`VaultRetriever` now lives in `memfw.retrieval.vault`).
- **Ports 0.4.0 is additive, with two visible edges.** A `Citation` built the
  old way constructs, compares and prints as before, but
  `dataclasses.asdict(citation)` now has `ref` and `ref_kind` keys (`None`),
  and `check_view_citations` refuses a citation that names no memory or
  carries a non-`mem:` ref before calling `exists`.
- **Adapter wheels pin `memfw-ports==0.4.0`.** Every adapter, `memfw-recall`
  and the new packages require the 0.4.0 `memfw-ports` wheel; an adapter venv
  with `memfw-ports` 0.3.0 cannot take the new adapter wheels (pip refuses the
  mix) and an old adapter wheel cannot take ports 0.4.0. Upgrade an adapter
  venv as one set (`memfw-ports`, then the adapters), separately from the
  memfw install it serves; memfw itself still ships `memfw.ports` in-tree and
  does not need the `memfw-ports` wheel.
- **New adapter versions.** `memfw-adapter-deepseek` 0.1.2, `-bm25s` 0.1.2,
  `-qdrant` 0.1.3, `-qmd` 0.1.2, `-pageindex` 0.1.2, `-ocr` 0.1.2,
  `-langextract` 0.1.1, `-lightrag` 0.1.1, `-graphrag` 0.1.1. Apart from
  Qdrant, only the version and the `memfw-ports` pin change (the version also
  appears in user agents, MCP `clientInfo` and producer metadata). A Qdrant
  wheel built from `main` between #110 and this release reports `0.1.2` but
  carries the 0.1.3 code; reinstall 0.1.3 so the version tells the two apart.
- **Qdrant 0.1.3 operator changes.** Defaults (30 s timeout, batch 16) and
  the collection fingerprint are unchanged, so no re-embed. `sync` now
  upserts after every batch, exits 1 when it stopped incomplete (it resumes
  on the next run) and 3 when another run holds the per-vault lock; a timer
  or wrapper that treated any non-zero exit as fatal should accept 1.
  `--max-seconds`, `--embedding-timeout` and `--embedding-batch-size`
  (or `MEMFW_EMBEDDING_TIMEOUT` / `MEMFW_EMBEDDING_BATCH_SIZE`) are new; an
  out-of-range value is an error (CLI exit 2) instead of being clamped.
- **New packages are opt-in.** `memfw-pipeline`, `memfw-wiki-gate`,
  `memfw-adapter-tailnet` and `memfw-adapter-wikigen` are separate wheels; installing memfw 0.5.0 installs and enables none of them.

### Known limitations

- Nothing in memfw consumes the wiki contracts yet: there is no wiki store,
  intake stage or MCP surface, and memfw has no `EntailmentChecker`
  implementation. A wikigen page is a candidate only, and the wiki gate is a
  library: no worker or intake stage calls it yet.
- `retrieval_health` is not yet on the HTTP `POST /context` answer or the
  `memfw://context/<slot>` resources; a client that has only the JSON of a
  `two_stage` reply infers whether an embedder is configured.
- `memfw-pipeline` ships only an example stage; the admitted view, metadata
  index, FTS / bm25s / Qdrant indexes and projections are not yet ported to
  it.
- The tailnet adapter reads machine identity only; it decides nothing about
  where data is sent, and its allowlist report needs an operator-written
  allowlist.
- The 0.4.0 recall limitations remain
  ([`docs/ports-and-recall.md`](docs/ports-and-recall.md) §4.1c-bis).

### Changed — release package versions

- memfw 0.5.0 (`pyproject.toml`, `memfw.__version__`, both MCP `serverInfo`
  blocks, `uv.lock`). Adapter patch releases as listed in the upgrade notes;
  `memfw-ports`, `memfw-recall` (0.4.0), `memfw-pipeline`, `memfw-wiki-gate`,
  `memfw-adapter-tailnet` and `memfw-adapter-wikigen` (0.1.0) keep the
  versions they were merged with. README (both languages): release badge and
  status, port and adapter counts, the 0.4.0 → 0.5.0 upgrade notes, the
  pipeline runner, the wiki gate, `retrieval_health`, the tailnet and wikigen
  adapters and the wiki contracts;
  COMPONENTS and the adapter docs: the `memfw-ports` 0.4.0 pins.

### Added

- `retrieval_health` (`retrieval-health/1`, `memfw.retrieval.health`): one top-level
  summary `{schema, mode, confidence, reasons, score_kind, note}` on `memory_search` (every
  mode), `memory_context` v=2 and `conversations_search` (default and `two_stage`), derived
  only from signals the output already carries (`degraded`, `errors`, `recall`,
  `recall_metrics.dropped`, slot `degraded` codes). Results, order and scores are unchanged;
  with no degradation the new key is the only difference. Fixed reason codes are listed in
  [`docs/retrieval-health.md`](docs/retrieval-health.md) (zh-CN alongside); the pure
  functions are reusable by clients. Each code has an `affects_mode` class (`yes`, `no`,
  `fallback`, `empty`); informational codes (`keyword_only`, `adapters_shadow`, `withheld:*`)
  never lower mode or confidence, and the cause code carries the degradation (no embedder:
  `semantic_unavailable`, degraded/medium), as in fleet-memory. An empty answer carries
  exactly one of `all_sources_failed`, `page_empty`, `empty_degraded` (a degraded or
  fallback path: no result is not proof of absence), `withheld_only`, `no_match`. memfw's
  table is the canonical one (fleet-memory's client codes included): `export_codes()` and the
  committed `docs/retrieval-health-codes.json` carry it as data. No port contract changed
  (`PORT_VERSIONS` unchanged).
- `memory_search` `two_stage`: `degraded_sources` (sorted provider names, only when
  non-empty) says which providers answered with a degraded page; `retrieval_health` reads it
  as `source_fallback:<name>`. A reply degraded only by a failed access event stays
  `full`/`high` with `access_not_recorded`.
- `conversations_search` `two_stage`: `retrieval_health` counts toward `max_bytes` (its
  "with sessions" form is reserved before sessions are packed), so near the cap one fewer
  session or turn window may fit than in 0.4.0; `truncated`/`next_offset` say so as usual.
- The `memfw` root and `memfw.retrieval` re-export lazily (`__getattr__`): importing
  `memfw.retrieval.health` loads no other memfw module; `from memfw import RelatedIndex` and
  `from memfw.retrieval import VaultRetriever` work as before (the latter now lives in
  `memfw.retrieval.vault`).
  MCP tool descriptions now say `conversations_get` `start`/`end` are turn indexes (not the
  search `ref`/`offset` position key), when `conversations_search` `query` may be empty, and
  that hybrid/two_stage `score` is a fused rank, not relevance.
- Test-suite guard (`tests/conftest.py`): an audit hook fails any test that opens, connects
  to, lists, links, touches or removes a path under the real `~/.local/share/memfw` or
  `~/.local/share/fleet-memory` (absolute and symlink-resolved forms); isolate `HOME` or
  pass a tmp path instead.
- `memfw-wiki-gate` 0.1.0 (`packages/memfw-wiki-gate`, wheel `memfw_wiki_gate`), LLM Wiki
  W1-2: deterministic checks of a generated `memfw-wiki-page/1` page against its
  `memfw-wiki-job/1` job and, at intake, an admitted-view reader (`SourceResolver`). Per
  claim: citation resolution (no page, `file:`/URL/bare pointer, conversation, generated or
  looped source), lifecycle (G6: non-current sources only in `timeline`, as `superseded`
  claims citing the replacement), the conflict rule, causal label and wording (G5, zh + en
  lexicon; unhedged causal and certainty wording refused), token consistency (numbers,
  times, dates, versions, ids, paths, quotes, names; CJK numerals equal Arabic digits) and,
  the caller's own NLI scores (`nli_scores` by cid, `nli_model`; the generator's
  `checks.nli` and `build.nli_model` are never trusted). Claims with any `Cf` character or a
  Latin/Cyrillic/Greek mixed word are dropped; lexicon matching ignores `Cf`/`Mn`/`Me`
  characters, whitespace and traditional forms; every Unicode digit reads as ASCII; units,
  signs and path/quote boundaries are compared. Per-claim verdicts with fixed reason codes,
  a page verdict (`publish` / `needs_attention` / `retry` / `reject`), counts, privacy = the
  strictest of *all* job sources (the page lists them all; rule shared with the W1-4
  generator; a stricter `previous_page` is refused) and the surviving claims as a `WikiPage`
  that passes `check_wiki_page`; open questions regenerated from the job; reports never hold
  page or source text, and sentence hashes only as a caller-keyed HMAC. The v2 design's
  write-gate attacks R1–R5, the v3 rules and the review rounds' evasions are 114 synthetic
  fixtures with 0 false accepts. Standard library only plus `memfw-ports` 0.4.0; built and smoke-tested in the
  standalone-wheel CI job. It pins `memfw-ports` to the ports API version (new ports-API pin
  table in `docs/ports-and-recall.md`), not to a port contract. See `docs/wiki-gate.md`.

- `memfw-pipeline` 0.1.0 (`packages/memfw-pipeline`, wheel `memfw_pipeline`), the v0.5.0 P1
  Stage contract and runner. A stage implements `freshness`, `inputs_digest`, `build`,
  `verify`, `publish` and optional idempotent `post_publish` steps; the runner gives every
  stage the same single-writer lock beside the target (`.<target>.lock`, `O_NOFOLLOW`, retried
  for `lock_wait_seconds`, then exit 75), `running` / `publishing` status that
  carries the last good counts, build into a directory beside the target, fsync, atomic
  exchange of real directories (`renameat2(RENAME_EXCHANGE)` / `renamex_np(RENAME_SWAP)`, else
  two renames with rollback), a record inside every published artifact
  (`.memfw-pipeline.json`: inputs digest, counts, extra fields) that decides whether a rebuild
  is needed, so a run killed after the swap cannot make a later run skip one, crash-leftover
  cleanup (refuses an ambiguous `.old-*` restore), shrink guard (empty or under half of the
  baseline, the artifact record first: exit 5 unless `--allow-shrink`), freshness (source
  revision, lag, `stale`; revisions that look like an address or host name are refused) and a
  status file with counts, fixed reason codes and stage-declared extra fields (each re-checked
  against its own recorded format) only. `memfw-pipeline
  run|plan|status|unit`; `unit` prints hardened systemd (service + timer) or launchd
  templates for review (with a non-root `user`, `AmbientCapabilities=`; extra sandboxing such
  as `SystemCallFilter=@system-service` with `SystemCallErrorNumber=EPERM`, `RestrictNamespaces`, `ProtectClock`, containerd and
  podman sockets inaccessible; a root unit needs `trust_paths = true`). Path-trust check, on by
  default when run as root. Standard library only at run time plus `memfw-ports` 0.4.0;
  memfw never imports it. Only an example stage
  ships; the real stages are ported in later phases. CI also lints and type-checks the
  package and builds its wheel in the standalone-wheels job. See `docs/pipeline.md`.

- `memfw-adapter-tailnet` 0.1.0 (`packages/adapters/memfw-adapter-tailnet`): an `Ingestor`
  that turns `tailscale status --json` (an exported file, or one bounded run of the CLI with a
  fixed argv and no shell) into staged `host-fact` candidates, one per machine that is new,
  returned, gone, renamed or changed IPs, OS or tags since the previous run; unchanged
  machines produce nothing, and candidates hold identity fields only, so a retry after a
  crash stages duplicates only. Hashed host ids from the Tailscale node ID; the MagicDNS
  suffix is kept only as a hash; user and login data, keys and endpoints are never read;
  shared-in nodes are left out unless `--include-shared`, provider exit nodes (`Location`)
  always. Candidates default to `privacy: local-private`. A names-free `report.json` counts
  `listed_offline`, `listed_missing`, `listed_ambiguous` and `unknown` against an operator
  allowlist the scan only reads and never matches on the self-reported host name. Off by
  default; memfw never imports it. Built and smoke-tested in the standalone-wheel CI job.
  See `docs/adapter-tailnet.md`.

- **Ports 0.4.0: LLM Wiki contracts (W1-1).** Additive only; nothing in memfw uses them
  yet, and every existing output is unchanged.
  - `Citation` gains optional `ref` / `ref_kind` (`memfw.ports.refs`): `mem:<id>[@v<n>]`,
    `kh:<path>[#<heading>]`, `att:<prx-id>:<n>` or `wiki:<page_key>[#<section>]`, plus
    `Citation.from_ref` and `Citation.to_json`. A citation built the old way constructs,
    validates, compares and prints exactly as before; `dataclasses.asdict` now lists the two
    new fields as `None`. A `DerivedView` citing a non-memory ref fails
    `check_view_citations` (fail closed).
  - `memfw.ports.wiki`: the `memfw-wiki-page/1` format (`WikiPage`, `WikiClaim`,
    `WikiSource`, `WikiBuild`, `parse_wiki_page`) and the `memfw-wiki-job/1` work package
    (`WikiJob`, `parse_wiki_job`, `wiki_job_digest`), with standard-library validation and
    JSON Schemas (`WIKI_PAGE_SCHEMA`, `WIKI_JOB_SCHEMA`; structural rules only, the parsers
    are the reference). Every claim is cited, `inference` only, causal grade at most its
    own sources' and never above `hypothesis`; page privacy is the strictest source level
    and only `public`/`shared`. Refs are canonical (one spelling per source), `mem:wiki-…`
    is reserved for wiki page records, JSON input refuses duplicate keys and deep
    nesting, and the job digest covers every source field and the concept's labels.
  - `check_view_citations` refuses a citation that names no memory or carries a non-`mem:`
    ref before asking `exists`.
  - `EntailmentChecker` port (`memfw.ports.entailment`, port version 1).
  - Contract kit: `check_wiki_page` and `check_entailment_checker` in
    `memfw.ports.testing`; the standalone wheel smoke runs both.
  - `PORTS_API_VERSION`, `memfw-ports` and `memfw-recall` move to `0.4.0`; `memfw-recall`
    and every adapter pin `memfw-ports==0.4.0` (their versions and code were unchanged in
    #113; the release gives each a patch version).

- `memfw-adapter-wikigen` 0.1.0 (`packages/adapters/memfw-adapter-wikigen`, W1-4): a
  `Deriver` (kind `wiki_page`) that turns one `memfw-wiki-job/1` package into one
  `memfw-wiki-page/1` page candidate. The fixed `wiki-v1` prompt forces the page sections
  and per-sentence citations to the job's own refs; a sentence with a ref that is not
  exactly a job ref, or that breaks a section, causal, lifecycle or conflict rule, is
  dropped and counted, never repaired. Only failures that can differ between runs are
  retried with the same seed (backend transport errors `unreachable`, `timeout`,
  `http_502`/`503`/`504`, and malformed JSON `empty_reply`/`not_json`); a reply that is
  JSON but not the page shape is refused at once as `bad_output:<code>`. The `open`
  conflict claims, the source list (all job sources, cited or not), privacy (strictest
  over all job sources) and digests are written by the program, and the page must pass
  `parse_wiki_page` and `check_wiki_page(page, job=job)` or nothing is returned. The job's
  generator block must match the worker (model tag and digest, generator version,
  `prompt_version` = `wiki-v1+<sha8>`, a hash of the instruction text and every
  output-affecting setting, raw values in the report's `prompt_settings`); token budget,
  context-window pre-check, per-call timeout and job deadline. Previous-page sentences
  enter the prompt only when every ref they cite is a current job ref, and a previous page
  stricter than the job's sources is left out. Ships an Ollama
  backend (literal loopback only, proxies ignored, redirects refused, digest from
  `/api/tags`) and a model-free stub; a non-local backend (the backend's own `local` claim,
  operator-trusted) needs `cloud: true` and an all-`public` job, and is sent only the
  concept label, open-question ids and conflict refs, and a previous page only if it is
  `public`. Candidate metadata carries `tenant` and `page_key`, not the concept.
  Unavailable when Ollama is unreachable or the model is missing. Optional `precheck` hook
  (used only when importable). Off by default; standard library only; memfw never imports
  it. Built and smoke-tested in the standalone-wheel CI job. See `docs/adapter-wikigen.md`.

### Fixed

- Qdrant adapter: the embedding timeout and batch size were fixed at 30 s and 16, so on a slow
  CPU-only host no incremental `sync` could ever finish a batch. They are now the options
  `embedding_timeout_seconds` / `embedding_batch_size`, the environment
  `MEMFW_EMBEDDING_TIMEOUT` / `MEMFW_EMBEDDING_BATCH_SIZE` and `sync` flags (defaults and
  fingerprint unchanged). Out-of-range values raise `ValueError`, also in the `EnvEmbedder`
  constructor (it used to clamp a batch size below 1).
- Qdrant adapter `sync`: shortest memories first; upserts after every embedding batch (16 points
  by default instead of 64 per upsert); a failed batch is retried one memory at a time; HTTP 400/413/422
  is `embedding_rejected` (that memory is skipped and does not count as the endpoint being
  down); timeouts, connection errors, 5xx and the other 4xx (`embedding_refused`) stop the run
  after three in a row; reports
  `embedded`, `failed_requests`, `failures`, `remaining`, `rejected`, `stopped`, exits 1 when
  incomplete and resumes on the next run; `--max-seconds`; one run per vault and collection
  (a `flock` outside the vault; a second run exits 3).
- Qdrant adapter search: the query embedding waits at most `min(timeout,
  max(budget.timeout_seconds, 30))`: the default stays 30 s, and a long sync timeout cannot pin
  the provider's worker.

### Docs

- README (both languages): "One ongoing conversation per app", a session-start rule
  built on `conversations_list` / `conversations_handoff`, how each app's `memfw-mcp`
  names itself, and two setup pitfalls (unexpanded `${VAR}` in MCP `env`; editable
  source in an OS-protected folder) (#109).

## [0.4.0] — 2026-11-18

The third numbered release: attachments and their text proxies as a third
source beside memories and notes, a typed-edge knowledge graph, `memfw-http/3`
with `/context` and reviewed `/forget` requests, operator-configured recall and
deriver adapters (all off by default), and the recall wiring that joins them:
the meta-index lifecycle for every provider and `kh:` / attachment-segment
resolvers. The T1–T7 knowledge base ↔ memory tests gate the release. New
behaviour is off by default or additive: with no new flag, setting or
`vault.yaml` key, outputs match 0.3.0 except for the fail-closed privacy fixes
(notes frontmatter split, the embedding locality gate, clearance before access
logging) listed in the upgrade notes below. `PORTS_API_VERSION` is 0.3.0
(twelve ports; `ProjectionStore` and `memfw.ports.proxies` are new); the
`memfw-http/2` contract is unchanged. The entries after this summary give the
details, newest first.

### Summary

- **Attachments (MM1a, MM1b).** A content-addressed blob store outside the
  vault, attachment records and `memfw attach add/list/show/verify`; referenced
  originals, `detach`, `relink`, `purge`, `scan` and `reown`; quota, staging and
  location guards (#75, #79, #80, #82, #96).
- **Attachment proxies and search (MM2a, MM2b).** The `memfw.proxy` v1 import
  format with canonical locators, `memfw attach proxy import`, a
  `proxy_segments` index, `ProxyProvider` and the opt-in MCP tools
  `attachment_search` / `attachment_get` behind `mcp.attachments` (#91, #93).
- **Attachments as evidence (MM3).** `att:` evidence is the `observation`
  class in the write gate (it caps fact and causal grades); `memfw eval causal
  --multimodal` (#95). An OCR proxy adapter, `memfw-adapter-ocr` 0.1.1
  (#86, #107).
- **Knowledge graph (K3, K4).** A typed-edge projection, bounded path patterns,
  `memfw graph rebuild/match` and the `ProjectionStore` port (#77). Projections
  outside the vault record the vault they belong to, and the corroboration
  projection moves out of the vault (#90).
- **Recall adapters, off by default.** `runtime.recall.adapters` loads
  operator-configured `RecallProvider`s: bm25s (#88, #99), Qdrant (#94, #100,
  #101), QMD notes (#89) and PageIndex (#85).
- **Deriver adapters, off by default.** GraphRAG (#97), LightRAG (#92) and
  LangExtract (#84) stage derived candidates for review; none writes the vault.
- **Recall wiring.** The meta-index lifecycle (`valid_until`, `superseded_by`,
  `lifecycle`) filters every provider, `kh:` note sections resolve through
  `NotesSegmentResolver` (#98); `runtime.recall.lifecycle_source`,
  `runtime.recall.notes` and attachment segments on the same resolver hook
  (#105).
- **Privacy hardening.** The notes frontmatter split follows YAML block
  scalars and fails closed when ambiguous; embedders, rerankers and query
  rewriters off this machine receive no private text and no queries unless
  allowed; `hybrid`/`vector` apply the clearance before access logging;
  `memory_remember` suggestions are bounded to the caller (#104).
- **HTTP and forgetting (R5, R6).** `memfw-http/3` beside `memfw-http/2`
  (selected per request), `POST /context`, `POST /forget` requests and
  `memfw forget requests/residue` (#78); a replay after deletion respects the
  caller's clearance (#87).
- **Event log forward compatibility.** A 0.4.x reader keeps and skips an
  unknown event kind marked `details.projection: "none"` (#81).
- **Context and evaluation.** The opt-in `current` context slot (U6, #71) and
  the C4 context pack evaluation `memfw eval context` (#76).
- **Conversations.** Two-stage conversation search with tag filters (M3c,
  #72) and a Qwen Code reader, the 15th (#83).
- **Tests and docs.** The T1–T7 knowledge base ↔ memory integration tests
  (#106), a deterministic usage-retention test (#102), README and COMPONENTS
  updates (#103).

### Upgrade notes

- **Embedding, reranker and rewriter locality gate.** An embedder whose
  endpoint is not a loopback literal (`localhost`, `127.0.0.0/8`, `::1`; never
  resolved through DNS) — an off-host `MEMFW_EMBEDDING_URL`, a `notes` /
  `memfw-notes-mcp` `--endpoint`, `memory-related-build` / `notes related-build
  --embedding-url` — now receives only `public`/`shared` memories and notes and
  no query text, and an off-host notes reranker (`--rerank-endpoint`) or query
  rewriter is skipped. Without the opt-in that means **no semantic search over
  non-public/shared text and degraded results**: `hybrid`/`vector` searches
  answer from FTS (`mode_used: fts`, `degraded: true`, `semantic_query_withheld`
  in `errors`, plus `semantic_partial` when the index holds memories without
  vectors), notes searches report `rerank_withheld` / `rewrite_withheld`, the
  `two_stage` hybrid provider reports itself unavailable, and stored vectors of
  private items are dropped on the next index rebuild (the manifest label
  `embedding_gate: public,shared` forces it). To opt in, accepting that private
  text leaves the machine: `MEMFW_EMBEDDING_ALLOW_REMOTE=1` (wins),
  `vault.yaml` `runtime: {embedding: {allow_remote: true}}` (MCP server, Runtime
  and vault commands) or `--allow-remote` on every command that takes an
  endpoint. A loopback endpoint or an in-process embedder is unchanged. See
  [`docs/ports-and-recall.md`](docs/ports-and-recall.md) §4.1d.
- **Attachments are one-way.** memfw 0.3.0 fails closed on a vault with an
  `attach` event (every command that reads the event log), so **upgrade every
  host that reads the vault before the first `memfw attach add`**; there is no
  downgrade to 0.3.0 after it. A vault that only gained `vault_id` stays
  readable by 0.3.0. See [`docs/attachments.md`](docs/attachments.md)
  (Compatibility).
- **Rebuild the edge and corroboration projections.** Both now live outside
  the vault, keyed by the vault's resolved path
  (`~/.local/share/memfw/edges/<key>.sqlite3`,
  `~/.local/share/memfw/corroboration/<key>.sqlite3`, schema 2), and record the
  vault's identity (path, `vault_id`, first event). After upgrading run
  `memfw graph rebuild` and `memfw corroboration rebuild` on every vault that
  uses them. Until then `graph match` refuses the old file ("rebuild it"),
  corroboration shows `{"degraded": "projection_missing"}`, and a file built
  from another vault at the same path is refused ("projection belongs to
  another vault: rebuild"; MCP `projection_other_vault`). An old vault-root
  `corroboration.sqlite3` is reported as `legacy_file`, ignored and never
  deleted; it counts toward the vault's size until you delete it.
- **Notes indexes re-read every note once.** Notes indexes record the
  frontmatter split they were built with (`index_versions`,
  `frontmatter_split` 2). The first `memfw notes index` (or memory hybrid index
  rebuild) after the upgrade re-reads every note before anything is embedded,
  and `shareable_item` trusts no stored note frontmatter until then. Expect one
  slower run.
- **A broken `vault.yaml` fails recall wiring closed.** A `vault.yaml` that
  exists but cannot be read or parsed now raises `recall adapter: bad_config`
  (MCP `isError: true`) for `memory_search mode=two_stage`, strict
  `memory_context` (MCP and HTTP `/context`) and the open-question queue and
  labels, instead of falling back to defaults. A missing `vault.yaml` still
  means defaults; a malformed key gives `bad_lifecycle_source` or `bad_notes`.
- **Event log forward compatibility.** An unknown event kind is accepted only
  with `details.projection: "none"`; an unmarked one is refused as in 0.3.0.
  A later writer that knows a marked kind must still compact it in the foreign
  format ([`docs/attachments.md`](docs/attachments.md)).
- **Stricter evidence and private locations.** A new `att:` pointer that does
  not resolve is refused (0.3.0 stored it as an opaque string). The
  conversation store, worker state and handoff folders now refuse
  `~/Documents`, `~/Desktop` and synced-folder symlink targets.
- **HTTP.** Without a `MemFW-Contract: memfw-http/3` header every
  `memfw-http/2` answer is byte-identical; the read-only service still deploys
  as script + `readonly.py` + `read_path.py` (`READ_PATH_VERSION` 2).

### Known limitations

The T1–T7 tests found gaps that stay open in 0.4.0 (fixes after the freeze,
except bug fixes): `two_stage` has no built-in notes provider and returns no
attachment segments, there is no note → citing-memories lookup and no broken
`kh:` link checker, short queries skip non-lexical adapters, a bare
`Coordinator` treats unlabeled memories as public, and one total deadline
covers recall and resolve. See
[`docs/ports-and-recall.md`](docs/ports-and-recall.md) §4.1c-bis.

### Tests — knowledge base ↔ memory integration (T1–T7)

- `tests/integration/test_kb_memory_t1_t7.py`: the T1–T7 scenarios that gate
  v0.4.0, on synthetic committed fixtures (`tests/fixtures/kb_memory`: a small
  notes tree, memories citing it, an eval question set, recorded OCR output).
  T1 unified recall over memories, `kh:` note sections and attachment proxy
  segments with R@10; T2 `kh:` evidence both ways and broken links; T3
  referenced PDF/PNG → OCR proxy → locator → original + hash; T4 the evidence
  gate with a note and an attachment; T5 `local-private` leaks = 0 over
  notes, HTTP, OKF and MCP plus the off-host embedder/reranker gate; T6 the
  builtin vs QMD notes overlap report (not a gate); T7 QMD or Qdrant down →
  FTS, flagged degraded. No network (loopback fakes only); no code changes (#106).
- Known limitations the tests work around are recorded in
  [`docs/ports-and-recall.md`](docs/ports-and-recall.md) §4.1c-bis: no built-in
  notes recall provider for `two_stage`, no attachment segments in `two_stage`,
  no note → citing-memories API, no broken `kh:` link checker, short queries
  skip non-lexical adapters, a bare `Coordinator` treats unlabeled memories as
  public (the runtime clearance applies the vault default), and one total
  deadline for recall and resolve (an adapter timeout at or above it can drop
  every hit when its service hangs).

### Fixed — OCR proxy adapter keeps proxies with control characters (`memfw-adapter-ocr` 0.1.1)

- `memfw-adapter-ocr`: segment text from an engine (a real `pdftotext` text
  layer, say) that held a form feed, another C0/C1 control, a bidi override or
  a zero-width mark made MM2a's parser refuse the whole proxy
  (`OcrError("proxy_format")`), so the document got no proxy at all. Such
  characters are now replaced with a space (controls, bidi) or dropped
  (U+200B, U+200E, U+200F, U+061C, U+FEFF) per segment, the same set
  `memfw.ports.proxies.clean_text` refuses; clean text is unchanged. Found by
  the T1–T7 real-data run (2 of 8 text-layer PDFs) (#107).

### Added — runtime recall wiring before v0.4.0 (wiring PR, part B)

- `vault.yaml` `runtime.recall.lifecycle_source` (default unset): the lifecycle
  meta-index path (production keeps it outside the vault, at
  `<base>/status/memory-meta-index.json`). Set, it is the meta-index of every
  MCP `two_stage` resolve (FTS, hybrid and adapter providers), of
  `memory_context` `strict` (MCP and HTTP `/context`), of the open-question
  queue and labels (`memfw.recall.builtin.document_meta`) and of `memfw dream`
  when `--lifecycle-source` is not given. The C4 context evaluation reads a
  private copy of an external `lifecycle_source` (symlinks resolved) and
  fingerprints it like a store. A `vault.yaml` that cannot be read fails
  closed (`bad_config`). Unset: byte-identical to before (harness test
  against the base ref, with and without a clearance, HTTP `/context`
  included).
- `runtime.recall.notes` (`root`, `index`, optional `legacy_privacy`, `tenant`;
  default unset): registers `NotesSegmentResolver` as the `kh:` resolver of
  `two_stage`, so QMD notes and PageIndex `kh:` refs come back as note sections
  under `notes`, apart from memory hits; its clearance is the server's, else
  `public,shared`. A malformed key raises `RecallAdapterError`
  (`bad_lifecycle_source`, `bad_notes`).
- MM2b on the #98 hook: `ProxySegmentResolver(index, tenant=...)` returns
  `SegmentResolution(source="attachments", tenant=<bound tenant>)` and leaves
  another tenant's segment unresolved; `default_resolvers(extra=...)` takes
  extra ref kinds (`attachment_segment`); the `att:` docstrings now describe
  the `attachment_segment` ref kind.

### Fixed — kernel privacy and lifecycle before v0.4.0 (wiring PR, part A)

- Notes frontmatter: a `---` line inside a YAML block scalar (`|`/`>`) no
  longer ends the frontmatter, so a later `privacy: local-private` is read as
  frontmatter, not body (it used to make the note public to `notes_search`,
  the `kh:` resolver and the PageIndex/QMD adapters). A column-0 `---` closes
  the block as before (ordinary notes split byte for byte as before); an
  indented first `---` is resolved with YAML, and an ambiguous close fails
  closed: `local-private`, or `unknown` when any reading was already unknown
  or unparsable (PageIndex trees, which hold only the three levels, store
  `local-private`). Quoted/explicit/merge privacy keys, unparsable readings,
  more than 64 candidates or an oversized block all count as ambiguous
  (bounded time). Notes indexes re-read every note once after the upgrade
  (`index_versions`), so frontmatter stored by the old split is never
  trusted. `memfw-adapter-pageindex` and `memfw-adapter-qmd`
  (both 0.1.1) mirror the rule; parity tests. `split_frontmatter` and
  `note_file_inside_root` are now public (old private names kept as aliases).
- Recall lifecycle join: a boolean `lifecycle_override` (the real meta-index's
  flag) is ignored instead of read as an unparsable lifecycle that the strict
  policy dropped; a string one is ranked as before.
- One lifecycle meta-index reader (`memfw.projection.load_lifecycle`) for
  recall and `memfw dream`, cached on (device, inode, mtime, ctime, size) with
  deeply read-only items; a default
  path that is a directory now counts as unreadable for dream too (fail closed).
- `memory_search` `hybrid`/`vector` (and their internal FTS fallback) apply the
  MCP clearance before `memory_access` and autowarm: a withheld hit is neither
  logged nor warmed (as `two_stage`). No clearance: unchanged.
- Embeddings stay local unless allowed: an embedding endpoint that is not a
  loopback address (`MEMFW_EMBEDDING_URL`, the `notes` commands' and
  `memfw-notes-mcp`'s `--endpoint`, `memory-related-build` / `notes
  related-build --embedding-url`) receives only `public`/`shared` memories and
  notes and no query text; a notes reranker (`--rerank-endpoint`) or query
  rewriter off this machine is skipped (`rerank_withheld`/`rewrite_withheld`),
  and a notes clearance now applies before reranking. `hybrid`/`vector`
  searches then answer from FTS (`degraded`, `semantic_query_withheld`, and
  `semantic_partial` when vectors are missing); stored vectors of private
  items are dropped on the next rebuild. Opt in with
  `MEMFW_EMBEDDING_ALLOW_REMOTE=1`, `runtime.embedding.allow_remote: true`
  (vault commands) or `--allow-remote` (every command that takes an
  endpoint). Loopback endpoints are unchanged. **Upgrade note:** a remote
  embedding endpoint without the opt-in means no semantic search.
- `memory_remember` suggestions are bounded to the caller (tenant, allowed
  sources, privacy clearance); only a suggestion that is shown is warmed and
  access-logged (and only with `record_access` on).
- Docs: `docs/attachments.md` states that a later version which knows a marked
  kind must still compact it in the foreign format (`details` + `event_line`).

### Added — GraphRAG deriver adapter (wave 4, off by default)

- New workspace package `packages/adapters/memfw-adapter-graphrag` (0.1.0,
  `memfw_adapter_graphrag:SPEC`, entry point `memfw.adapters`): GraphRAG 3.3.0
  (MIT) as an offline batch `Deriver` (kinds `graph` and `reports`).
  `stage_input` copies only `public`/`shared` documents of a `memfw projection
  export` generation (manifest class, file hash and the vault's current
  privacy all checked) to `<root>/input/<id>.v<version>.<hash>.txt` (the vault
  file's own bytes); `run_index` runs `graphrag index` as a subprocess only
  when called, over a `settings.yaml` the adapter writes itself (explicit
  literal-loopback model endpoints unless `cloud: true`, CSV tables, fixed
  directories; refused if changed or if `.env` exists), with a minimal
  environment and `DO_NOT_TRACK=1`, only for GraphRAG 3.3.0, killing the
  process group on timeout; `derive` reads the CSV output tables back, keeping
  only rows whose every source (community reports: through their entities,
  relationships, covariates and child communities) is a requested, shareable,
  current memory. Caps `inference` / causal `association`; `candidates(view)`
  yields staging candidates (`causal: none`). Tests never run GraphRAG. Docs:
  `docs/adapter-graphrag.md`. memfw never imports it; default output is
  unchanged.

### Added — LightRAG deriver adapter (wave 4, off by default)

- New workspace package `packages/adapters/memfw-adapter-lightrag` (0.1.0,
  `memfw_adapter_lightrag:SPEC`, entry point `memfw.adapters`): a `Deriver`
  (kinds `graph` and `answer`) over an operator-run `lightrag-server` 1.5.7
  (MIT), literal-loopback HTTP only (proxies ignored, redirects refused), API
  key read from the environment variable the settings name. Before any sync or
  query the server's `/health` must show release 1.5.7 and loopback LLM and
  embedding hosts (unless `cloud: true`). `sync()` deletes stale server copies
  and sends only `public`/`shared` memories as `<id>.v<version>.<hash>.md`; a
  graph row is kept, and an answer (`naive` mode only) accepted, only when
  every source is a requested, still-shareable, current memory. Caps `inference` / causal `association`; `candidates(view)`
  yields staging candidates (`causal: none`). Docs: `docs/adapter-lightrag.md`.
  memfw never imports it; default output is unchanged.

### Added — LangExtract deriver adapter (wave 4, off by default)

- New workspace package `packages/adapters/memfw-adapter-langextract` (0.1.0,
  `memfw_adapter_langextract:SPEC`, entry point `memfw.adapters`): a `Deriver`
  (kind `entities`) over LangExtract 1.7.2 (Apache-2.0, operator-installed,
  checked at runtime). Only `public`/`shared` memories are sent; the view is
  canonical JSON of character-grounded entities and relations, capped at
  `inference` / causal `none`; `candidates(view)` turns rows into staging
  candidates. The model is a memfw `ModelBackend` (bundled loopback-only
  Ollama backend; a non-local one needs `cloud: true`) inside a per-run token
  budget. Contract tests replay output recorded under the pinned release; see
  `docs/adapter-langextract.md`. memfw never imports it; default output is
  unchanged.

### Fixed — Qdrant adapter maps a broken HTTP answer to unreachable (memfw-adapter-qdrant 0.1.2)

- `http.client.HTTPException` (for example `IncompleteRead` on a malformed or
  cut-short chunked body, or a bad status line) is now `unreachable` in
  `QdrantClient.call` (so the coordinator reports the provider degraded) and
  `embedding_unreachable` in `EnvEmbedder.embed`, instead of escaping as an
  unexpected error.

### Fixed — deterministic usage-retention cost test

- `test_a_write_at_the_row_cap_does_not_scan_the_table` no longer times the
  write (a 20 ms wall-clock bound failed on a loaded CI runner). It now counts
  work: both retention `DELETE`s traced from the real write must plan as a
  `SEARCH` on `used` (an index or rowid range, never a `SCAN` of the table),
  nothing may be sorted (`TEMP B-TREE`), and the whole write must stay under
  3 SQLite VM steps per capped row (about 200k today; the old `NOT IN`
  delete took about 1.4M).

### Added — Qwen Code reader; reader coverage notes

- New conversation reader `qwen` (alias `qwen-code`) for the Qwen Code CLI:
  `~/.qwen/projects/<project>/chats/<session>.jsonl`, `chats/archive/` and
  `subagents/<parent>/agent-<id>.jsonl` (labelled `subagent`, session id
  `<parent>:agent-<id>`). An archived session keeps one locator: moving it
  between `chats/` and `chats/archive/` never stores it twice, and the active
  copy wins when both exist. Thoughts are
  dropped, function calls become tool calls, tool results become `tool`
  turns, notification and goal-runtime prompts are `system` turns; a
  `script` / `scheduled_task` session source labels the session `automated`.
  Redaction is the store's, as for every reader. `qwen` edit tools
  (`edit`, `write_file`, `notebook_edit`) are known to the handoff pack. Other readers'
  output and `memfw history agents` entries are unchanged.
- `antigravity`: documented that a root with no `brain/<id>/.system_generated/logs`
  (only `tempmediaStorage`, empty `conversations/`, opaque `implicit/*.pb`)
  correctly yields zero sessions; no parser change.
- README: TRAE SOLO is listed as not readable (its agent database is not
  plain SQLite).

### Fixed — bm25s adapter data_dir checks (memfw-adapter-bm25s 0.1.1)

- A `data_dir` that names the vault in another case on a case-insensitive file
  system (`<parent>/VAULT/idx`) is now refused like one under the vault: each
  existing ancestor is compared with the vault by device and inode
  (`os.path.samefile`), besides the resolved names.
- A relative `data_dir` (or the default under `HOME`) is resolved once when the
  provider is built; `index_dir()` is always that absolute path, so a later
  working-directory change neither moves the index nor escapes the vault check.

### Fixed — Qdrant adapter response cap is deterministic (memfw-adapter-qdrant 0.1.1)

- Embedding and Qdrant responses are read through one bounded reader: a
  `Content-Length` above the 8 MiB cap is refused before reading, otherwise
  the body is read in chunks and refused as soon as the running total passes
  the cap. A cap breach is always `embedding_response_too_large` /
  `response_too_large`, never `embedding_unreachable`; a malformed embedding
  body is `embedding_bad_response`. The test fakes read the request body and
  ignore an early client close, which made the size test fail on Linux CI.

### Added — attachment search: proxy_segments, ProxyProvider, MCP tools (MM2b)

- Active proxies are searchable through their own index (`proxy_segments`,
  same tokenizer and two-character fallback as memory search), stored
  outside the vault (`~/.local/share/memfw/proxy_index/<key>.sqlite3`, 0600)
  and rebuildable from a read-only vault, filtered by tenant and privacy
  (strictest of proxy and attachment), without superseded proxies, inactive
  attachments or attachments past their retention. Memory search, HTTP, OKF
  and projections never read it. `drop_proxy_index` / `proxy_index_residue`
  are the hooks `attach purge` uses.
- Built-in recall provider `ProxyProvider` (`ref_kind="attachment_segment"`,
  locator in `extra`, never text) with `ProxySegmentResolver` returning
  `SegmentResolution(source="attachments")`, so segments land in
  `RecallResult.segments`, never as memory hits; not part of memory recall.
- `attach purge` now deletes the attachment's proxy files and drops the
  proxy index (`still_containing` reports leftovers; `recover` finishes an
  interrupted purge). Detached, missing and purged attachments leave
  `attachment_search`, `attachment_get` and originals.
- MCP `attachment_search` and `attachment_get`, listed only with
  `mcp.attachments: true` in `vault.yaml` (default off: `tools/list`
  unchanged). Results are re-checked against the proxy file, the attachment
  record and the MCP clearance. Originals only with
  `mcp.local_originals: true` (default off), after re-hashing the blob; the
  HTTP service never serves attachments.

### Added — attachment proxies: import format, locators, storage (MM2a)

- `memfw attach proxy import ATT-ID FILE` stores a text proxy (OCR,
  transcript, keyframes, extract, caption) of an attachment, from a JSON file
  in the new versioned import format `memfw.proxy` v1 (`memfw.ports.proxies`,
  documented in `docs/attachment-proxies.md`). `coverage` and
  `source_sha256` are required, and `source_sha256` must equal the
  attachment's sha256; `supersedes` replaces an active proxy of the same
  attachment (`proxy_supersede` event; the old file stays, inactive). Also
  `attach proxy list`, `show` and `budget`.
- Locators are stored in canonical units only: PDF points with a bottom-left
  origin, image pixels with a top-left origin, seconds for audio and video.
  Producers may write pixels at a DPI, top-left points, fractions or
  milliseconds (`coordinates`) and memfw converts. Pages, boxes and times
  outside the dimensions the producer declares in `source` are refused and
  nothing is stored (memfw does not read the original; record-level
  dimensions are a later step). PDF boxes are in unrotated page space.
- Proxies live in `attachments/proxies/<prx-id>.md`, default to
  `local-private`, can never be less private than their attachment, and
  have their own budget (`attachments_proxy_max_bytes`, 64 MiB): they are
  excluded from the vault's physical size, so they never make a forgetting
  policy pick a memory. `fsck` checks them; `memfw recover` removes an
  uncommitted one. `proxy_add` / `proxy_supersede` events carry
  `projection: "none"`.
- Ports API (still the unreleased `0.3.0` that K3 started):
  `memfw.ports.proxies` (format, `Locator`, conversions, bounds),
  `memfw.ports.testing.check_proxy_import` for adapters, and the recall ref
  kind `attachment_segment` (`<prx-id>:<n>`, extra key `locator`) for MM2b.
- No output changes for a vault without proxies.

### Added — Qdrant recall adapter

- `packages/adapters/memfw-adapter-qdrant` 0.1.0 (wheel `memfw_adapter_qdrant`):
  a semantic `RecallProvider` over a Qdrant **v1.20.x** service (Apache-2.0),
  REST with the standard library (no `qdrant-client`). Loopback only unless
  `allow_remote: true`; loopback requests never use `HTTP(S)_PROXY`;
  redirects refused; API key from `api_key_env` only. Queries are embedded
  with memfw's own embedder (`MEMFW_EMBEDDING_*`, no new model) and ask for
  `with_payload: ["memory_id", "tenant"]`, `with_vector: false`.
  `memfw-adapter-qdrant sync` writes only `public`/`shared` memories (a
  `clearance` with `local-private` is refused), stores no text and deletes
  points that are no longer sendable. Off by default (`runtime.recall.adapters`);
  `available()` reports `unreachable` and friends so the coordinator degrades
  to FTS (T7). Contract tests replay recorded v1.20.2 exchanges and use a
  loopback fake; no Qdrant in CI (`docs/adapter-qdrant.md`).

### Added — bm25s recall adapter and operator-configured recall adapters

- `vault.yaml` `runtime.recall.adapters` (off by default): a list of
  `{factory: "module:factory", options, weight, timeout_seconds}` entries the
  MCP `memory_search` `mode=two_stage` coordinator runs beside FTS (and
  hybrid). The runtime passes each factory its `options` plus `vault` and
  `embedder`; a bad entry fails `two_stage` closed with a fixed
  `RecallAdapterError` code, an adapter that cannot serve is reported as
  degraded while the others answer. Without the key, nothing is imported and
  every output is unchanged (`docs/ports-and-recall.md` section 4.1b).
- `packages/adapters/memfw-adapter-bm25s` 0.1.0 (wheel `memfw_adapter_bm25s`):
  an in-process `RecallProvider` over memories with bm25s 0.3.16 (MIT) and
  jieba segmentation. Tenant filtering as FTS; only memories whose privacy
  level is in its `clearance` (default `public,shared`) are indexed. The index
  is built on demand outside the vault (`~/.local/share/memfw/adapters/bm25s/`,
  hashed tokens, no corpus), never writes the vault, and can be prebuilt with
  `memfw-adapter-bm25s build`. Contract, swap drill and standalone wheel
  coverage; `eval/retrieval_r0/bm25s_compare.py` reports it against FTS
  (`docs/adapter-bm25s.md`).

### Added — event log forward compatibility for marked non-memory kinds

- **Forward-compat contract for v0.4.x:** a v0.4.x reader accepts an event
  of a kind it does not know only when the event carries
  `details.projection == "none"`, a newer writer's promise that the event
  never changes memory state. Such events are kept byte-for-byte (also
  through `log compact`, whose summary keeps all their details and their
  canonical line), skipped by `replay`, counted by `log verify`
  (`unknown_non_memory_event_count`, `unknown_non_memory_event_kinds`),
  reported by `fsck` (`unknown_non_memory_events`, not an error) and
  refused by `rollback`. An unknown kind without the marker is still
  refused exactly as in 0.3.0. Kinds added after v0.4.0 that never change a
  memory must be written with the marker; the eight MM1a attachment kinds
  are known to every v0.4.x reader and stay unmarked (no on-disk change).
  Without such events every command's output is unchanged. See
  `docs/attachments.md` ("Unknown event kinds").

### Fixed — recall reads the meta-index lifecycle; `kh:` note refs resolve

- The recall vault resolver (`VaultIdResolver`, behind every `two_stage`
  provider) joins documents that carry a `fleet_path` tag to the lifecycle
  meta-index (`<vault>/status/memory-meta-index.json`, or the
  `lifecycle_source` passed to `default_resolvers`). Its `lifecycle`,
  `lifecycle_override`, `superseded_by`, `valid_until`, `valid_to` and
  `privacy` now reach the strict `FilterPolicy`, so a memory the meta-index
  marks retired, superseded or expired (or whose expiry does not parse) is
  dropped, and kept with a label under `include_retired` as documented. A
  failed join or an unreadable meta-index drops tagged documents under the
  strict policy (new reason `unjoined`). `compat_fts`, `mode=fts` and the
  default `two_stage` output are unchanged, as is everything for a vault
  without imported documents or without a meta-index, or whose meta-index
  has no such fields.
- `kh:<path>#<heading>` refs (QMD notes, PageIndex) can resolve to sections
  of the built-in notes index through the new `NotesSegmentResolver`, with
  the `notes_search` privacy rules (fail closed on unknown privacy, missing
  or stale notes, symlinks and path escapes). Sections come back as
  `RecallResult.segments`, apart from memory hits, and as a `notes` list in
  `memory_search mode=two_stage` only when there are any. Not registered by
  default. `SchemePathResolver` is the registration point for an `att:`
  resolver (MM2b); none is implemented here.

### Fixed — `/forget` replay after deletion respects clearance

- `POST /forget` (memfw-http/3) records the target memory's effective
  privacy level (`sensitivity` included) as `target_privacy` in the request
  header. Once the memory is deleted, an exact replay is `duplicate: true`
  only when the caller's clearance admits that level; a lower-clearance
  same-tenant process gets the same 404 `memory not found` as a
  never-recorded missing id, so it no longer learns that a hidden memory
  existed and was forget-requested. A request staged before this fix (no
  `target_privacy`) counts as `unknown` (fail closed). `memfw forget
  requests` rows gain `target_privacy` (`null` for such older requests);
  nothing else in its output changes.

### Fixed — out-of-vault projections know which vault they belong to; corroboration moves out of the vault

- **Vault identity.** The edge projection (K3) and the corroboration
  projection (U4) record the vault's identity in their meta table: its
  resolved path, `vault.yaml`'s `vault_id` when set, and the hash of its first
  event (`memfw.core.local_data.vault_identity`). `memfw graph match`,
  `memfw corroboration show`, MCP `memory_search` with `labels: true` and the
  open-question queue refuse a file built from another vault at the same path
  (before, deleting a vault and running `memfw init` there let the new vault
  read the old vault's edges, concept names included). The message is
  "projection belongs to another vault: rebuild"; MCP shows
  `corroboration: {"degraded": "projection_other_vault"}`, and
  `memfw project obsidian` leaves the counts out with a warning.
- **K4 live check:** a concept node that is neither a concept of the vault
  (file or alias) nor named in `about`/`applies_to` by a memory the caller may
  see is hidden. Only the memories linked to that node in the projection are
  re-read (cost grows with the node's degree, not the vault).
- **Corroboration moves out of the vault**, to
  `~/.local/share/memfw/corroboration/<key>.sqlite3` (0600, directory 0700),
  so it never counts toward the physical-size budget or the transaction
  fingerprint. `corroboration rebuild` prints the full path (before: the
  vault-relative `corroboration.sqlite3`), refuses any path inside the vault
  (before: inside `memories/`), and notes an old vault-root
  `corroboration.sqlite3` (`"legacy_file"` plus one stderr line); the old file
  is ignored and never deleted.
- Edge and corroboration rebuilds read the vault under its lock, so a rebuild
  beside a write no longer fails with "A pending transaction needs explicit
  `memfw recover`".
  On a read-only vault (the lock file cannot be opened) the vault is read
  without the lock, between two transaction fingerprints, retried up to 3
  times, then the rebuild fails with "try again".
- `memfw forget residue` checks the corroboration projection at its new
  place, and a leftover vault-root `corroboration.sqlite3` too (store
  `corroboration_legacy`, listed only when the file exists; remedy: delete it).
- `memfw concepts lint-relations` strips `memory:`/`mem:` from
  `superseded_by`, `evidence` and `experience.evidence`, as the edge builder
  does (before: a prefixed reference was reported dangling, and a prefixed
  cross-tenant one was missed).
- Docs (`docs/ontology.md`): moved, symlinked and `$HOME`-rooted vaults, and
  the `max_expansions` limit with many hidden neighbours.
- **Deploy:** the edge projection changes schema (`1` → `2`) and the
  corroboration projection changes place (and schema). After upgrading, run
  `memfw graph rebuild` and `memfw corroboration rebuild` on every deployed
  vault that uses them: until then `graph match` answers "another schema
  version: rebuild it" and corroboration shows `projection_missing`. Nothing
  is deleted: the edge rebuild replaces its own file in place, and a
  vault-root `corroboration.sqlite3` stays until you delete it by hand (it
  still counts toward the vault's size until you do).

### Added — OCR proxy adapter (MM4, `memfw-adapter-ocr` 0.1.0, off by default)

- New workspace package `packages/adapters/memfw-adapter-ocr`: turns an
  attachment's original (PDF or image) into a proxy import file of text
  segments with multimodal locators (PDF points, bottom-left origin; image
  pixels, top-left origin), `coverage`, `source_sha256`, producer and
  per-segment confidence when the engine gives one.
- Engines sit behind the adapter's own `OcrBackend` interface: `mcp` (an OCR
  server named in the settings, run as a child process over the MCP stdio
  protocol; synchronous OCR tools by default, async jobs only with
  `allow_async_jobs`; private `out_dir`, a hard deadline with a watchdog, the
  whole process group stopped at the end, a page budget), `pdftotext`
  (text-layer PDFs, capped and page-limited) or any `module:factory` (outside
  the local-only guarantee). Engine paths come only from configuration.
- Local only, nothing uploaded; only privacy levels in `clearance` (default
  `public`, `shared`) are handed to an engine; the original's sha256 is checked
  before and after; the engine reads a private copy.
- `memfw-ocr-proxy run | check`, and the `ocr` deriver through the
  `memfw.adapters` entry point (disabled until listed). The output is MM2a's
  `memfw.proxy` v1 import format; constants and validation come from
  `memfw.ports.proxies` (MM2a, #91) when the installed ports have it.
  memfw does not import the package; default output is unchanged.

### Added — attachments as observation evidence (MM3)

- `att:<id>[#locator]` evidence is the new `observation` class. A memory whose evidence
  is only observations (no raw data or link) cannot be `epistemic: fact` and stops at
  `causal: hypothesis`; the cap runs before the gate's early return. Observations are
  not outside-vault evidence, so they never carry `causal: verified` on their own.
- The repository layer (`memfw.storage.evidence`) checks `att:` pointers a write adds
  (attachment committed and readable; locator fits the media type: PDF `page`[+`bbox`],
  image `bbox`, audio `t`, video `t` or `frame`[+`bbox`]) and classifies `file:`/
  `sha256:` (and other raw/link) pointers that name a registered attachment — by hash or
  prefix, blob path, a copy's content, recorded location, embedded id, or a digest in
  any common hex/base64 spelling inside any pointer — as observation,
  after folding case, percent-encoding, NFKC and invisible characters. The gate stays
  pure (`validate_epistemic_write(..., evidence_classes=...)`).
- Legacy rule: a stored `verified`/`fact` memory that the rule would now refuse is not
  refused on writes that keep its level; a write that raises the level reclassifies all
  of its evidence. `memfw fsck` reports `evidence_observation_review` and
  `evidence_attachment_invalid`.
- Backlinks: `attachment_backlinks` (read-only); `memfw attach show` `cited_by` now
  matches every spelling.
- `memfw eval causal --multimodal`: 59 synthetic cases (spurious multimodal causal
  claims, 37 bypass attempts, hygiene, legacy/update cases, controls); 0 spurious
  promotions, 0 fact upgrades, every bypass blocked. Write-time hashing of cited files
  is bounded (size must match an original; 32 files / 64 MiB per write; fail closed).
- **Changed:** a new `att:` pointer that does not resolve is now refused (it was stored
  as an opaque string). Memories without `att:`/`file:`/`sha256:` evidence (or without
  registered attachments) are gated exactly as before.

### Added — QMD recall adapter (R6 PR-2a, off by default)

- New workspace package `packages/adapters/memfw-adapter-qmd` (0.1.0,
  wheel `memfw_adapter_qmd`, stdlib plus pinned PyYAML 6.0.3, nothing else
  of memfw than `memfw-ports`):
  two `RecallProvider`s over a running QMD 2.5.3 (MIT) MCP server, `qmd`
  (memory projections → memory ids through the projection manifest) and
  `qmd-notes` (Knowledge-Hub notes → `kh:<path>#<heading>` refs). They return
  references, scores and a source label only; hits that do not map to a
  visible document are dropped, and `local-private`/`unknown` are never
  returned. Loopback endpoint by default, proxy bypass for loopback, no
  redirects. When QMD is unavailable or times out the coordinator reports it
  degraded and FTS answers. memfw does not import it; with no adapter config
  nothing changes. QMD's MIT notice ships in the wheel
  (`memfw_adapter_qmd/THIRD_PARTY.md`).
- Contract tests replaying fixtures recorded from a live QMD 2.5.3 over a
  throwaway synthetic index (`tests/fixtures/qmd_contract/`, lexical only:
  no embeddings; CI uses a loopback fake, no real QMD), the
  standalone-wheel smoke (`--qmd`) and the T6 consistency report
  `scripts/qmd_notes_consistency.py` (builtin notes index vs the adapter;
  overlap only, not a gate).

### Added — PageIndex recall adapter (`memfw-adapter-pageindex` 0.1.0, off by default)

- New workspace package `packages/adapters/memfw-adapter-pageindex`: a
  `RecallProvider` for long documents that searches PageIndex-style
  (VectifyAI/PageIndex, MIT; tree format of PageIndex 0.7.0, no PageIndex code
  imported or copied). A `ModelBackend` reads each document's table-of-contents
  tree (titles, node ids, page ranges, optional short summaries; never body
  text) and names the sections; `mode: titles` ranks titles without a model.
- Returns path refs `kh:<path>#<heading>` and `att:<id>#page=<n>` (the page
  part of the multimodal locator) with `extra` limited to `kind`, `page`, `why`.
  `kh:` refs follow `parse_kh_ref`, so a registered `NotesSegmentResolver`
  resolves them; with the default resolvers (and for `att:` until MM2b) the
  coordinator drops them as `unresolved`.
- Gates: a non-local backend needs `cloud: true` in the adapter settings;
  `local-private` trees are never sent to a non-local backend; a per-run token
  and call budget; a backend must declare the `pageindex_tree_search` task.
- `memfw-pageindex build-md | import-tree | list`: explicit offline tree
  building (Markdown headings) and import (PageIndex PDF output, `text`
  dropped), stored as `0600` JSON outside the vault. A note's privacy is read
  from its frontmatter with `yaml.safe_load` (`PyYAML==6.0.3`) and
  `memfw.ports.privacy.metadata_level`; anything unreadable is `local-private`.
- Registered through the `memfw.adapters` entry point `pageindex`; disabled
  until listed. memfw does not import it; default output is unchanged.

### Added — two-stage conversation search and tag filters (M3c)

- `memfw conversations search` and MCP `conversations_search` gain tag
  filters over the worker's `conv-v2` tags: `--task-type`, `--outcome`,
  `--project`, `--component`, `--intent-like` (MCP `task_type`, `outcome`,
  `project`, `component`, `intent_like`). A session without tags (its newest
  session summary untagged) never matches one.
- **Changed:** MCP `conversations_search` now honours `archived_only` and
  `with_archive_state` (before, it ignored them and answered as without).
- `--mode two_stage` (MCP `mode`): ranked sessions from the union of the
  keyword search, a semantic match over "summary + tags" (the runtime's
  existing embedder; `degraded: ["no_embedder"]` without one; only a
  loopback embedder or `conversations.cloud: true`, else `semantic_off`;
  8 s budget, no retries, bounded packed cache) and the tag
  filters, fused by reciprocal rank, each list cut to the caller's visible
  apps before ranking or counting. Per session: summary and tags labelled
  `untrusted_model_output: true`, archive state and redacted turn windows
  around hits (`windows`, `window_turns`), all within `max_bytes`.
- `WorkState.session_labels` (read-only). Without the new options the output
  of every command and tool is byte-identical to before.

### Added — `memfw-http/3`, `/forget` and `forget residue` (R5/R6)

- **`memfw-http/3` beside `memfw-http/2`; `POST /forget` and `memfw forget
  residue` (roadmap R5/R6).** The read-only service selects the contract per
  request from the `MemFW-Contract` header; without it (or with
  `memfw-http/2`) every answer is byte-identical to before, `/forget` and
  `/context` included (404). An unknown value is 400 `contract is not
  available`. Every `/health` answer carries `MemFW-Contracts: memfw-http/2,
  memfw-http/3`, and the v3 body adds `contracts`, `modes` and `endpoints`.
  v3 `/search` adds `recorded_at` to each hit and keeps `mode` to the served
  vocabulary (`fts`); v3 `/related` adds `why` and `provenance_type`; v3
  `POST /context` returns the `memory_context` v2 pack (`core` slot) built
  read-only through `memfw.runtime.http` (no cache, no access events, no usage
  signals; needs the `memfw` package and a configured
  `MEMFW_READONLY_CLEARANCE`, else 404 and left out of `/health`
  `endpoints`). v3 `POST /forget` (write
  token only) records one deletion-request candidate under
  `<vault>/_candidates/forget/` and never deletes; the target must be visible
  to the caller (tenant and clearance), hidden and missing targets answer the
  same 404, and `request_id` makes it idempotent within the tenant (replay
  `duplicate: true`, different content 409). The candidate file is
  `forget-<sha256(tenant)[:16]>-<request_id>.md`, so request ids never
  collide across tenants, and while the memory exists the visibility check
  runs before the duplicate/409 check: another tenant or a lower clearance
  replaying a recorded request gets the same 404; a recorded `request_id`
  reused with a missing, another tenant's or a withheld memory id is that
  same 404 too, never 409. At most 1000 requests per tenant
  wait for review; beyond that the tenant's new one is 429 `too_many_requests`. Tenant binding and the MM0 clearance (sensitivity
  key included) apply to every v3 addition. `memfw forget requests` lists the
  requests; `memfw forget residue <id>` checks, read-only, that a forgotten
  memory is gone from the FTS index, the related, corroboration, usage and
  (optionally) entity projections, naming the rebuild for each store that
  still carries it (exit 0 clean, 1 residue, 2 usage error, 3 the memory is
  still live). Hard delete is unchanged. New v3 sample set in
  `tests/fixtures/contract_v3/`; the v2 set is unchanged, and
  `tests/fixtures/contract_v2_raw/` pins the raw v2 bytes (status line,
  headers, body) recorded from the service before v3.

### Added — `current` context slot (U6, off by default)

- `memory_context(v=2, slots=["current"])` and `memfw://context/current`: short-lived
  context (active ticket, meeting, current task) kept apart from `core`. Memories tagged
  `current` that are valid now (`valid_from`/`valid_to`, else a TTL of
  `runtime.context.current_ttl_days`, default 7, from `created_at`, which mechanical
  writes never refresh; a `created_at` more than 5 minutes in the future is not current),
  not superseded, newest first, with `core`'s budget, overflow and visibility (policy, tenant, sources, MM0
  clearance). New `memfw.context.CurrentSlotProvider`. Never cached, so an expired memory is
  never served. Enabled by `vault.yaml` `runtime.context.current: true`; off, `tools/list`,
  `resources/list` and every pack are byte-identical and the slot is unknown. See
  `docs/ports-and-recall.md` §4.3a.

### Added — typed-edge projection and path patterns (K3/K4)

- **K3 typed-edge projection and K4 bounded path patterns.** `memfw graph
  rebuild` builds the edge projection outside the vault, at
  `~/.local/share/memfw/edges/<key>.sqlite3` (keyed like the usage
  projection, via the shared `memfw.core.local_data` rule; 0600, rebuildable,
  never writes the vault, never counted by the physical budget or the
  transaction fingerprint; a vault-root `edges.sqlite3` from a pre-release
  build is ignored and noted, never deleted) from the relation table's fields: `about`, `applies_to`, `broader`,
  `superseded_by` (concept `supersedes` read backwards), `evidence` with
  `observed_in`/`cites`, causal and experience links, `related`, concept
  relations, and the open-question `conflicts`/`resolved_by` (two new rows in
  `relations.yaml`). Each edge carries both ends' tenant and privacy level;
  cross-tenant edges are never built. `memfw graph match --from X --path
  "about>broader*1..2><about"` follows a bounded path (at most 4 hops, capped
  fan-out and results, deterministic order, each edge once per path,
  optional `--acyclic`), filtered by tenant, clearance (default
  `public,shared`) and sources, with every memory re-checked against the live
  vault; a hidden node never shows and a hidden start answers like a missing
  one. New L0 port `ProjectionStore` (`rebuild`/`version`/`query` with a
  keyset `after` cursor so a match pages past hidden edges, contract
  `check_projection_store`, SQLite and in-memory implementations) and
  `memfw.ports.patterns`; `PORTS_API_VERSION` and the `memfw-ports` /
  `memfw-recall` wheels move to 0.3.0;
  `memfw-adapter-deepseek` moves to 0.1.1 (its `memfw-ports` pin is now
  0.3.0). No MCP tool; existing outputs are
  unchanged. K2 follow-ups: relation rows are immutable and hashable, the
  loader refuses `version: true` and non-string node kinds, `import-seed`
  copies only a well-formed `broader`, and `broader_on_merged` is reported.
  See `docs/ontology.md`.

### Added — C4 context pack evaluation

- **C4 context pack evaluation** (`memfw eval context SNAPSHOT`): coverage,
  waste, P50/P95 latency and token cost of the `memory_context` slots
  (`core`, `handoff`, `topics`, and `current` when the snapshot enables it or
  `--current` simulates it) and of the whole pack, swept over per-slot caps and simulated resident-prefix caps
  (default 1000/2000/4000/6000). Runs the real slot providers on a private
  copy of a read-only snapshot (refuses one that needs `memfw recover` or an
  output inside it; fails if the snapshot or a store changes during the run);
  later searches come from `memory_access` events, C5 `memory_used` signals or
  a seeded synthetic query set; the JSON/markdown report holds counts, ratios
  and timings only and is deterministic apart from timings. No default changes.
  `share_feed` takes an optional `source` (default unchanged).
  See docs/evaluation.md, "C4 context pack evaluation".

### Added — attachments: blob store, records, `memfw attach` (MM1a)

- Originals are stored once per sha256 as blobs **outside the vault**
  (`<attachments_blob_root>/<vault_id>/<sha[:2]>/<sha>`, default root
  `~/.local/share/memfw/blobs`; directories 0700, files 0600; a git work
  tree, the vault, synced folders and symlinked roots are refused). The vault
  keeps one small record per registration, `attachments/<att-id>.md`
  (`att-<uuid7>`; never under `memories/`), with `privacy` (default
  `local-private`; a missing field reads `local-private`; tags and
  `sensitivity` only tighten).
- `memfw attach add FILE` (`--embed`, `--privacy`, `--media-type`,
  `--origin`, `--note`, `--captured-at`, `--retention`, `--rights`, `--tag`),
  `attach list [--json]`, `attach show ID` (with `cited_by`: memories whose
  evidence is `att:ID`) and `attach verify [ID]...` (re-hashes records and
  blobs, writes nothing, exit 1 on a missing or changed one). Limits
  `attachments_max_bytes` (25 MiB) and `attachments_max_total_bytes` (2 GiB)
  in `vault.yaml`; audio and video need `--embed`. No exit serves
  attachments yet.
- Event kinds `attach` (written) and `attach_update`, `detach`,
  `attach_purge`, `attach_missing`, `attach_relink`, `proxy_add`,
  `proxy_supersede` (reserved). One event manifest,
  `core.models.MEMORY_EVENT_KINDS` / `NON_MEMORY_EVENT_KINDS`, replaces the
  two skip lists in `replay()` and the segment snapshot; a test makes every
  new kind pick one. `fsck` reports attachment problems
  (`attachment_<status>`) and never registers an `external_delete` for them;
  orphan blobs are reported, never deleted. The first attach adds `vault_id`
  to `vault.yaml` (one `config_change`).
- `vault_id` is a random UUID generated once under the vault lock
  (concurrent first adds agree on one); originals are staged in
  `<blob root>/.staging` and moved into the vault's blob directory under the
  lock. `attachments_blob_root` must be absolute or `~`-prefixed. `~/Documents`,
  `~/Desktop` and the targets of symlinked synced-folder markers are refused
  as blob roots; a refused root writes nothing, not even `vault_id`. Record
  frontmatter is delimited by whole `---` lines and every record is parsed
  back before it is written. `attach list`, `add`, `verify` and `fsck` read
  the event log once per operation. `verify`/`fsck` report stale staging
  files (`blob_stage_stale`, a warning), and `fsck` checks the blob directory
  as soon as `vault.yaml` has a `vault_id`.
- Without attachments every command's output is byte-identical
  (`tests/test_attachments_compat.py`). **memfw 0.3.0 fails closed on a vault
  with an `attach` event** (every command that reads the event log). **No
  downgrade to 0.3.0 after the first `attach add`; upgrade all hosts that read
  the vault before first use.** See [`docs/attachments.md`](docs/attachments.md).

### Fixed — concurrent first attachment adds no longer fail on directory creation

- Two processes adding the first attachment at the same time could both
  find the blob root (or `<root>/.staging`, `<root>/<vault_id>`, a shard
  directory) missing; the one that lost the `mkdir` race failed with
  `FileExistsError` and its add was lost. The loser now re-checks what the
  winner made: a plain directory is used (tightened to 0700 if looser), a
  symlink or a non-directory is still refused (`BlobLocationError`).
  Missing parents are made as before.

### Added — referenced attachments, detach, relink, purge, scan (MM1b)

- **Referenced originals:** `memfw attach add FILE --reference` (or
  `ROOT:PATH`) records `storage: referenced` with `location:
  <root>:<relative path>` and the original's sha256/size, copying nothing.
  Roots are named per host in `vault.yaml` (`attachments_roots: {kh:
  /mnt/notes}`). Audio and video inside a root default to a
  reference; outside every root they need `--embed`. The embed limits do not
  apply to references.
- **Path safety:** POSIX relative locations only (no `..`, `.`, empty
  component, backslash or control character); a filesystem path must lie
  lexically inside a root (never resolved first; `..` refused); opening walks
  from the root with `dir_fd` + `O_NOFOLLOW`, refusing any symlink component
  and anything but a regular file; each component must match the on-disk name
  exactly (case and Unicode normalization), so a location means one file on
  macOS and on Linux. Every refusal writes nothing.
- `memfw attach verify` re-hashes referenced originals: `referenced_missing`
  (with `candidates`: same sha256 under the same root, never relinked
  automatically), `referenced_modified`, `referenced_unsafe`,
  `referenced_unavailable` (root not mounted/configured; a warning).
  `--record-missing` appends `attach_missing`; `memfw attach relink ID PATH`
  accepts only a file with the same sha256 and size (`attach_relink`).
- `memfw attach detach ID` (soft, `detach` event; `list` hides it, `--all`
  shows it).
- `memfw attach purge ID --yes` (hard delete; without `--yes` only the plan:
  citing/mentioning memories for human decision, shared-blob notice, copies
  a purge cannot reach). Removes the record, the blob unless another record
  shares it (the original is then kept and the quota passes to an heir), this
  vault's stale stages, the memory index, and VACUUMs vault SQLite stores
  still holding the record's text. A referenced original is never touched.
  `memfw recover` finishes an interrupted purge or rolls back an interrupted
  relink. `purge --orphans --yes` reclaims unreferenced blobs (blocked while
  a committed record is unreadable).
- **Copies of a vault:** the blob directory carries an owner marker
  (`.owner`); purge, `purge --orphans`, purge recovery and adds refuse a
  directory another vault marked (a copy shares `vault_id`), and verify/fsck
  report `blob_store_foreign_owner`. `memfw attach reown --new-vault-id --yes`
  splits a copy off (new `vault_id`, blobs copied); `memfw attach reown --yes`
  re-owns after a move, and is refused while the vault path recorded in the
  marker still holds a vault with the same `vault_id` (`--force-take-over`
  overrides). The purge plan lists the attachment's MM2a proxies (MM2b
  removes them with the attachment); `scan` counts hard-linked
  files; a blocked `purge --orphans --yes` reports `purged: false`, exit 1.
- `fsck` only `lstat`s referenced originals (`fsck --hash-referenced`
  re-hashes); directory listings are cached per pass. An empty root or one
  whose every original is gone is `referenced_unavailable`, never missing;
  `attach_missing` is re-checked under the lock and a new known kind
  `attach_found` clears it. Roots may not be `/`, home, or contain the vault
  or blob root; originals with several hard links are refused; purge
  recovery scrubs SQLite stores like a purge; `scan` walks through
  `O_NOFOLLOW` directory fds.
- The embedded quota total is now the larger of the log total and the
  stored blob files' sizes, so orphan reuse and detach/re-add cycles cannot
  bypass `attachments_max_total_bytes`; log compaction keeps `heir` and
  `projection`.
- `memfw attach scan ROOT:[DIR]` is a dry-run inventory (counts, sizes, media
  types, likely-private by path, already registered, refused symlinks/names;
  `--hash` adds duplicate sha256 counts) that writes nothing.
- The MM1b event writes (`detach`, `attach_missing`, `attach_relink`,
  `attach_purge`, known since MM1a, and the new known kind `attach_found`) carry `details.projection: "none"`
  (event-log forward-compat marker) and never a location or name.
  See [`docs/attachments.md`](docs/attachments.md).

### Fixed — MM1a review follow-ups (attachment quota, staging, location guard)

- `attach add` no longer parses every attachment record under the vault lock
  to check `attachments_max_total_bytes` (400 attachments made an add ~10x
  slower). New `attach` events carry `details.size` and `details.blob`
  (`new` | `shared`); the total is summed from the log, and a log with an
  older `attach` event falls back to the sizes (`lstat`) of the stored blob
  files. No record is parsed whatever the number of attachments.
- Staging files under the shared `<blob root>/.staging` are named
  `.stage-<tag>-….tmp`, where `<tag>` is the first 16 hex of the sha256 of the
  vault's resolved path (a moved vault gets a new tag, so its older stale
  stages are then only counted, never deleted). `attach verify` / `fsck` report only this vault's
  stale stages by name; other vaults' (and untagged) ones are a count
  (`attachment_blob_stage_foreign`, a warning). `BlobStore.reclaim_stale_stages`
  removes only this vault's.
- A blob root that changed between staging and the vault lock is refused
  before `<new root>/<vault_id>` is created (no empty directory is left).
  The unused `BlobStore.stage` is removed.
- The conversation store, worker state and handoff folders
  (`check_private_location`) now also refuse `~/Documents` and `~/Desktop`
  (macOS iCloud "Desktop & Documents") and a synced-folder marker's symlink
  target, the same rules as the attachment blob root.

## [0.3.0] — 2026-11-17

The second numbered release: a layered core behind swappable ports, recall and
context packs v2, epistemic types and reviewed experience points, uncertainty
as a first-class record, a concept taxonomy, per-memory privacy levels with
opt-in clearance on every exit, and a settled, optionally model-tagged
conversation store. New behaviour is off by default or additive: with no new
flag, setting or `vault.yaml` key, outputs match 0.2.0 except for the
fail-closed and security fixes and two `memfw-http/2` changes (`/related`
without a tenant answers 404 for a tenanted memory; `/stage` with a known
`proposal_id` but changed content answers 409). See the upgrade notes below.
The entries after this summary give the details, newest first.

### Summary

- **Architecture, ports and runtime.** Package layers L0 ports → L5 interface,
  enforced by `tests/test_architecture_layers.py` (shrink-only allowlist, 25
  entries → 1). `memfw.runtime` is the composition root; the MCP server only
  parses, calls the Runtime and serialises (A0–A4). The read-only service's
  read path moved into the library, `memfw.index.readonly` (A1). Eleven ports
  in `memfw.ports.PORT_VERSIONS` (`PORTS_API_VERSION` 0.2.0); ten have swap
  drills with two or more implementations through one contract kit, and
  `ModelBackend` is checked against the extractive and DeepSeek backends.
  Standalone `memfw-ports` / `memfw-recall` wheels (0.2.0) are built in CI
  (A5).
- **Recall and context.** Opt-in `memory_search mode=two_stage` through the
  recall coordinator (`compat_fts` or `strict` policy; providers never write
  the vault). `memory_context v=2` returns a versioned `ContextPack` with
  `core`, `handoff` and `topics` slots, also served as three MCP resources
  (C1, C2), with a per-process cache of ids and versions and `if_version`
  (C3). `memory_used` (C5, off by default) and a session-safe
  `record_access: false` switch so a session never writes the vault (C6).
  The backend-neutral recall projection `memfw projection export` (R6).
- **Epistemic and experience.** Optional `epistemic` type and `causal` grade
  with one write gate (a `verified` causal claim needs an identified,
  evidenced record and a human), reviewed `type: experience-point` memories
  staged through `memfw experience draft` / `from-github` / `invalidate`, and
  the pseudo-causality evaluation `memfw eval causal` (R3, #32; see
  `docs/epistemic-and-experience.md`).
- **Uncertainty.** `type: open-question` memories (U1), `open_question:<id>`
  and `conflicted` recall labels (U2), the corroboration projection and its
  display on recall rows plus `memfw corroboration queue` (U4, U5a), and dream
  turning conflicts into open-question candidates with `dream
  accept-question` / `reject-question` (U3).
- **Knowledge structure.** Concept `broader` parents with derived `narrower`
  (K1) and the versioned relation-type table `ontology/relations.yaml` with
  `memfw concepts relations` / `lint-relations` (K2).
- **Privacy.** A `privacy` field (`public`, `shared`, `local-private`), vault
  defaults `legacy_privacy_default` and `new_privacy_default`, and opt-in
  clearance on the HTTP service, OKF export, the notes MCP server and the
  memfw MCP server (MM0). A top-level `sensitivity:` key is read the same at
  every exit and kept on rewrite (#69). Fail-closed fixes: privacy and
  lifecycle tags in the strict policy, supersede keeps the tenant and scope.
- **Security and integrity.** Requests to a loopback or this-host service
  (local embeddings, the local relevance judge, model helpers) never go
  through an environment proxy, and the judge no longer follows redirects
  (#74). From the #35 audit: tenant-exact `/related` and MCP
  reads, `MEMFW_ALLOWED_SOURCES` on every MCP read, history tools honouring
  opt-out and `can_read`, the service bind guard and `/stage` tenant binding
  (S1–S9); vault integrity fixes (frontmatter ending only at a closing `---`,
  complete `rollback`, `delete` pruning body objects, event-summary hashes);
  service, MCP, index, CLI and OKF fixes.
- **Conversations.** Conversation store schema 2 (turn ownership per locator,
  migrated on first open) and reader fixes; a DeepSeek `conv-v2` tagging
  adapter behind the new `ModelBackend` port, **off unless
  `conversations.cloud: true` and a key are set** (M3a); settled ingest and
  archive state (`--settled-minutes`, M3e).
- **CLI.** Open questions from `memfw add` / `update`, `--json` on `list`,
  `show` and `index search`, grouped `--help` panels, and
  [`docs/cli-mcp-parity.md`](docs/cli-mcp-parity.md).
- **OKF and Obsidian.** `memfw okf import` stages a bundle's concepts as
  candidates (never the vault); `memfw project obsidian` writes a read-only,
  deterministic Obsidian board (O1a).

### Compatibility / upgrade notes

- **Upgrade every host that reads or writes a shared vault together.** memfw
  0.2.0 reads 0.3.0 files without error, but it lists every key it does not
  know in `ignored_keys` and **drops it on rewrite**. The memory fields new in
  0.3.0 are `epistemic`, `causal`, `causal_record`, `limitations`,
  `experience` (R3), `open_question` (U1), `privacy` (MM0) and the top-level
  `sensitivity:` key (#69). So a 0.2.0 rewrite:
  - loses the epistemic type, causal grade and record, limitations and the
    experience-point record;
  - loses the `open_question` block (the memory keeps `type: open-question`;
    `fsck` then reports `open_question_missing_block`, and a person can restore
    the block with `memfw update ID --question … [--conflicts …]`);
  - loses `privacy` and the top-level `sensitivity:` key, which loosens the
    memory to the vault's legacy default.

  Concept files keep `broader` under 0.2.0 (it lints it as
  `unknown_relation`). 0.2.0 accepts and keeps the new `vault.yaml` keys.
- **No downgrade after compaction.** Once 0.3.0 compacts a vault's event log,
  memfw 0.2.0 can no longer read that log. A vault 0.3.0 never compacted stays
  readable by 0.2.0.
- **`memfw-http/2` clients.** `/related` without a `tenant` sees only
  empty-tenant memories: asking about a tenanted memory answers 404 and other
  tenants' neighbours are dropped, so clients must send the tenant they read.
  `/stage` with an existing `proposal_id` but different content answers 409
  `conflict` instead of `duplicate: true`. Clients must handle both.
- **Read-only HTTP service.** Deploy `scripts/memfw_readonly_service.py`,
  `src/memfw/index/readonly.py` and `src/memfw/index/read_path.py` from the
  same revision. `read_path.py` now carries `READ_PATH_VERSION = 2`, and the
  script and `readonly.py` refuse to start with an older sibling. Rebuild the
  service's mirror index (`memfw index rebuild` on the served root) after the
  upgrade; the service never writes.
- **Clearance needs the privacy defaults first.** Setting
  `MEMFW_READONLY_CLEARANCE=public,shared` (or `--clearance` on OKF export,
  the notes server or `MEMFW_MCP_CLEARANCE`) on a vault whose memories have no
  `privacy` field withholds all of them, because a missing field counts as
  `local-private`. Set `legacy_privacy_default` (and, if new memories should
  get a level, `new_privacy_default`) with `memfw privacy legacy-default` /
  `new-default` before enabling a clearance. Unset clearance filters nothing,
  as in 0.2.0.
- **Conversation store schema 2.** The first open by 0.3.0 migrates the store
  in one transaction (turn ownership backfilled, cursors cleared), so the next
  ingest re-reads every session once. The session fingerprint now includes a
  UTC-normalised `updated_at`, so the next conversation worker run may
  re-process sessions once: up to every session, namely those whose
  timestamps were not plain UTC or whose turns or offsets change on the
  re-read. Readers skip a store directory that is a symlink
  resolving outside its root (a warning names it); point the root at the real
  directory.
- **Cloud tagging is off; turn it on after the re-processing.** The DeepSeek
  adapter is a separate wheel; the worker uses it only with `conversations:
  {backend, cloud: true}` in `vault.yaml` and `DEEPSEEK_API_KEY` set.
  Otherwise the worker behaves as in 0.2.0. A session the one-time
  re-processing above picks up counts as *changed*, and `backfill_days` /
  `replan_limit` only hold back *unchanged* sessions, so those caps do not
  limit it. Run the worker on the default extractive backend after upgrading,
  before enabling the cloud backend; its re-planning of the then unchanged history is capped
  (30 days, newest 1000 sessions by default).
- **Expected stricter output.** Fail-closed fixes can hide rows 0.2.0 showed
  or keep records 0.2.0 dropped: `privacy:`, `sensitivity:` and `lifecycle:`
  tags match in any case and the strictest wins under the `strict` policy, an
  unrecognised privacy value is never admitted under a clearance,
  `supersede` writes the replacement in the predecessor's tenant, and a hard
  budget forget never deletes a live supersede chain.
- **Derived stores.** `corroboration.sqlite3` and the Obsidian output are
  projections you can delete and rebuild at any time; the `memory_used` store
  (outside the vault) is disposable. None of them is part of the vault.
- **Packages.** `memfw` 0.3.0 still ships `memfw.ports` itself and does not
  depend on `memfw-ports`. `memfw-ports` and `memfw-recall` are 0.2.0
  (`PORTS_API_VERSION` 0.2.0), `memfw-adapter-deepseek` is 0.1.0. Nothing is
  published to PyPI.

### Fixed — loopback requests never go through an environment proxy (security)

- `memfw.notes.http.open_http` (embeddings for notes, memory hybrid search
  and the notes model helpers) kept urllib's default `ProxyHandler`: with
  `HTTP_PROXY` / `HTTPS_PROXY` set and `no_proxy` not listing the host, a
  request to a local embedding or model service on `localhost` or
  `127.0.0.1` was sent through the proxy, memory and note text included.
  A request to this host now always goes direct: `localhost`, `127.0.0.0/8`,
  `::1`, `::ffff:127.x`, the short and numeric IPv4 forms the socket layer
  reads as 127/8 (`127.1`, `2130706433`, `0x7f.0.0.1`) and the unspecified
  `0.0.0.0` / `::` (all decided on the literal host, never by DNS). Other
  hosts still honour the environment's proxy settings. Helpers:
  `bypasses_proxy` (that rule) and the stricter `is_loopback_host` /
  `is_loopback_url` (canonical loopback only, for "text stays on this
  machine" decisions).
- The local relevance judge (`MEMFW_JUDGE_URL`, `local_judge_transport`)
  used plain `urlopen`: the same proxy leak, and it followed redirects with
  memory text. It now goes through `open_http` (direct for this host, 3xx
  refused). `scripts/day11_runner.py` (Ollama calls) does too.

### Fixed — a top-level `sensitivity:` key is read the same at every exit

- A hand-added top-level `sensitivity:` front matter key was treated as
  private by the O1a projections (the reference reader) but ignored by HTTP
  `/search` / `/related`, OKF export and the memfw MCP server. Now
  `memfw.ports.privacy` and its stdlib copy in `memfw/index/read_path.py`
  read it like the reference reader: no value, `null`, `""` or an empty list
  adds nothing, `private` (any case) is `local-private`, anything else
  (including non-strings and list items other than `private`) is `unknown`;
  strictest wins. Parsed `MemoryDocument`s keep the value
  (`top_level_sensitivity`, not a field: render is unchanged), and dream
  open questions treat such a memory as private. A parity test checks the
  two copies, the parsed-document path and the reference reader on an
  exhaustive matrix. Without a clearance nothing changes. Deploy note:
  `read_path.py` changed, so ship the HTTP service's three files together
  (`docs/READONLY-SERVICE-UPGRADE.md`).
- Write side: a rewrite no longer drops the key. `render` writes the parsed
  value back (files without it keep their bytes), and `update`, `supersede`,
  cold archive/restore and `rollback` carry it. `update(sensitivity=...)`
  sets it (`None` removes it). Like the `privacy` field, only a human or an
  approved proposal may remove or loosen it, and a non-human rollback that
  would drop it is refused. It is no longer listed in `ignored_keys`. A
  reviewed merge now refuses a duplicate that is more private than its
  representative (field, tags or key) only when it copies text (`retain`
  or `merged_body`); a delete-only merge is allowed. Consolidation review
  passes the vault's `legacy_privacy_default` to the judge, which then makes
  the most private member of a pair the representative (deterministic,
  live and local-MLX pairs; ties keep the judge's order). A judge without
  that level keeps its order as before. Agent proposals
  may carry `sensitivity`; approving one applies it, removal included.
- Recall's strict policy (`two_stage`, context v2 `strict`, open-question
  labels) reads the key too. OKF export writes `memfw.privacy:
  local-private` for a memory whose key restricts it, so a round trip stays
  private. The HTTP privacy check refuses front matter with a duplicate key
  (`unknown` under a clearance). The reference reader treats `Null`/`NULL`
  as null like YAML. `read_path.py` gains `READ_PATH_VERSION = 2`;
  `readonly.py` and the HTTP script refuse an older copy at startup.

### Added — CLI tidy-up: open questions, `--json`, grouped help

- `memfw add --type open-question --question TEXT [--conflicts ID ...]
  [--missing TEXT] [--tenant T]` creates an open question from the CLI, and
  `memfw update ID --question/--conflicts/--missing` edits its block
  (`--conflicts` replaces the list; repeatable or comma-separated).
  `memfw update ID --resolved-by ANSWER` resolves it (sets `valid_to`; not
  combinable with other changes). All go through the U1 gates: human actor,
  no epistemic/causal/causal_record/experience, conflicts and answer exist in
  the question's tenant; `--dry-run` runs the same checks. No MCP or HTTP
  write path was added. `memfw update --privacy LEVEL` sets a memory's level.
- `--json` on `list`, `show` and `index search` (the read commands that did
  not print JSON). Without the flag their output is byte-identical to before
  (`tests/harness/cli_tidy_probe.py`, opt-in with `MEMFW_ARCH_BASE_REF`).
- `memfw --help` groups the top-level commands into Daily, Approval, Ops and
  Export panels (`memfw.cli.HELP_PANELS`). Command names and behaviour are
  unchanged.
- `docs/cli-mcp-parity.md`: every capability × CLI / MCP / HTTP, each gap
  marked by design or accidental (filled or open).

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
  (walked tree without git or when git lists nothing) must be free of
  merge-conflict markers (seven `<`, `|` or `>` alone or before a space, or a
  line exactly `=======`; Markdown setext headings, binary files and files
  over 5 MB excepted).
- Usage projection (`SqliteUsageSink.record`): an index on `at_epoch` (added to
  existing files with `CREATE INDEX IF NOT EXISTS`), and the count retention
  deletes below the `max_rows`-th newest rowid instead of a sorted
  `NOT IN (... LIMIT 100000)`; the same rows are kept, and a write at the
  100000-row cap drops from ~30 ms to ~2 ms. An existing usage directory the
  user owns is tightened to 0700, not only a new one. Docstring size note
  corrected (about 7.4 MB at the cap, plus the index).
- Settled history ingest: a forced re-read marker is cleared only after the
  cursor is saved, and a forced pass (re-label, opt-out change) that finds a
  locator busy leaves one, so the owed full re-read is not lost; a file whose
  mtime is in the future and that has no usable timestamps falls back to its
  ctime (not on Windows, where it is the creation time) instead of staying
  unsettled until the clock catches up; an unstatable path (a NUL in it)
  counts as unknown activity instead of failing the session.
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
  `ignored_keys`; a write by 0.2.0 drops the block, after which `fsck`
  reports `open_question_missing_block` and a person can restore the block
  with `memfw update ID --question … [--conflicts …]`. Files without the block
  keep their bytes.

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

### Recall projection

R6 PR-1: the backend-neutral recall projection, documented in
`docs/qmd-projection.md`. memfw contains no QMD code; it couples to a recall
backend only through exported files and the `resolve` interface.

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

### Added — epistemic type, causal grade, experience points (R3, #32)

- Memories may carry `epistemic` (`experience`, `fact`, `judgment`,
  `inference`), `causal` (`none`, `association`, `hypothesis`, `verified`),
  `causal_record`, `limitations` and `experience`. Defaults stay off the file,
  so older memories parse and render byte-identically. `memfw add` / `update`
  take `--epistemic`, `--causal` and `--causal-record`.
- One write gate (`memfw.core.epistemic`) for add, update, proposals, the
  human approval that applies one, and dream candidate staging: `causal:
  verified` needs a non-empty estimand and identification with no untested
  assumptions, evidence outside the vault (chat or model narrative caps a
  claim at `hypothesis`), robustness or limitations, and a human writer or a
  recorded human decision. Agent and dream writes stop at `hypothesis`.
- MCP `memory_search`, `memory_get` and `memory_context` rows carry
  `epistemic` and `causal`; `memory_get` returns the whole causal record. OKF
  export carries the fields inside the `memfw:` envelope.
- Reviewed experience points: `type: experience-point` with the reviewed
  record (task, conditions, actions, evidence, review, counterexamples,
  `invalidated_when`). `memfw experience draft --from FILE [--apply]` stages a
  candidate under `_candidates/experience/` (never the vault), `memfw
  experience from-github --repo OWNER/NAME --pr N` drafts one from a merged
  pull request through `gh` (read-only), and `memfw experience invalidate ID
  --reason …` stages a pending-revision proposal. Schema:
  `docs/experience-points.md`.
- Pseudo-causality evaluation: `memfw eval causal [--json]` runs synthetic
  confounded, narrative-only and fully identified materials through the real
  write path and reports spurious promotions, false accepts and false
  rejects. See `docs/epistemic-and-experience.md` and `docs/evaluation.md`.

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

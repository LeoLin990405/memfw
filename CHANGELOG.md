# Changelog

All notable changes to memfw. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). Entries name the pull request that
carried the change; `#N` links to `LeoLin990405/memfw` pull request `N`.

The vault format did not change between `0.1.1` and `0.2.0`: `vault.yaml`
stays at `schema_version: 1`, and files written by an earlier version keep
their bytes (see [Compatibility and upgrade](#compatibility-and-upgrade)).

## [0.2.0] — 2026-11-15

The first release since the standalone core: notes, concepts, the transcript
index, the conversation store, handoff packs, dream, OKF export and the
upgrade fixes found by running this branch against a copy of the live
2,164-memory vault.

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
  memory is byte-identical (#33).

### Index, search and retrieval

- Per-term matching instead of one quoted phrase, for FTS5 and the
  `unicode61` fallback (#20).
- R0 evaluation harness, one shared read path and the MCP multi-hop pilot
  (#18); short-text expansion measured on the baseline and hybrid FTS
  restored (#18).
- Alias-expanded search: a concept alias reaches the memories that mention
  it, inside the same read-path gates (#24, #25).
- Retrieval traces keep full candidate hashes (#5); opt-in query-coverage
  abstention (#9); retrieval feedback and shadow tooling (#4).
- A stale index is detected and rebuilt before a search instead of failing
  with `no such table`; `--dry-run` reports `would_rebuild` (#33).

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

- `memfw budget configure|show`, `stats`, `forget run --dry-run` and
  `log verify` work on a copied vault end to end (verified in `0.2.0`).
- `index rebuild --dry-run` reports the count it would index.
- The budget keeps its `10MB` profile semantics: body-only accounting, with
  physical usage reported separately.

### Compatibility and upgrade

- Running `0.2.0` against the deployed `0.1.0` vault: parse → render was
  byte-identical for **2,164 of 2,164** files after #33 (before it, every file
  gained six default lines on rewrite).
- `fleet-memfw-sync.py`'s full command surface (`import`, `index rebuild`,
  `budget configure --apply`, `budget show`, `stats`, `forget run --dry-run`
  for decay and consolidate, `log verify`) runs on a copied vault with
  `0.2.0`; `budget configure --apply` produces the same `vault.yaml` bytes.
- A full sync import of 2,164 files succeeded with zero failures, and a
  second incremental run skipped all 2,164 (no rewrite churn).
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

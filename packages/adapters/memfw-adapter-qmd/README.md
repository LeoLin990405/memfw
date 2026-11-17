# memfw-adapter-qmd

[QMD](https://github.com/tobi/qmd) as a memfw `RecallProvider`. Two adapters,
both **off by default**:

| Adapter | Searches | Returns |
|---|---|---|
| `qmd` | a QMD collection over a `memfw projection export` tree (`<generation>/files`) | `memory_id` candidates, mapped through the projection manifest |
| `qmd-notes` | a QMD collection over a Knowledge-Hub tree that is also readable on this host | `path` candidates `kh:<path>#<heading>` |

Each candidate carries only the reference, QMD's score and a fixed source label
(`extra={"kind": "qmd-memory"}` / `"qmd-note"`). No title, snippet or body from
QMD is ever returned; the kernel reads bodies from the vault after resolve and
filter.

Standard library plus PyYAML 6.0.3 (MIT, pinned; notes frontmatter is parsed
with `yaml.safe_load`, the same reading as memfw's own notes index). Depends on
`memfw-ports` (the `RecallProvider` contract) and nothing else of memfw; memfw
never imports this package. Import name
`memfw_adapter_qmd`.

## External project

- QMD, <https://github.com/tobi/qmd>, npm `@tobilu/qmd`.
- License: **MIT** (re-verified from the `LICENSE` file of the published
  `@tobilu/qmd` 2.5.3 package: "MIT License, Copyright (c) 2024-2026 Tobi
  Lutke").
- Pinned version: **2.5.3** (`QMD_VERSION`). The contract fixtures
  (`tests/fixtures/qmd_contract/` in the memfw repository) were recorded from a
  live 2.5.3 server over a throwaway synthetic index (no embeddings, so lexical
  answers only; text fields redacted). Run the contract tests, and re-record against the new version with
  `python -m memfw_adapter_qmd.record`, before upgrading QMD.
- Attribution: `memfw_adapter_qmd/THIRD_PARTY.md` (shipped in the wheel) carries
  QMD's MIT notice.
- No QMD code is included or vendored. The adapter talks to a separately
  running `qmd mcp --http` server. `memfw_adapter_qmd.paths.handelize`
  re-implements QMD's published path-normalisation rule so results can be
  matched to known files; the contract tests check it against outputs of the
  original function.
- Telemetry: the adapter sends nothing but the MCP requests below. (QMD itself
  is a local tool; configure it per its own documentation.)

## How it queries QMD

One MCP session per provider over Streamable HTTP (`initialize`,
`notifications/initialized`, then `tools/call`). Each search is one call to
QMD's `query` tool (R6 fusion design §5.1):

```json
{"searches": [{"type": "lex", "query": "<q>"}, {"type": "vec", "query": "<q>"}],
 "limit": N, "minScore": 0, "candidateLimit": max(20, 4N),
 "collections": ["<collection>"], "rerank": false}
```

Only `structuredContent.results[].file`, `.score` and `.line` are read. QMD
stores handelized paths (`Week 1 notes.md` → `Week-1-notes.md`), so a result
is never parsed into a name: it is looked up in a table of the handelized names
the adapter already knows (manifest paths, or files under `notes_root`). A
result that is not in the table, or that two known names share, is dropped.

## Settings

Settings come only from operator config (never from a request): either the
`AdaptersConfig` entry-point route (`enabled: ["qmd"]`, `"qmd": {...}`), or
memfw's runtime recall loader for `two_stage` (`vault.yaml`
`runtime.recall.adapters: [{factory: "memfw_adapter_qmd:memory_factory",
options: {...}}]`, `memfw.runtime.recall_adapters`). The loader adds
`vault` and `embedder` to the settings; both are accepted and ignored. Any
other unknown key is refused. `qmd-notes` returns `kh:` refs; a coordinator
resolves them only through `memfw.recall.builtin.NotesSegmentResolver` (note
sections, returned apart from memory hits), which is not registered by default.

Common: `endpoint` (default `http://localhost:8181/mcp`), `allow_remote`
(default `false`: the endpoint must be a loopback literal), `collection`,
`searches` (default `["lex", "vec"]`), `rerank` (default `false`), `min_score`
(default `0`), `fetch_factor` (default `2`: ask QMD for `limit × 2` rows,
capped at 100, because rows are dropped after mapping).

`qmd`: `manifest` (required: a projection `manifest.json` or the generation
directory, e.g. a `current` symlink; re-read when it changes),
`max_manifest_age_seconds` (default none), `require_lifecycle_ok` (default
`true`). Default collection `memfw-memory`.

`qmd-notes`: `notes_root` (required), `clearance` (`public`/`shared`, default
both; `local-private` is refused), `legacy_privacy` (level of a note without a
`privacy` field, default `local-private`, which returns nothing until set),
`rescan_seconds` (default 300). Default collection `knowledge-hub`.

## Fail-closed rules

- `qmd`: a hit is returned only if its manifest entry has
  `privacy_class == "none"`, the call's tenant, and is current: `lifecycle`
  empty or `active`, no `superseded_by`, no `valid_to`, and `valid_until`
  empty or in the future (an unparsable one counts as expired).
  `local-private` and `unknown` documents are never returned, and QMD never
  contributes retired or historical memories. The coordinator then resolves the id
  against the vault and applies its own filter.
- `qmd-notes`: a hit is returned only if the note file exists under
  `notes_root` (no symlinks), its privacy level is in `clearance`, and the
  call's tenant is the shared tenant `""`. The level is computed like memfw's
  own notes reader: frontmatter parsed with `yaml.safe_load`, then
  `memfw.ports.privacy.metadata_level` (`privacy`, `sensitivity`,
  `privacy:`/`sensitivity:` tags; strictest wins). A YAML error, non-mapping
  frontmatter, unreadable tags, an unterminated or misplaced `---` block, or a
  stray `privacy:`/`sensitivity:` line near the top makes it `unknown`.
- `available()` never touches the network. It reports `no_endpoint`,
  `bad_endpoint`, `endpoint_not_loopback`, `no_manifest`, `bad_manifest`,
  `manifest_lifecycle_not_ok`, `manifest_stale` or `no_notes_root`.
- QMD down, slow or off-protocol: `search` raises `ProviderUnavailable`
  (`unreachable`, `timeout`, `server_error`, `busy`) or `QmdResponseError`;
  the coordinator marks the provider degraded and the other providers (FTS)
  answer.

## Network

Loopback requests bypass `HTTP(S)_PROXY`; redirects are refused; responses are
capped at 4 MiB; every request uses the time left of the coordinator's budget
as its socket timeout. QMD 2.5.3 listens on `localhost` only (it has no
`--host` option).

`memfw_adapter_qmd.fake.FakeQmd` is a loopback-only fake of the endpoint for
tests.

# memfw components

Optional pieces built on the memfw engine. The README links here.

## Notes MCP tools

- `notes_search` — FTS/vector/hybrid section search;
- `notes_related` — derived note-to-note edges;
- `notes_get`, `notes_status`;
- `retrieval_feedback`, `retrieval_trace`.

Under `--clearance` / `MEMFW_NOTES_CLEARANCE` (with `--legacy-privacy-default`) every tool sees
only notes whose privacy level the clearance admits, read from the note file; `notes_status`
counts only visible notes. Unset, nothing is filtered.

## Notes index

`memfw.notes` is a derived, section-level index for a Markdown notes tree. It is
independent of the memory vault and never writes the notes:

- SQLite FTS5 lexical recall (trigram, or jieba + unicode61 fallback);
- optional sqlite-vec vector recall;
- OpenAI-compatible embedding provider (`HttpEmbeddingProvider`);
- heading/frontmatter chunks, wikilinks, incremental hash-based updates;
- RRF hybrid search with FTS-only degradation.

```bash
memfw notes index --root ~/notes --db /tmp/notes.sqlite \
  --endpoint http://127.0.0.1:8001/v1/embeddings \
  --model qwen3-embedding --dimension 1024
memfw notes search "corpus collocations" --db /tmp/notes.sqlite \
  --endpoint http://127.0.0.1:8001/v1/embeddings \
  --model qwen3-embedding --dimension 1024
memfw notes status --db /tmp/notes.sqlite
memfw notes serve --root ~/notes --db /tmp/notes.sqlite \
  --endpoint http://127.0.0.1:8001/v1/embeddings \
  --model qwen3-embedding --dimension 1024
```

See [Notes index](notes-index.md) for the index layout, MCP server, and
checkpoint/resume behavior.

Install the optional vector extension with `pip install 'memfw[notes]'`.
Without it, the same API still works with full-text search and stores vectors
as a fallback BLOB table.


## Code retrieval

`memfw code` provides an agent-oriented, local code search path without a new service:

- chunks by Python file, top-level function/class (stdlib AST; no tree-sitter required);
- lexical FTS + optional local Qwen3-Embedding + structural definition/caller lookup;
- RRF fusion; lexical+structure fallback when embeddings are unavailable; rerank off;
- lightweight repo map: entry files, directory summary, recent files.

```bash
memfw code index --root . --db /tmp/memfw-code.sqlite
memfw code search "where is VaultRetriever defined" --db /tmp/memfw-code.sqlite
memfw code map --db /tmp/memfw-code.sqlite
```

For semantic code search, add `--embedding-url`, `--embedding-model`, and `--dimension` to
`index` and `search`. Code chunks, symbols, and vectors are disposable projections; source files
remain authoritative.


## Read-only HTTP service

`scripts/memfw_readonly_service.py` serves the `memfw-http/2` contract (read and write tokens,
`/search`, `/related`, `/stage`, `/health`). Its read path is library code,
`memfw.index.readonly`, so a deployment copies three files from the same revision:

1. `scripts/memfw_readonly_service.py` (the HTTP entry point);
2. `src/memfw/index/readonly.py` (query rules, `search_vault`);
3. `src/memfw/index/read_path.py` (shared read-path fields).

The two library files need only the standard library and PyYAML. The script loads its read
path from `readonly.py` beside it, else the checkout's `src/memfw/index/`, else an installed
`memfw`, and logs which one it chose; with none of them it exits with an error naming the
missing files. `read_path.py` carries `READ_PATH_VERSION` (2 in 0.3.0 and 0.4.0); the script and
`readonly.py` refuse to start with an older copy, so a partial upgrade fails at startup instead
of on every `/search`. It never writes, so run `memfw index rebuild` once after an upgrade.

`--clearance` / `MEMFW_READONLY_CLEARANCE` (for example `public,shared`) limits `/search` and
`/related` to those privacy levels; unset, nothing is filtered. A memory without a `privacy`
field takes the vault's `legacy_privacy_default` (`local-private` when unset), so set that
first or every older memory is withheld. See
[`READONLY-SERVICE-UPGRADE.md`](READONLY-SERVICE-UPGRADE.md).

The same three files also serve `memfw-http/3`, chosen per request by the
`MemFW-Contract: memfw-http/3` header; without it every answer is the v2 one, byte for byte.
v3 adds `recorded_at` to `/search` hits, `why` and `provenance_type` to `/related`,
`POST /context` (the `memory_context` v2 `core` pack, built read-only; it needs the `memfw`
package and a configured clearance, else it is 404 and left out of `/health`) and
`POST /forget` (write token only: one deletion-request candidate under
`<vault>/_candidates/forget/`, never a deletion; hidden and missing targets answer the same
404; `request_id` makes it idempotent within the tenant; at most 1000 waiting per tenant).
`memfw forget requests` lists the requests and `memfw forget residue ID` checks that a
forgotten memory is gone from every projection. See [`v1-integration.md`](v1-integration.md).


## Standalone wheels: `memfw-ports` and `memfw-recall`

Two uv workspace members build wheels from memfw's own `src/memfw/ports` and
`src/memfw/recall` (no copied sources):

| Distribution | Import name | Contents |
|---|---|---|
| `memfw-ports` (`packages/memfw-ports`) | `memfw_ports` | port protocols, value types, the contract kit `memfw_ports.testing`, the stdlib-only reference implementations |
| `memfw-recall` (`packages/memfw-recall`) | `memfw_recall` | the recall coordinator, without memfw's built-in vault providers |

An adapter author can install them and run the contract checks without installing memfw. Both
carry `memfw.ports.PORTS_API_VERSION` (currently `0.3.0`). They are wheels only (no sdist, no
editable install) and are **not published to PyPI**:

```bash
uv build --wheel --package memfw-ports
uv build --wheel --package memfw-recall
```

The CI job `standalone-wheels` builds both, installs them into a fresh venv without memfw and
runs `tests/standalone/wheel_smoke.py`. memfw itself does not depend on them. Inside a memfw
process use `memfw.ports`; `memfw_ports` is a separate copy whose classes never match memfw's.
See [`ports-and-recall.md`](ports-and-recall.md#11-packaging-boundaries).


## Pipeline runner: `memfw-pipeline`

`packages/memfw-pipeline` (wheel `memfw_pipeline`, version 0.1.0) is the Stage contract and
runner for derived artifacts (admitted view, metadata index, FTS / bm25s / Qdrant indexes,
projections). A stage implements `freshness`, `inputs_digest`, `build`, `verify`, `publish` and
optional idempotent `post_publish` steps; the runner adds a single-writer lock beside the target
(`.<target>.lock`, `O_NOFOLLOW`, retried for a few seconds, then exit 75), a `running` status, a
build into a directory beside the target, a record inside every published artifact
(`.memfw-pipeline.json`: inputs digest, counts), an atomic directory exchange (two renames with
rollback as fallback), a shrink guard (an empty build or one under half of the baseline, which
is the live artifact's record first, exits 5 unless `--allow-shrink`), freshness, and a status
file of counts and fixed reason codes only. Inputs whose digest matches the live artifact's
record skip the build but still run post-publish steps. The path-trust check is on by default
when the runner runs as root. `memfw-pipeline unit` prints a hardened systemd or launchd template to review; nothing is
installed. Standard library only at run time plus `memfw-ports` 0.3.0; memfw never imports it.
Build with `uv build --wheel --package memfw-pipeline` (not published to PyPI). This release has
only an example stage; the real stages are ported later. See [`pipeline.md`](pipeline.md).


## DeepSeek tagging adapter

`packages/adapters/memfw-adapter-deepseek` (wheel `memfw_adapter_deepseek`, version 0.1.1)
implements the `ModelBackend` port with the standard library only. It gives the conversation
worker a `conv-v2` summary plus tags (project, task type, components, intent, outcome) per
chunk and session; it extracts no facts or claims. It is **off by default**: the worker uses it
only when `vault.yaml` sets `conversations: {backend: "memfw_adapter_deepseek:factory", cloud:
true}` and `DEEPSEEK_API_KEY` is set, and each run has a token and cost budget. The key is sent
only to the provider's API host. memfw never imports the adapter; it is built as its own wheel
(`uv build --wheel --package memfw-adapter-deepseek`) and not published to PyPI. See
[`conversations-share.md`](conversations-share.md#deepseek-tagging-adapter).


## bm25s recall adapter

`packages/adapters/memfw-adapter-bm25s` (wheel `memfw_adapter_bm25s`, version 0.1.1) is an
in-process `RecallProvider` that ranks memories with bm25s 0.3.16 (MIT), jieba-segmented. It is
**off by default**: MCP `memory_search` `mode=two_stage` runs it only when `vault.yaml` lists
`memfw_adapter_bm25s:factory` under `runtime.recall.adapters`. It indexes only memories whose
privacy level is in its `clearance` (default `public,shared`), into a private directory outside
the vault (`~/.local/share/memfw/adapters/bm25s/`). memfw never imports it. See
[`adapter-bm25s.md`](adapter-bm25s.md).

## Qdrant recall adapter

`packages/adapters/memfw-adapter-qdrant` (wheel `memfw_adapter_qdrant`, version 0.1.2) is a
semantic `RecallProvider` over a Qdrant v1.20.x service the operator runs, bound to loopback with
telemetry off (`QDRANT__TELEMETRY_DISABLED=true`). It embeds queries with memfw's own embedder
and asks for memory ids only. It is **off by default**; `memfw-adapter-qdrant sync` sends only
`public`/`shared` memories, never `local-private`, and stores no text. memfw never imports it. See
[`adapter-qdrant.md`](adapter-qdrant.md).

## LightRAG deriver adapter

`packages/adapters/memfw-adapter-lightrag` (wheel `memfw_adapter_lightrag`, version 0.1.0)
implements the `Deriver` port as a loopback HTTP client of an operator-run `lightrag-server`
1.5.7 (bound to `127.0.0.1`, with an API key): graph and answer views whose every source is a
requested, shareable, current memory. **Off by default**; rows become memory only as staged
candidates. See [`adapter-lightrag.md`](adapter-lightrag.md).

## GraphRAG deriver adapter

`packages/adapters/memfw-adapter-graphrag` (wheel `memfw_adapter_graphrag`, version 0.1.0)
implements the `Deriver` port with GraphRAG 3.3.0 as an offline batch job: shareable documents
from a `memfw projection export` generation go in, `graphrag index` runs as a subprocess only
when the operator calls it (loopback model endpoints unless `cloud: true`), and its CSV output
tables come back as graph and community-report views. **Off by default**; rows become memory
only as staged candidates. See [`adapter-graphrag.md`](adapter-graphrag.md).

## QMD recall adapter

`packages/adapters/memfw-adapter-qmd` (wheel `memfw_adapter_qmd`, version 0.1.1) implements
the `RecallProvider` port twice, with the standard library and PyYAML, over a running
[QMD](https://github.com/tobi/qmd) (MIT, pinned 2.5.3) MCP server: `qmd` searches a collection
built from a `memfw projection export` and returns memory ids mapped through the projection
manifest; `qmd-notes` searches a Knowledge-Hub collection and returns `kh:<path>#<heading>`
references. Both return references, QMD's scores and a source label only, drop anything they
cannot map to a visible document (`local-private` and `unknown` are never returned), and are
**off by default** (`AdaptersConfig` `enabled`). The endpoint must be loopback unless
`allow_remote` is set, and loopback requests bypass any proxy. When QMD is down or slow the
coordinator marks the provider degraded and FTS answers. memfw never imports the adapter. See
[`qmd-projection.md`](qmd-projection.md#qmd-recall-adapter) and the package README.

## LangExtract deriver adapter

`packages/adapters/memfw-adapter-langextract` (wheel `memfw_adapter_langextract`, version 0.1.0)
implements the `Deriver` port: character-grounded entities and relations from `public`/`shared`
memories via LangExtract 1.7.2 (installed by the operator), with a memfw `ModelBackend` as the
model (a loopback-only Ollama backend ships with it) and a per-run token budget. **Off by
default**; its rows become memory only as staged candidates. See
[`adapter-langextract.md`](adapter-langextract.md).

## Tailnet host inventory adapter

`packages/adapters/memfw-adapter-tailnet` (wheel `memfw_adapter_tailnet`, version 0.1.0,
standard library only) implements the `Ingestor` port for a Tailscale tailnet's machine list.
`memfw-adapter-tailnet scan` reads `tailscale status --json` (an exported file, or one run of
the CLI with a fixed argv, no shell, a deadline, an output cap and no proxy variables), keeps
name, first MagicDNS label (the tailnet suffix only hashed), tailnet IPs, OS, online, last
seen and tags per machine under a hashed host id (user and login data, keys and endpoints
are never read; shared-in nodes are left out unless opted in, provider exit nodes always),
and stages one identity-only `host-fact` candidate per new, returned, gone, renamed or
otherwise changed machine under `_candidates/tailnet/`; unchanged machines produce nothing
and a retry stages duplicates only. Candidates are `local-private` unless the operator says
otherwise. A names-free `report.json` counts allowlisted machines offline too long, entries
that match no machine or several, and machines no entry vouches for (never by self-reported
host name); discovery never edits that allowlist or decides where data is sent.
**Off by default**; memfw never imports it. See [`adapter-tailnet.md`](adapter-tailnet.md).

## PageIndex recall adapter

`packages/adapters/memfw-adapter-pageindex` (wheel `memfw_adapter_pageindex`, version 0.1.1) is
a `RecallProvider` for long documents that searches PageIndex-style table-of-contents trees
(titles, node ids, page ranges, optional short summaries; never body text). A memfw
`ModelBackend` picks the sections, or `mode: titles` ranks titles without a model. It returns
`kh:<path>#<heading>` and `att:<id>#page=<n>` refs only. A non-local backend needs `cloud: true`,
`local-private` trees are never sent to one, and each run has a token and call budget. Trees are
built or imported offline with `memfw-pageindex build-md | import-tree | list` and stored as
`0600` JSON outside the vault. **Off by default**; memfw never imports it. See the package
README.

## OCR proxy adapter

`packages/adapters/memfw-adapter-ocr` (wheel `memfw_adapter_ocr`, version 0.1.1, standard
library only) turns a PDF or image attachment into a `memfw.proxy` v1 import file: text
segments with canonical locators, `coverage`, `source_sha256`, the producer and per-segment
confidence when the engine gives one. Engines: an OCR server run as a child process over MCP
stdio (with a deadline, a page budget and the whole process group stopped at the end),
`pdftotext` for text-layer PDFs, or a `module:factory` (outside the local-only guarantee).
Nothing is uploaded; only privacy levels in its `clearance` (default `public,shared`) reach an
engine, and the original's sha256 is checked before and after. `memfw-ocr-proxy run | check`
writes the file; `memfw attach proxy import` stores it. **Off by default**; memfw never imports
it. See the package README and [`attachment-proxies.md`](attachment-proxies.md).

## Corroboration projection

`memfw corroboration rebuild` writes the corroboration projection outside the vault
(`~/.local/share/memfw/corroboration/<key>.sqlite3`, tagged with the vault's identity): per memory, the
independent (app, session) pairs that mention it, from its own `conv:` evidence, staged
conversation-worker claims and accepted experience points. One count per app and session;
carriers that cite the memory itself, generated provenance and hidden or opted-out apps do
not count. Ids, names,
counts and times only; the file is `0600`, rebuilds are byte-identical and the vault is only
read.

```bash
memfw corroboration rebuild --root ./vault
memfw corroboration show MEMORY_ID --as-app codex --root ./vault
```

`show` trims the answer to the apps `--as-app` may read. MCP `memory_search` with
`labels: true` adds `corroboration` (`count`, `apps`, `sessions`, `first_seen`, `last_seen`) to
every row, trimmed to the server app's `can_read`, or `{"degraded": CODE}` when it cannot be
shown; without `labels` the output is unchanged. `memfw corroboration queue` lists unresolved
open questions in review order, those whose conflicts are corroborated by at least two visible
apps first (read-only, `--policy strict` by default). See
[`epistemic-and-experience.md`](epistemic-and-experience.md#corroboration-derived).


## Obsidian projection

`memfw project obsidian --out DIR [--root VAULT] [--clearance public,shared] [--tenant T]`
writes a read-only Obsidian board: a page per memory, open-question and concept pages, wikilinks
for `about`, supersession, open-question conflicts and answers, concept `broader` / `narrower`,
Bases views and a sha256 manifest. It implements the `Projector` port. The output is
deterministic (no run timestamps, stable order), so it can be committed and diffed. A memory the
clearance hides leaves no page, link, count or concept page. The vault is only read, and the
output may not be inside the vault or a protected path. A rebuild touches only files listed in
the previous manifest. Nothing runs unless the command is invoked. See
[`obsidian-projection.md`](obsidian-projection.md).


## Concept taxonomy and relation types

Concept files may carry `broader` (parent oids); `narrower` is derived and never written.
Lint reports invalid, dangling, merged or cyclic parents. The versioned relation-type table
`src/memfw/ontology/relations.yaml` lists every typed relation memfw already stores, with its
allowed node kinds and privacy/tenant rules; `memfw concepts relations` prints it and
`memfw concepts lint-relations` (read-only) checks a vault against it. Nothing on the read
path follows `broader`. See [`ontology.md`](ontology.md).


## V1 integration status

See [`docs/v1-integration.md`](v1-integration.md) for the read-only MemFW V1
surface and the Conductor federation adapter. V1 does not replace Conductor's
proposal/approval lifecycle or write into MemFW automatically.

Conductor v1 (released 2026-11-07) talks to MemFW through the `memfw-http/2`
contract: score, separate read and write tokens, `/stage`, root-only endpoints,
redirect refusal, and a `forget` that says when it is not supported.

**Hardening after the 2026-11-08 memory test** (PRs #15 and #16):

- **Tenant scoping:** `/related` and MCP `memory_related` are tenant-scoped. Tenant and scope are written to the edge table and filtered before the cap, and the live tenant is re-checked before an ID or title is returned. Related reasons keep their real method, and scores are bounded to [0, 1].
- **`memory_remember`:** candidates written in the same second no longer overwrite each other, and every one of them can be applied.
- **Outbound HTTP:** it never follows redirects and never forwards `Authorization`.
- **Notes indexing:** symlinks that escape the notes root are skipped.
- **Reranking:** a caller can no longer choose the rerank endpoint; only the server can configure it.
- **Readonly service:** `scripts/memfw_readonly_service.py` is the HTTP entry point; its read path is the library module `memfw.index.readonly` (stdlib plus PyYAML at import time). It runs without `memfw` installed when `readonly.py` and `read_path.py` from `src/memfw/index/` sit beside it. [`docs/READONLY-SERVICE-UPGRADE.md`](READONLY-SERVICE-UPGRADE.md) covers moving an older readonly deployment to `memfw-http/2`, including a `/health` contract check.
- **Field aliases:** `valid_until` and `superseded` are accepted as input aliases of `valid_to` and `superseded_by`. Output always uses the canonical names. A conflicting pair is rejected, and supersession IDs are compared exactly.
- **Test suite:** the full suite on `main` after #16 is 652 passed, 2 skipped (offline; live and network tests excluded).

**Retrieval: read path and multi-hop pilot** (PR #18, 2026-11-09):

- **Evaluation harness** (`eval/retrieval_r0/`): synthetic cases in six classes (time updates, conflicts, deletion, multi-tenant, poisoning, multi-hop), a power analysis, full-chain multi-hop gold labels, and coverage of every existing path (HTTP `/search`, MCP default hybrid).
- **Unified read path:**
  - HTTP `/search` and MCP share one implementation, and results carry provenance and `why`.
  - The tenant is bound to the token and filtered in SQL before ranking.
  - Freshness is checked against the Markdown source of truth. Only the top 10 returned hits are checked, using a stat-keyed cache that compares before and after the read, and the check fails closed.
- **MCP multi-hop pilot:** one round of SAG2-style query-time term expansion, with ID/keyword disambiguation and a degraded fallback. No judge model is reachable from any entry point.
- **Measured on the synthetic set** (160 questions per class, 3 standalone runs):
  - multi-hop recall gain +0.5;
  - single-hop drop 0;
  - p95 latency ratio 1.78 (gate: ≤ 2).

  These are synthetic results, not field results.
- **Full suite:** 709 passed, 2 skipped.


## Relatedness projection

`memfw.related` builds a disposable edge table from explicit wikilinks/`related`,
shared tags/courses, shared entities, and lexical overlap:

```bash
memfw notes related-build --notes-db /tmp/notes.sqlite --related-db /tmp/related.sqlite
memfw notes related "Week 02/...md" --related-db /tmp/related.sqlite
memfw memory-related-build --root ./vault --related-db /tmp/related.sqlite
memfw memory-related memory-id --related-db /tmp/related.sqlite

memfw notes related-sections-build --notes-db /tmp/notes.sqlite \
  --related-db /tmp/related.sqlite --max-sections 2000
```

Section/item edges carry tenant/scope filters. A 100-pair human annotation CSV
can be generated with `memfw.related_annotation.generate_pairs` and calibrated
with `calibrate` after labels are filled.

The MCP tool `memory_related` exposes memory edges; `notes_related` is available
through the notes MCP when a related DB is configured. Edges are derived, never
write Markdown, and only current memories are indexed.

The optional Conductor write bridge stages a human-approved proposal at the
remote service's non-authoritative `/stage` endpoint; it writes only
`_candidates/` and still requires MemFW human confirmation.

Semantic edges are opt-in: add `--embedding-url`/`--embedding-model` to the
rebuild command and `sqlite-vec` is used for KNN. All indexed sources in one
build share the same embedder/fingerprint. The current QMD coverage uses the
local memfw notes index; remote-only QMD hits are not included until they have a
local vector projection. Semantic scores remain a separate method
and never replace explicit links.

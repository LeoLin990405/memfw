# memfw components

Optional pieces built on the memfw engine. The README links here.

## Notes MCP tools

- `notes_search` — FTS/vector/hybrid section search;
- `notes_related` — derived note-to-note edges;
- `notes_get`, `notes_status`;
- `retrieval_feedback`, `retrieval_trace`.

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
- **Readonly service:** `scripts/memfw_readonly_service.py` runs standalone with no `memfw` import. [`docs/READONLY-SERVICE-UPGRADE.md`](READONLY-SERVICE-UPGRADE.md) covers moving an older readonly deployment to `memfw-http/2`, including a `/health` contract check.
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
local 4,979-note memfw notes index; remote-only QMD hits are not included until
they have a local vector projection. Semantic scores remain a separate method
and never replace explicit links.

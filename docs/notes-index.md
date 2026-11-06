# Notes index

`memfw.notes` is a disposable, section-level retrieval index for a Markdown
notes tree. It is separate from the memory vault and never writes the notes.

## Layout

- `documents` / `sections` / `links` metadata;
- `sections_fts` FTS5 lexical index (trigram when available, jieba +
  unicode61 fallback otherwise);
- `vec_sections` (sqlite-vec) or `section_vectors` (BLOB fallback) optional
  vector index;
- `meta` records the embedding dimension/model marker.

## Indexing

- SHA-256 per file; unchanged files are skipped;
- frontmatter parsed as metadata, headings become sections, long sections are
  split with overlap;
- wikilinks extracted for later graph expansion;
- `index(..., commit_every=N)` commits checkpoints, so an interrupted run can
  resume from the last committed batch;
- writes happen only to the derived SQLite database, never to the source tree.

## Search

- FTS and vector candidates are merged with RRF;
- FTS-only mode is automatic when no embedder is configured or when the
  embedding endpoint fails;
- filters: path prefix, tags, course;
- results expose path, title, heading, content, score, source and tags.

## Embedding providers

`HttpEmbeddingProvider` speaks the OpenAI `/v1/embeddings` shape and batches
requests. Deployments can use a local llama.cpp server, an OpenAI-compatible
proxy, or a cloud endpoint. Vectors are only stored if the embedding dimension
matches the index dimension.

## MCP

`memfw notes serve` exposes stdio MCP tools:

- `notes_search`
- `notes_get`
- `notes_status`

The server is read-only and local-only.


## Retrieval policy

`NotesRetriever` wraps `NotesIndex` with `RetrievalConfig`:

- `mode`: `fts`, `vector` or `hybrid`;
- `candidates`, `final_k`, `rrf_k`;
- optional `RerankerProvider`, `LanguageModelProvider`, `SparseRetriever`;
- optional query rewrite; the original query is always retained;
- reports actual mode, degradation, rewrites and errors.

See [retrieval architecture](retrieval-architecture.md) for the staged plan
and Stage A evaluation.


## Evaluation and feedback commands

```bash
memfw notes eval --db notes.sqlite --queries golden.yml --mode hybrid
memfw notes feedback-export --db feedback.sqlite --out pairs.jsonl
memfw notes manifest-write --db notes.sqlite --out index-manifest.json
memfw notes manifest-verify --db notes.sqlite --manifest index-manifest.json
memfw notes shadow --baseline-db old.sqlite --candidate-db new.sqlite \
  --queries golden.yml --mode hybrid
```

- `eval` reports Recall/MRR/nDCG, answerable/unanswerable counts and latency.
- `feedback-export` only uses explicit feedback; uncited results remain unknown.
- manifest write/verify prevents mixing model/chunker generations.
- shadow compares two indexes read-only; it never switches production.


## Voice-assisted golden annotation

```bash
memfw notes annotate --db notes.sqlite --out golden-human.yml --annotator leo
```

- Records one query at a time; each entry is saved immediately.
- Fields: query, kind, expected paths, evidence/notes.
- Path prompt: empty finishes; `?keyword` searches note paths; number selects.
- `no_answer` requires an explanation and no expected path.
- Resume an existing file by running the same command again.
- Voice: use the operating system's dictation into the terminal (macOS fn-fn);
  memfw receives only text and never records or uploads audio.
- The resulting YAML is accepted by `memfw notes eval`.


### Typeless / voice input

`memfw notes annotate` is plain terminal input, so Typeless can dictate each
prompt directly. Voice-friendly shortcuts:

- query: say `结束` or `完成` to finish;
- kind: `无答案` / `无法回答` -> `no_answer`; `中文` -> `chinese`;
- paths: say `搜索 Korsgaard` to list candidates, then `选择 2` to pick;
- paths: `完成` to end the path list;
- Typeless never needs to dictate a full long path.


## Batch manual annotation

Generate a batch packet:

```bash
memfw notes annotate-batch   --queries queries.jsonl   --db notes.sqlite   --out annotation-batch.csv   --markdown annotation-batch.md   --meta annotation-batch.json   --candidates 8   --mode hybrid   --endpoint http://127.0.0.1:8001/v1/embeddings   --model qwen3-embedding --dimension 1024
```

- CSV: one row per query/candidate; fill `relevant=1` for directly answering notes;
- `answerable=0` marks a query the vault cannot answer;
- `extra_paths` accepts additional paths separated by `;`;
- Markdown is a human-readable review sheet;
- No labels are inferred; blank cells stay unlabeled.

Import after manual review:

```bash
memfw notes annotate-import   --csv annotation-batch.csv   --db notes.sqlite   --out golden-human.yml   --annotator leo
```

- Unknown paths, duplicate queries and invalid no-answer entries are rejected;
- The result is immediately usable by `memfw notes eval`.

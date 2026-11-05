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

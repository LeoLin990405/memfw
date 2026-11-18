# memfw-adapter-pageindex

A memfw `RecallProvider` for long documents (Knowledge-Hub notes, PDF
attachments) that searches the way [PageIndex](https://github.com/VectifyAI/PageIndex)
does: no vectors; a model reads each document's table-of-contents tree and
names the sections or pages most likely to answer the query. It returns
those nodes as references, never their text.

Depends on `memfw-ports` (nothing else of memfw) and `PyYAML==6.0.3` (MIT),
which reads a note's frontmatter exactly as memfw does.
memfw never imports it. Import it as `memfw_adapter_pageindex`.

## PageIndex: licence, version, what is used

- PageIndex (VectifyAI) is MIT-licensed: re-checked from the repository's
  `LICENSE` at tag `v0.7.0` (2026-11-17; PyPI `pageindex` 0.7.0 also says MIT).
- **Pinned format:** trees are read in the JSON shape PageIndex **0.7.0**
  writes (`structure` nodes with `title`, `node_id`, `start_index`,
  `end_index`, `line_num`, `summary`, `text`, `nodes`).
- **No PageIndex code is imported, vendored or copied**, and the `pageindex`
  package is not a dependency (its dependency tree includes cloud LLM SDKs).
  The adapter has its own Markdown heading parser and its own tree-search
  prompt. PageIndex's PDF tree builder calls its own LLM client, which memfw
  cannot gate, so PDF trees are produced by PageIndex **outside memfw**, by the
  operator, and imported here as files. Run it only on documents you may send
  to the model it is configured with.

## Enable (off by default)

```yaml
adapters:
  enabled: [pageindex]
  pageindex:
    index_dir: /absolute/path/to/pageindex-index   # outside the vault
    mode: reasoning                                 # or: titles (no model)
    backend: "your_module:factory"                  # a memfw ModelBackend
    backend_options: {}
    cloud: false            # must be true for a backend whose `local` is not True
    max_run_tokens: 200000  # per provider instance (one process)
    max_calls: 500
```

Without the adapter in `enabled`, nothing changes.

For MCP `memory_search(mode="two_stage")`, memfw's runtime recall loader
(`runtime.recall.adapters` in `vault.yaml`, added by the bm25s adapter PR and
not on `main` yet) takes the same settings as `options`; the factory ignores
the `vault` and `embedder` keys that loader adds:

```yaml
runtime:
  recall:
    adapters:
      - factory: "memfw_adapter_pageindex:factory"
        options: {index_dir: /absolute/path/to/pageindex-index, mode: titles}
        timeout_seconds: 20     # a model call needs more than the 1.5 s default
```

## Building trees (offline, explicit)

```
memfw-pageindex build-md --root KH_ROOT --source "kh:notes/a.md" --index-dir DIR [--privacy shared]
memfw-pageindex import-tree --source att:ID --tree pageindex-output.json --pages N \
    --sha256 HEX --index-dir DIR [--privacy LEVEL] [--keep-summaries] [--pageindex-version 0.7.0]
memfw-pageindex list --index-dir DIR
```

- `build-md` reads one note under the root (no symlinks, no `..`), builds the
  tree from its ATX headings (not inside fenced code) and stores no text. The
  privacy level is the strictest of `--privacy` (default `local-private`) and
  the note's own frontmatter, read like the kernel's `metadata_level`:
  `privacy`, every `privacy:*` / `sensitivity:*` tag and a top-level
  `sensitivity`. The frontmatter block is split exactly as memfw's notes
  index splits it (first line `---`, closed only by the next `---` line; a
  `...` line does not close it), parsed with `yaml.safe_load`, and the level
  is `memfw.ports.privacy.metadata_level` of it; a YAML error, an unclosed
  block, frontmatter that is not a mapping, or a `tags` that is not a list
  makes the note `local-private`.
- `import-tree` validates PageIndex output (page ranges within `1..N`,
  unique node ids, at most 5000 nodes, depth 12), drops `text`, `doc_name` and
  `doc_description`, and keeps summaries (cut to 300 characters) only with
  `--keep-summaries`. Default privacy `local-private`.
- Each tree is one `0600` JSON file (`memfw-pageindex-tree/1`) in the index
  directory (created `0700`; an existing one open to others is tightened to
  `0700`, one owned by another user is refused). `list` prints sources and
  counts, no titles. Through the runtime loader, an `index_dir` inside the
  vault (or containing it) is refused (`index_dir_in_vault`).

## Search

- **`reasoning`**: one `ModelBackend.run("pageindex_tree_search", data,
  max_tokens=...)` per search. `data` holds the query and, for up to
  `max_documents` trees of the caller's tenant, each node's id, depth, title,
  page range or line and stored summary. The reply is `{"node_ids": [...]}`
  (PageIndex's `{"thinking", "node_list"}` is accepted). Unknown ids are dropped.
  The rendered prompt is estimated (about 3 characters per token, 1 per CJK
  character) and trailing nodes are dropped until it fits `max_prompt_tokens`;
  if not even the query fits, the search is refused (`prompt_too_large`).
  A backend serves the task by listing it in `tasks` and rendering
  `render_tree_search_prompt(data)`.
- **`titles`**: no model; nodes ranked by shared query terms in the title
  (and summary).

Gates, all fail closed:

- a backend whose `local` is not `True` needs `cloud: true`; otherwise the
  provider is unavailable (`cloud_not_allowed`);
- a `local-private` tree is never put in a request to a non-local backend;
- a backend that lists `tasks` without `pageindex_tree_search` is unavailable
  (`backend_task_unsupported`). The DeepSeek adapter (0.1.0) only answers the
  conversation tag tasks, so it cannot serve this adapter yet;
- per-run budget: estimated prompt plus `max_output_tokens` must fit
  `max_run_tokens`, and calls must fit `max_calls`; then the provider stops
  (`budget_spent`). A backend's own budget error (`BackendBudgetExhausted`)
  stops it too; a `BackendUnavailable` (network, auth, deadline) only degrades
  that one search (`backend_unavailable`) and the next search tries again.

## References

| Source | Ref returned |
|---|---|
| Markdown note `kh:<path>.md` | `kh:<path>#<heading>` |
| PDF attachment `att:<id>` | `att:<id>#page=<n>` |
| PDF in Knowledge-Hub `kh:<path>.pdf` | `kh:<path>#page=<n>` |

`n` is the section's first page (1-based), the `page` part of memfw's
multimodal locator (`#page=N[&bbox=x0,y0,x1,y1]`, PDF points, bottom-left
origin). PageIndex has no boxes, so no `bbox` is emitted. Spaces, quotes,
angle brackets, backslash, backtick, control characters, `%` and `#` are
percent-encoded (UTF-8). `extra` holds only `kind` (`section`), `page` (PDF)
and `why` (`tree_search` or `title_match`).

**Resolution.** `kh:` refs use the grammar of the kernel's `parse_kh_ref`
(percent-encoded UTF-8), so the kernel's `NotesSegmentResolver` (#98) can
resolve them to sections of the built-in notes index, with the notes privacy
rules, once a caller registers it (`default_resolvers(..., schemes={"kh":
...})`). With the default resolvers (no schemes) every PageIndex candidate is
dropped as `unresolved` (counted in `metrics.dropped`, no error, other
providers unaffected), and `att:` refs stay unresolved until MM2b adds their
resolver.

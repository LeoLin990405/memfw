# GraphRAG deriver adapter

`packages/adapters/memfw-adapter-graphrag` (import `memfw_adapter_graphrag`, version 0.1.1)
implements the `Deriver` port ([ports-and-recall.md](ports-and-recall.md#24-deriver)) with
[Microsoft GraphRAG](https://github.com/microsoft/graphrag) as an **offline batch job**. memfw
never imports, vendors or starts GraphRAG: the operator installs it in its own environment and
decides when an index run happens.

| | |
|---|---|
| External project | GraphRAG `3.3.0`, MIT (checked in the release's `LICENSE` and wheel metadata) |
| How it runs | `graphrag index` as a subprocess, only when `run_index()` is called |
| Port | `Deriver` (PORT_VERSION 1), kinds `graph` and `reports` |
| Default | Off. memfw never imports the adapter |
| Telemetry | None found in the `graphrag` package; its model layer (`graphrag-llm` and that package's dependencies) was not audited. Each run gets `DO_NOT_TRACK=1` |

## The three steps

### 1. Input: a projection export

```bash
memfw projection export --root /srv/vault --out /srv/projection
```

`stage_input(<generation dir>, vault)` reads that generation's `manifest.json` and
`files/memories/<id>.md` and stages a memory as `<root>/input/<id>.v<version>.<hash>.txt` only
when all of these hold:

- the manifest classes it `privacy_class: none`;
- its bytes match the manifest's `content_sha256`;
- the vault, read again now, holds it as `public` or `shared`: the fail-closed reference
  reader classes it `none` and its MM0 level (`memfw.ports.privacy.metadata_level`) is
  `public` or `shared`. A memory with **no `privacy` field** takes the MM0 fallback
  `local-private` and is not copied; `local-private` and `unknown` never are;
- the projection was rendered from the memory's current body (an older export is refused).

What is staged is the vault file's own bytes, not the projection rendering: `<version>` and
`<hash>` (the first 12 hex digits of their SHA-256) label exactly what GraphRAG indexed, so a
memory that changes, even one without a version, gets a new name and its old rows drop out of
every view. Each file is written through a fresh temporary file (`O_EXCL`, `O_NOFOLLOW`); an
entry in `input/` that is not a regular file (a subdirectory, which GraphRAG would also read,
a symlink, a directory named `*.txt`) stops staging (`bad_input_dir`).

The projection export projects every privacy class by default, so this re-check is what keeps
private memories out; exporting with `--skip-privacy unknown,local-private` as well does no
harm. The input directory belongs to the adapter: other `*.txt` files there are removed, and
`<root>/input/.memfw-input.json` lists the staged files and their hashes. The GraphRAG root
may not be inside the vault. At most `max_memories` documents are staged.

### 2. The batch run

`run_index()` runs

```
<command> index --root <root> --method <standard|fast>
```

with no shell, `stdin` closed, output appended to `<root>/logs/memfw-graphrag-index.log`, a
timeout (`timeout`, default 6 hours) and only these environment variables: `PATH`, `HOME`,
`LANG`, `LC_ALL`, `TMPDIR`, `SYSTEMROOT` (when set), the names listed in `pass_env`, and
`DO_NOT_TRACK=1`. Before it starts:

- the adapter's own `settings.yaml` must be in place and unchanged. The adapter writes it
(`write_settings()`, JSON, which YAML reads as is) from its settings only:

- both models (`completion_model`, `embedding_model`) on GraphRAG's `openai` provider with an
  **explicit `api_base`** (`api_base`, and `embedding_api_base` if it differs). Without
  `cloud: true` each must be plain `http` to a **literal loopback address** (`127.0.0.0/8` or
  `[::1]`; `localhost` and other names are refused); with `cloud: true`, `https` elsewhere is
  allowed. The key GraphRAG is handed is a fixed dummy a local endpoint ignores;
- CSV tables, file storage, and every `base_dir` fixed inside the root (`input`, `output`,
  `update_output`, `cache`, `logs`; the vector store at `output/lancedb`); claim extraction
  and snapshots off.

`write_settings()` records the file's SHA-256 in `<root>/.memfw-settings.sha256`. `run_index()`
refuses when the file or its hash differs from what the current settings would write
(`settings_changed`), when `${` appears in it (`settings_placeholder`), or when `<root>/.env`
exists (`dotenv_present`; GraphRAG would read it). `pass_env` may not name a variable ending in
`_BASE`, `_BASE_URL`, `_ENDPOINT`, `_HOST` or `_PROXY`, nor `ALL_PROXY`, `NO_PROXY`,
any `PYTHON*` variable, or any `DYLD_*` / `LD_*` loader variable. It also refuses when `stage_input` has not run
(`no_input`), when the command does not resolve (`missing_dependency`), or when the GraphRAG
installed in the command's environment is not 3.3.0 (`version_mismatch`) or cannot be told
(`version_unknown`). The version is read from that environment's
`graphrag-<version>.dist-info` (beside the script, or beside the interpreter its `#!` line
names); nothing is executed to find it. This is a guard against running the wrong release
by mistake, not proof: a dist-info can be edited, and the command could be a wrapper that
runs something else.

The run gets its own session; on timeout the whole process group is killed
(`index_timeout`). A non-zero exit is `index_failed`. Point the models at a local
OpenAI-compatible endpoint (for example Ollama's `/v1`); smaller local models give a weaker
graph.

### 3. Reading the output back

The adapter's settings select the CSV table provider; it reads GraphRAG's output tables (`documents`, `text_units`, `entities`, `relationships`, `communities`,
`community_reports`, `covariates`) as CSV with the standard library, not parquet. A symlinked or missing
table counts as empty.

`derive(vault, DeriveRequest(kind, memory_ids))` traces every row back to memories:
`text_unit_ids` → `text_units.document_id` → `documents.title` (`<id>.v<version>.<hash>.txt`).
A list cell counts only when it is `[...]` holding nothing but quoted ids, whitespace and
commas; anything else (a NumPy summary such as `['u1' 'u2' ... 'u1000']`, which hides the
elided ids, or an unquoted item) drops the row. A row
is kept only when **every** text unit behind it maps to a memory that is in the request, still
shareable and still at the version that was indexed; anything that also draws on another
file, a memory that turned private or a stale version is dropped.

- `graph`: entity rows (`name`, `entity_type`, `description`) and relation rows (`source`,
  `target`, `description`).
- `reports`: community reports (`community`, `level`, `title`, `summary` cut to
  `max_report_chars`), kept only when **everything the report may draw on** passes: the
  community's own `text_unit_ids`, the text units of each entity in `entity_ids` and each
  relationship in `relationship_ids`, the text unit of every covariate (claim) whose subject
  is one of those entities, and the same for every child community, recursively. An id that
  does not resolve, a list cell that is not a clean list, a cycle, or nesting deeper than 64
  communities drops the report. Each community is traced once and memoised, so shared
  children do not multiply the work.

The view cites exactly the memories its rows name, caps at `epistemic_cap: inference` and
`causal_cap: association`, and carries at most `max_rows` rows. Re-run steps 1 and 2 after the
vault changes; until then, changed memories simply drop out of the views.

## Candidates, never memory

`candidates(view)` returns one `IngestItem` per row with `deriver`, `kind`,
`external_version`, `sources`, `source_count`, `epistemic: inference`, `causal: none` and
`untrusted_model_output: true`. Stage them with
`memfw.ports.staging.stage_candidates(vault, "graphrag", items, proposal_id=..., source="graphrag")`;
they land under `<vault>/_candidates/graphrag/` for the existing review. Nothing here writes a
memory or raises a cap.

## Configuration

```yaml
adapters:
  enabled: [graphrag]
  graphrag:
    root: /srv/graphrag/memfw        # absolute; not inside the vault
    completion_model: qwen2.5:7b-instruct
    embedding_model: nomic-embed-text
    # api_base: http://127.0.0.1:11434/v1   # literal loopback unless cloud: true
    # embedding_api_base: ""                # defaults to api_base
    # command: graphrag              # or an absolute path to the executable
    # method: standard               # standard | fast
    # timeout: 21600
    # cloud: false
    # pass_env: [LOCAL_ENDPOINT_KEY] # variable names only
    # max_memories: 200
    # max_rows: 100
    # max_report_chars: 2000
```

Refusal codes from `available()`: `no_root`, `bad_root`, `bad_option`, `bad_endpoint`, `key_in_config`.

## Tests and fixtures

`tests/test_adapter_graphrag.py` never runs GraphRAG. The input side uses memfw's real
`export_projection`; the batch run goes through an injected runner that records argv and
environment; the output side reads `tests/fixtures/graphrag_contract/output/*.csv`. Those
tables follow GraphRAG 3.3.0's final column layout (`graphrag/data_model/schemas.py`) and
were written with pandas `to_csv`, the call its CSV table provider makes, so list cells carry
the Python and NumPy reprs GraphRAG leaves; they were not produced by an index run. Record a
real run's output before moving the pin.

# GraphRAG deriver adapter

`packages/adapters/memfw-adapter-graphrag` (import `memfw_adapter_graphrag`, version 0.1.0)
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
`files/memories/<id>.md` and copies a document into `<root>/input/<id>.v<version>.txt` only
when all three hold:

- the manifest classes it `privacy_class: none`;
- its bytes match the manifest's `content_sha256`;
- the vault, read again now, holds it as `public` or `shared`: the fail-closed reference
  reader classes it `none` and its MM0 level (`memfw.ports.privacy.metadata_level`) is
  `public` or `shared`. A memory with **no `privacy` field** takes the MM0 fallback
  `local-private` and is not copied; `local-private` and `unknown` never are.

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

- `<root>/settings.yaml` must exist and **every URL in it** (a line scan, comments included)
  must be `http`/`https` to a loopback address, with at least one present: GraphRAG's default
  model endpoint is a cloud API, so a file with no URL is refused (`no_local_endpoint`), as
  is any other host (`cloud_not_allowed`) or a `${VAR}` placeholder in a URL
  (`endpoint_placeholder`), unless the adapter settings say `cloud: true`;
- `stage_input` must have run (`no_input`);
- the command must resolve (`missing_dependency`).

A non-zero exit is `index_failed`; a timeout is `index_timeout`. Point the models at a local
OpenAI-compatible endpoint (for example Ollama's `/v1`); smaller local models give a weaker
graph. Keys, if your local endpoint needs one, stay in the environment (`pass_env` lists the
variable's name).

### 3. Reading the output back

Set `table_provider: {type: csv}` in `settings.yaml`: the adapter reads GraphRAG's output
tables (`documents`, `text_units`, `entities`, `relationships`, `communities`,
`community_reports`) as CSV with the standard library, not parquet. A symlinked or missing
table counts as empty.

`derive(vault, DeriveRequest(kind, memory_ids))` traces every row back to memories:
`text_unit_ids` → `text_units.document_id` → `documents.title` (`<id>.v<version>.txt`). A row
is kept only when **every** text unit behind it maps to a memory that is in the request, still
shareable and still at the version that was indexed; anything that also draws on another
file, a memory that turned private or a stale version is dropped.

- `graph`: entity rows (`name`, `entity_type`, `description`) and relation rows (`source`,
  `target`, `description`).
- `reports`: community reports (`community`, `level`, `title`, `summary` cut to
  `max_report_chars`), kept only when the whole community's text units pass.

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
    # command: graphrag              # or an absolute path to the executable
    # method: standard               # standard | fast
    # timeout: 21600
    # cloud: false
    # pass_env: [LOCAL_ENDPOINT_KEY] # variable names only
    # max_memories: 200
    # max_rows: 100
    # max_report_chars: 2000
```

Refusal codes from `available()`: `no_root`, `bad_root`, `bad_option`, `key_in_config`.

## Tests and fixtures

`tests/test_adapter_graphrag.py` never runs GraphRAG. The input side uses memfw's real
`export_projection`; the batch run goes through an injected runner that records argv and
environment; the output side reads `tests/fixtures/graphrag_contract/output/*.csv`. Those
tables follow GraphRAG 3.3.0's final column layout (`graphrag/data_model/schemas.py`) and
were written with pandas `to_csv`, the call its CSV table provider makes, so list cells carry
the Python and NumPy reprs GraphRAG leaves; they were not produced by an index run. Record a
real run's output before moving the pin.

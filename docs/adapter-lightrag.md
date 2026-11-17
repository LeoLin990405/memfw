# LightRAG deriver adapter

`packages/adapters/memfw-adapter-lightrag` (import `memfw_adapter_lightrag`, version 0.1.0)
implements the `Deriver` port ([ports-and-recall.md](ports-and-recall.md#24-deriver)) against
a [LightRAG](https://github.com/HKUDS/LightRAG) server. memfw never imports, vendors or starts
LightRAG: the operator runs `lightrag-server`, and the adapter talks to it over loopback HTTP
with the standard library.

| | |
|---|---|
| External project | `lightrag-server` from `lightrag-hku` 1.5.7, MIT (checked in the release's `LICENSE` and wheel metadata) |
| How it runs | A separate service the operator runs; this adapter is its HTTP client |
| Port | `Deriver` (PORT_VERSION 1), kinds `graph` and `answer` |
| Default | Off. memfw never imports the adapter |
| Telemetry | The adapter sends nothing but the requests below. LightRAG has optional Langfuse tracing that turns on only when `LANGFUSE_*` variables are set: do not set them for the server |

## Run the server on loopback, with a key

`lightrag-server` listens on `HOST=0.0.0.0` **without authentication** by default. Before
pointing memfw at it:

- bind it to loopback: `HOST=127.0.0.1` (or `--host 127.0.0.1`), default port 9621;
- give it an API key: set the `LIGHTRAG_API_KEY` environment variable for the server
  process (from your secret store; never write the value into `vault.yaml`, the adapter
  settings, a fixture or a commit);
- export the same key to the memfw process under the variable the adapter settings name in
  `api_key_env` (default `LIGHTRAG_API_KEY`). The settings hold only the variable's **name**.

The adapter refuses a `base_url` that is not `http`/`https` to a loopback address
(`bad_base_url`), so no setting can send memories to another host. By default it also
refuses to run when that variable is empty (`missing_key`); `require_api_key: false` exists
only for a server you have locked down some other way.

## What it sends and reads

- `sync(vault, memory_ids)`: one `POST /documents/text` per memory, body
  `{"text": "# <title>\n\n<body>", "file_source": "<id>.v<version>.md"}`. Only `public` or
  `shared` memories are sent: the fail-closed reference reader must class a memory `none` and
  its MM0 level must be `public` or `shared`; `local-private`, `unknown`, a `sensitivity:*`
  tag or **no `privacy` field** (MM0 fallback `local-private`) never leaves. Bodies are cut to
  `max_chars`. A 409 (that name is already there) counts as `present`.
- `derive(vault, DeriveRequest("graph", ids, {"query": ..., "mode": ...}))`:
  `POST /query/data` with `{"query", "mode", "top_k"}`. The view holds entity rows (`name`,
  `entity_type`, `description`) and relation rows (`source`, `target`, `keywords`,
  `description`), each with the memory ids it came from.
- `derive(vault, DeriveRequest("answer", ids, {"query": ...}))`: `POST /query` with
  `include_references: true`, `stream: false`. The view holds the answer (labelled
  `untrusted_model_output`) and its source ids.
- `health()`: `GET /health`, status fields only.

Every request carries `X-API-Key`. Proxy variables are ignored for the loopback host and
redirects are refused. Errors are short codes (`http_401`, `unreachable`, `bad_response`,
`response_too_large`, `query_failed`, `insert_failed`); the key never appears in a `repr`,
an error or a log.

## The server's output is data

LightRAG names a row's sources in `file_path`, several joined with `<SEP>`. A source counts
only when it parses as `<id>.v<version>.md`, the id is in the request, the memory is still
shareable, and its vault version is still the version that was sent. A **graph** row is kept
only when every one of its sources counts; anything merged with another document, a private
memory or a stale version is dropped. An **answer** is refused whole (`ValueError`) when any
reference does not count, when the server says it found no context (`llm_generated: false`),
or when it carries no references. The view cites exactly the memories its rows name; caps
are `epistemic_cap: inference` and `causal_cap: association`.

## Candidates, never memory

`candidates(view)` returns one `IngestItem` per graph row (or one for an answer) with
`deriver`, `kind`, `external_version`, `sources`, `source_count`, `epistemic: inference`,
`causal: none` and `untrusted_model_output: true`. Stage them with
`memfw.ports.staging.stage_candidates(vault, "lightrag", items, proposal_id=..., source="lightrag")`;
they land under `<vault>/_candidates/lightrag/` for the existing review. Nothing here writes a
memory or raises a cap.

## Configuration

```yaml
adapters:
  enabled: [lightrag]
  lightrag:
    base_url: "http://127.0.0.1:9621"
    api_key_env: LIGHTRAG_API_KEY   # the variable's NAME, never its value
    # require_api_key: true
    # mode: mix                     # local | global | hybrid | naive | mix
    # top_k: 20
    # timeout: 60
    # max_memories: 20
    # max_chars: 8000
    # max_rows: 50
    # max_query_chars: 1000
    # max_answer_chars: 4000
```

## Tests and fixtures

`tests/test_adapter_lightrag.py` opens no socket: an in-process opener answers from
`tests/fixtures/lightrag_contract/exchanges.json`, whose bodies follow the 1.5.7 route models
(`QueryResponse`, `QueryDataResponse`, `InsertResponse`, `/health`) and the `/query/data`
route example. They were written from that release's source, not captured from a live
server; capture real exchanges before moving the pin.

# LightRAG deriver adapter

`packages/adapters/memfw-adapter-lightrag` (import `memfw_adapter_lightrag`, version 0.1.1)
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

The adapter refuses a `base_url` that is not `http`/`https` to a literal loopback address
(`127.0.0.0/8` or `[::1]`; `localhost` and other names are refused, since a name can resolve
elsewhere) (`bad_base_url`), so no setting can send memories to another host. By default it also
refuses to run when that variable is empty (`missing_key`); `require_api_key: false` exists
only for a server you have locked down some other way.

## The server's own model hosts

Before `sync` and every query the adapter reads `GET /health` (with the key, so the server
shows its configuration) and refuses to go on unless:

- `core_version` is `1.5.7` (`version_mismatch`);
- the configuration is shown (`health_unauthenticated`) and names both `llm_binding_host` and
  `embedding_binding_host` (`health_incomplete`);
- those, a rerank host if one is set, and every host-like value in the per-role LLM settings
  are `http`/`https` to a literal loopback address (`cloud_not_allowed`). Start the server
  with, for example, `LLM_BINDING_HOST=http://127.0.0.1:11434` and
  `EMBEDDING_BINDING_HOST=http://127.0.0.1:11434` (its Ollama default names `localhost`,
  which is refused). `cloud: true` in the adapter settings lifts only this last rule.

Limits of this gate, by design:

- it trusts the server's own answer. `/health` reports what the server says it is configured
  with; a modified or misconfigured server can claim loopback hosts and still send text
  elsewhere. The gate catches a wrongly started server, not a hostile one;
- it checks only the keys listed above (`llm_binding_host`, `embedding_binding_host`, the
  rerank host and host-like keys under the per-role LLM settings). A model route the server
  configures some other way is not seen.

## What it sends and reads

- **One vault per server.** A LightRAG server (or workspace) must serve exactly one memfw
  vault: `sync` deletes every document named like this adapter's that is not current and
  shareable in *this* vault, so pointing two vaults at one server makes each delete the
  other's documents.
- `sync(vault, memory_ids)`: first lists the server's documents (`POST /documents/paginated`,
  every page) and deletes, in one `DELETE /documents/delete_document`
  (`delete_llm_cache: true`), each document named like this adapter's whose memory is gone,
  no longer shareable, or no longer at that version and hash, whether or not it is in the
  request; documents with other names are left alone. A refused deletion (`busy`,
  `not_allowed`) stops the sync before anything is sent. Then one `POST /documents/text` per
  requested memory not already there under its current name, body
  `{"text": "# <title>\n\n<body>", "file_source": "<id>.v<version>.<hash>.md"}`, where
  `<hash>` is the first 12 hex digits of the SHA-256 of that text: every change to a memory,
  versioned or not, is a new name. Only `public` or
  `shared` memories are sent: the fail-closed reference reader must class a memory `none` and
  its MM0 level must be `public` or `shared`; `local-private`, `unknown`, a `sensitivity:*`
  tag or **no `privacy` field** (MM0 fallback `local-private`) never leaves. Bodies are cut to
  `max_chars`. A 409 (that name is already there) counts as `present`. Run `sync` after the
  vault changes: until then stale server copies are only filtered out of views, not removed.
- `derive(vault, DeriveRequest("graph", ids, {"query": ..., "mode": ...}))`:
  `POST /query/data` with `{"query", "mode", "top_k"}`. The view holds entity rows (`name`,
  `entity_type`, `description`) and relation rows (`source`, `target`, `keywords`,
  `description`), each with the memory ids it came from.
- `derive(vault, DeriveRequest("answer", ids, {"query": ...}))`: `POST /query` with
  `mode: naive`, `include_references: true`, `stream: false`. **Only `naive` mode** is
  allowed for answers (any other `mode` is refused): in the graph modes the answer's context
  includes merged entity and relation descriptions from any document on the server (an older
  version, a memory that turned private, a document the operator added) that its
  `references` do not list, so the reference check below could not catch them. Graph views
  keep the configured `mode`. The view holds the answer (labelled
  `untrusted_model_output`) and its source ids.
- `health()`: `GET /health`, status fields only (the gate above uses the full answer).

Every request carries `X-API-Key`. Proxy variables are ignored for the loopback host and
redirects are refused. Errors are short codes (`http_401`, `unreachable`, `bad_response`,
`response_too_large`, `query_failed`, `insert_failed`); the key never appears in a `repr`,
an error or a log.

## The server's output is data

LightRAG names a row's sources in `file_path`, several joined with `<SEP>`. A source counts
only when it parses as `<id>.v<version>.<hash>.md`, the id is in the request, the memory is still
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
    # cloud: false                  # allow non-loopback LLM/embedding hosts on the server
```

## Tests and fixtures

`tests/test_adapter_lightrag.py` opens no socket: an in-process opener answers from
`tests/fixtures/lightrag_contract/exchanges.json`, whose bodies follow the 1.5.7 route models
(`QueryResponse`, `QueryDataResponse`, `InsertResponse`, `/health`) and the `/query/data`
route example. They were written from that release's source, not captured from a live
server; capture real exchanges before moving the pin.

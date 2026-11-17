# Qdrant recall adapter

`packages/adapters/memfw-adapter-qdrant` (import `memfw_adapter_qdrant`, version 0.1.0) is a
semantic `RecallProvider` (see [Ports and the recall coordinator](ports-and-recall.md)) backed by
a [Qdrant](https://github.com/qdrant/qdrant) service that the operator runs. It embeds the query
with memfw's own embedder, asks Qdrant for the nearest points of this vault and tenant, and
returns `memory_id` refs with Qdrant's scores and `why: semantic`, never text. memfw never
imports it; it is **off** until `vault.yaml` names it.

| | |
|---|---|
| External project | Qdrant server **v1.20.x** (fixtures from v1.20.2), Apache-2.0, `github.com/qdrant/qdrant` (licence re-read from its `LICENSE`) |
| Client | none: REST with the standard library (`urllib`); PyYAML 6.0.3 (MIT) reads `vault.yaml` exactly as memfw does. `qdrant-client` 1.20.0 is Apache-2.0 too but is not used, so the adapter controls redirects and proxies itself and pulls in no gRPC, httpx or numpy |
| Port | `RecallProvider`, `PORT_VERSION` 1 (`memfw-ports` 0.3.0) |
| Coupling | cross-process service over HTTP (architecture section 3) |
| Default | off; with it off every output is byte-identical |

## Run Qdrant (loopback, telemetry off)

Qdrant reports anonymous usage telemetry unless it is disabled; disable it with
`QDRANT__TELEMETRY_DISABLED=true` (or `telemetry_disabled: true` in its `config.yaml`). Bind it
to loopback only:

```sh
docker run -d --name memfw-qdrant -p 127.0.0.1:6333:6333 \
  -e QDRANT__TELEMETRY_DISABLED=true \
  -v "$HOME/.local/share/memfw/qdrant:/qdrant/storage" qdrant/qdrant:v1.20.2
```

The adapter cannot see the server's telemetry setting; it is the operator's to set. Its
availability check reads the server version from `GET /` and refuses anything outside 1.20.x
(`unsupported_version`).

## Fill the collection

```sh
export MEMFW_EMBEDDING_URL=...        # the same embedder the memfw server uses
memfw-adapter-qdrant sync --vault /path/to/vault     # status: availability only
```

`sync` (`memfw_adapter_qdrant.sync`) makes the collection hold exactly this vault's sendable
memories:

1. creates the collection (embedder dimension, cosine) and keyword payload indexes on `vault`,
   `tenant`, `embedder` and `memory_id` when it is missing; a different dimension refuses
   (`dimension_mismatch`);
2. lists this vault's points (`scroll`, payload `memory_id`, `sha`, `embedder`; no vectors);
3. embeds and upserts each memory whose content hash or embedder changed. Payload: `memory_id`,
   `tenant`, `vault` (first 24 hex digits of sha256 of the vault's resolved path), `embedder`
   (16 hex digits of the embedder fingerprint) and `sha` (hash of tenant and text). **No text is
   stored in Qdrant**; point ids are UUIDv5 of vault key and memory id (idempotent);
4. deletes this vault's points that are no longer sendable (removed, unreadable, now
   `local-private` or `unknown`).

**Never `local-private`.** Only memories whose level (`memfw.ports.privacy.effective_level`,
the strictest of the `privacy` field or the vault's `legacy_privacy_default`, the `privacy:`
and `sensitivity:` tags and a top-level `sensitivity` key) is in `clearance` are embedded and
sent. `clearance` is `public`, `shared` or both; a `clearance` naming `local-private` is refused
when the adapter is built. `unknown` never goes. Memory files are read with the fail-closed
reference reader; one it cannot parse is not sent.

The embedder sees the text of sendable memories (and, at search time, the query), exactly the
endpoint the memfw server's hybrid search already uses. No new model is added.

## Enable it

```yaml
runtime:
  recall:
    adapters:
      - factory: "memfw_adapter_qdrant:factory"
        options: {url: "http://127.0.0.1:6333", collection: memfw}
        weight: 1.0
        timeout_seconds: 3.0
```

The MCP `memory_search` `mode=two_stage` coordinator then runs it beside FTS (and hybrid) and
fuses the lists (the `runtime.recall.adapters` loader; see
[§4.1b in Ports and the recall coordinator](ports-and-recall.md)). The runtime passes its own
embedder (`MEMFW_EMBEDDING_*`) to the factory; outside the runtime (the `sync` command) the same
variables build a standard-library client of the same endpoint whose fingerprint equals memfw's
`HttpEmbeddingProvider`, so vectors and queries agree. Every point carries the fingerprint and a
search matches only points of the current one, so a changed embedder returns nothing until
`sync` re-embeds.

Options (anything else is refused):

| Option | Default | Meaning |
|---|---|---|
| `url` | `http://127.0.0.1:6333` | REST endpoint; `http`/`https`, no path, query or credentials |
| `allow_remote` | `false` | a host that is not a loopback literal is refused unless `true` |
| `collection` | `memfw` | collection name (several vaults may share one) |
| `api_key_env` | none | name of the environment variable holding the API key (sent as `api-key`); the key is never a setting |
| `clearance` | `public,shared` | levels to send: `public`, `shared` or both |
| `timeout_seconds` | 2.0 | per-request timeout (the coordinator's budget still applies) |
| `name` | `qdrant` | provider name |

## The search request

`POST /collections/{collection}/points/query` with the query vector, `limit`, a `must` filter
on `vault`, `embedder` and the caller's `tenant` (exact), `with_payload: ["memory_id",
"tenant"]` and `with_vector: false` (the REST name of "no vectors"). The response is data: a
point without a valid memory id, of another tenant, with a non-numeric score or seen twice is
dropped. Every candidate is then resolved against the vault and filtered by the coordinator's
policy (validity, lifecycle, `as_of`, source, privacy) and by the MCP server's clearance.

## Network rules

On the literal host of `url`, never DNS, the same rules as memfw's `notes/http.open_http`:

- loopback (`localhost`, `127.0.0.0/8`, `::1`, `::ffff:127.x`) by default, anything else only
  with `allow_remote: true`;
- a loopback request never goes through `HTTP(S)_PROXY`; a remote one keeps the environment's
  proxy settings;
- redirects are refused (urllib would copy the `api-key` header);
- the URL comes only from configuration, never from a request (SSRF).

## Degrade (T7)

`available()` checks the vault and the embedder, then `GET /` (version) and
`GET /collections/{c}/exists`, with a one-second timeout, and reuses a success for 30 s. When the
service is down it reports `unreachable` (`no_collection`, `unauthorized`,
`unsupported_version`, `http_<status>` otherwise); a failing search raises
`ProviderUnavailable` with the same code. The coordinator labels it
`unavailable:<code>`, sets `degraded`, and FTS answers.

## Tests

`tests/test_adapter_qdrant.py`, no real Qdrant:

- **Recorded exchanges.** `tests/fixtures/qdrant_contract/v1.20.2.json` holds the requests and
  responses of the calls above for Qdrant 1.20.2. A replay server answers exactly what was
  recorded; the tests check the adapter's requests against the recorded ones and parse the
  recorded responses. The fixture was transcribed from the v1.20.2 OpenAPI schema, not captured
  from a running server; re-record it against a live 1.20.x server before upgrading the pin.
- **Fake server.** `memfw_adapter_qdrant.fake.FakeQdrant` (loopback, exact cosine, keyword
  filters) runs the behaviour tests; one test checks that it answers in the recorded shapes.
- Covered: the contract kit with the vault byte-identical, the reference-only request, no
  `local-private` or `unknown` memory or any text reaching the service or the embedder, sync
  idempotence and deletes, vault and embedder isolation, settings and URL rules, proxy bypass
  for loopback only, refused redirects, every availability code, T7 through the coordinator,
  hostile responses, the embedder fingerprint, the CLI, the registry spec, and the runtime
  wiring (`runtime.recall.adapters`; skipped until that loader is on main).
- `tests/standalone/wheel_smoke.py --qdrant` runs it from its wheel without memfw against the
  fake.

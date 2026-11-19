# Qdrant recall adapter

`packages/adapters/memfw-adapter-qdrant` (import `memfw_adapter_qdrant`, version 0.1.2) is a
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
   `local-private` or `unknown`). This runs **first**, right after the listing, so a failing
   embedder or upsert cannot keep a withdrawn memory in Qdrant.

Step 3 runs **batch by batch** (`embedding_batch_size` memories, default 16), **shortest text
first** (then id): each batch is embedded and upserted before the next starts, so a later failure
keeps what was already written. Shortest first because on a slow host embedding time grows with
the text: short memories land before a budget runs out, and a long one that keeps timing out sits
at the tail instead of blocking the rest. Failures:

- a failed batch of several memories is **retried once, one memory at a time**, so one bad memory
  cannot sink the other fifteen;
- HTTP 4xx (`embedding_rejected`: the endpoint refuses that input, e.g. longer than the model's
  context) skips that memory (`rejected`) and does **not** count as the endpoint being down;
- timeouts, connection errors, 5xx and malformed answers do count: **three in a row** stop the
  run (`stopped: failures`); the endpoint is most likely down;
- a Qdrant error stops the run at once (earlier batches stay).

`--max-seconds S` stops cleanly before the next request once `S` seconds are spent. The printed
report adds `embedded`, `failed_batches` (failed embedding requests, retries included),
`failures` (code → count), `remaining` (still to embed), `rejected` (of those, refused by the
endpoint) and `stopped` (`max_seconds`, `failures` or empty); when anything remains the exit
status is **1**. Re-run `sync`: written points are `unchanged` by their hash, so the next run
embeds exactly what is still missing (a rejected memory is tried again each run, which costs
one quick 4xx).

Only one `sync` per Qdrant URL, collection and vault runs at a time: the CLI holds a
non-blocking `flock` on a file under `$XDG_STATE_HOME/memfw-adapter-qdrant/` (default
`~/.local/state/…`, never in the vault); a second run exits with status **3**. The kernel
releases the lock when the process ends, so a crashed run never leaves it held. Without `fcntl`
(Windows) there is no lock. On a schedule, keep the interval at least `--max-seconds` plus one
`--embedding-timeout` (a request in flight when the budget runs out still finishes or times out).

### Embedding timeout and batch size

| Setting | Option (wins) | Environment | CLI | Default | Range |
|---|---|---|---|---|---|
| request timeout | `embedding_timeout_seconds` | `MEMFW_EMBEDDING_TIMEOUT` | `--embedding-timeout` | 30 s | (0, 600] |
| texts per request (and per upsert) | `embedding_batch_size` | `MEMFW_EMBEDDING_BATCH_SIZE` | `--embedding-batch-size` | 16 | [1, 256] |

Defaults are unchanged: 16 texts per embedding request with a 30 s timeout. Differences from
earlier releases: upserts go per embedding batch (16 points by default) instead of 64 at a time,
memories are embedded shortest first, and a value out of range is refused (`ValueError`, also
from the `EnvEmbedder` constructor, which used to clamp a batch size below 1; CLI exit 2).
Neither setting changes a vector, so the embedder **fingerprint is unchanged** and existing
points stay valid.

**Rule:** batch size × seconds per memory (for the longest memories) must stay below the
timeout. When `sync` reports `embedding_unreachable` with a batch above 1 it says to lower
`--embedding-batch-size`.

They configure the adapter's own embedder (the standard-library client built from
`MEMFW_EMBEDDING_*`: the `sync` command, and the runtime when it has no embedder of its own).
A search embeds its one query with the same client but never waits longer than the
coordinator's budget for the provider: the query request's timeout is `min(timeout,
budget.timeout_seconds)` (1.5 s by default), so a long sync timeout cannot keep the provider's
worker busy. One query is one request, so the batch size does not matter at search time. An embedder the
runtime injects (memfw's `HttpEmbeddingProvider`: 120 s timeout, 3 retries) is used as given and
never modified; with it the timeout option has no effect, and `embedding_batch_size` only sets
how many memories `sync` embeds and upserts at a time.

### Slow CPU host

On a CPU-only host without AVX one long memory (about 900 tokens) can take tens of seconds to
embed, so a batch of 16 never finishes within 30 s and every incremental `sync` fails with
`embedding_unreachable`. There:

```sh
memfw-adapter-qdrant sync --vault /path/to/vault \
  --embedding-batch-size 1 --embedding-timeout 300   # >= 120; and a --max-seconds budget if run on a schedule
```

Prefer these **flags** on the `sync` command (or its scheduled job) to
`MEMFW_EMBEDDING_TIMEOUT` / `MEMFW_EMBEDDING_BATCH_SIZE` in an environment shared with the memfw
server: the flags change only the sync. For the
first, bulk sync of a large vault, run `sync` once against a faster embedding host that serves
**the same model file** (same model name and dimension, no instructions), pointing at the same
Qdrant collection, then let the slow host keep it up to date. The vectors are interchangeable
because the fingerprint is `model|dimension|instructions`: the same blob gives the same
fingerprint and the slow host's next `sync` sees every point as `unchanged`. A different model,
quantisation or dimension gives other vectors, so check that the model name and file really are
identical; a changed name gives a new fingerprint and a full re-embed.

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
| `api_key_env` | none | name of the environment variable holding the API key (sent as `api-key`); the key is never a setting; a remote URL must then be `https` |
| `clearance` | `public,shared` | levels to send: `public`, `shared` or both |
| `timeout_seconds` | 2.0 | per-request timeout to Qdrant (the coordinator's budget still applies) |
| `embedding_timeout_seconds` | `MEMFW_EMBEDDING_TIMEOUT`, else 30 | timeout of one embedding request, (0, 600]; the adapter's own embedder only (see above) |
| `embedding_batch_size` | `MEMFW_EMBEDDING_BATCH_SIZE`, else 16 | texts per embedding request and points per `sync` upsert, [1, 256] |
| `name` | `qdrant` | provider name |
| `vault_id` | `vault.yaml` `vault_id`, else none | key for this vault's points; **required with `allow_remote`** (a shared remote collection must not key by a host path) |

## The search request

`POST /collections/{collection}/points/query` with the query vector, `limit`, a `must` filter
on `vault`, `embedder` and the caller's `tenant` (exact), `with_payload: ["memory_id",
"tenant", "vault", "embedder"]` and `with_vector: false` (the REST name of "no vectors"). The
response is data: a point without a valid memory id, of another tenant, vault or embedder,
with a non-numeric score or seen twice is dropped. Every candidate is then resolved against the vault and filtered by the coordinator's
policy (validity, lifecycle, `as_of`, source, privacy) and by the MCP server's clearance.

## Network rules

On the literal host of `url`, never DNS, the same rules as memfw's `notes/http.open_http`:

- loopback (`localhost`, `127.0.0.0/8`, `::1`, `::ffff:127.x`) by default, anything else only
  with `allow_remote: true`;
- a loopback request never goes through `HTTP(S)_PROXY`; a remote one keeps the environment's
  proxy settings;
- redirects are refused (urllib would copy the `api-key` header);
- the URL comes only from configuration, never from a request (SSRF).

## What stays in Qdrant until the next `sync`

Qdrant is an external store that memfw does not write on its own. When a memory is forgotten,
deleted or made `local-private`, its point (ids, tenant, hashes and its **vector**) stays in the
collection until the next `memfw-adapter-qdrant sync`, which deletes it before it embeds or
sends anything else. memfw's forget residue check covers the vault and memfw's own projections,
not external stores. Run `sync` after a forget or a privacy tightening (or on a schedule), and
treat the collection as holding whatever the last successful `sync` saw. Searches still never
return such a point to a caller: every hit is resolved against the vault and filtered again.

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
  recorded responses. **The fixture was transcribed from the v1.20.2 OpenAPI schema, not
  captured from a running server.** Re-record it against a live 1.20.x server before enabling
  the adapter on a real vault, and again before upgrading the pin.
- **Fake server.** `memfw_adapter_qdrant.fake.FakeQdrant` (loopback, exact cosine, keyword
  filters) runs the behaviour tests; one test checks that it answers in the recorded shapes.
- Covered: the contract kit with the vault byte-identical, the reference-only request, no
  `local-private` or `unknown` memory or any text reaching the service or the embedder, sync
  idempotence and deletes, vault and embedder isolation, settings and URL rules, proxy bypass
  for loopback only, refused redirects, every availability code, T7 through the coordinator,
  hostile responses, the embedder fingerprint, the CLI, the registry spec, and the runtime
  wiring (`runtime.recall.adapters`); embedding timeout and batch size (defaults 30 s / 16 per
  request, validation, options over environment, fingerprint unchanged, the query timeout), a
  slow embedder that needs batch 1 and a long timeout, a failed batch that keeps earlier upserts
  and resumes, one rejected memory in a batch of 16 (the other 15 land), rejected memories at
  the head (the rest land), 4xx vs 5xx, the stop after three failures (also with nothing
  after), shortest first, `max_seconds`, the query timeout capped by the budget and the sync
  lock (exit 3).
- `tests/standalone/wheel_smoke.py --qdrant` runs it from its wheel without memfw against the
  fake.

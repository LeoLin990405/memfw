# memfw-adapter-qdrant

A `RecallProvider` for the memfw recall coordinator backed by a
[Qdrant](https://github.com/qdrant/qdrant) service you run yourself, pinned to
the **v1.20.x** server line. It embeds the query with memfw's own embedder
(`MEMFW_EMBEDDING_*`; no new model), asks Qdrant over REST for the nearest
points of this vault and tenant with `with_payload: ["memory_id", "tenant", "vault", "embedder"]`
and `with_vector: false`, and returns memory ids and scores only. Import it as
`memfw_adapter_qdrant`.

Licences (re-checked from each project's `LICENSE`): the Qdrant server and the
`qdrant-client` Python package are Apache-2.0. This adapter does not use
`qdrant-client`: it speaks the REST API with the standard library, so it can
refuse redirects and keep loopback requests off any `HTTP(S)_PROXY`. This
package is Apache-2.0, like memfw, and depends only on `memfw-ports` and
PyYAML 6.0.3 (MIT, to read `vault.yaml` exactly as memfw does).

- **Loopback only by default.** `url` defaults to `http://127.0.0.1:6333`; any
  other host is refused unless `allow_remote: true`. Run the server bound to
  loopback with telemetry disabled:

  ```sh
  docker run -d --name memfw-qdrant -p 127.0.0.1:6333:6333 \
    -e QDRANT__TELEMETRY_DISABLED=true \
    -v "$HOME/.local/share/memfw/qdrant:/qdrant/storage" qdrant/qdrant:v1.20.2
  ```

- **Never `local-private`.** `memfw-adapter-qdrant sync --vault PATH` writes
  only memories whose privacy level is `public` or `shared` (the `clearance`
  setting cannot include `local-private`), stores no text (payload: id, tenant,
  vault key, embedder fingerprint, content hash) and deletes points of
  memories that are no longer sendable.
- **Remote needs `vault_id`.** A non-loopback `url` needs `allow_remote: true`
  and a `vault_id` (default: `vault.yaml`'s `vault_id`), so hosts sharing a
  collection never collide; an `api_key_env` then needs `https`.
- **Until the next `sync`.** A forgotten or newly private memory's point
  (vector, ids, hashes) stays in Qdrant until `sync` runs; memfw's forget
  residue check does not cover external stores.
- **Off by default.** Enable it in `vault.yaml`:

  ```yaml
  runtime:
    recall:
      adapters:
        - factory: "memfw_adapter_qdrant:factory"
          options: {url: "http://127.0.0.1:6333", collection: memfw}
  ```

- **Incremental `sync`.** Shortest memories first; each embedding batch is
  upserted as soon as it is embedded. A failed batch is retried one memory at
  a time; a memory the endpoint refuses (HTTP 4xx) is skipped, and only
  timeouts, connection errors and 5xx (three in a row) stop the run. What is
  left is reported (`failed_batches`, `remaining`, `rejected`, exit 1) and the
  next run continues with it. `--max-seconds` stops cleanly between requests.
  One `sync` per vault and collection at a time (a second exits 3); on a
  schedule keep the interval at least `--max-seconds` plus one timeout.

## Slow CPU host

Embedding timeout and batch size default to 30 s and 16 texts per request.
Set them with the options `embedding_timeout_seconds` (0–600] and
`embedding_batch_size` [1–256], the environment `MEMFW_EMBEDDING_TIMEOUT` /
`MEMFW_EMBEDDING_BATCH_SIZE`, or `sync --embedding-timeout` /
`--embedding-batch-size` (options and flags win over the environment). On a
CPU-only host without AVX, where one long memory takes tens of seconds:

```sh
memfw-adapter-qdrant sync --vault PATH --embedding-batch-size 1 --embedding-timeout 300
```

(a timeout of at least 120 s; rule: batch size x seconds per memory below the
timeout). Prefer the flags to the environment variables when the environment
is shared with the memfw server. Do the first, bulk sync against a faster
embedding host serving the same model file into the same collection: the
vectors are interchangeable because the fingerprint is
`model|dimension|instructions`, so the slow host then only embeds new or
changed memories. Neither setting changes the fingerprint. They apply to the
adapter's own embedder; a search embeds its query with it but never longer
than the coordinator's budget. An embedder the memfw runtime injects keeps
its own settings.

`memfw_adapter_qdrant.fake.FakeQdrant` is a loopback-only fake of the REST
calls the adapter makes, for tests. See `docs/adapter-qdrant.md` in the memfw
repository.

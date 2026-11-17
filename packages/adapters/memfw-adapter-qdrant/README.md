# memfw-adapter-qdrant

A `RecallProvider` for the memfw recall coordinator backed by a
[Qdrant](https://github.com/qdrant/qdrant) service you run yourself, pinned to
the **v1.20.x** server line. It embeds the query with memfw's own embedder
(`MEMFW_EMBEDDING_*`; no new model), asks Qdrant over REST for the nearest
points of this vault and tenant with `with_payload: ["memory_id", "tenant"]`
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
- **Off by default.** Enable it in `vault.yaml`:

  ```yaml
  runtime:
    recall:
      adapters:
        - factory: "memfw_adapter_qdrant:factory"
          options: {url: "http://127.0.0.1:6333", collection: memfw}
  ```

`memfw_adapter_qdrant.fake.FakeQdrant` is a loopback-only fake of the REST
calls the adapter makes, for tests. See `docs/adapter-qdrant.md` in the memfw
repository.

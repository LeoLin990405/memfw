# memfw-adapter-lightrag

A memfw `Deriver` that reads entity/relationship graphs (kind `graph`, `POST /query/data`)
and answers (kind `answer`, `POST /query`) from a [LightRAG](https://github.com/HKUDS/LightRAG)
server, over literal-loopback HTTP only. `sync()` deletes the server's stale copies and offers
`public` / `shared` memories as `<id>.v<version>.<hash>.md`; every view row is mapped back to
the memory and exact text it came from, and anything that names another document, a private
memory or a stale copy is dropped (graph) or refused (answer, `naive` mode only). Nothing is
sent to a server whose `/health` shows another release or non-loopback model hosts. Rows become memory only as staged candidates
(`candidates(view)`).

- **External project:** LightRAG `lightrag-server` 1.5.7 (`lightrag-hku`), MIT (verified
  from the release's `LICENSE` and wheel metadata). Run it yourself; memfw never starts it.
- **Bind it to loopback and set a key.** The server defaults to `HOST=0.0.0.0` without
  authentication. Start it with `HOST=127.0.0.1` and `LIGHTRAG_API_KEY` set; this adapter
  refuses a non-loopback `base_url` and, by default, refuses to run without a key in the
  environment variable named by `api_key_env`.
- **Off by default.** Standard library only; depends on `memfw-ports`. Import it as
  `memfw_adapter_lightrag`.

See `docs/adapter-lightrag.md` in the memfw repository.

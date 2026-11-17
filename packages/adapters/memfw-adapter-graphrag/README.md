# memfw-adapter-graphrag

A memfw `Deriver` over [Microsoft GraphRAG](https://github.com/microsoft/graphrag), run as an
offline batch job:

1. `stage_input(projection_generation, vault)` copies the `public` / `shared` documents of a
   `memfw projection export` generation into `<root>/input/<id>.v<version>.txt` (manifest
   class, file hash and the vault's current privacy are all checked);
2. `write_settings()` writes `<root>/settings.yaml` from the adapter's own settings (explicit
   literal-loopback `api_base` for both models unless `cloud: true`, CSV tables, fixed
   directories), and `run_index()` runs `graphrag index --root <root>` as a subprocess only if
   that file is unchanged and no `.env` is present, with a small environment and
   `DO_NOT_TRACK=1`;
3. `derive(vault, DeriveRequest("graph" | "reports", ids))` reads the CSV output tables back,
   keeping only rows whose every source is a requested, still-shareable memory at the indexed
   version. Rows become memory only as staged candidates (`candidates(view)`).

- **External project:** GraphRAG `3.3.0`, MIT (verified from the release's `LICENSE` and wheel
  metadata). Install it in its own environment. The adapter reads its CSV tables with the
  standard library, not parquet.
- **Telemetry:** none found in the `graphrag` package itself; its model layer
  (`graphrag-llm` and that package's dependencies) was not audited. The run's environment
  carries `DO_NOT_TRACK=1`.
- **Off by default.** Standard library only; depends on `memfw-ports`.

See `docs/adapter-graphrag.md` in the memfw repository.

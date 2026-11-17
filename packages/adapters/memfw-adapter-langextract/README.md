# memfw-adapter-langextract

A memfw `Deriver` that extracts entities and relations from `public` / `shared` memories
with [LangExtract](https://github.com/google/langextract) and returns a read-only view in
which every row points at a character span of a memory body. Rows become memory only as
candidates through memfw's staging (`candidates(view)` then
`memfw.ports.staging.stage_candidates`).

- **External project:** LangExtract `1.7.2`, Apache-2.0 (verified from the release's
  `LICENSE` and wheel metadata). Install it yourself: `pip install langextract==1.7.2`.
  Without it, or with another version, the deriver reports itself unavailable.
- **Model:** a memfw `ModelBackend` named in the settings. The bundled
  `memfw_adapter_langextract.ollama:factory` talks to Ollama on loopback only. A backend
  that is not `local` needs `cloud: true`.
- **Off by default.** Standard library only; depends on `memfw-ports`. Import it as
  `memfw_adapter_langextract`; importing it never imports LangExtract.

```yaml
adapters:
  enabled: [langextract]
  langextract:
    backend: "memfw_adapter_langextract.ollama:factory"
    backend_options: {model: "qwen2.5:7b-instruct"}
```

See `docs/adapter-langextract.md` in the memfw repository for the privacy rule, the
per-run token budget, the view format and the refusal codes.

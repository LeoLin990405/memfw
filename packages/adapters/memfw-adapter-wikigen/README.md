# memfw-adapter-wikigen

A memfw `Deriver` (kind `wiki_page`) for the LLM Wiki. It reads one `memfw-wiki-job/1`
package (released text only), asks a model for the sentences of one page, and returns one
`memfw-wiki-page/1` page in which every sentence cites the job's own sources. The
open-question/conflict section and the source list are written by the program, not the
model; a sentence whose citation is not exactly one of the job's refs is dropped, never
repaired. The page becomes a reviewable candidate through memfw's staging
(`candidates(view)` then `memfw.ports.staging.stage_candidates`), never memory.

- **Model:** a memfw `ModelBackend` named in the settings. The bundled
  `memfw_adapter_wikigen.ollama:factory` (the default) talks to Ollama on a literal
  loopback address only, ignores proxy variables and refuses redirects; it reports the
  model digest, which must match the job's. `memfw_adapter_wikigen.stub:factory` runs no
  model (tests, dry runs). A backend that is not `local` needs `cloud: true` and an
  all-`public` job, and then gets only the concept label and open-question ids/conflict
  refs (no aliases, definition or question text) and a previous page only if it is
  `public`. `local` is the backend's own claim, trusted as configured by the operator.
- **Page privacy** is the strictest level over all the job's sources (everything the model
  saw), and the page lists all of them, cited or not.
- **Output-affecting settings** (instruction text, `temperature`, `seed`, `num_ctx`,
  `include_previous`, `max_claims_per_section`, `max_output_tokens`, model tag, cloud
  gate) are hashed into `prompt_version` = `wiki-v1+<sha8>` and so into the job digest;
  token budget, per-call timeout and a job deadline bound every run. Only transport errors
  and malformed JSON are retried (same seed); other bad replies are refused at once.
- **Off by default.** Standard library only; depends on `memfw-ports`. Without a reachable
  Ollama (or with the model missing) the deriver reports itself unavailable.

```yaml
adapters:
  enabled: [wikigen]
  wikigen:
    backend: "memfw_adapter_wikigen.ollama:factory"
    backend_options: {model: "qwen2.5:7b-instruct"}
```

See `docs/adapter-wikigen.md` in the memfw repository for the steps, the sentence rules,
the configuration and the refusal codes.

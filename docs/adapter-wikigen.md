# Wiki page generator adapter (wikigen)

`packages/adapters/memfw-adapter-wikigen` (import `memfw_adapter_wikigen`, version 0.1.0)
implements the `Deriver` port ([ports-and-recall.md](ports-and-recall.md#24-deriver)) for
the LLM Wiki (v0.5.0, PR W1-4). It reads one `memfw-wiki-job/1` work package, asks a model
for the sentences of one page about one ontology concept, and returns one
`memfw-wiki-page/1` page as a read-only `DerivedView`. A page is never memory and never
evidence: it becomes a staged candidate that a person reviews per section.

| | |
|---|---|
| External project | None. Standard library only; depends on `memfw-ports` 0.4.0 |
| Port | `Deriver` (PORT_VERSION 1, kind `wiki_page`); ships two `ModelBackend`s (PORT_VERSION 1): Ollama on literal loopback, and a model-free stub |
| Where it runs | On a worker machine that holds the model (a local Ollama). Never on the storage host. The worker reads job packages only; it never opens the vault |
| Default | Off. memfw never imports the adapter; `cloud` is `false` |
| Telemetry | None |

## What it does

`generate(job)` (or `derive(vault, DeriveRequest("wiki_page", (), {"job": <JSON>}))`, or
`{"job_path": <file>}`; `vault` is never read and other options are ignored):

1. **Identity.** A job names the generator it was built for: `generator`,
   `generator_version`, `model`, `model_digest` and `prompt_version`, all inside its
   `job_digest`. The deriver computes its own block (`generator_config()`; job builders
   use it) from the backend's model tag, the digest the backend reports, and a
   `prompt_version` of the form `wiki-v1+<sha8>`: `sha8` is the first 8 hex characters of
   the sha256 over the rendered instruction text plus a canonical JSON of every setting that
   can change the output: `temperature` (the Python repr of the float, e.g. `"0.0"`),
   `seed`, `num_ctx`, `include_previous`, `max_claims_per_section`, `max_output_tokens`,
   `model` (the backend's model tag) and `cloud_gate` (whether the cloud redaction of step 2
   applies, since it changes the data block). The raw settings are in the run report under
   `prompt_settings`. Any difference refuses the job before a model call:
   `model_digest_mismatch` or `generator_mismatch:<field>`. A worker never swaps models,
   prompts or settings silently, and changing any of them changes `prompt_version`, hence
   the job digest and the candidate id: it is new work.
2. **Privacy.** A job holds released (`public` / `shared`) text only (`memfw-wiki-job/1`
   refuses anything else); the prompt carries the concept's labels and definition and each
   source's ref, kind, status, causal grade and text, never the tenant, job id, view version
   or a path. Of the previous page only the sentences whose every citation ref is exactly
   one of this job's source refs enter the prompt; the others are left out and counted
   (`prompt.previous_claims_dropped` in the report). A previous page whose `privacy` is
   stricter than every job source is left out of the prompt (`previous_stricter`) rather
   than raising the page's level: the page's privacy is the strictest of the job's sources.

   **Cloud gate.** A backend whose `local` is not `True` needs `cloud: true` in the
   settings, and then:
   - every source of the job must be `public` (`cloud_needs_public`);
   - the previous page goes into the prompt only if its own `privacy` is `public`;
     otherwise it is omitted and the report records `previous_not_public`;
   - the concept's aliases and definition and the open questions' `question` / `missing`
     free text are never sent: only the concept label (`pref_label`) and, per open
     question, its id and `conflict_refs`. The program still writes the `open` conflict
     claims from the job locally (step 7).

   `local` is operator-trusted: the adapter believes a backend's own `local` attribute. The
   operator chooses which backend module is configured, and a backend that claims to be
   local when it is not defeats the cloud gate. The bundled Ollama backend enforces a
   literal loopback address; the stub talks to nothing.
3. **Prompt** (`wiki-v1`). Fixed instructions force the structure: the model answers one
   JSON object with `summary`, `facts`, `timeline`, `relations` and `missing_evidence`
   (at most one sentence per open question); each sentence cites 1–8 refs **copied exactly**
   from the job. The model is told not to write open questions, conflicts or a source
   list. The job's data goes in one `<data>` block, JSON with `<`/`>` escaped, so a source
   text cannot close it; everything in it is data, not instructions.
4. **Budget and time.** Before a call, the estimated prompt plus `max_output_tokens` must
   fit `num_ctx` (`context_too_small`; Ollama would otherwise cut the prompt silently) and
   the job's token budget `max_run_tokens` (`BackendBudgetExhausted`, all attempts
   together). A reply whose reported tokens reach `num_ctx` is refused
   (`context_overflow`). Each call gets `min(request_timeout, time left)`; the job stops at
   `deadline_seconds` (`deadline`).
5. **Model.** `backend.run("wiki_page_generate", {"system", "prompt", "options":
   {"temperature", "seed", "num_ctx"}, "timeout"}, max_tokens=max_output_tokens)`. A call
   is retried (same seed, up to `max_attempts` in all, inside the deadline and the budget)
   only for failures that can differ between runs: transport errors from the backend
   (`BackendUnavailable` codes `unreachable`, `timeout`, `http_502`, `http_503`,
   `http_504`) and malformed JSON (`empty_reply`, `not_json`). A reply that is JSON but not
   the page shape (`unknown_key` such as `open` or `sources`, `section_not_list`,
   `duplicate_key`, `not_object`, `too_deep`, `reply_too_large`) or not text at all
   (`no_text`) is refused at once as `bad_output:<code>`; malformed JSON that persists ends
   the same way. A transport error that persists is raised as `BackendUnavailable` with
   its code. With the same seed and temperature 0 a retry may well repeat the same reply
   (for example one cut at `max_output_tokens`): the retry is for nondeterministic serving,
   not a repair.
6. **Sentences.** Each sentence is checked on its own and **dropped, never repaired**: a
   ref that is not exactly one of the job's refs (`unknown_citation`; no case folding,
   version guessing or prefix fixing), no or too many or repeated cites, a causal grade
   outside `relations`, above `hypothesis` or above its own sources' strongest, a kind not
   allowed in its section, a non-current source outside `timeline` (or a `timeline`
   sentence citing one that is not kind `superseded`), a side taken in one of the job's
   conflicts (`conflict_side`, `conflict_both`), text the ports refuse (controls, bidi
   characters, over 1000 characters), a duplicate, or one past `max_claims_per_section`.
   Drops are counted by reason; a section that lost more than half is listed in
   `needs_attention` (design 1.2).
7. **What the program writes.** The `open` section's `conflict` claims (per open question
   with two sides in the job: one claim per side, citing only that side, text = the
   question as the job states it), the page `sources` (**all** the job's sources, in job
   order, field for field, cited or not), `open_questions` (every question of the job),
   `privacy` (the strictest level over all the job's sources: everything the model saw
   counts, not only what a kept sentence cites), `built_from_digest` (over all those
   sources), the claim ids and the `build` block (`nli_model: none`, every claim
   `tokens: not_run`; scoring is a later step). Listing every job source keeps the ports
   invariant (a page's `privacy` equals the strictest of its listed `sources`; uncited
   sources are allowed). A job with many sources can therefore make the page exceed
   16 KiB, which refuses it as `page_invalid:too_large`; the source list is never trimmed.
8. **Validation.** The assembled document goes through `memfw.ports.wiki.parse_wiki_page`
   and `memfw.ports.testing.check_wiki_page(page, job=job)`. No summary sentence left:
   `summary_empty` (no retry; a deterministic model would answer the same). Anything the
   ports refuse refuses the whole page (`page_invalid:<code>`, e.g. over 16 KiB). Nothing is
   ever half-returned.
9. **Optional pre-check.** `precheck: "module:function"` names a callable
   `(WikiPage, WikiJob) -> WikiPage | None`, for example a thin wrapper around
   `memfw-wiki-gate` (W1-2) once it exists. It is used only when importable (otherwise the
   report says `not_importable`), its result is validated against the job again, and an
   error or an empty result refuses the page. The storage host re-runs every deterministic
   check at intake in any case.

The view's `content` is canonical JSON `{"format": "memfw-wikigen-view/1", "page": <page>,
"report": <counts>}`; the report holds attempts, token use, kept/offered/dropped counts by
reason, `prompt_settings`, `needs_attention` and the pre-check status, never text. The
view's citations are the refs the kept claims actually cite (a subset of the page
`sources`); its caps are `epistemic_cap: inference` and the page's strongest claim grade
(at most `hypothesis`).

## Candidates, never memory

```python
from memfw.ports.staging import stage_candidates
from memfw_adapter_wikigen import candidates

items = candidates(view)  # one IngestItem: the page's canonical JSON
stage_candidates(vault, "wikigen", items,
                 proposal_id=job.job_digest, source="wikigen")
```

The candidate's metadata holds ids and counts only: `page_key`, `tenant`, `privacy`,
`job_digest`, `generator_version`, `model`, `model_digest`, `prompt_version`, `claims`,
`sources`, `needs_attention`, `epistemic: inference`, `causal`. It does not name the
concept: `tenant` routes the candidate to that tenant's staging, and `page_key` (a hash of
tenant and concept) identifies the concept without naming it. The same job with
the same deterministic output stages the same candidate (a duplicate, not a second file).
Where candidates land on the storage host and how pages are approved is the `wiki-intake`
stage and `memfw wiki review/apply` (W1-3, W1-6), not this adapter.

## Configuration

```yaml
adapters:
  enabled: [wikigen]
  wikigen:
    backend: "memfw_adapter_wikigen.ollama:factory"     # the default
    backend_options:
      model: "qwen2.5:7b-instruct"
      base_url: "http://127.0.0.1:11434"
      # model_digest: "sha256:…"   # optional pin; any other digest on the server is refused
      # timeout: 900               # seconds per generate request
    # cloud: false
    # temperature: 0.0             # output-affecting settings: part of prompt_version and the job digest
    # seed: 0
    # num_ctx: 16384
    # max_output_tokens: 2048
    # max_run_tokens: 48000        # per job, all attempts
    # max_attempts: 2
    # request_timeout: 900
    # deadline_seconds: 1800
    # max_claims_per_section: 12
    # include_previous: true       # show the approved page the job replaces
    # precheck: ""                 # "module:function", used only when importable
```

- **Ollama backend.** Posts to `{base_url}/api/generate` (`stream: false`,
  `format: "json"`, `system` + `prompt`, `options` = temperature, seed, num_ctx and
  `num_predict`) and reads the model digest from `GET {base_url}/api/tags` (a model the
  server does not list: `model_missing`). The base URL must be plain `http` to a
  **literal** loopback address (`127.0.0.1`, `127.x.y.z`, `[::1]`); a host name (even
  `localhost`), credentials, a path, a query or a fragment is `bad_base_url`. Proxy
  variables are ignored (an empty `ProxyHandler`) and redirects are refused.
- **Stub backend.** `memfw_adapter_wikigen.stub:factory` runs no model: it copies the first
  sentence of each source and cites it, or replays canned `replies`. For tests, smoke
  runs and dry runs of the pipeline.
- **No keys in config.** `api_key`, `key`, `token`, `authorization` or `password` in the
  settings or `backend_options` is refused (`key_in_config`).
- **Unavailable, not broken.** A bad setting or an unusable backend still yields a deriver
  that reports `available() == False` with a code: `no_backend`, `bad_backend_spec`,
  `backend_not_importable`, `backend_unavailable:<code>` (for Ollama: `unreachable` when
  nothing listens, `model_missing`, `model_digest_mismatch`, `bad_base_url`),
  `backend_task`, `backend_incomplete`, `cloud_not_allowed`, `key_in_config`, `bad_option`.
  `available()` asks the backend for its model digest, so a stopped Ollama shows up there.

## Refusal codes of one job

`WikigenError.code`: `bad_job`, `job_option`, `job_unreadable`, `bad_job:<format code>`,
`model_digest_mismatch`, `generator_mismatch:<field>`, `cloud_not_allowed`,
`cloud_needs_public`, `context_too_small`, `context_overflow`, `deadline`,
`bad_output:<code>`, `summary_empty`, `page_invalid:<code>`, `precheck_error`,
`precheck_empty`, `precheck_page_invalid:<code>`. Backend failures stay
`BackendUnavailable` (short codes; a transport error that persists after the retries keeps
its code) and a spent budget `BackendBudgetExhausted`. `previous_not_public` and
`previous_stricter` are report reasons, not refusals. No code holds job or reply text.

## Known limits

- The conflict claims repeat the open question's text for each side; the sides' own
  statements are in the cited sources, not rewritten. The fixed "not a causal proof" line
  of `relations` (design 1.2) belongs to the Markdown renderer, not to the page format.
- The token estimate is conservative (about 3 characters per token, 1 per CJK character),
  not the model's tokenizer.
- With `temperature: 0` and a fixed seed Ollama is usually but not provably byte-for-byte
  repeatable across versions or hardware; the page records model digest and sampling so a
  difference is visible.
- `backend.local` is trusted, not verified (step 2): a third-party backend module that
  wrongly reports `local` bypasses the cloud gate. Configure only backends you trust.
- Because a page lists every job source, a large job can exceed the 16 KiB page limit
  (`page_invalid:too_large`); the job builder has to keep jobs small enough.
- The causal-wording vocabulary (G5), the token-consistency check and entailment scoring
  are not here: they are `memfw-wiki-gate` (W1-2) and `memfw-adapter-nli` (W1-5).

## Tests

`tests/test_adapter_wikigen.py` calls no model. The stub backend covers the end-to-end
page, the `check_deriver` and `check_wiki_page` contracts, determinism and staging; canned
replies cover every sentence rule, every whole-reply refusal, the retry, the identity
check, the budget, the deadline, the cloud gate and the pre-check. The Ollama backend runs
only against an in-process fake HTTP server on `127.0.0.1`, with every proxy variable set
to an address the suite's socket guard refuses, so a request that went through a proxy
would fail the test. The `standalone-wheels` CI job runs the deriver from its wheel without
memfw (`wheel_smoke.py --wikigen`).

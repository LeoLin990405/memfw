# LangExtract deriver adapter

`packages/adapters/memfw-adapter-langextract` (import `memfw_adapter_langextract`, version
0.1.0) implements the `Deriver` port ([ports-and-recall.md](ports-and-recall.md#24-deriver))
with [Google LangExtract](https://github.com/google/langextract). It reads the memories a
request names, asks a model for the entities and relations they state, and returns a
read-only `DerivedView` in which every row points at a character span of a memory body.
Nothing is written to the vault: rows become reviewable candidates only through memfw's
staging.

| | |
|---|---|
| External project | LangExtract `1.7.2`, Apache-2.0 (checked in the release's `LICENSE` and wheel metadata) |
| How it runs | In-process optional dependency the operator installs: `pip install langextract==1.7.2`. Not vendored, not a dependency of memfw or of this wheel |
| Port | `Deriver` (PORT_VERSION 1); ships one `ModelBackend` (PORT_VERSION 1, Ollama on loopback) |
| Default | Off. memfw never imports the adapter; importing the adapter never imports LangExtract |
| Telemetry | None found in LangExtract 1.7.2; the adapter adds none |

## What it does

`derive(vault, DeriveRequest("entities", memory_ids))`, one run:

1. **Select.** Each requested memory is read with the fail-closed reference reader. Only
   memories whose privacy is `public` or `shared` are used: the reference reader must
   class them `none` *and* the MM0 level (`memfw.ports.privacy.metadata_level`) must be
   `public` or `shared`. `local-private`, `unknown`, a `sensitivity:*` tag, an unreadable file
   or a memory with **no `privacy` field** (MM0 fallback `local-private`; the vault's
   `legacy_privacy_default` is not consulted, which can only be stricter) is never sent.
   No request option widens this (`include_private` is ignored). At most `max_memories`
   memories are used and each body is cut to `max_chars` characters before anything is sent.
2. **Extract.** LangExtract chunks each body (`max_char_buffer`), builds a few-shot prompt
   from the adapter's fixed instructions and one example, parses the model's replies and
   aligns every extraction to a character interval of the body. It runs sequentially
   (one worker, one pass), with URL fetching, progress output and schema constraints off.
3. **Model.** LangExtract's model is a bridge to a memfw `ModelBackend`: every prompt is one
   `backend.run("langextract_extract", {"prompt": ...}, max_tokens=max_output_tokens)` call.
   The backend is named in the settings (`module:factory`), as for `conversations.backend`.
4. **Budget.** Each run has a token budget (`max_run_tokens`). Before a call its worst case
   (estimated prompt plus `max_output_tokens`) must fit what is left; afterwards the reported
   usage (or the estimate) is charged. A call that does not fit raises
   `BackendBudgetExhausted` and the run returns nothing, never a partial view.
5. **View.** The view's `content` is canonical JSON (`format: memfw-langextract-v1`): one row
   per grounded extraction with `memory_id`, `class` (`entity` or `relation`), `text`,
   `start`/`end` (character offsets into the body), `alignment` and short attributes
   (`type` for an entity, `subject`/`predicate`/`object` for a relation). A row is dropped
   when LangExtract could not place it, its span falls outside the body, an exact match's
   span does not hold its text, or its class is not one of the two. The view cites each
   memory that yielded a row (with its version); its caps are `epistemic_cap: inference`
   and `causal_cap: none`.

## Candidates, never memory

```python
from memfw.ports.staging import stage_candidates
from memfw_adapter_langextract import candidates

items = candidates(view)      # one IngestItem per row
stage_candidates(vault, "langextract", items, proposal_id="lx-2026-11-17", source="langextract")
```

Each candidate carries `deriver`, `kind`, `external_version`, `memory_id`, `memory_version`,
`extraction_class`, `char_start`, `char_end`, `epistemic: inference` and `causal: none`. They
land under `<vault>/_candidates/langextract/` and go through the existing review and gates;
the adapter never raises a cap and never writes a memory, and `causal` is never upgraded.

## Configuration

```yaml
adapters:
  enabled: [langextract]
  langextract:
    backend: "memfw_adapter_langextract.ollama:factory"
    backend_options: {model: "qwen2.5:7b-instruct", base_url: "http://127.0.0.1:11434"}
    # cloud: false            # required true for a backend that is not local
    # max_run_tokens: 20000   # per derive() call
    # max_output_tokens: 1024 # per model call
    # max_memories: 20
    # max_chars: 8000         # per memory body
    # max_char_buffer: 1000   # LangExtract chunk size
```

- **Backends.** Any `ModelBackend` whose `tasks` lists `langextract_extract` works. The
  bundled `memfw_adapter_langextract.ollama:factory` is `local`: it posts to
  `{base_url}/api/generate` (`stream: false`, `temperature` 0, `num_predict` = the call's
  `max_tokens`), accepts only plain `http` to a loopback address, ignores proxy variables
  and refuses redirects. A backend whose `local` is not `True` is refused unless the
  adapter settings say `cloud: true` (the `conversations.cloud` rule). The DeepSeek adapter
  answers only the `conv-v2` tag tasks today, so it is refused here (`backend_task`).
- **No keys in config.** `api_key`, `key`, `token`, `authorization` or `password` in the
  settings or `backend_options` is refused (`key_in_config`); a backend reads its own
  credentials from its own environment, as the DeepSeek adapter does.
- **Refusals are codes.** A bad setting or an unusable backend still yields a deriver that
  reports `available() == False` with a short reason: `no_backend`, `bad_backend_spec`,
  `backend_not_importable`, `backend_unavailable:<code>`, `backend_task`,
  `cloud_not_allowed`, `key_in_config`, `bad_option`, `missing_dependency` (LangExtract not
  installed) or `version_mismatch` (installed, but not 1.7.2). It never falls back to
  another model.

## Tests and fixtures

`tests/test_adapter_langextract.py` uses no network and no model.
`tests/fixtures/langextract_contract/recorded.json` was recorded by running the adapter's
`run_langextract` under LangExtract 1.7.2 with a canned model: it holds the two prompts
LangExtract built and the annotated documents it returned (spans, alignment statuses, one
extraction it could not ground). The tests replay it through the deriver and the
`check_deriver` contract, and cover the privacy selection, the budget, the gate, hostile
output, staging and the Ollama backend. The `standalone-wheels` CI job also runs the
deriver from its wheel without memfw (`wheel_smoke.py --langextract`). Re-record the fixture
before moving the pin.

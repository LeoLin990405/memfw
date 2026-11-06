# Canonical samples for `memfw-http/2`

This directory is the MemFW-side reference set for the cross-repository contract
(`docs/plans/wave2/contract-memfw-http-v2.md`, section 9). Conductor copies these
files byte for byte into its own fixture directory and replays its client against
them, so every file here is canonical, stable and free of secrets.

## Naming

Each case is a pair:

```text
<case>.request.json     the JSON request body
<case>.response.json    the JSON response body
```

`health.ok.request.json` is `{}` (the health check has no body). The HTTP status
belongs to the case, not to the file: it is one of 200, 400, 401, 403, 404, 409,
413 and 500 as listed in the contract, and it is asserted by the replay test.

Cases: `health.ok`, `search.ok`, `search.as_of_superseded_after_t`,
`search.unauthorized`, `search.too_large`, `related.edges_ok`,
`related.fts_degraded`, `related.not_found`, `stage.ok`, `stage.duplicate`,
`stage.conflict`, `stage.forbidden_read_token`, `stage.too_large`.

## Serialisation and replay

Every file is exactly

```python
json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
```

encoded as UTF-8. The replay test
(`tests/test_readonly_service.py::test_contract_fixtures`) rebuilds a deterministic
seed vault, drives the real HTTP handler for each case (`stage.duplicate` and
`stage.conflict` after `stage.ok`, because the case is about an existing
candidate), and compares the response with `<case>.response.json`. Floating-point
fields (`score`, `raw_score`) are compared with a relative tolerance so a SQLite
patch release does not break the set; every other field, key and ordering is
compared exactly.

The seed vault is built from fixed timestamps and `MarkdownStorageMixin.render`,
so the Markdown bytes, the index projection and therefore `content_hash` and the
bm25 ranks are identical on every machine.

## Regenerating

Only after an intentional contract or seed refresh:

```sh
MEMFW_UPDATE_CONTRACT_FIXTURES=1 python -m pytest \
  tests/test_readonly_service.py -k contract_fixtures
```

The command rewrites every `*.request.json` and `*.response.json` from the same
seed. Review the diff before committing: these files are the contract both
repositories test against.

## What must never appear here

No credentials (the replay server uses fixed local test credentials and the files
carry none), no host paths, no usernames and no real memory content. The seed
documents are synthetic ("corpus keyword" notes) and are recreated by the test on
every run.

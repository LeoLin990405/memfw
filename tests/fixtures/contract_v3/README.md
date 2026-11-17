# Canonical samples for `memfw-http/3`

The MemFW-side reference set for contract `memfw-http/3`, which the service
serves beside `memfw-http/2` (see `docs/v1-integration.md`, "Contract
memfw-http/3"). Every request in this set is sent with the header
`MemFW-Contract: memfw-http/3`, except `contract.unknown`, which sends
`MemFW-Contract: memfw-http/9`. The `memfw-http/2` set in
`tests/fixtures/contract_v2/` is unchanged.

Naming, serialisation and the seed vault are those of the v2 set: each case is
`<case>.request.json` plus `<case>.response.json`, written as
`json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"`.
`context.ok` seeds two extra procedural memories (one per tenant), because the
`core` slot carries anchors and procedural memories only.

Cases and their HTTP status: `health.ok` (200), `search.ok` (200),
`search.mode_unavailable` (400), `related.edges_ok` (200),
`related.fts_degraded` (200), `context.ok` (200), `stage.ok` (200),
`forget.ok` (200), `forget.duplicate` (200, after `forget.ok`),
`forget.conflict` (409, after `forget.ok`), `forget.not_found` (404: another
tenant's memory answers like a missing one), `forget.forbidden_read_token`
(403), `contract.unknown` (400, tagged `memfw-http/2`).

The replay test (`tests/test_readonly_service_v3.py::test_contract_v3_fixtures`)
compares floats with a relative tolerance and everything else exactly. One
field depends on the clock: `context.ok` stores `generated_at` as the
placeholder `"<generated_at>"`, and the test puts the same placeholder in the
live answer before comparing. A client replaying the set ignores that one
value.

Regenerate only after an intentional contract or seed change, and review the
diff:

```sh
MEMFW_UPDATE_CONTRACT_FIXTURES=1 python -m pytest \
  tests/test_readonly_service_v3.py -k contract_v3_fixtures
```

No credentials, host paths, usernames or real memory content appear here.

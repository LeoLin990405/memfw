# Golden raw bytes for `memfw-http/2`

Raw HTTP exchanges recorded from the read-only service **as it was before
`memfw-http/3`** (the `origin/main` of the day the v3 work was reviewed). Each
case is `<case>.request.http` plus `<case>.response.http`: the exact request
bytes (tokens as the placeholders `{read_token}` / `{write_token}`) and the
exact response bytes with the `Date:` header line removed. `cases.json` holds,
per case, whether the related projection is configured, which cases run first
on the same vault (`before`), and `headers_only` (the request is answered 413
from `Content-Length` alone, so only its head is sent).

The cases are every v2 sample of `tests/fixtures/contract_v2/` plus error and
method paths: unknown path (GET and POST), `/forget` and `/context` (404 under
v2), invalid JSON, an empty query, a wrong token, PUT, DELETE and HEAD.

`tests/test_readonly_service_v3.py::test_v2_raw_bytes_equal_the_pre_v3_service`
replays each case twice against the current service, with no
`MemFW-Contract` header and with `MemFW-Contract: memfw-http/2`, and requires
the same status line, headers and body bytes. The one allowed difference is
the documented `MemFW-Contracts: memfw-http/2, memfw-http/3` header on
`GET /health`.

These files are a record of the old behaviour, not of the current code: do
not regenerate them from the current checkout. To re-record from a pre-v3
checkout (a `git worktree add --detach` of the old commit), run from that
checkout:

```sh
PYTHONPATH=src:. python <this repo>/tests/fixtures/contract_v2_raw/generate.py <out-dir>
```

No credentials, host paths, usernames or real memory content appear here.

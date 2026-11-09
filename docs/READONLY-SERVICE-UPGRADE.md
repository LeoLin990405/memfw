# Upgrading the readonly service to memfw-http/2

A pre-v2 readonly process answers `GET /health` with
`{"ok": true, "service": "memfw-readonly"}` and no `contract` field.
Conductor then treats the deployment as the wrong contract.

This repository's `scripts/memfw_readonly_service.py` already implements
`memfw-http/2`. Its `GET /health` body includes `"contract": "memfw-http/2"`
and `"service": "memfw"`, plus `index_present` and `related_index_present`.
`/health` needs no token and returns no paths or credentials.

## Deploy

Install this script in place of the pre-v2 process. Keep the vault root on
`MEMFW_READONLY_ROOT` or pass `--root` (the flag falls back to that variable).
Do not bind `0.0.0.0`; the supported unit is `scripts/memfw-readonly.service`
(loopback by default, or an explicit tailnet `--host`). Read and write tokens
must differ, and a token file must be mode `0600`.

```sh
systemctl --user daemon-reload
systemctl --user restart memfw-readonly
```

## Check a URL

`check_readonly_contract(url)` performs one `GET /health`. Pass either the
service base URL or a URL that already ends in `/health`. Redirects are not
followed. There is no default host.

```sh
.venv/bin/python scripts/memfw_readonly_service.py \
  --check-url http://127.0.0.1:<port>/health
```

Exit 0 means the body advertises `memfw-http/2`. Exit 1 means the service was
unreachable, not JSON, or still missing `contract`. The same function is what
the tests call against a local server.

Do not point the check at a live host. Tests bind `127.0.0.1` on an
ephemeral port and use a local fake for the pre-v2 body.

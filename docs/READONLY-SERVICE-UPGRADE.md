# Upgrading the readonly service to memfw-http/2

A pre-v2 readonly process answers `GET /health` with
`{"ok": true, "service": "memfw-readonly"}` and no `contract` field.
Conductor then treats the deployment as the wrong contract.

This repository's `scripts/memfw_readonly_service.py` already implements
`memfw-http/2`. Its `GET /health` body includes `"contract": "memfw-http/2"`
and `"service": "memfw"`, plus `index_present` and `related_index_present`.
`/health` needs no token and returns no paths or credentials.

## Deploy

Install the service in place of the pre-v2 process. Copy three files from
the same memfw revision into the service directory (`~/memfw-remote/` in
`scripts/memfw-readonly.service`):

1. `scripts/memfw_readonly_service.py` (the HTTP entry point);
2. `src/memfw/index/readonly.py` (the read path: query rules, `search_vault`);
3. `src/memfw/index/read_path.py` (shared read-path fields).

The two library files need only the standard library and PyYAML. The script
loads its read path from the first of: `readonly.py` beside it, the checkout's
`src/memfw/index/readonly.py`, an installed `memfw`. It logs the file it chose
at startup. With only the script copied and no `memfw` installed it exits with
an error naming the missing files.

Keep the vault root on
`MEMFW_READONLY_ROOT` or pass `--root` (the flag falls back to that variable).
Do not bind `0.0.0.0`; the supported unit is `scripts/memfw-readonly.service`
(loopback by default, or an explicit tailnet `--host`). Read and write tokens
must differ, and a token file must be mode `0600`.

```sh
systemctl --user daemon-reload
systemctl --user restart memfw-readonly
```

### Privacy clearance (MM0)

Deploy the three files **together**, from one revision. Since MM0 the script
and `readonly.py` call privacy helpers that live in `read_path.py`
(`metadata_level`, `parse_clearance`, `admits`, ...); a stale `read_path.py`
beside a new `readonly.py` fails every `/search` and `/related` with a 500.

`MEMFW_READONLY_CLEARANCE` (or `--clearance`) lists the privacy levels the
service may return: any of `public`, `shared`, `local-private`, comma
separated. Unset, nothing is filtered (the behaviour before MM0). A malformed
value stops the service at startup.

A clearance must ship together with the served vault's privacy defaults.
A memory with no `privacy` field has the level `legacy_privacy_default` from
that vault's `vault.yaml`, `local-private` when unset. So
`MEMFW_READONLY_CLEARANCE=public,shared` on a vault that does not set it
hides every memory written before MM0. Before turning the clearance on:

```sh
memfw privacy legacy-default shared --root <served vault>   # old memories stay served
memfw privacy new-default shared --root <served vault>      # optional: stamp new writes
```

`new_privacy_default` stamps a level on new memories that set none (unset:
no field is written, as before MM0). Tags still apply on top of both:
`sensitivity:private` or `privacy:local-private` makes a memory
`local-private`, so such memories stop being served under `public,shared`.

A hand-added top-level `sensitivity:` key counts too (v0.3.0): `private` (any
case) makes the memory `local-private`; any other non-empty value makes it
`unknown`, which no clearance admits. The rule lives in `read_path.py`
(`sensitivity_key_levels`), so this is another reason to deploy the three
files together. To fail closed, `read_path.py` carries `READ_PATH_VERSION`
(now `2`) and both `readonly.py` and the script refuse an older copy (the
script checks it too, so an older `readonly.py` cannot hide it): the service exits
at startup with an error naming the version instead of serving under the old
rules. A file whose front matter repeats a key (two `privacy:` or
`sensitivity:` lines) is `unknown` under a clearance, as core refuses it.
Without a clearance nothing changes.

## memfw-http/3 beside v2

The same three files serve `memfw-http/3` to clients that send the request
header `MemFW-Contract: memfw-http/3`; without it every answer is the v2 one,
byte for byte, so existing clients need no change. v3 adds `POST /forget`
(write token only; it records a deletion request under
`<vault>/_candidates/forget/` and never deletes) and `POST /context` (the
`memory_context` v2 pack). `/context` needs the `memfw` package installed
where the script runs; without it `/context` answers 404 and the v3 `/health`
leaves it out of `endpoints`. `READ_PATH_VERSION` stays `2`: v3 changed
neither `read_path.py` nor `readonly.py`, but deploy the three files from one
revision as always.

Every `/health` answer carries the header `MemFW-Contracts: memfw-http/2,
memfw-http/3`. To see the v3 body:

```sh
curl -s -H 'MemFW-Contract: memfw-http/3' http://127.0.0.1:<port>/health
```

`--check-url` still checks for `memfw-http/2`, which a v3 service keeps
serving.

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

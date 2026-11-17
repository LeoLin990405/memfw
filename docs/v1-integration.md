# MemFW V1 integration status

V1 is a **read-only federation candidate**, not a production cutover and not a
complete replacement of Conductor's proposal/approval memory.

## Stable V1 surface

- Markdown is authoritative; hash-chained JSONL events remain the audit log.
- `memory_search` defaults to FTS5 and optionally delegates to the shared
  NotesRetriever hybrid when a local OpenAI-compatible embedding endpoint is
  configured; unavailable embeddings degrade to FTS.
- `memory_get`, `memory_remember` (staging only), `memory_context`, and
  `memory_status` are available over local MCP.
- Temporal fields `valid_from`, `valid_to`, `superseded_by` live in Markdown and
  are projected separately; Conductor's existing `memory_rows` schema stays
  byte-compatible.
- A human `supersede` keeps the old body, marks its validity end, and appends a
  `supersede` event.
- Optional tenant/source allowlists are local-bound, not a centralized account
  service.

## Conductor adapter

The Conductor V1 adapter is prepared in the isolated worktree
a dedicated worktree, branch `feat/memfw-v1-federation`:

- uses the read-only HTTP service, not a vault copy or mount;
- accepts MemFW `supersede` events;
- excludes documents with `valid_to`/`superseded_by` from current recall;
- preserves temporal/tenant/related metadata in hits;
- binds `local-dev-user` to an absolute local MemFW path;
- supports `MEMFW_PRIMARY=1` for MemFW-primary recall while Conductor PG remains
  the proposal destination.

Focused Conductor federation tests: 91 passed.

## Remaining replacement gap

A full replacement needs:

1. an authoritative MemFW vault path reachable by Conductor (a local snapshot is
   not enough);
2. a write bridge from approved Conductor proposals to MemFW
   `_candidates/` / human confirmation / Markdown + event;
3. a restart/cutover window and rollback;
4. real-user evaluation of the bound vault.

Until then, V1 is read-only and does not bypass MemFW's human-confirmation
principle.


## Read-only remote service

`scripts/memfw_readonly_service.py` is a small HTTP facade over an existing
MemFW vault that implements contract **`memfw-http/2`** and, selected per
request, **`memfw-http/3`** (see "Contract memfw-http/3" below; this section
describes v2, which stays byte-identical). It uses the standard
library plus the project's PyYAML dependency and no web framework (the read
path is the library module `memfw.index.readonly`, which a standalone copy
loads from `readonly.py` and `read_path.py` beside the script); it reads the
disposable SQLite index and Markdown only, and its single write path, `/stage`,
writes one candidate file under `<vault>/_candidates/remember/` and never the
vault. Every response body, including errors, carries
`"contract": "memfw-http/2"`.

### Endpoints

| Method and path | Auth | Purpose |
|---|---|---|
| `GET /health` | none | liveness and contract version |
| `POST /search` | read or write token | FTS recall with tenant/source/temporal filters |
| `POST /related` | read or write token | related items from the edges projection, else lexical FTS |
| `POST /stage` | write token only | stage one Conductor proposal as a candidate |

Under `memfw-http/2`, `forget`/delete does not exist (contract §8): `POST
/forget` and `POST /context` return 404 as before. Under either contract no
endpoint modifies or removes authoritative memories (v3's `/forget` only
records a deletion *request*), and `DELETE`, `PUT`, `PATCH` and `HEAD` return
404. A `GET` on a `POST` endpoint also returns 404.

`GET /health` needs no token and returns only non-sensitive fields: `ok`,
`service: "memfw"`, `contract`, `index_present` (the SQLite index exists) and
`related_index_present` (the related projection exists and carries its `related_items`/`related_edges` tables). It never
returns paths, hostnames or tokens.

### Authentication and configuration

Authenticated requests send the shared token in the `X-Memfw-Token` header.

| Setting | Meaning |
|---|---|
| `X-Memfw-Token` | request header carrying the shared token |
| `MEMFW_READONLY_TOKEN` / `MEMFW_READONLY_TOKEN_FILE` | read token (environment first, then file) |
| `MEMFW_WRITE_TOKEN` / `MEMFW_WRITE_TOKEN_FILE` | write token (environment first, then file) |
| `MEMFW_READONLY_ROOT` / `--root` | vault root to serve |
| `MEMFW_RELATED_DB` / `--related-db` | optional related projection (SQLite) for `/related` |
| `--host` / `--port` | bind address and port; defaults `127.0.0.1:18080` |

- Environment variables are read before the corresponding `*_FILE`, and the
  file content is stripped of surrounding whitespace (a trailing newline is
  fine).
- `/search` and `/related` accept either the read or the write token. `/stage`
  accepts only the write token; with no write token configured it always
  answers 403 `forbidden`, and the read token on `/stage` is also 403.
- Tokens are compared with `hmac.compare_digest` and never appear in logs,
  error messages or responses. Missing or wrong tokens are 401
  `unauthorized`.

### Limits

| Item | Value |
|---|---|
| `/search` `query` | 1..512 characters |
| `/search` `scope` | optional string, ≤ 512 characters (default empty) |
| `limit` on `/search` and `/related` | 1..50; out-of-range values are truncated to the bound, not an error |
| result `summary` | ≤ 500 characters |
| `/search` and `/related` request body | ≤ 8 KiB (413 above that) |
| `/stage` request body | ≤ 128 KiB (413 above that) |
| `/stage` `content` | ≤ 64 KiB of UTF-8 bytes (413 above that) |
| client timeouts (contract §4) | `/health` 1 s, `/search` 1 s, `/related` 1 s, `/stage` 3 s |

The `/stage` request-body cap is the binding limit: valid UTF-8 content whose
JSON escaping pushes the body over 128 KiB is refused with 413 before the
content cap is considered.

### Errors

Every non-2xx response uses the same shape and carries the contract tag:

```json
{"contract": "memfw-http/2", "error": {"code": "bad_request", "message": "…"}}
```

| HTTP | `error.code` |
|---|---|
| 400 | `bad_request` |
| 401 | `unauthorized` |
| 403 | `forbidden` |
| 404 | `not_found` |
| 409 | `conflict` |
| 413 | `too_large` |
| 429 | `too_many_requests` (`memfw-http/3` `/forget` only: the pending-request bound) |
| 500 | `internal` (fixed message, no exception details) |

`message` never contains a token, a host path or the raw request body.

### Temporal semantics

`/search` filters on `valid_from`, `valid_to` and `superseded_by` in SQL,
before the result limit:

- **default** (no `as_of`, `include_history=false`): current documents only,
  i.e. `valid_to` and `superseded_by` are both empty;
- **`as_of=T`**: documents valid at `T`, i.e. `valid_from` empty or ≤ `T`, and
  `valid_to` empty or > `T`. A document that was superseded *after* `T` is
  still returned; this fixes the v1 bug of dropping it;
- **`include_history=true`**: no time filter, every version is returned.

`as_of` must be an ISO-8601 timestamp; anything else is 400 `bad_request`. The
index stores temporal values as UTC ISO-8601 strings and the comparison runs on
that canonical form in SQL. When both `as_of` and `include_history=true` are
supplied, `include_history` wins and no time filter applies.

### Alias scope

`/search` accepts an optional string `scope` (default empty; MCP
`memory_search` takes the same argument). It only affects alias expansion
from `concepts/`: a concept label written as `{label, scope}` resolves only
when the request's `scope` equals that label's scope, and never in any other
scope. Unscoped labels resolve in every scope and stay globally unique. When a
scoped label and an unscoped label of the same text name different concepts,
the request in that scope gets a `duplicate_alias` ambiguity and no winner;
other scopes are unaffected. `scope` does not filter lexical hits or tenants.
The parameter is optional and the response shape is unchanged. One behaviour
does change for vaults that already store `{label, scope}` aliases: before
this release those labels resolved in every request, and now they resolve only
when the request names their scope. Vaults with unscoped labels only see no
difference. `scope` must be a string of at most 512 characters (400
`bad_request`: "scope must be a string" / "scope too long").

On a unicode61 index (no FTS5 trigram), query terms are segmented with jieba
through `memfw.index.fts_terms.query_terms`, the same builder as the library
search. If `memfw` or `jieba` cannot be imported, the service falls back to
regex terms, sets `degraded: true` and names the cause in `errors`.

### Score

`score` is normalised to `[0, 1]`, larger is better, and is monotonic in
relevance. For bm25 hits the service uses:

```text
x = max(0, -raw_score)
score = x / (1 + x)
```

`raw_score` keeps the SQLite FTS5 bm25 value (smaller is better). The
two-character substring branch has no bm25: `raw_score` is `null` and
`score = 1 / (1 + rank)` for the returned rank starting at 0. Both branches are
monotonic in their input.

### `degraded`

`degraded` is `true` only when a request could not take its preferred path.

- `/search` always runs the FTS path (the only implemented mode), sets
  `mode: "fts"` and reports `degraded: false`.
- `/related` reports `degraded: false` with `mode: "edges"` when it uses the
  related projection. When the projection is missing or unusable it falls back
  to lexical FTS and reports `mode: "fts"`, `degraded: true` and
  `reasons: ["lexical"]`.

`reasons` is drawn from the fixed contract vocabulary `link`, `related_field`,
`shared_tag`, `shared_course`, `shared_entity`, `lexical`, `semantic` and
`section` (edge methods map onto it, an unknown method is reported as
`lexical`), and the lexical fallback emits only `lexical`.

### `/search` result fields

Each result carries `id`, `title`, `summary` (≤ 500 characters), `source`,
`tier`, `layer`, `tenant`, `related`, `valid_from`, `valid_to`,
`superseded_by` (null when empty), `score`, `raw_score` and `content_hash`
(`"sha256:"` plus the sha256 of the memory's Markdown bytes). The response top
level is `contract`, `mode`, `degraded`, `errors` and `results`.

`recorded_at` (below) is not a `memfw-http/2` result field: the contract
fixtures fix these keys, and the roadmap adds it to `/search` with
`memfw-http/3`. MCP `memory_search` already returns it.

### `recorded_at`

`recorded_at` is transaction time: the server-clock instant memfw wrote a memory
or a staged candidate, in UTC. It sits beside the valid-time fields
`valid_from`/`valid_to`. Callers cannot set it and updates keep it. A memory
written before the field existed has no `recorded_at` key on disk (its bytes do
not change); readers then report its `created_at`, which memfw stamped from the
same clock, and null when neither is present.

### Transport and deployment

The service speaks plain HTTP with a bearer-style shared header token. That is
acceptable only on a private tailnet between MemFW and Conductor; it is not a
public-network design. The default bind is `127.0.0.1`; deployment sets
`--host` explicitly to the tailnet address.

On the deployment host, the service is rooted at the vault directory, bound to
the tailnet on port 18080, and protected by `X-Memfw-Token`. Conductor binds
`memfw_url` and reads `MEMFW_READONLY_TOKEN` from its environment. No vault sync
is performed.

The systemd **user** unit is the only supported management route; the template
is `scripts/memfw-readonly.service` and the installed unit is
`~/.config/systemd/user/memfw-readonly.service`, managed with
`systemctl --user`. Token rotation and HA remain follow-ups.

### Migrating from v1

v1 staged candidates under `<vault parent>/_candidates/memfw/remember/*.md`.
The v2 service no longer reads or writes that directory: the only canonical
staging directory is `<vault>/_candidates/remember/` (see "Candidate staging /
write bridge"). Old candidates are not migrated automatically.

1. Inspect each old candidate instead of trusting its name; the old file names
   are **not** `conductor-<proposal_id>.md`:

   ```bash
   find "<vault parent>/_candidates/memfw/remember" -maxdepth 1 -name '*.md' -print
   sed -n '1,40p' "<vault parent>/_candidates/memfw/remember/<old-name>.md"
   ```

2. For each candidate worth keeping, rename it to the v2 name and complete its
   frontmatter by hand, then move it into the canonical directory:

   ```bash
   mv "<vault parent>/_candidates/memfw/remember/<old-name>.md" \
      "<vault>/_candidates/remember/conductor-<proposal_id>.md"
   ```

   The frontmatter must carry `proposal_id` (matching
   `^[A-Za-z0-9_-]{1,128}$`), `source`, `tenant`, `tags`, `related`, `title`,
   `created_at`, `content_hash` and `authority: false`; the old files do not
   have all of these. Alternatively, let Conductor re-stage the proposal
   through `/stage`. Do not copy a file without completing the frontmatter, or
   `memfw candidates` and `candidate-apply` will not treat it as a valid
   candidate.

3. Create the write-token file mode 0600 and reference it from the service's
   `EnvironmentFile`:

   ```bash
   install -m 0600 /dev/null "$HOME/memfw-remote/memfw-write-token"
   # put the token on one line in that file, then add to the EnvironmentFile:
   #   MEMFW_WRITE_TOKEN_FILE=~/memfw-remote/memfw-write-token
   ```

   The read token stays in `MEMFW_READONLY_TOKEN`/`MEMFW_READONLY_TOKEN_FILE`;
   both are honoured, environment first, then file. Never put a token value in
   the unit file or in shell history.

4. Changing the bind from loopback to the tailnet address is an explicit
   `--host <tailnet address>` edit in the unit's `ExecStart`; binding to every
   interface is not a supported deployment.

5. Upgrade the running service:

   ```bash
   systemctl --user daemon-reload && systemctl --user restart memfw-readonly
   ```

6. Before re-enabling Conductor's remote source, verify the contract:
   `curl -s http://<host>:18080/health` must return
   `"contract": "memfw-http/2"` and `"service": "memfw"`. `/health` needs no
   token.


## Contract memfw-http/3

v3 adds the evidence fields of roadmap §4 item 3 to every read result, a
`mode` vocabulary, `recorded_at`, a read-only `POST /context` and the
deletion-request endpoint `POST /forget` (roadmap R5/R6). v2 stays served,
unchanged, for at least one release cycle.

### Selecting the contract

The request header `MemFW-Contract` selects the contract for that request:

| `MemFW-Contract` | Contract |
|---|---|
| absent, empty or `memfw-http/2` | v2: every response byte-identical to the v2-only service |
| `memfw-http/3` | v3 |
| anything else (case-sensitive), or the header repeated | 400 `bad_request`, message `contract is not available`, tagged `memfw-http/2` |

The rule applies to every method and path, error paths included: under v3 a
401, 403, 404, 409 or 413 body carries `"contract": "memfw-http/3"`. Paths
stay the same (no path prefix), so a client keeps one base URL.

**Discovery.** Every `GET /health` response, whatever the contract, carries
the response header `MemFW-Contracts: memfw-http/2, memfw-http/3`; the v2
body is unchanged. A client that wants v3 sends `MemFW-Contract:
memfw-http/3` on `/health`: a v3 server answers the v3 body below, a
v2-only server ignores the header and answers `"contract": "memfw-http/2"`,
which tells the client to stay on v2. The v3 `/health` body adds:

```json
{"ok": true, "service": "memfw", "contract": "memfw-http/3",
 "index_present": true, "related_index_present": true,
 "contracts": ["memfw-http/2", "memfw-http/3"], "modes": ["fts"],
 "endpoints": ["/health", "/search", "/related", "/stage", "/context", "/forget"]}
```

`endpoints` lists `/context` only when the deployment can serve it (it needs
the `memfw` package and a configured clearance; see below). No field is a path, host name or token.

### Endpoints under v3

| Method and path | Auth | v3 behaviour |
|---|---|---|
| `GET /health` | none | v2 body plus `contracts`, `modes`, `endpoints` |
| `POST /search` | read or write token | v2 result plus `recorded_at` on every hit; `mode` checked against `modes` |
| `POST /related` | read or write token | v2 result plus `why` and `provenance_type` on every result |
| `POST /stage` | write token only | unchanged apart from the contract tag |
| `POST /context` | read or write token | the `memory_context` v2 pack (`core` slot), read-only; only with a clearance configured |
| `POST /forget` | write token only | records one deletion-request candidate; never deletes |

Request limits, the error shape, token handling, tenant binding
(`MEMFW_TENANT`) and the privacy clearance (`MEMFW_READONLY_CLEARANCE`) are
the v2 ones and apply to the new endpoints too; `/context` and `/forget`
bodies are capped at 8 KiB. A body field naming an endpoint (`*_endpoint`,
`*_url`, ...) is 400 on every endpoint of both contracts.

### `mode`

`/search` takes an optional `mode` (default `fts`); the response always
carries the `mode` it ran. The values served over HTTP are the ones
`/health` lists in `modes`: only `fts` today. Any other value, including
`multihop` (an MCP pilot, roadmap R2; HTTP opens it only after a planner
decision), is 400 `mode is not available`, as under v2.

### Evidence fields

Every `/search` hit already carries, under both contracts, `provenance_type`
(`user_said`, `agent_inferred`, `imported`, `derived`), `why` (`lexical`,
`semantic`, `expansion`, `related`, or `alias(<label>→<concept id>)` for an
ontology alias hit), `index_version`, `sot_version` and `stale`. v3 adds:

- `/search`: `recorded_at` (see "`recorded_at`" above: transaction time,
  `created_at` for older files, null when neither exists), read from the
  hit's own Markdown;
- `/related`: `why` (`related` on the edges projection, `lexical` on the FTS
  fallback) and `provenance_type` (a memory's own; `imported` for a note).

These fields are computed only for results the tenant and the clearance have
already admitted. They carry an enum, a timestamp or an alias label the
query itself matched; never another memory's id or text. `related` lists keep
the v2 trimming (a withheld or other-tenant id is never listed).

### `POST /context`

Request (all optional): `tenant` (bound as on `/search`), `limit_tokens`
(integer, default 2000, clamped to `1..100000`), `policy` (`strict`, the
default, or `compat_fts`), `slots` (only `["core"]`), `if_version` (a
`pack_version`), `v` (only `2`). Wrong types or values are 400 with a fixed
message (`slot is not available`, `policy must be compat_fts or strict`, ...).

The response is `{"contract": "memfw-http/3", "v": 2, "limit_tokens": …}`
plus the `ContextPack` of MCP `memory_context` `v=2`: `pack_version`,
`policy`, `generated_at` and `slots` (one `core` slot of anchors and
procedural memories). With `if_version` equal to the current
`pack_version`, the slots are left out and `unchanged: true` is set.

- **Read-only.** Built with the context cache, `memory_access` events and
  usage signals all off; nothing under the vault is written.
- **Privacy.** The tenant is exact (empty matches only empty), and with a
  clearance only memories whose level it admits are carried; the level
  includes the privacy field, the `privacy:`/`sensitivity:` tags and the
  top-level `sensitivity` key. A withheld memory appears nowhere in the
  pack, `truncated_ids` included.
- **Slots.** `handoff` (the conversation share, read as a calling app) and
  `topics` (vault-wide counts) have no HTTP caller identity yet and are
  refused.
- **Needs a clearance.** `/context` returns memory text, so it is never
  served unfiltered: with `MEMFW_READONLY_CLEARANCE` (`--clearance`) unset
  it is 404 `not found`, exactly like an unknown path, and `/health` leaves
  it out of `endpoints`. Set a clearance (the remote default is
  `public,shared`; `public,shared,local-private` admits every level) to
  serve it. `/search` and `/related` keep the pre-MM0 behaviour without one.
- **Needs `memfw`.** The pack is built by `memfw.runtime.http`. A
  three-file deployment without the `memfw` package answers 404 `context is
  not available`, and `/health` then leaves `/context` out of `endpoints`.

### `POST /forget`

`/forget` records a request that a human deletes a memory. It never deletes,
closes or edits anything: the memory file, the event log and every
projection are untouched, and the response says `deleted: false`.

Request: `request_id` (required, `^[A-Za-z0-9_-]{1,128}$`, the idempotency
key), `memory_id` (required, a memory id), `reason` (optional, at most 500
characters), `source` (optional single line, default `conductor`) and
`tenant` (bound as on `/stage`: with `MEMFW_TENANT` set, only that tenant).
Without a `MEMFW_TENANT` binding the body `tenant` is self-asserted: any
caller holding the write token can name any tenant, exactly as on `/stage`
and `/search`. Tenant scoping then separates tenants' data, it does not
authenticate them; bind `MEMFW_TENANT` (one service per tenant) where
callers must not act for each other.

`memory_id` must match `^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$`, the id shape
`memfw` generates. A memory whose id contains anything else (a `.`, for
example, which hand-written or imported files can carry) cannot be named
over HTTP: the request is 400 `memory_id is invalid`. Such a memory can
only be deleted locally (`memfw delete`), after a human review as always.

```json
{"contract": "memfw-http/3", "requested": true, "duplicate": false,
 "request_id": "<request_id>", "memory_id": "<memory_id>",
 "candidate_id": "forget-<tenant_key>-<request_id>",
 "path": "_candidates/forget/forget-<tenant_key>-<request_id>.md",
 "deleted": false, "authority": false}
```

- **Auth.** Write token only, exactly like `/stage`: read token 403, no write
  token configured 403, missing or wrong token 401.
- **Visibility.** The memory must be one the caller could read: in the
  request's tenant and admitted by the clearance. A missing memory, another
  tenant's and a withheld one all answer the same 404 `memory not found`,
  and nothing is written.
- **Idempotent, per tenant.** `request_id` is scoped to the tenant:
  `<tenant_key>` is the first 16 hex digits of `sha256(tenant)` (the empty
  tenant included), so two tenants never share a candidate file and one
  tenant's `request_id` is neither an oracle nor a pre-claim for another.
  The same request again (same `request_id`, memory, tenant, source and
  reason) is 200 with `duplicate: true` and no rewrite; the same
  `request_id` with another memory, source or reason is 409 `conflict`
  (`request exists with different content`), answered only once the named
  memory passes the visibility check: a caller that may not see it (another
  tenant, a lower clearance) gets the same 404 `memory not found` whether
  or not a request for it was recorded. When the named memory is gone, only
  an exact replay of the tenant's own request is `duplicate: true`; anything
  else (another tenant, or a recorded `request_id` reused with another
  memory id) is the same 404, never 409, so a reused `request_id` cannot
  tell a missing memory from a hidden one. Publication is
  atomic (temporary file + hard link), so concurrent requests converge on one
  file.
- **The candidate.** `<vault>/_candidates/forget/forget-<tenant_key>-<request_id>.md`, a
  directory of its own so `memfw candidates` / `candidate-apply` never see a
  deletion request. Frontmatter: `type: forget-request`, `status: pending`,
  `request_id`, `memory_id`, `tenant`, `source`, `reason`,
  `target_content_hash` (the memory's bytes at request time), `created_at`,
  `recorded_at`, `deleted: false`, `authority: false`. The body names the
  memory id only; no memory text is copied. Symlinked directory components
  are refused with 500, as for `/stage`.
- **Bounded, per tenant.** At most `MAX_PENDING_FORGET_REQUESTS` (1000)
  request files of one tenant (those named `forget-<tenant_key>-*`) wait in
  `_candidates/forget/`, so one tenant cannot fill another's queue; beyond
  that the tenant's new request is 429
  `too_many_requests` (`too many pending forget requests`) and writes
  nothing, until a human reviews and removes some. Replays of recorded
  requests and the visibility 404 are answered as usual. Concurrent requests
  can pass the count together, so the bound is approximate.
- **Review.** `memfw forget requests --root <vault>` lists the requests (read
  only) with `target_present` and `target_changed`. Deleting stays a human
  action (`memfw delete`, unchanged; the tombstone redesign of roadmap §6.3
  waits for its own approval).

### Residue check

`memfw forget residue <memory_id> --root <vault> [--related-db P]
[--entities-db P]` checks, read-only, that a forgotten memory left nothing in
the derived stores: the FTS index, the related projection (default
`<vault>/.memfw-related.sqlite`), `corroboration.sqlite3`, the usage
projection and, when given, the entity projection. A hard-deleted (or
never-existing) id may appear in none of them; a closed memory
(`valid_to`/`superseded_by`) may keep FTS rows for history queries but must
not be listed as current, and may not appear in the current-only related
projection. The JSON report names each store, its row counts, whether it is
residue and the rebuild that clears it; exit 0 is clean, 1 residue, 2 a
usage error (bad id or vault root, as for every typer command), 3 a live
memory. An unreadable store counts as residue (fail closed), and so do FTS
rows of a closed memory whose current state cannot be told (no
`memory_temporal` row). No SQLite
sidecar is created, so a store in a read-only directory is read: a file
without a pending `-wal`/`-journal` is opened `immutable=1`, one with a
pending sidecar is copied (with it) to a private temporary directory and the
copy is read. Either way the check reads a snapshot and assumes no writer is
changing the store meanwhile; run it after a rebuild, not during one. The
store and its sidecars are stat-ed before and after the read: a change
re-runs it once, a second change reports the store unreadable. OKF exports on
disk are not checked.

### Fixtures

`tests/fixtures/contract_v3/` holds the v3 sample set, built and replayed like
the v2 one. `tests/test_readonly_service_v3.py` replays it and also runs every
v2 sample three ways (no header, `memfw-http/2`, `memfw-http/3`): the first
two must be byte-identical, and each v3 answer must equal the v2 answer plus
only the additions listed above.


## Relatedness

`memfw.related` is a rebuildable edge projection over current memories and notes.
V1 implements explicit links/`related`, shared tags/courses, shared entities, and
lexical overlap. `memory_related` is exposed over MCP; `notes_related` is available from the
notes MCP when configured. Semantic edges are opt-in through `build_semantic`
using sqlite-vec KNN and one shared embedding fingerprint. QMD notes remain
outside semantic edges until a local vector index is available.


## Section-level edges and relatedness annotation

- `RelatedIndex.rebuild_sections` builds bounded section-level edges
  (`source=notes_sections`); the QMD 2,000-section subset produced 39,980 edges.
- `RelatedIndex` items/edges carry tenant/scope columns; `related()` accepts
  tenant/scope filters. Memory build uses each document's tenant/source.
- `related_annotation.generate_pairs` creates a 100-pair positive/negative batch;
  `calibrate` computes precision/recall by threshold after human labels.
  The current QMD batch is at
  `~/Documents/Codex/memfw-related-annotation-20261106/related-pairs.csv`.
- This is still a generated candidate set; human labels are required before
  changing a production threshold.


## Candidate staging / write bridge

`POST /stage` is the only write in the service and is deliberately
non-authoritative: it writes one candidate to
`<vault>/_candidates/remember/conductor-<proposal_id>.md` and never the vault.
That directory is the only canonical candidate location, and
`memfw candidates --root <vault>` lists what is staged there.

Each staged candidate (from `/stage` or MCP `memory_remember`) records
`recorded_at`, the server-clock write time, in its frontmatter. A candidate that
is not applied within 30 days of `recorded_at` expires. Expiry only hides and
marks; no file is deleted:

- `memfw candidates` hides expired candidates; `--include-expired` lists them
  with `status: "expired"`.
- `memfw candidates-expire --root <vault> [--dry-run]` writes the mark
  (`lifecycle: expired`, `expired: true`, `expired_at`, `status: expired`). The
  body and its `content_hash` are unchanged, and the output always reports
  `"deleted": 0`.
- `memfw candidate-apply` still applies an expired candidate given its path.
- Applied candidates never expire. Candidates staged before `recorded_at`
  existed age from `created_at`; a candidate with neither stamp (or one without
  a timezone) never expires.

The request carries `proposal_id`, `title` and `content`:

- `proposal_id` is required and matches `^[A-Za-z0-9_-]{1,128}$`;
- `title` is required and at most 200 characters;
- `content` is required and at most 64 KiB of UTF-8 bytes.

The write is idempotent by content hash. `content_hash` =
`"sha256:" + hex(sha256(content as UTF-8 bytes))`; the same value is written
into the candidate frontmatter so a later apply can compare it. Re-staging the
same `proposal_id` with the same
content returns 200 with `duplicate: true` and does not rewrite the file; the
same `proposal_id` with different content returns 409 `conflict`.

The candidate file's frontmatter carries `proposal_id`, `source`, `tenant`,
`tags`, `related`, `title`, `created_at`, `content_hash` and
`authority: false`.

```json
{"contract": "memfw-http/2", "staged": true, "duplicate": false,
 "candidate_id": "conductor-<proposal_id>",
 "path": "_candidates/remember/conductor-<proposal_id>.md",
 "authority": false}
```

`path` is relative to the vault root and never a host path.

Publication is atomic: the candidate is written to a unique temporary file in
the directory and hard-linked into place, so concurrent requests for the same
`proposal_id` converge on one file - the winner publishes, the others get
`duplicate: true` for the same content or 409 for different content, and no
writer can replace an existing candidate. A `_candidates` (or `remember`)
component that is a symlink, or that resolves outside the vault, is refused with
500 `internal` and nothing is written; the same applies to a symlinked target
file. `source` is refused with 400 `bad_request` when it contains control
characters or line breaks. The other string fields (`title`, `tenant`, `tags`,
`related`) accept multi-line values: the frontmatter escapes every string onto
one double-quoted YAML line, so a value cannot carry the `---` frontmatter
delimiter into the header, `memfw candidates` still reads the candidate, and a
retry with the same content is `duplicate: true` rather than 409.

A human still has to apply the candidate with
`memfw candidate-apply --apply` before it becomes an authoritative memory.
Legacy candidates with a missing or empty `proposal_id` are refused in both
dry-run and apply modes with `missing_proposal_id`, before any authoritative
write. Re-stage them through `/stage` to obtain the required identity; this
fail-closed rule was added in commit `025ee6c`.

Conductor's optional `MEMFW_WRITE_BRIDGE=1` path stages a human-approved
proposal after its own approval. The approval result includes `memfw_staging`,
and a staging failure does not roll back that approval.

The deployed service is a systemd user unit:

```text
~/.config/systemd/user/memfw-readonly.service
```

with `EnvironmentFile=%h/memfw-remote/memfw-readonly.env`. Deployment
requirement: that file holds the read credential and, once `/stage` is used, the
write credential, both mode 0600; the unit is enabled and started with
`systemctl --user enable --now memfw-readonly`, and `GET /health` must return
`"contract": "memfw-http/2"`. The v1 deployment note recorded the host running
this unit with the read credential; this round did not observe the host, and the
write credential is added at migration (see "Migrating from v1").

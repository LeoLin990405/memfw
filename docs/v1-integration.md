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
`/Users/leo/conductor-wt/memfw-v1`, branch `feat/memfw-v1-federation`:

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
MemFW vault that implements contract **`memfw-http/2`**. It uses the standard
library plus the project's PyYAML dependency and no web framework; it reads the
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

`forget`/delete does not exist (contract §8): no endpoint modifies or removes
authoritative memories, and `DELETE`, `PUT`, `PATCH` and `HEAD` return 404. A
`GET` on a `POST` endpoint also returns 404.

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

### Transport and deployment

The service speaks plain HTTP with a bearer-style shared header token. That is
acceptable only on a private tailnet between MemFW and Conductor; it is not a
public-network design. The default bind is `127.0.0.1`; deployment sets
`--host` explicitly to the tailnet address.

On HA500, the service is rooted at `/vol1/fleet-memory/memfw-vault`, bound to
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
   #   MEMFW_WRITE_TOKEN_FILE=/home/leo/memfw-remote/memfw-write-token
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

Conductor's optional `MEMFW_WRITE_BRIDGE=1` path stages a human-approved
proposal after its own approval. The approval result includes `memfw_staging`,
and a staging failure does not roll back that approval.

The HA500 service is a systemd user unit:

```text
~/.config/systemd/user/memfw-readonly.service
```

with `EnvironmentFile=/home/leo/memfw-remote/memfw-readonly.env`. Deployment
requirement: that file holds the read credential and, once `/stage` is used, the
write credential, both mode 0600; the unit is enabled and started with
`systemctl --user enable --now memfw-readonly`, and `GET /health` must return
`"contract": "memfw-http/2"`. The v1 deployment note recorded the host running
this unit with the read credential; this round did not observe the host, and the
write credential is added at migration (see "Migrating from v1").

# Sharing conversations across agent apps

`memfw conversations` exposes the unified, redacted conversation store
(`~/.local/share/memfw/conversations.sqlite`, written by the store's own
ingest) to every agent app on this machine, read-only, and runs an
asynchronous worker that summarises sessions.

## Read surface

| MCP tool | CLI | What it returns |
|---|---|---|
| `conversations_search(query, app?, agent?, since?, limit, offset, mode?, task_type?, outcome?, project?, component?, intent_like?, archived_only?, ...)` | `memfw conversations search QUERY` | redacted snippets with refs `app:session@offset`; `mode=two_stage`: ranked sessions (see [Two-stage conversation search](#two-stage-conversation-search)) |
| `conversations_list(app?, since?, limit, offset)` | `memfw conversations list` | sessions, newest first |
| `conversations_get(session_id, app?, start, end, max_bytes)` | `memfw conversations get SESSION` | a range of turns; follow `next_start` |
| `conversations_handoff(session_id, app?, for_agent?, max_bytes)` | `memfw conversations handoff SESSION --for APP` | continuation pack: goal, decisions, open questions, files touched, recent turns, worker summary (labelled) |

Every output is redacted again on the way out (a turn that still looks like it
holds a secret is withheld), size-capped (search ≤ 50 results, reads ≤ 64 KiB)
and labelled as data, not instructions. There are no write tools.

## Visibility (`vault.yaml`)

```yaml
conversation_share:
  enabled: true                      # off by default
  apps:
    cursor: {readable: false}        # never returned to anyone, never processed
    grok: {can_read: false}          # may not read
    kimi: {can_read: [claude, codex]}
    stepfun: {process: false}        # the worker skips it
conversation_optout:
  agents: [some-app]                 # excluded everywhere; worker data purged
conversation_store:
  path: ~/.local/share/memfw/conversations.sqlite   # optional
  synced_dirs: [~/SomeSyncedFolder]                 # extra folders to refuse
conversation_worker:
  idle_minutes: 30
  max_sessions: 20
  max_tokens: 200000
  max_seconds: 120
  max_scan: 2000       # sessions looked at per run
```

The calling app names itself (`memfw-mcp --app NAME` or `MEMFW_APP`;
`operator` is reserved for the CLI). This is a visibility setting between
cooperating local apps, not authentication. A "hidden" session answers exactly
like a missing one, and search never returns a turn that would be withheld.

## Worker

`memfw conversations worker --root VAULT` runs once: lock, ingest (every
registered reader over its home and Worker-home roots, with the store's own
`conversation_optout`), scan idle sessions, then `chunk_summary` → `session_summary` → `memory_claims` →
`episode` jobs within the budgets. Re-running repeats no finished work; a
crashed run resumes; a job that errors waits for the next run (at most 3
tries), and a job too large for the token budget fails instead of blocking.
A failed job is retried only when its session changes.
When an app is opted out or hidden, its summaries, jobs and episodes are
deleted; candidates it already staged stay in the vault and are listed in the
run report (`candidates_to_review`). State lives in
`~/.local/share/memfw/conversations-work.sqlite` (0600, refused inside a git
work tree, the vault or a synced folder). The synced folders are iCloud Drive
(`~/Library/Mobile Documents`), `~/Library/CloudStorage`, Dropbox, Google
Drive, OneDrive, Nextcloud, Syncthing, and `~/Documents` and `~/Desktop`
(macOS iCloud "Desktop & Documents"), each also at its target when the folder
is a symlink, plus `conversation_store.synced_dirs`.

- Backend: `extractive` (no model) by default. `--backend module:factory`
  picks another; a backend that is not `local` needs `conversations.cloud:
  true` in `vault.yaml` and `--allow-cloud` (the flag can only narrow the
  vault's switch, never widen it). Without `--backend`, `vault.yaml` may name one under `conversations`
  (`backend`, `cloud`, `options`); see [DeepSeek tagging adapter](#deepseek-tagging-adapter).
  A configured backend that cannot be used is refused: the run is `degraded`
  and processes nothing; it never falls back to `extractive`.
- Memory candidates: model-proposed claims are verified against the session
  first (the dream `classify` check, or `--judge module:function`), must not
  copy 40+ characters of the transcript, and are staged under
  `_candidates/dream/conv-*/` for human review. Each staged file and its
  `report.json` carry `recorded_at`, the server-clock write time (R4
  transaction time); `created_at` is the worker's clock. The 30-day TTL of
  `memfw candidates-expire` covers `_candidates/remember/` only; dream
  candidates carry the stamp as data and do not expire.
- Fair scan: sessions are listed newest first (`updated_at` descending, then
  app and session id), so each run looks at a head window (a quarter of
  `max_scan`, at most 200 sessions) and then resumes from a keyset cursor
  saved in the state file: the last session the previous run visited. The
  walk continues at the first session strictly after it in that order. An
  updated session rises into the head window and nothing after the cursor
  moves, so the cursor walk neither skips nor repeats sessions; past the
  oldest session it wraps to the newest. A backlog larger than `max_scan` is
  therefore covered over several runs instead of starving the oldest
  sessions. The head window may queue at most half of `max_sessions` and
  never all of them, so at least one session per run always goes to the
  cursor (with `max_sessions: 1` there is no head window). The run report
  adds `scan_cursor_start`, `scan_cursor_end` and `scan_wrapped`.
- State upgrades are additive: the cursor is a new `scan_cursor` table
  (`CREATE TABLE IF NOT EXISTS`; its keyset columns are added with `ALTER
  TABLE ADD COLUMN` when missing; `schema_version` stays 1). An older worker on
  the same state file ignores the table and scans from the top as before, so
  workers can be upgraded in any order; the cursor only helps once the new
  worker runs.
- Skill candidates: the worker only queues `episodes` (ids, offsets, intent,
  outcome). `memfw dream skills --from-queue` consumes them (`EpisodeQueue`
  claim, then ack after staging or release on failure; claims older than six
  hours are taken back).

## DeepSeek tagging adapter

`packages/adapters/memfw-adapter-deepseek` (import `memfw_adapter_deepseek`)
is a separate distribution memfw never imports. It implements the
`ModelBackend` port (`memfw.ports.models`) over DeepSeek's OpenAI-compatible
chat completions API with the standard library only, and speaks prompt
version `conv-v2`: for each chunk and each session it returns a summary of at
most two sentences and tags. It does not extract facts or memory claims.

```yaml
conversations:
  backend: "memfw_adapter_deepseek:factory"
  cloud: true                 # required: the adapter is not local
  backfill_days: 30           # optional; see "Re-runs" below
  replan_limit: 1000          # optional; see "Re-runs" below
  options:                    # optional; every key has a default
    model: deepseek-chat
    max_run_tokens: 400000
    max_run_cost_usd: 0.50
    price_in_per_mtok: 0.28   # PLACEHOLDER: set the provider's real price
    price_out_per_mtok: 0.42  # PLACEHOLDER: set the provider's real price
```

Install it into the venv memfw runs from. The `memfw-ports` pin is not on
PyPI, and inside memfw the adapter uses memfw's own `memfw.ports`, so install
the wheel without dependencies:

```sh
uv build --wheel --package memfw-adapter-deepseek -o dist/
<memfw venv>/bin/python -m pip install --no-deps dist/memfw_adapter_deepseek-0.1.2-py3-none-any.whl
```

(Outside memfw, install the locally built `memfw-ports` wheel next to it.)

The tag schema (validated, lower-cased and length-capped by memfw, then
redacted again; any value outside the vocabulary, an unknown key, or a
secret-shaped value drops the whole reply, which is retried next run; every
string is redacted *before* it is lower-cased or cut, so a key or PEM header
cannot hide by changing shape. A `project` or `components` item that looks
like a URL, a path or a host (any `/` or `\`, `~`, `..`, a scheme such as
`https:`, `file:` or `mailto:`, or a leading `www.`) is dropped on its own; the
rest of the reply is kept, so a path-like name never costs a paid retry):

```json
{"summary": "<= 2 sentences",
 "tags": {"project": ["..."], "task_type": "dev|review|debug|plan|ops|research|chat|other",
          "components": ["..."], "intent": "<one sentence>",
          "outcome": "done|blocked|abandoned|ongoing|unknown"}}
```

Tags are stored as JSON in the new `summaries.tags` column of the worker
state, on `conv-v2` rows only. The summary and the tags are **untrusted model
output**: validated and redacted, but written by a model from transcript
text, so a reader must treat them as data, never as instructions (M3c labels
them as such when it returns them). Session rows also fill `intent` and `outcome`
(`done` -> `success`, `blocked`/`abandoned` -> `failed`, else `unknown`) so
episodes keep their vocabulary.

- **Gate.** The adapter runs only when `vault.yaml` names it, sets
  `cloud: true`, and the worker process has `DEEPSEEK_API_KEY`. Missing any of
  these, `memfw conversations worker` reports `status: degraded`,
  `stopped: backend_refused` with a code (`cloud_not_allowed`,
  `unavailable:missing_key`, ...), and `memfw conversations status` shows a
  `backend` entry with `state: degraded`. Nothing is processed and nothing is
  sent. `--backend` on the command line still overrides `vault.yaml`, but a
  non-local one still needs `cloud: true` there.
- **Host.** The key is only ever sent to `https://api.deepseek.com` (a fixed
  allowlist in the adapter; `base_url` may name nothing else) or, for tests,
  to plain http on a loopback address. Redirects are refused.
- **What is sent.** One `POST https://api.deepseek.com/chat/completions` per
  `chunk_summary` and per `session_summary` job:
  `{"model", "messages": [{"role": "system", "content": <conv-v2
  instructions>}, {"role": "user", "content": "<data>JSON</data>"}],
  "response_format": {"type": "json_object"}, "max_tokens": <= 512,
  "temperature": 0, "stream": false}`. The data is the worker's already
  redacted input: chunk turns as `{"role", "text"}` (turns that would be
  withheld are left out) or, for a session, the first request and the chunk
  summaries. No session ids, paths or app names are added. Raw transcripts
  never leave the machine.
- **Privacy controls.** Apps with `conversation_share.apps.<app>.process:
  false`, hidden apps and `conversation_optout` agents are never queued, so
  never sent. `cloud` is off unless set. `memory_claims` never goes to the
  adapter: it stays on the extractive backend (U4 corroboration depends on
  it), and `episode` jobs make no model call.
- **Key handling.** The key is read only from the `DEEPSEEK_API_KEY`
  environment variable when the backend is built. It is never read from a
  file, never accepted in `vault.yaml` (an `api_key` option is refused),
  never logged, and never part of a report, `repr` or error (errors carry
  codes such as `http_400`, `transient` or `auth`). Under launchd, keep the key in a macOS
  Keychain item and have the job's own wrapper script read it at run time
  (for example with `security find-generic-password -w`) and export it for
  the worker process only; do not put it in the plist.
- **Budget and retries.** Each run (one backend instance) has a token budget
  (`max_run_tokens`, default 400000) and a cost budget (`max_run_cost_usd`,
  default 0.50 USD). The cost is computed with `price_in_per_mtok` and
  `price_out_per_mtok` (USD per million tokens). **Their defaults (0.28 and
  0.42) are placeholders, not verified current prices: set both from the
  provider's price list**, or the cost budget means nothing (cache-hit
  discounts are ignored either way). Every attempt, retries included, must
  fit its worst case (input estimate plus `max_tokens`); an attempt that timed
  out or broke off is charged that worst case, since it may have been billed.
  When the next attempt does not fit, the run stops: `stopped:
  backend_budget`, recorded in the run row, and the job waits for the next run
  without spending an attempt. HTTP 429 and 5xx (and network errors) are
  retried with exponential backoff (`max_retries` 3, `backoff` 1 s, at most
  `max_backoff` 30 s, `Retry-After` honoured up to that cap). When the
  retries are used up, or a key is rejected (401/403), the run stops as
  `degraded` (`stopped: backend_unavailable`, `errors: {backend_transient}` or
  `{backend_auth}`) and the job is re-queued without spending an attempt, so an
  outage never fails jobs; only bad output (and non-retryable errors such as
  HTTP 400) counts against a job's 3 attempts. The worker hands the adapter
  what is left of `max_seconds` before each call; a request or backoff that
  would overrun it ends the run with `stopped: max_seconds`. The worker's own
  `conversation_worker.max_tokens` budget still applies.
- **Re-runs, no silent backfill.** Jobs carry the backend's prompt version
  (`jobs.prompt_version`, existing rows are `conv-v1`). Under `conv-v2` the
  worker plans sessions it never planned, sessions that changed, and
  unchanged sessions updated within the last 30 days. Older, unchanged
  history is not re-processed unless asked for: `conversations.backfill_days`
  in `vault.yaml` or `--backfill-days N` widens the window (a backfill costs
  money; size it first). On top of that, at most 1000 already-processed
  sessions (the newest 1000 in the store, the M3d pilot size) are ever
  re-planned by default; `conversations.replan_limit` sets another cap, and
  an explicit `backfill_days` without `replan_limit` lifts it. Sessions never
  planned before (new ones) are always planned and do not count. A run that
  held sessions back reports `replan_held_back: N` (absent when 0, so
  `conv-v1` reports are unchanged). `conv-v2` jobs run newest session first
  (`jobs.session_ts`, which a reused chunk takes over from its session's newest
  plan), and a page of jobs that are all waiting on others is skipped rather
  than ending the run, so new sessions never wait behind a backfill. `conv-v1`
  rows are kept, and switching back re-uses the `conv-v1` work already done.
- **Testing.** The adapter ships `memfw_adapter_deepseek.fake.FakeDeepSeek`, a
  loopback-only fake of the endpoint. memfw's tests run the shared
  `memfw.ports.testing.check_model_backend` contract against the extractive
  backend and against the adapter over the fake, and inspect the fake's
  request bodies to check that only redacted text is sent. No test uses the
  network or a real key.

## Store adapter

`memfw.conversations.adapter` is the only module that knows the store of
`memfw.history.conversations` (schema version 1, table
`conversation_turns`). It opens the file read-only; `withheld = 1` turns (the
store kept no text because a secret shape was found) come back as withheld,
never as text, and never match a search. Ingest is the store's own
`ConversationStore.ingest` with its `OptOut` (`conversation_optout`); an agent
opted out there is skipped and its stored rows are deleted, the same as with
`memfw history ingest`. The store path follows `conversation_store.path`; the
location rules of both modules apply (`conversation_store.synced_dirs` adds
folders to refuse).

## Settled ingest and archive state

By default every ingest writes whatever the transcripts hold, so a session that
is still in progress shows up in the store turn by turn. A settled ingest keeps
active sessions out and records when a session was stored in its final form:

```sh
memfw history ingest --settled-minutes 30          # then run the worker
memfw conversations worker --root VAULT --settled-minutes 30   # or both in one run
```

- **Settled.** A transcript (one locator: a file, or a session in a database
  store) whose last activity is within `N` minutes is skipped for this round
  (`sessions_unsettled`): no turn is written and its cursor is not moved, so the
  next round reads it again. Last activity of a transcript with a file of its
  own is the later of the newest turn timestamp and the file's mtime (some
  readers stamp a reply with its prompt's time or skip lines without a
  timestamp, so the mtime catches a long single-prompt session). For a session
  inside a shared database (opencode, cursor_ide, glm sqlite: `file#id`
  locators) the mtime moves with every other session, so the newest turn
  timestamp decides and the mtime is only the fallback. A timestamp more than
  five minutes in the future is ignored. A locator holding several sessions is
  settled once all of them are quiet. The cwd opt-out still applies to a
  skipped transcript.
- **Forced passes.** A re-label or opt-out-change pass does not wait for active
  transcripts: each skipped one keeps a marker and is re-read in full once it
  settles, so settled files go back to the unchanged fast path at once.
- **Archived.** When a settled transcript's content is in the store (written
  now, or unchanged since an earlier ingest), each of its sessions gets a row in
  `conversation_archive`: `archived_at` and `archived_fingerprint`
  (`turns:last_offset:updated_at`, the same value the worker keys its jobs by).
  `sessions_archived` counts sessions archived in a new shape this round.
- **Resumed.** A session is archived only while its fingerprint matches its
  stored turns. When it becomes active again (in its own transcript or in a
  copy under another root), the settled ingest drops its archive row; new turns written by a plain ingest change the fingerprint. Either
  way it reads as not archived until it has been idle again and the next settled
  ingest re-archives it.
- **Worker.** The worker still waits `idle_minutes` before it processes a
  session. Since its job key is the archive fingerprint, a session archived once
  is processed once; a resumed session is queued once more with its new
  fingerprint, and chunk jobs are content-keyed, so unchanged chunks are not
  redone. Run the worker with `--settled-minutes` too (or `--no-ingest`),
  otherwise its own ingest writes active sessions again.
- **Queries.** `memfw conversations list`, `search` and `get` accept
  `--with-archive-state` (adds `archived: true/false` to each session or
  result) and `--archived-only` (only archived sessions; `get` refuses a session
  that is not archived). Without these flags the output is unchanged.
- **Upgrade.** The table and the cursor column `settled_fingerprint` are added
  when the store is opened; cursors are kept, so nothing is re-read. Sessions
  stored before are not archived until the next settled ingest sees them quiet.
  Without `--settled-minutes`, `history ingest` behaves and reports as before.

## Two-stage conversation search

The worker's `conv-v2` tags (see [DeepSeek tagging adapter](#deepseek-tagging-adapter))
make sessions searchable by what kind of work they were. The calling agent is a
model itself, so there is no intent classifier: it translates the user's intent
into filters.

**Tag filters** (CLI `--task-type`, `--outcome`, `--project`, `--component`,
`--intent-like`; MCP `task_type`, `outcome`, `project`, `component`,
`intent_like`) match the session's tags. The session's newest summary
decides: when it is untagged, an older tagged row does not count. Values are
lower-cased like the tags; `task_type` and `outcome` must be in the tag
vocabulary (`dev|review|debug|plan|ops|research|chat|other`,
`done|blocked|abandoned|ongoing|unknown`), `project` and `component` match one
list item exactly, `intent_like` is a substring of `intent`; matches are
re-checked on the parsed tags. A session without tags (never processed, or a
`conv-v1` newest summary) never matches a tag
filter. They work in the default mode too (they narrow the turn hits) and
combine with `--app`, `--since` and `--archived-only`.

**`--mode two_stage`** (MCP `mode: "two_stage"`) returns sessions, not turns.

1. *Candidates* are the union of three lists, each cut to the caller's visible
   apps (share policy and the requested app) before anything is ranked or
   counted:
   - `fts`: sessions with turns holding every query term (the default search,
     up to 200 visible hits);
   - `semantic`: sessions whose "summary + tags" text is close to the query,
     embedded by the embedder the runtime already has (`MEMFW_EMBEDDING_URL`,
     `_MODEL`, `_DIMENSION`, the one `memory_search` hybrid uses; the CLI reads
     the same variables). The newest 200 summaries that pass the filters are
     candidates; only uncached ones are sent, at most 64 per call (the rest are
     scored on a later call), and vectors are memoised per process as packed
     float32, at most 32 MiB. The embedder gets 8 seconds in total per call
     with no retries. Without an embedder this list is empty and the response
     says `degraded: ["no_embedder"]`; a timeout or error gives
     `embedder_error`.
     **Gate:** summaries are sent only to an embedder that is local (its
     endpoint host is `localhost`, `127.0.0.0/8`, `::1` or `::ffff:127.x`,
     or the provider says `local = True`; the strict, canonical rule; such a
     request never goes through an environment proxy) or when `vault.yaml` has `conversations: {cloud: true}`,
     the same switch a non-local worker backend needs. Otherwise the list is
     skipped with `degraded: ["semantic_off"]` and the endpoint is never
     contacted (a malformed `cloud` value counts as off);
   - `tags`: sessions whose tags match the tag filters (up to 500, newest
     summaries first).

   Every filter applies to every list. The lists are fused by reciprocal rank
   (k = 60); `matched_by` says which lists a session came from.
2. *Per session* the response holds the session info, `archived`, `rank`,
   `score`, `model_output` (`summary`, `tags`, `prompt_version`, labelled
   `untrusted_model_output: true` and `[model_summarized: not verified]`, or
   `null` when the worker has nothing) and up to `windows` (default 2, max 5)
   redacted turn windows: `window_turns` (default 1, max 3) turns on each side
   of a hit, each turn cut to 1200 characters. Hits are the store's matches;
   for a session found only by meaning, its first 1000 turns are scanned for
   any query term (5000 turns at most across all sessions of a call); a tag-only call has no windows (read the session with
   `conversations_get`). The whole response stays within `max_bytes` (default
   16 KiB, 4–64 KiB): sessions first, then windows by rank; `truncated` says
   something was left out and `next_offset` pages on. A first session too big
   on its own is shrunk (model summary cut, then tags and model output
   dropped, then `cwd` and `project` emptied); if it still does not fit it is
   skipped and `next_offset` moves past it, so paging always advances.
   `archived` is `null` when the store cannot tell.

The query may be empty when a tag filter is given:

```sh
memfw conversations search "" --mode two_stage --task-type debug --project memfw
memfw conversations search "retry backoff" --mode two_stage --outcome done --since 2026-11-01
```

```json
{"query": "", "mode": "two_stage", "task_type": "debug", "project": "memfw"}
```

Summaries and tags are model output: the response's `notice` and
`untrusted_model_output: true` say so, and they are redacted again on the way
out like every turn. A hidden or opted-out app's sessions are never candidates,
never counted in `candidates`, and their summaries never reach the embedder.
Nothing is written. Without `--mode` and the filters the output is unchanged.

## Scheduling

`memfw conversations schedule --print --root VAULT` prints a launchd plist
(template: `scripts/launchd/com.memfw.conversations-worker.plist.template`).
memfw never installs it. To install it yourself, save the output to
`~/Library/LaunchAgents/com.memfw.conversations-worker.plist` and run
`launchctl bootstrap gui/$(id -u) <that file>`.

## Enabling it in an app

Register the MCP server once per app with that app's name, for example:

- Claude Code: `claude mcp add memfw -- memfw-mcp --root VAULT --app claude`
- Codex (`config.toml`): `[mcp_servers.memfw]` with `command = "memfw-mcp"`
  and `args = ["--root", "VAULT", "--app", "codex"]`
- Cursor, Gemini, Kimi, Grok, OpenCode: add an `mcpServers` / `mcp` entry with
  the same command and `--app cursor|gemini|kimi|grok|opencode`
- Conductor Workers run with an isolated HOME: the entry goes in the worker
  profile, and the sandbox must allow reading the store and the vault.

# Sharing conversations across agent apps

`memfw conversations` exposes the unified, redacted conversation store
(`~/.local/share/memfw/conversations.sqlite`, written by the store's own
ingest) to every agent app on this machine, read-only, and runs an
asynchronous worker that summarises sessions.

## Read surface

| MCP tool | CLI | What it returns |
|---|---|---|
| `conversations_search(query, app?, agent?, since?, limit, offset)` | `memfw conversations search QUERY` | redacted snippets with refs `app:session@offset` |
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
work tree, the vault or a synced folder).

- Backend: `extractive` (no model) by default. `--backend module:factory`
  picks another; a backend that is not `local` also needs `--allow-cloud`.
  `vault.yaml` cannot choose a backend.
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
  next round reads it again. Last activity is the newest timestamp among the
  turns read this round; a transcript without timestamps uses the file's
  mtime. A locator holding several sessions is settled once all of them are
  quiet. The cwd opt-out still applies to a skipped transcript.
- **Archived.** When a settled transcript's content is in the store (written
  now, or unchanged since an earlier ingest), each of its sessions gets a row in
  `conversation_archive`: `archived_at` and `archived_fingerprint`
  (`turns:last_offset:updated_at`, the same value the worker keys its jobs by).
  `sessions_archived` counts sessions archived in a new shape this round.
- **Resumed.** A session is archived only while its fingerprint matches its
  stored turns. When it becomes active again, the settled ingest drops its
  archive row; new turns written by a plain ingest change the fingerprint. Either
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

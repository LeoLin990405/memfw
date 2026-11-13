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
  `_candidates/dream/conv-*/` for human review.
- Skill candidates: the worker only queues `episodes` (ids, offsets, intent,
  outcome). The skill step consumes them with `EpisodeQueue.claim` / `ack`.

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

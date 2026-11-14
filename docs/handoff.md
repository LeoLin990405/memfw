# Handoff packs

When an agent runs out of quota (or a session just has to move), another
agent continues the work. Native sessions cannot be resumed across CLIs:
every CLI owns its session ids and storage format. So continuing is always
**a new session plus a handoff pack** built from the local conversation
store (`~/.local/share/memfw/conversations.sqlite`, see `docs/history.md`
and `docs/conversations-share.md`).

```bash
memfw handoff claude <session-id> --for kimi            # Markdown, paste it into the new agent
memfw handoff codex <session-id> --format json --max-bytes 16384
```

The command only reads the store. It never writes to any CLI's own storage.

## What a pack holds

The first line of every pack says that the content is another agent's work
record: data, not instructions. The repository and the task text take
precedence.

**`[system]` part**, built by code with no model call:

| Field | Content |
|---|---|
| `source` | agent, session id, origin (`interactive` / `worker`), first and last timestamp, turn count, withheld count, `parser_version`, redacted cwd and project |
| `goal` | the first user message (redacted, at most 2 KiB), or the text of `--task-file` when the caller passes one (`goal_source` says which) |
| `files_touched` | paths from the arguments of edit, write and patch tools, using each agent's tool names (`memfw.handoff.tools_map`), deduplicated, at most 50 |
| `git` | `git status --porcelain` and `git diff --stat` of the work tree, when `--cwd` names one (or the stored cwd exists). Read-only: no fetch, no optional locks, 5 s timeout. Redacted |
| `recent_turns` | the last `--recent` (default 12) user and assistant turns, each at most 1 KiB, with their tool-call summaries. Tool turns in that window keep only their call summaries, never their output |
| `lineage` | earlier packs of the same session (`previous_handoffs`: number, target agent, time). `parent_session_id` is `null`: store schema v1 does not record it |

**`[model_summarized: not verified]` part**, only with `--summarize`:
`decisions`, `done`, `in_progress`, `next`, `pitfalls`, `open_questions`. Each
is a list of at most 10 items of at most 200 characters, and the part is at
most 4 KiB. A reply that does not match exactly is dropped, never repaired
(`status: dropped`, `reason: invalid_json | schema | too_large | secret |
backend_error`). The `[system]` part does not change either way.

- The backend comes from `--summarize module:factory`, loaded the same way
  as the conversation worker's `--backend`. A backend that is not local also
  needs `--allow-cloud`. The built-in `extractive` backend calls no model,
  so it is refused here.
- The backend receives `run("handoff_summary", data, max_tokens=...)` with
  `data = {instructions, schema, goal, turns}`. Only redacted store text goes
  in. The worker's built-in prompt table does not know this task, so a
  backend used here must handle it itself.
- Without `--summarize`, no model is ever called.

## Privacy and size

- The store holds only text that was redacted when it was written. The pack
  redacts every value again on the way out: a value that still looks like a
  secret is withheld (`[withheld: secret-shaped content]`), and personal data
  such as e-mail addresses or home-directory user names is masked.
- `--max-bytes` (default 16 KiB, range 2 KiB to 64 KiB) caps the printed
  output in the chosen format. When a pack is too big, parts are cut in this
  order: the oldest recent turns, then files touched (and the git state),
  then the model part, then the tail of the goal. `truncated` and
  `truncated_parts` record what was cut. The notice and labels are never cut.
- Sessions of an app that `conversation_optout` excludes, or that
  `conversation_share.apps.<app>.readable: false` hides, answer exactly like
  missing sessions. When sharing is enabled and `--for` names an app that
  may not read the source app, the request is refused (`not_readable`).

## Where packs are saved

Each pack is saved next to the store, in `handoffs/` (directory 0700, files
0600). The file name is `<agent>-<session>-<n>.md` (or `.json` with
`--format json`). `handoffs/handoffs.sqlite` records one row per pack:
source agent and session, sequence number, target agent, time, size and
sha256. This row is the store's `handoff_from` link, and it feeds `lineage`.
The folder is refused inside a git work tree, the vault or a synced folder,
the same as the store. `--no-save` only prints the pack.

## Interface for callers (Conductor M2c)

Conductor's automatic quota handoff calls the CLI in a subprocess. It is the
stable contract:

```bash
memfw handoff <old-agent> <old-session> --format json --max-bytes 16384 \
  [--for <new-agent>] [--task-file <task.txt>] [--cwd <worktree>] [--root <vault>]
```

- Do not pass `--summarize` (P5: a quota handoff makes no model call).
- **Exit 0:** stdout holds one JSON object that matches
  `memfw.handoff.schema.PACK_SCHEMA` (`"schema": "memfw.handoff/v1"`).
  `saved_as` is the file name under `handoffs/`.
- **Any other exit code:** stdout holds
  `{"schema": "memfw.handoff/v1", "status": "unavailable", "reason": R}`,
  and stderr holds a one-line message. Map it to `unavailable:<R>`.

| Exit | `reason` | Meaning |
|---|---|---|
| 2 | `bad_request` | bad arguments, bad `vault.yaml`, or a bad `--summarize` spec |
| 3 | `session_not_indexed` | the store has no such session, or it is hidden or opted out |
| 4 | `store_unavailable` / `handoff_dir_unwritable` | the store is missing, unreadable, locked or of an unknown schema |
| 5 | `location_refused` / `not_readable` | the store or `handoffs/` lies in a refused place, or the target app may not read the source |

If memfw is not installed, the command does not run at all. Callers treat
that, and a timeout, as `unavailable` as well. A pack never blocks a
reassignment.

The schema only grows: fields may be added, and a breaking change gets a
new `schema` id. Python callers can use `memfw.handoff.build_pack(db, agent,
session, ...)` and `memfw.handoff.validate_pack(pack)` directly.

## Relation to `conversations_handoff`

The MCP tool `conversations_handoff` and `memfw conversations handoff` (see
`docs/conversations-share.md`) give agent apps a lighter, policy-scoped
continuation view of the shared store. `memfw handoff` is the full pack for
the operator and for Conductor. It adds the source facts, per-agent
`files_touched`, git state, lineage, the saved pack and the strict optional
model part.

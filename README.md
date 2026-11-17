<div align="center">

<img src="docs/assets/banner.svg" alt="memfw banner" width="100%">

# memfw

**A long-term memory for AI agents that you can read, diff and trust.**

Markdown is the source of truth · hash-linked JSONL events · rebuildable FTS5 · verify-first consolidation · one redacted conversation store for every agent · local MCP server · layered, swappable ports.

[**简体中文**](README.zh-CN.md) · **English**

<a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue?style=flat-square" alt="License"></a>
<a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.11+"></a>
<a href="#-status-and-boundaries"><img src="https://img.shields.io/badge/release-v0.3.0-2563EB?style=flat-square" alt="Release v0.3.0"></a>
<a href="#-mcp-tools"><img src="https://img.shields.io/badge/MCP-15_tools_(%2B1_opt--in),_3_resources-8B5CF6?style=flat-square" alt="15 MCP tools (+1 opt-in), 3 resources"></a>
<a href="#-conversation-store-every-agent-one-place"><img src="https://img.shields.io/badge/agent_readers-14-10B981?style=flat-square" alt="14 agent readers"></a>
<a href="#-architecture-layers-and-ports"><img src="https://img.shields.io/badge/ports-11-0EA5E9?style=flat-square" alt="11 ports"></a>
<a href="#-evaluation-summary"><img src="https://img.shields.io/badge/evaluation-bounded-D97706?style=flat-square" alt="Bounded evaluation"></a>

</div>

## What is memfw?

memfw is a local memory engine for AI agents. It does three jobs:

1. **Keeps memories as files.** Every memory is a plain Markdown file in a vault folder, every
   change is appended to a hash-linked event log, and search runs on an index you can rebuild at
   any time.
2. **Collects what agents said.** It reads the session stores of 14 agent CLIs and apps into one
   local, redacted conversation store, so any agent can search, read and continue another
   agent's work.
3. **Turns that into better memory, carefully.** "Dream" passes check memories against the
   transcripts and propose confirmations, corrections, merges, lessons and skill drafts. They
   only ever stage candidates; a person decides what goes into the vault.

Agents use it through a local **MCP server** (`memfw-mcp`); people use the `memfw` CLI. Inside,
memfw is a small kernel behind port contracts: search backends, projectors and ingestors plug in
through a port, never into the vault (see [Architecture](#-architecture-layers-and-ports)).

```
agent CLIs / apps ──readers──► conversation store (local, redacted)
        │                              │
        │ MCP                          ├─► conversations worker ─► summaries, episodes
        ▼                              ├─► handoff packs (continue in another agent)
     memfw-mcp ──► vault ◄── human ◄───┴─► dream: verify · curate · distill · skills
                    ├─ memories/*.md      (source of truth)          (candidates only)
                    ├─ events.jsonl       (hash-linked audit log)
                    └─ index.sqlite3      (FTS5; rebuildable)
```

## 🧭 Design principles

1. **Local-first.** The vault, the indexes and the conversation store live on your machine. The
   core calls no model and needs no network; anything that could reach a cloud model is off by
   default and needs an explicit `--allow-cloud`.
2. **Event-backed Markdown.** Markdown files are the authority. Every state change is appended to
   a hash-linked JSONL event chain (`memfw log verify` proves it is intact). SQLite indexes are
   derived projections that can be deleted and rebuilt.
3. **Verify-first.** Nothing a model proposes becomes memory until it has been checked against
   evidence and approved by a person. Dream and the conversation worker stage candidates; they
   never write the vault.
4. **Human > mechanism > model.** Models may propose; deterministic mechanisms check budgets,
   provenance and state transitions; a person stays responsible for content and anchor decisions.
5. **Inference is not testimony.** Model-inferred content stays distinguishable from what a user
   actually said, with sources and uncertainty kept. Model summaries are labelled as not verified.
6. **Engine, not platform.** memfw makes no assumptions about hosts, SSH or remote search
   backends. Those belong to the application that embeds it.

## 🚀 Install and quick start

**Requirements:** Python 3.11+. [uv](https://docs.astral.sh/uv/) is recommended.

```sh
git clone <this-repository> memfw
cd memfw
uv venv .venv && uv sync --frozen --extra dev   # or: python -m venv .venv && pip install -e '.[dev]'
. .venv/bin/activate
```

This installs two commands: `memfw` (CLI) and `memfw-mcp` (stdio MCP server).
`memfw --version` prints the installed version (`memfw 0.3.0`); the release smoke test checks it.
`memfw --help` groups the commands into Daily, Approval, Ops and Export panels.

Create a vault, add a memory and search it:

```sh
memfw init ./demo-vault
memfw add --root ./demo-vault \
  --id note-1 --title 'Local demo' --body 'The greenhouse sensor reports 18 degrees.'
memfw index rebuild --root ./demo-vault
memfw index search greenhouse --root ./demo-vault
memfw log verify --root ./demo-vault
```

Connect an MCP client. Name the app with `--app` (or `MEMFW_APP`) so the conversation-sharing
rules can apply to it:

```json
{
  "mcpServers": {
    "memfw": {
      "command": "memfw-mcp",
      "args": ["--root", "/path/to/your/vault", "--app", "claude"]
    }
  }
}
```

For Claude Code: `claude mcp add memfw -- memfw-mcp --root /path/to/vault --app claude`. Codex,
Cursor, Gemini, Kimi, Grok, OpenCode and others take the same command with their own `--app`
name (see [`docs/conversations-share.md`](docs/conversations-share.md)).

## 🗂️ Vault layout

`memfw init` creates a plain folder:

| Path | What it is |
|---|---|
| `vault.yaml` | vault settings: layers, tiers, budgets, `permissions: propose_then_confirm`, history sources, sharing rules |
| `memories/<id>.md` | **the memories**: Markdown with YAML frontmatter (layer, tier, `about`, `related`, `valid_from` / `valid_to`, optional `status`, `evidence` and `privacy`) |
| `events.jsonl`, `events.state.json`, `.log/segments/` | the hash-linked event log and its archived segments |
| `.history/<id>/` | retained earlier versions (for `rollback`) |
| `.proposals/` | agent proposals bound by hash to their exact payload |
| `.archives/` | deterministic `tar.zst` cold archives (cold tier) |
| `_candidates/remember/`, `_candidates/dream/`, `_candidates/skills/` | staged candidates waiting for a person (`_candidates/dream-rejections/` records rejected open-question candidates) |
| `index.sqlite3`, `.memfw-history.sqlite` | derived FTS5 indexes (disposable; rebuild any time) |

The conversation store and the worker state are kept **outside** the vault, by default under
`~/.local/share/memfw/` (see [Privacy](#-privacy-and-permissions)). If the vault is a git
repository, add `index.sqlite3` and `.memfw-history.sqlite*` to its `.gitignore`.

An index written by an older memfw has no temporal projections. Writers are lazy about it:
the MCP server (`memfw-mcp`) and `memfw index search` check the schema generation (`PRAGMA user_version`)
before using the file and rebuild it when it is stale, so an upgrade needs no manual step there.
The read-only HTTP service (`scripts/memfw_readonly_service.py`) never writes, so it **does not**
rebuild: it detects the missing tables and degrades instead (no temporal filtering when
`memory_temporal` is absent, and no results at all when `memory_access` is absent). Run
`memfw index rebuild` once before pointing the read-only service at an upgraded vault.

## 🧠 Memory commands

| Area | Commands |
|---|---|
| Write and read | `add`, `list`, `show`, `update [--dry-run]`, `delete` (hash-only tombstone), `rollback`, `supersede`; `list`, `show` and `index search` take `--json` |
| Integrity | `fsck`, `recover`, `log verify`, `stats` |
| Search | `index rebuild`, `index search`; `resolve` (batch, read-only lifecycle/validity/privacy check of recall hits) |
| Concepts | `concepts` (one canonical concept per thing, with aliases that may be scoped; optional `broader` parents, `narrower` derived), `concepts relations` (the versioned relation-type table), `concepts lint-relations` |
| Uncertainty | `add --type open-question --question … [--conflicts ID …] [--missing …]`, `update ID --question/--conflicts/--missing`, `update ID --resolved-by ANSWER`; `corroboration rebuild`, `corroboration show ID --as-app APP`, `corroboration queue` (review order); `dream accept-question` / `reject-question` |
| Epistemic and experience | `add` / `update --epistemic --causal --causal-record` (one write gate; `causal: verified` needs a human), `experience draft` / `from-github` / `invalidate` (reviewed experience points, staged as candidates), `eval causal` |
| Privacy | `add --privacy LEVEL`, `update --privacy LEVEL`, `privacy legacy-default [LEVEL]`, `privacy new-default [LEVEL]` (levels `public`, `shared`, `local-private`) |
| Review gate | `candidates`, `candidate-apply [--apply]`, `candidates-expire` (30-day TTL, never deletes), `record-proposal`, `approve-proposal`, `review` |
| Retention | `budget`, `forget` (`fifo`, `lru`, `decay`, `salience`, `consolidate_then_forget`), `tiers` / `tier` (hot and cold), `cold-archive`, `cold-restore`, `layer` / `layer-set`, `consolidate` |
| Related memories | `memory-related-build`, `memory-related` |
| Projections | `okf export` / `okf validate` / `okf import` ([OKF v0.2](https://github.com/GoogleCloudPlatform/open-knowledge-format) bundles), `project obsidian` (a read-only Obsidian board), `notes` (a Markdown notes index), `code` (local code retrieval) |

Search matches each query word on its own and ranks by bm25, so a whole sentence finds relevant
memories. `memory_search` `mode` defaults to `hybrid`, which is FTS5 unless a local OpenAI-compatible
embedding endpoint is configured. Every command prints
`--help`; global `--now` and `--id-seed` make runs deterministic for tests. Which capability is
reachable from the CLI, MCP and HTTP (and why a gap is there) is in
[`docs/cli-mcp-parity.md`](docs/cli-mcp-parity.md).

### Knowing what is not known

- **Open questions.** A `type: open-question` memory records what is *not* known: the
  question, the memories that conflict, what is missing, and later the memory that resolved it
  (`resolved_by`). Creating or resolving one needs a person (`memfw add --type open-question`,
  `memfw update --resolved-by`) or an approved proposal.
- **Conflicts become questions.** When `dream run` finds two current memories about one concept
  that disagree, it also stages an open-question candidate naming both, without picking a
  winner. `memfw dream accept-question` writes it through the same gate; `reject-question`
  drops it and records the rejection so the pair is not proposed again.
- **Recall labels.** `memory_search` marks a hit that an unresolved, visible open question is
  about with `open_question:<id>`, and one the question lists as conflicting with `conflicted`
  (always in `mode=two_stage`, otherwise with `labels: true`). The tool tells agents to say
  "unknown" rather than guess.
- **Corroboration.** `memfw corroboration rebuild` writes a rebuildable
  `corroboration.sqlite3`: per memory, the independent (app, session) pairs that mention it.
  Ids, names, counts and times only, trimmed to what the asking app may read. With `labels: true`,
  MCP `memory_search` shows it on every row; `memfw corroboration queue` lists unresolved
  questions with corroborated conflicts first.
- **Concept taxonomy.** Concept files may name `broader` parents; `narrower` is derived, never
  written, and nothing on the read path follows it (no query expansion). The relation-type
  table lists every typed relation memfw already stores. See [`docs/ontology.md`](docs/ontology.md).

Details: [`docs/epistemic-and-experience.md`](docs/epistemic-and-experience.md).

## 💬 Conversation store: every agent, one place

`memfw history ingest` reads each agent's own session files and writes normalised turns into one
local store (`~/.local/share/memfw/conversations.sqlite`). It is incremental: unchanged sessions
are skipped, appended ones are read from the last stored turn. Before a turn is written, every
text field is scanned for secrets; a hit withholds the whole turn, everything else is redacted.
Raw lines are never copied, and memfw never writes to an agent's own storage.
`--settled-minutes N` skips sessions active in the last `N` minutes and marks the settled ones
archived (`memfw conversations list --with-archive-state` / `--archived-only`).

**14 registered readers** (`memfw history agents` lists them and which stores exist on this
machine, including the isolated homes of orchestrated workers):

| Reader | Agent or app | Default store |
|---|---|---|
| `claude` | Claude Code | `~/.claude/projects` |
| `codex` | Codex CLI | `~/.codex/sessions`, `~/.codex/archived_sessions` |
| `gemini` | Gemini CLI | `~/.gemini/tmp` |
| `antigravity` | Google Antigravity (app, IDE and CLI) | `~/.gemini/antigravity*` |
| `cursor` | Cursor CLI (`cursor-agent`) | `~/.cursor/chats` |
| `cursor-ide` | Cursor IDE | Cursor's `User` folder (macOS and Linux) |
| `opencode` | OpenCode | `~/.local/share/opencode` |
| `kimi` | Kimi Code CLI | `~/.kimi-code/sessions` |
| `kimi-desktop` | Kimi desktop app | the app's embedded agent home |
| `grok` | Grok CLI | `~/.grok/sessions` |
| `glm` | Z Code CLI (Zhipu GLM) | `~/.zcode/cli`, `~/.zcode-cli/sessions` |
| `deepseek` | DeepSeek harness CLI (`dsh`) | `~/.dsh/sessions` |
| `minimax` | MiniMax Agent CLI | `~/.minimax/v2/sessions` |
| `stepfun` | StepFun `stepcode` | `~/.stepcode/agent/sessions` |

```sh
memfw history agents
memfw history ingest                 # every registered reader
memfw history ingest claude codex    # only these
```

### Sharing across apps

`memfw conversations` (and the matching MCP tools) give every agent app read-only access to the
store, filtered by the visibility rules in `vault.yaml`. Sharing is **off** until you set
`conversation_share.enabled: true`.

```sh
memfw conversations list --limit 20
memfw conversations search "migration rollback" --app codex
memfw conversations get SESSION_ID --start 0 --max-bytes 16384
memfw conversations handoff SESSION_ID --for kimi
```

```yaml
conversation_share:
  enabled: true
  apps:
    some-app: {readable: false}          # never returned to anyone, never processed
    other-app: {can_read: [claude, codex]}
conversation_optout:
  agents: [private-app]                  # never ingested; stored rows are deleted
```

Every output is redacted again on the way out, size-capped and labelled as data, not
instructions. A hidden session answers exactly like a missing one. This is a visibility setting
between cooperating local apps, not authentication.

### Conversations worker

`memfw conversations worker` is one idempotent, locked, budgeted run: ingest, find idle sessions,
then `chunk_summary` → `session_summary` → `memory_claims` → `episode` jobs. Re-running repeats no
finished work, a crashed run resumes, and a keyset scan cursor walks a large backlog over several
runs without skipping or repeating sessions. It prints counts only.

- The default backend is `extractive` (no model). `--backend module:factory` picks another, or
  `vault.yaml` names one under `conversations: {backend, cloud, options}`. A backend that is not
  local always needs `conversations.cloud: true` in `vault.yaml`; on the command line it also
  needs `--allow-cloud`. A configured backend that cannot run is never swapped for the
  extractive one: the run is reported `degraded`.
- The optional DeepSeek adapter (`packages/adapters/memfw-adapter-deepseek`, a separate wheel)
  tags sessions with the `conv-v2` schema (summary, project, task type, components, intent,
  outcome). It is **off** unless `conversations.cloud: true` is set and `DEEPSEEK_API_KEY` is
  present, and it runs under a per-run token and cost budget.
- Claims a model proposes are verified against the session first and staged under
  `_candidates/dream/conv-*/` for review. Episodes are queued for `dream skills --from-queue`.
- `memfw conversations status` shows job and episode counts (no text).
  `memfw conversations schedule --print` prints a launchd plist; installing it is left to you.

### Transcript index

`memfw history index | list | search | read` is a separate, read-only index over session logs you
configure yourself (`history_sources` in `vault.yaml`, or `--source NAME=PATH`): Claude Code and
Codex JSONL, generic worker and ticket JSONL, and plain `.log` files. Each hit carries an opaque
ref such as `codex:3f9c2a71d04e8b65@18342` that `memfw history read` opens as a redacted,
size-capped window. Dream uses this index as its evidence. See
[`docs/history.md`](docs/history.md).

## 🔁 Handoff: continue in another agent

When an agent runs out of quota, or a task has to move, start a new session in another agent and
give it a handoff pack. Native sessions cannot be resumed across CLIs, so the pack is built from
the conversation store:

```sh
memfw handoff claude SESSION_ID --for kimi                   # Markdown to paste
memfw handoff codex SESSION_ID --format json --max-bytes 16384 --cwd ./worktree
```

The pack opens with a notice that it is another agent's work record, not instructions. Its
`[system]` part is built by code: source facts, the goal (first user message or `--task-file`),
files touched, read-only `git status` / `diff --stat`, recent turns, and lineage of earlier packs.
A `[model_summarized: not verified]` part is added only with `--summarize module:factory`; an
invalid reply is dropped, never repaired. Packs are redacted again, capped by `--max-bytes`
(2–64 KiB, trimmed in a fixed order), and saved privately next to the store (`--no-save` prints
only). Exit codes and the JSON schema (`memfw.handoff/v1`) are a stable contract for
orchestrators. See [`docs/handoff.md`](docs/handoff.md).

## 🌙 Dream: verify-first consolidation

`memfw dream run` is an offline, deterministic pass that never writes the vault. Results land in
`_candidates/dream/<dream_id>/` with a `report.json`:

| Step | What it proposes |
|---|---|
| **Verify** (always) | for each cited memory, transcript evidence decides `confirm`, `correct` (one replacement value, never a secret- or account-like one) or `mark-disputed`; no evidence, no candidate |
| **Curate** | inside a concept: `merge` (same content, synonyms folded), `supersede` (same attribute, different value, including negation), `mark-stale`; never across different subjects |
| **Distill** | a `type: lesson` memory from at least two independent events (different cited sessions), with counterexamples named |
| **Budget** | demotions when the injected layer exceeds 200 lines / 25 KB |

```sh
memfw dream run --root /path/to/vault --dry-run
memfw dream run --root /path/to/vault --focus "deploy" --max-sessions 20
```

Memories gain two optional fields, `status` (`confirmed | unconfirmed | disputed`) and
`evidence` (history refs). Older files parse unchanged. A curate conflict also yields an
open-question candidate (see [Knowing what is not known](#knowing-what-is-not-known)):

```sh
memfw dream accept-question DREAM_ID MEM_A MEM_B --apply   # a person writes the question
memfw dream reject-question DREAM_ID MEM_A MEM_B --apply   # or drops it for good
```

### Skill candidates

`memfw dream skills` drafts `SKILL.md` files from procedures that repeat across sessions:

- injected boilerplate (harness reminders, instruction-file echoes, orchestrator templates seen
  verbatim in many sessions) is stripped first, and "continue" turns do not open tasks;
- each tool call becomes step signatures: program plus a fixed sub-command vocabulary, never free
  arguments such as hosts, paths or branches; read-only inspection, bare interpreters and agent
  plumbing are not steps;
- episodes cluster by what was done, retried or fanned-out copies of one request count as one
  session, and a procedure needs `--min-sessions` (3) independent sessions from at least two
  agents or two days;
- a step is kept only when it appears in enough of those sessions, and an inspection-only
  procedure gives no candidate.

Each candidate under `_candidates/skills/<id>/` holds `SKILL.md` (when to use, preconditions,
steps with intent, command, inputs and the checks seen), `evidence.json` and `candidate.json`
(`approved: false`, `installed: false`, a confidence score). Text is redacted, paths and URLs are
masked, and terms listed in `skills_distill.private_terms` become `<private>`. Nothing is
installed; a person copies a reviewed `SKILL.md` into place.

```sh
memfw dream skills --root /path/to/vault --dry-run --summary   # report only
memfw dream skills --root /path/to/vault --from-queue          # use the worker's episodes
```

An optional local `--drafter module:function` may reword drafts; its output is verified like
evidence. See [`docs/dream.md`](docs/dream.md).

## 🤖 MCP tools

`memfw-mcp --root VAULT --app NAME` serves one vault over stdio. It has no write tool for the
vault itself. It lists 15 tools; a 16th, `memory_used`, appears only when it is switched on.

| Tool | Purpose |
|---|---|
| `memory_search` | search the vault; `mode` = `hybrid` (default), `fts`, `vector`, `multihop` or the opt-in `two_stage` (recall coordinator, `policy` `compat_fts` or `strict`); `source`, `include_history`, `as_of`, `labels` (open-question labels and corroboration) |
| `memory_get` | read one memory by id |
| `memory_remember` | stage a candidate under `_candidates/remember/` with tags, related ids, suggestions and an optional `privacy` level |
| `memory_related` | derived related-memory edges (tenant-scoped) |
| `memory_context` | token-budgeted resident context: anchors and procedural memories first; `v=2` returns a versioned `ContextPack` with slots `core`, `handoff` and `topics`, overflow, an optional `strict` policy and `if_version` (reply `unchanged: true` when nothing changed) |
| `memory_status` | vault size, layer and tier counts, log summary |
| `history_list`, `history_search`, `history_read` | the transcript index: sessions with lineage, redacted snippets with refs, capped windows |
| `conversations_list`, `conversations_search`, `conversations_get` | the shared conversation store, filtered by the calling app's visibility |
| `conversations_handoff` | a lighter, policy-scoped continuation view for agent apps |
| `dream_run` | one dream; a dry run by default, deterministic rules only, at most 50 staged dreams per tenant and one per minute |
| `dream_list` | this tenant's staged dreams, newest first |
| `memory_used` (opt-in) | record which memories a session used; listed only with `runtime: {usage: {enabled: true}}` or `MEMFW_USAGE_SIGNAL=1`; writes a store outside the vault, never the vault |

It also serves three **MCP resources**, `memfw://context/core`, `memfw://context/handoff` and
`memfw://context/topics` (`?policy=` and `?if_version=` accepted): each is that slot's v=2
pack for the calling app. A slot that fails or times out comes back empty with a `degraded`
reason; the others are not affected. `pack_version` is computed from ids, versions and hashes
only, never body text. Details: [`docs/ports-and-recall.md`](docs/ports-and-recall.md) §4.

`MEMFW_MCP_CLEARANCE=public,shared` limits every read tool and resource to those privacy levels
(unset: no filtering). `runtime: {search: {record_access: false}}` (or `MEMFW_RECORD_ACCESS=0`)
makes a session write nothing to the vault.

## 🔒 Privacy and permissions

- **Raw transcripts stay where they are.** memfw reads agent session files and never writes to
  them. Only redacted text reaches the store or the indexes; a turn that looks like it holds a
  secret is withheld entirely, never stored as text.
- **Redaction twice.** Text is redacted on the way in and again on the way out of every read,
  handoff and candidate. E-mail addresses and home-directory user names are masked.
- **Private locations.** The store, the worker state and the handoff folder default to
  `~/.local/share/memfw/` with directories `0700` and files `0600`. A location inside a git work
  tree, inside the vault or inside a synced folder (iCloud, Dropbox, Google Drive, OneDrive,
  Syncthing …) is refused; `conversation_store.synced_dirs` adds more.
- **Opt-out and visibility.** `conversation_optout` excludes an agent everywhere and deletes its
  rows; `conversation_share.apps` controls who may read whom and what the worker processes.
- **Privacy levels and clearance.** A memory may carry `privacy: public | shared |
  local-private`. The level a reader acts on is the strictest of the field (or the vault's
  `legacy_privacy_default` when it is missing, `local-private` when that is unset), `privacy:`
  and `sensitivity:` tags and a top-level `sensitivity:` key; an unrecognised value is never
  admitted. A clearance is opt-in on every exit: the HTTP service (`MEMFW_READONLY_CLEARANCE`),
  `okf export --clearance`, the notes server and the memfw MCP server (`MEMFW_MCP_CLEARANCE`).
  Writes keep the level, and only a person may loosen it.
- **No silent model calls.** Every model hook (`--backend`, `--summarize`, `--judge`,
  `--drafter`) is off by default, and a non-local one needs `--allow-cloud`; a non-local
  conversation backend or handoff summariser also needs `conversations.cloud: true` in
  `vault.yaml`.
  The MCP `dream_run` cannot name any code to run.
- **Human gate.** `permissions: propose_then_confirm` is the vault default: agents propose, a
  person applies (`candidate-apply`, `update`, `supersede`).
- **Fail closed.** Under the `strict` policy, privacy (`privacy:`, `sensitivity:`) and
  `lifecycle:` tags match in any case and the strictest tag wins, so tag order or spelling never
  widens access; an unknown policy name is an error. `supersede` writes the replacement in
  the predecessor's tenant and scope (`fsck` reports older mismatches as
  `supersede_scope_mismatch`). Budget enforcement with a hard forget never deletes a live
  supersede chain.

## ⬆️ Reader-upgrade order

Older memfw readers reject frontmatter keys they do not know, so a memory carrying the new
`status` or `evidence` fields would vanish from them. When several tools share a vault:

1. **Upgrade every reader first:** each memfw install, the MCP server and any service that reads
   the vault, so unknown keys are ignored (and listed as `memfw list` warnings).
2. **Then upgrade the writers:** the `memfw` CLI that applies candidates and the conversations
   worker.
3. **Only then apply** `status` / `evidence` from dream candidates.

Candidate files, reports and the worker state only ever gain keys or tables, so older readers and
workers keep working (an older worker simply ignores the scan cursor). Details in
[`docs/dream.md`](docs/dream.md#memory-fields).

**From 0.2.0 to 0.3.0, upgrade every host that shares a vault together.** memfw 0.2.0 reads
0.3.0 files, but a rewrite by 0.2.0 drops every memory field it does not know: `epistemic`,
`causal`, `causal_record`, `limitations`, `experience`, the `open_question` block, `privacy` and
a top-level `sensitivity:` key (so it can loosen a memory). Once 0.3.0 compacts a vault's event
log, 0.2.0 can no longer read that log. The conversation store moves to schema 2 on first open
(the worker may re-process sessions once, up to every session; run it on the extractive backend
before enabling a cloud backend), and two `memfw-http/2` answers changed:
`/related` without a tenant gives 404 for a tenanted memory, and `/stage` with a known
`proposal_id` but changed content gives 409. See
[`CHANGELOG.md` → Compatibility / upgrade notes](CHANGELOG.md#compatibility--upgrade-notes).

## 🌐 Integration and federation

memfw is an engine; orchestrators integrate with it at arm's length:

- a **read-only HTTP service** (`memfw-http/2` contract: separate read and write tokens, `/stage`
  for candidates, root-only endpoints) lets another system federate memfw recall without
  copying or mounting the vault. Deploy `scripts/memfw_readonly_service.py` together with
  `src/memfw/index/readonly.py` and `src/memfw/index/read_path.py` from the same revision
  (or with memfw installed); the script alone exits with an error naming the missing files, and
  a `read_path.py` older than `READ_PATH_VERSION = 2` is refused at startup. A clearance
  (`MEMFW_READONLY_CLEARANCE=public,shared`) needs the vault's `legacy_privacy_default` set
  first, or every memory without a `privacy` field is withheld
  ([`docs/v1-integration.md`](docs/v1-integration.md),
  [`docs/READONLY-SERVICE-UPGRADE.md`](docs/READONLY-SERVICE-UPGRADE.md));
- the Conductor orchestrator calls `memfw handoff … --format json` for automatic quota handoffs
  and runs its workers with the MCP server ([`docs/handoff.md`](docs/handoff.md));
- external memory readers that share a vault follow the reader-upgrade order above.

## 🧱 Architecture: layers and ports

memfw is split into layers that may only depend downward, and CI enforces it:

| Layer | What lives there |
|---|---|
| `L0-ports` | `memfw.ports`: port protocols, value types and the contract kit; standard library only |
| `L2` kernel | models, storage, the event log, policies, ontology, and the vault facade `Repository` |
| `L1` projections and read path | `index`, `notes`, `history`, `related`, `retrieval`, … (rebuilt from the vault) |
| `L3` capabilities | `recall`, `context`, `capture`, `conversations`, `dream`, `handoff`, `okf`, … |
| `L4-runtime` | `memfw.runtime`, the composition root: settings, recall, search and context wiring |
| `L5-interface` | the CLI and the MCP server, which only parse, call the Runtime and serialise |

Rows are in dependency order, lowest first. The L-numbers are kept from the design spec;
projections (L1) rank above the kernel (L2) because every projection is rebuilt from the vault
and imports the kernel.

- **Architecture guards.** `tests/test_architecture_layers.py` parses every module and fails the
  build on an upward import, an unlisted same-layer import, a cycle, a library reference to
  `scripts/` or an unclassified module. Violations that existed when the guard landed sit in a
  shrink-only allowlist: it went from 25 entries to 1.
- **Ports with swap drills.** `memfw.ports.PORT_VERSIONS` lists eleven ports
  (`PORTS_API_VERSION` 0.2.0). Ten (`RecallProvider`, `Projector`, `Ingestor`, `Deriver`,
  `Clock`, `IdGen`, `ClaimVerifier`, `ClaimStager`, `ContextSlotProvider`, `UsageSink`) each
  have at least two implementations that pass the same `memfw.ports.testing` check
  (`tests/test_swap_drills.py`); the eleventh, `ModelBackend`, is checked against the extractive
  and DeepSeek backends (`tests/test_adapter_deepseek.py`). Stdlib-only reference
  implementations live in `memfw.ports.reference`; the Obsidian projection implements
  `Projector`.
- **Recall coordinator.** `memory_search mode=two_stage` (opt-in) routes a query to recall
  providers, resolves every hit against the vault, filters it through a policy (`compat_fts`,
  or `strict` with privacy, status, lifecycle and archive checks), fuses and labels the result,
  with per-provider and total deadlines. Providers never write the vault; only returned hits
  are warmed and access-logged. The other modes are unchanged.
- **Standalone wheels.** `packages/memfw-ports` and `packages/memfw-recall` build the wheels
  `memfw_ports` and `memfw_recall` from memfw's own sources, so an adapter author can run the
  contract kit without installing memfw. CI builds them and smoke-tests them in a clean venv.
  They are **not published to PyPI**; build them locally with
  `uv build --wheel --package memfw-ports` (and `memfw-recall`). Both are version 0.2.0; the
  DeepSeek adapter wheel is 0.1.0.

Details: [`docs/architecture.md`](docs/architecture.md) and
[`docs/ports-and-recall.md`](docs/ports-and-recall.md).

## 📊 Evaluation summary

| Signal | Result |
|---|---|
| Corpus processed | 10 conversations · 5,882 turns · 1,986 questions (LoCoMo) |
| Questions with valid evidence | 1,982 |
| Retention policies compared | FIFO, LRU, decay, salience, consolidate-then-forget |
| Overall adjusted contrasts | **0 / 40** significant (4 scopes × 10 pairwise, Holm adjusted) |
| Retrieval ceiling (full-corpus BM25, 128-token) | **36.56%** evidence recall vs. **95.04%** query-conditioned oracle |
| Local Mem0 baseline (n=18) | memfw 66.7% vs. Mem0 62.5% evidence recall @100%; no significant difference |

These are **bounded negative results**, not quality claims: small samples, single-repeat runs, and
a logical retention projection rather than a physical-vault capacity test. Dream curate is scored
on synthetic duplicate, conflict and stale cases with `python -m eval.dreams`
([`eval/dreams/`](eval/dreams)). Full evidence and limits: [`docs/evaluation.md`](docs/evaluation.md).

## 📚 Docs map

| Document | Covers |
|---|---|
| [`docs/history.md`](docs/history.md) | transcript index: sources, formats, lineage, secret scan, refs |
| [`docs/conversations-share.md`](docs/conversations-share.md) | sharing rules, the worker, scheduling, enabling it in each app |
| [`docs/handoff.md`](docs/handoff.md) | handoff packs, size and privacy rules, the orchestrator contract |
| [`docs/dream.md`](docs/dream.md) | dream operations, matching, memory fields, skill candidates, limits |
| [`docs/okf.md`](docs/okf.md) | OKF v0.2 export and validation |
| [`docs/epistemic-and-experience.md`](docs/epistemic-and-experience.md) | epistemic type, causal grade, the write gate, and retrieval labels |
| [`docs/experience-points.md`](docs/experience-points.md) | experience points: draft schema, `from-github`, invalidation |
| [`docs/ontology.md`](docs/ontology.md) | concept taxonomy (`broader` / `narrower`) and the relation-type table |
| [`docs/cli-mcp-parity.md`](docs/cli-mcp-parity.md) | every capability × CLI / MCP / HTTP, with each gap marked by design or accidental |
| [`docs/obsidian-projection.md`](docs/obsidian-projection.md) | the read-only Obsidian board: layout, clearance, rebuild rules |
| [`docs/COMPONENTS.md`](docs/COMPONENTS.md) | notes index, code retrieval, read-only HTTP service, standalone wheels, DeepSeek adapter, corroboration, Obsidian projection, relatedness |
| [`docs/retrieval-architecture.md`](docs/retrieval-architecture.md) | retrieval design |
| [`docs/architecture.md`](docs/architecture.md) | package layers, import guards, the shrinking allowlist, the composition root, the byte-identical harness |
| [`docs/ports-and-recall.md`](docs/ports-and-recall.md) | port contracts, the recall coordinator, `two_stage`, context packs, slots and cache, standalone wheels, writing an adapter |
| [`docs/v1-integration.md`](docs/v1-integration.md), [`docs/READONLY-SERVICE-UPGRADE.md`](docs/READONLY-SERVICE-UPGRADE.md) | federation surface and service upgrades |
| [`docs/evaluation.md`](docs/evaluation.md), [`reports/`](reports), [`results/`](results) | evaluation evidence |
| [`docs/RELEASE-READINESS.md`](docs/RELEASE-READINESS.md) | measured checks and known limitations |
| [`docs/legacy-design-notes.md`](docs/legacy-design-notes.md) | earlier design notes (historical) |

## 📍 Status and boundaries

Release status: **v0.3.0** (2026-11-17). Apache-2.0; runs offline on one machine; not published
to PyPI. Implemented: the vault and hash-linked event log, retention and the `10MB` budget, the
disposable FTS5 index and the read path (per-term matching, concept aliases, multi-hop),
epistemic types, causal grades and reviewed experience points, the layered core with eleven
ports and the `memfw.runtime` composition root, `two_stage` recall, context packs v2 with
slots, resources and `if_version`, open questions, recall labels and corroboration, the concept
taxonomy and relation-type table, privacy levels with opt-in clearance, the MCP servers, the
notes index and code retrieval, the read-only transcript index, the conversation store with 14
readers, settled ingest, sharing and the worker (with an optional, off-by-default DeepSeek
tagging adapter), handoff packs, dream (verify, curate, distill, budget, skills, open-question
candidates), OKF v0.2 export and import, the Obsidian projection and the read-only HTTP service
(`memfw-http/2`). Changes are listed in [`CHANGELOG.md`](CHANGELOG.md).

**Upgrade boundary.** `vault.yaml` stays at `schema_version: 1`, and a file without the new
keys keeps its bytes through parse → render. New frontmatter (`epistemic`, `causal`,
`causal_record`, `limitations`, `experience`, `open_question`, `privacy`, a top-level
`sensitivity:`) is written only when set, and memfw 0.2.0 drops it on rewrite, so upgrade every
host that shares a vault in one go. The FTS5 index is a disposable projection and
is **not** migrated in place: the MCP server (`memfw-mcp`) and `memfw index search` rebuild a
stale index before they use it; the read-only HTTP service never writes, so deploy its three
files together and run `memfw index rebuild` once before pointing it at an upgraded vault.

**Boundaries.** Matching is lexical — a paraphrase with no shared word is missed; dream only
stages proposals (there is no `dream apply`: a person applies candidates through `update` /
`supersede`, and an open-question candidate through `dream accept-question`);
the notes, transcript and conversation indexes are derived and separate from the vault;
`two_stage` and context v2 are MCP-only (not in the HTTP service or the CLI); the
read-only HTTP service is authenticated by read/write tokens and refuses an unconfigured
write path; scheduling ships as a printed launchd plist; the evaluation is small. See
[`docs/dream.md`](docs/dream.md#limitations) and
[`docs/RELEASE-READINESS.md`](docs/RELEASE-READINESS.md).

## 🛠️ Development

`dev` installs the runtime, the test tools and `sqlite-vec` (there is no `test` extra):

```sh
uv sync --frozen --extra dev
.venv/bin/python -m pytest -q
.venv/bin/python -m mypy src
.venv/bin/python -m ruff check src/memfw tests
```

The suite includes the architecture guards (`tests/test_architecture_layers.py`), the swap
drills (`tests/test_swap_drills.py`) and the static checks of the standalone wheels
(`tests/test_standalone_ports_wheel.py`; set `MEMFW_STANDALONE_WHEELS=1` to also build, install
and smoke-test them, which needs `uv`).

Source layout under [`src/memfw`](src/memfw): `ports` (contracts), `core`, `storage`, `log`,
`ontology` (vault state, events, concepts), `index`, `retrieval`, `notes`, `code_retrieval`
(search), `projections` (the Obsidian board), `policies`, `budget`, `curation`, `capture`
(retention and review), `history` (transcript index, conversation store, readers), `recall`,
`context`, `conversations`, `handoff`, `dream`, `okf` (capabilities), `runtime` (composition
root), `mcp` and the CLI (interfaces). The standalone wheels and the DeepSeek adapter are built
from `packages/`.

## 🤝 Contributing

Issues and pull requests are welcome. Before opening a PR:

1. run `pytest -q`, `mypy src` and `ruff check src/memfw tests`;
2. do not include personal data, credentials, private notes, real transcripts or real memories;
3. keep evaluation claims bounded and link the report or artifact that supports them.

## 📄 License

Licensed under the [Apache License 2.0](LICENSE). The Traditional-to-Simplified character map in
`src/memfw/ontology/t2s.py` is derived from [OpenCC](https://github.com/BYVoid/OpenCC) (Apache-2.0);
see [`NOTICE`](NOTICE).

`memfw okf export` writes bundles in
[Open Knowledge Format v0.2](https://github.com/GoogleCloudPlatform/open-knowledge-format)
(GoogleCloudPlatform). That is a format specification only: memfw is compatible with it and
contains no OKF code, and `NOTICE` therefore carries no OKF entry. The evaluation table's Mem0
row is a comparison baseline only — memfw uses no Mem0 code.

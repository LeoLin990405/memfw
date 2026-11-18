<div align="center">

<img src="docs/assets/banner.svg" alt="memfw banner" width="100%">

# memfw

**A long-term memory for AI agents that you can read, diff and trust.**

Markdown is the source of truth · hash-linked JSONL events · rebuildable FTS5 · verify-first consolidation · one redacted conversation store for every agent · attachments as checked evidence · local MCP server · layered, swappable ports · optional adapters, all off by default.

[**简体中文**](README.zh-CN.md) · **English**

<a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue?style=flat-square" alt="License"></a>
<a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.11+"></a>
<a href="#-status-and-boundaries"><img src="https://img.shields.io/badge/release-v0.3.0-2563EB?style=flat-square" alt="Release v0.3.0"></a>
<a href="#-mcp-tools"><img src="https://img.shields.io/badge/MCP-15_tools_(%2B3_opt--in),_3_resources_(%2B1_opt--in)-8B5CF6?style=flat-square" alt="15 MCP tools (+3 opt-in), 3 resources (+1 opt-in)"></a>
<a href="#-conversation-store-every-agent-one-place"><img src="https://img.shields.io/badge/agent_readers-15-10B981?style=flat-square" alt="15 agent readers"></a>
<a href="#-architecture-layers-and-ports"><img src="https://img.shields.io/badge/ports-12-0EA5E9?style=flat-square" alt="12 ports"></a>
<a href="#-optional-adapters"><img src="https://img.shields.io/badge/adapters-9_(off_by_default)-64748B?style=flat-square" alt="9 adapters (off by default)"></a>
<a href="#-evaluation-summary"><img src="https://img.shields.io/badge/evaluation-bounded-D97706?style=flat-square" alt="Bounded evaluation"></a>

</div>

## What is memfw?

memfw is a local memory engine for AI agents. It does three jobs:

1. **Keeps memories as files.** Every memory is a plain Markdown file in a vault folder, every
   change is appended to a hash-linked event log, and search runs on an index you can rebuild at
   any time.
2. **Collects what agents said.** It reads the session stores of 15 agent CLIs and apps into one
   local, redacted conversation store, so any agent can search, read and continue another
   agent's work.
3. **Turns that into better memory, carefully.** "Dream" passes check memories against the
   transcripts and propose confirmations, corrections, merges, lessons and skill drafts. They
   only ever stage candidates; a person decides what goes into the vault.

Memories can also cite **attachments** (PDFs, images, audio, video): the originals stay outside
the vault, the vault keeps a small record, and a write gate makes sure a screenshot or a
recording never counts as more than an observation (see [Attachments](#-attachments-evidence-beyond-text)).

Agents use it through a local **MCP server** (`memfw-mcp`); people use the `memfw` CLI. Inside,
memfw is a small kernel behind port contracts: search backends, projectors and ingestors plug in
through a port, never into the vault (see [Architecture](#-architecture-layers-and-ports)).
Other engines (bm25s, Qdrant, QMD, PageIndex, OCR, LangExtract, LightRAG, GraphRAG) are separate adapter packages
that memfw never imports and that stay off until you configure them (see
[Optional adapters](#-optional-adapters)).

```
agent CLIs / apps ──readers──► conversation store (local, redacted)
        │                              │
        │ MCP                          ├─► conversations worker ─► summaries, episodes
        ▼                              ├─► handoff packs (continue in another agent)
     memfw-mcp ──► vault ◄── human ◄───┴─► dream: verify · curate · distill · skills
                    ├─ memories/*.md      (source of truth)          (candidates only)
                    ├─ attachments/*.md   (records; originals live outside the vault)
                    ├─ events.jsonl       (hash-linked audit log)
                    └─ index.sqlite3      (FTS5; rebuildable)
```

## 🧭 Design principles

1. **Local-first.** The vault, the indexes and the conversation store live on your machine. The
   core calls no model and needs no network; anything that could reach a cloud model is off by
   default and needs an explicit `--allow-cloud` (or `cloud: true` in an adapter's settings).
   Every adapter is off by default, and no bundled adapter sends a `local-private` memory or
   attachment to a non-local service.
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
| `vault.yaml` | vault settings: layers, tiers, budgets, `permissions: propose_then_confirm`, history sources, sharing rules, attachment settings (`attachments_*`, and `vault_id`, added by the first attach) |
| `memories/<id>.md` | **the memories**: Markdown with YAML frontmatter (layer, tier, `about`, `related`, `valid_from` / `valid_to`, optional `status`, `evidence` and `privacy`) |
| `events.jsonl`, `events.state.json`, `.log/segments/` | the hash-linked event log and its archived segments |
| `.history/<id>/` | retained earlier versions (for `rollback`) |
| `.proposals/` | agent proposals bound by hash to their exact payload |
| `.archives/` | deterministic `tar.zst` cold archives (cold tier) |
| `_candidates/remember/`, `_candidates/dream/`, `_candidates/skills/` | staged candidates waiting for a person (`_candidates/dream-rejections/` records rejected open-question candidates) |
| `_candidates/forget/` | deletion requests from `memfw-http/3` `POST /forget`, waiting for a person (nothing is deleted) |
| `attachments/<att-id>.md` | one small record per registered attachment (never under `memories/`); the original is a blob outside the vault or a referenced file |
| `attachments/proxies/<prx-id>.md` | text proxies of an attachment (OCR, transcript, keyframes, extract, caption) with canonical locators |
| `index.sqlite3`, `.memfw-history.sqlite` | derived FTS5 indexes (disposable; rebuild any time) |

The conversation store, the worker state, attachment blobs (`blobs/`) and the rebuildable
projections (`edges/`, `corroboration/`, `proxy_index/`, the bm25s adapter's index under `adapters/`) are kept **outside**
the vault, by default under `~/.local/share/memfw/` (see [Privacy](#-privacy-and-permissions)).
Each out-of-vault projection records which vault it belongs to and refuses to answer for another
vault at the same path ("projection belongs to another vault: rebuild it"). If the vault is a
git repository, add `index.sqlite3` and `.memfw-history.sqlite*` to its `.gitignore`.

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
| Epistemic and experience | `add` / `update --epistemic --causal --causal-record` (one write gate; `causal: verified` needs a human), `experience draft` / `from-github` / `invalidate` (reviewed experience points, staged as candidates), `eval causal [--multimodal]` |
| Attachments | `attach add FILE [--embed \| --reference]`, `attach list` / `show` / `verify`, `attach detach` / `relink` / `purge` / `reown` / `scan`; `attach proxy import` / `list` / `show` / `budget` (see [Attachments](#-attachments-evidence-beyond-text)) |
| Typed-edge graph | `graph rebuild`, `graph match --from X --path "about>broader*1..2><about" [--acyclic]` |
| Privacy | `add --privacy LEVEL`, `update --privacy LEVEL`, `privacy legacy-default [LEVEL]`, `privacy new-default [LEVEL]` (levels `public`, `shared`, `local-private`) |
| Review gate | `candidates`, `candidate-apply [--apply]`, `candidates-expire` (30-day TTL, never deletes), `record-proposal`, `approve-proposal`, `review` |
| Retention | `budget`, `forget` (`fifo`, `lru`, `decay`, `salience`, `consolidate_then_forget`), `forget requests` (deletion requests staged over HTTP), `forget residue ID` (read-only: is a forgotten memory gone from every projection?), `tiers` / `tier` (hot and cold), `cold-archive`, `cold-restore`, `layer` / `layer-set`, `consolidate` |
| Related memories | `memory-related-build`, `memory-related` |
| Projections | `okf export` / `okf validate` / `okf import` ([OKF v0.2](https://github.com/GoogleCloudPlatform/open-knowledge-format) bundles), `projection export` (files for an external recall backend such as QMD), `project obsidian` (a read-only Obsidian board), `notes` (a Markdown notes index), `code` (local code retrieval) |
| Evaluation | `eval context SNAPSHOT` (context-pack coverage, waste, latency and token cost on a read-only snapshot), `eval causal` |

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
- **Corroboration.** `memfw corroboration rebuild` writes a rebuildable projection
  (outside the vault, under `~/.local/share/memfw/corroboration/`): per memory, the independent (app, session) pairs that mention it.
  Ids, names, counts and times only, trimmed to what the asking app may read. With `labels: true`,
  MCP `memory_search` shows it on every row; `memfw corroboration queue` lists unresolved
  questions with corroborated conflicts first.
- **Concept taxonomy.** Concept files may name `broader` parents; `narrower` is derived, never
  written, and nothing on the read path follows it (no query expansion). The relation-type
  table lists every typed relation memfw already stores. See [`docs/ontology.md`](docs/ontology.md).
- **Typed edges and path patterns.** `memfw graph rebuild` builds a rebuildable edge projection
  outside the vault from the relation table's fields (`about`, `applies_to`, `broader`,
  `superseded_by`, evidence, causal and experience links, `related`, concept relations and the
  open-question `conflicts` / `resolved_by`); each edge carries both ends' tenant and privacy,
  and cross-tenant edges are never built. `memfw graph match --from X --path PATTERN` follows a
  bounded path (at most 4 hops, capped fan-out and results, deterministic order), filtered by
  tenant, clearance (default `public,shared`) and sources, with every memory re-checked against
  the live vault: a hidden node never shows, and a hidden start answers like a missing one.
  The projection sits behind the new `ProjectionStore` port. There is no MCP tool for it.

Details: [`docs/epistemic-and-experience.md`](docs/epistemic-and-experience.md).

## 📎 Attachments: evidence beyond text

A memory can cite an original (a PDF, an image, a recording, a video) as `att:<id>[#locator]`.
memfw keeps the original out of the vault and treats it as an **observation**, never as proof.

- **Two ways to keep an original.** `memfw attach add FILE --embed` stores the bytes once per
  sha256 as a blob outside the vault (default `~/.local/share/memfw/blobs`, directories `0700`,
  files `0600`; git work trees, the vault, synced folders, `~/Documents` and `~/Desktop` are
  refused). `--reference` (or `ROOT:PATH`) records only `<root>:<relative path>` plus sha256 and
  size, under roots named per host in `vault.yaml` (`attachments_roots`); nothing is copied.
  Audio and video inside a root default to a reference. Limits: `attachments_max_bytes`
  (25 MiB) and `attachments_max_total_bytes` (2 GiB) for embedded originals.
- **Records and privacy.** The vault keeps `attachments/<att-id>.md` with a `privacy` level
  (default `local-private`; tags and `sensitivity` only tighten it). Referenced paths are opened
  from the root with `O_NOFOLLOW`, component by component, so a symlink or a `..` never escapes.
- **Lifecycle.** `attach verify` re-hashes (`referenced_missing` comes with same-hash candidates,
  never relinked automatically), `attach relink ID PATH` accepts only the same sha256 and size,
  `attach detach` is soft, `attach purge ID --yes` is the hard delete (without `--yes` it prints
  the plan, including the memories that cite it), `attach scan ROOT:[DIR]` is a dry-run
  inventory, and `attach reown` splits off or re-owns the blob directory of a copied vault.
  A purge also deletes the attachment's proxy files and drops it from the proxy index.
  `memfw recover` finishes an interrupted purge or relink.
- **Text proxies.** `memfw attach proxy import ATT-ID FILE` stores an OCR text, transcript,
  keyframe list, extract or caption in the versioned import format `memfw.proxy` v1
  ([`docs/attachment-proxies.md`](docs/attachment-proxies.md)). `source_sha256` must equal the
  attachment's hash; locators are stored in canonical units (PDF points with a bottom-left
  origin, image pixels with a top-left origin, seconds for audio and video), and anything outside
  the declared page, box or time range is refused. A proxy is never less private than its
  attachment and has its own budget (`attachments_proxy_max_bytes`, 64 MiB). The optional
  [OCR adapter](#-optional-adapters) produces such files from a local OCR engine.
- **Observation evidence gate.** A memory whose evidence is only observations (an `att:`
  pointer, or a `file:` / `sha256:` pointer that turns out to name a registered attachment in
  any spelling) cannot be `epistemic: fact` and stops at `causal: hypothesis`. A new `att:`
  pointer must resolve and its locator must fit the media type. `memfw eval causal --multimodal`
  runs 59 synthetic cases, 37 of them bypass attempts; every bypass is blocked.
- **Search.** Active proxies get their own rebuildable index outside the vault
  (`~/.local/share/memfw/proxy_index/`), filtered by tenant and by the stricter privacy of
  proxy and attachment; memory search, HTTP, OKF and projections never read it. The built-in
  `ProxyProvider` returns `attachment_segment` refs (locator, never text) as
  `RecallResult.segments`, never as memory hits. The MCP tools `attachment_search` /
  `attachment_get` appear only with `vault.yaml` `mcp.attachments: true`; every result is
  re-checked against the proxy file, the attachment record and `MEMFW_MCP_CLEARANCE`, and an
  original is returned only with `mcp.local_originals: true`, after re-hashing the blob. The
  HTTP service never serves attachments.

A memfw 0.3.0 reader fails closed on a vault with an `attach` event, so upgrade every host before
the first `attach add` (see Reader-upgrade order below). Details:
[`docs/attachments.md`](docs/attachments.md).

## 💬 Conversation store: every agent, one place

`memfw history ingest` reads each agent's own session files and writes normalised turns into one
local store (`~/.local/share/memfw/conversations.sqlite`). It is incremental: unchanged sessions
are skipped, appended ones are read from the last stored turn. Before a turn is written, every
text field is scanned for secrets; a hit withholds the whole turn, everything else is redacted.
Raw lines are never copied, and memfw never writes to an agent's own storage.
`--settled-minutes N` skips sessions active in the last `N` minutes and marks the settled ones
archived (`memfw conversations list --with-archive-state` / `--archived-only`).

**15 registered readers** (`memfw history agents` lists them and which stores exist on this
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
| `qwen` | Qwen Code CLI | `~/.qwen/projects` |

Not readable: TRAE SOLO keeps its agent conversations in a database file that is not plain SQLite (it appears encrypted), so it has no reader.

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

**Two-stage search and tag filters.** `conversations search` (and MCP `conversations_search`)
filters on the worker's `conv-v2` tags with `--task-type`, `--outcome`, `--project`,
`--component` and `--intent-like`; an untagged session never matches a filter. `--mode
two_stage` fuses, by reciprocal rank, the keyword search, a semantic match over "summary + tags"
(the runtime's embedder; only a loopback one unless `conversations.cloud: true`, else it is
reported `semantic_off`) and the tag filters, each list cut to the caller's visible apps first.
Each session comes back with its summary and tags labelled `untrusted_model_output: true` and
redacted turn windows around the hits, all within `max_bytes`. Without these options the output
is unchanged.

```sh
memfw conversations search "flaky test" --mode two_stage --task-type debug --outcome done
```

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
vault itself. It lists 15 tools; three more appear only when switched on: `memory_used`, and
`attachment_search` / `attachment_get`.

| Tool | Purpose |
|---|---|
| `memory_search` | search the vault; `mode` = `hybrid` (default), `fts`, `vector`, `multihop` or the opt-in `two_stage` (recall coordinator plus any configured [recall adapters](#-optional-adapters), `policy` `compat_fts` or `strict`; resolved note sections come back as a separate `notes` list); `source`, `include_history`, `as_of`, `labels` (open-question labels and corroboration) |
| `memory_get` | read one memory by id |
| `memory_remember` | stage a candidate under `_candidates/remember/` with tags, related ids, suggestions and an optional `privacy` level |
| `memory_related` | derived related-memory edges (tenant-scoped) |
| `memory_context` | token-budgeted resident context: anchors and procedural memories first; `v=2` returns a versioned `ContextPack` with slots `core`, `handoff` and `topics` (plus the opt-in `current` slot), overflow, an optional `strict` policy and `if_version` (reply `unchanged: true` when nothing changed) |
| `memory_status` | vault size, layer and tier counts, log summary |
| `history_list`, `history_search`, `history_read` | the transcript index: sessions with lineage, redacted snippets with refs, capped windows |
| `conversations_list`, `conversations_search`, `conversations_get` | the shared conversation store, filtered by the calling app's visibility; `conversations_search` takes the tag filters and `mode: two_stage` |
| `conversations_handoff` | a lighter, policy-scoped continuation view for agent apps |
| `dream_run` | one dream; a dry run by default, deterministic rules only, at most 50 staged dreams per tenant and one per minute |
| `dream_list` | this tenant's staged dreams, newest first |
| `memory_used` (opt-in) | record which memories a session used; listed only with `runtime: {usage: {enabled: true}}` or `MEMFW_USAGE_SIGNAL=1`; writes a store outside the vault, never the vault |
| `attachment_search`, `attachment_get` (opt-in) | search attachment text proxies; read one attachment's metadata and proxy segments (optionally within a locator); listed only with `mcp.attachments: true`; originals only with `mcp.local_originals: true` |

It also serves three **MCP resources**, `memfw://context/core`, `memfw://context/handoff` and
`memfw://context/topics` (`?policy=` and `?if_version=` accepted): each is that slot's v=2
pack for the calling app. A slot that fails or times out comes back empty with a `degraded`
reason; the others are not affected. `pack_version` is computed from ids, versions and hashes
only, never body text. Details: [`docs/ports-and-recall.md`](docs/ports-and-recall.md) §4.

A fourth resource, `memfw://context/current`, and the `current` slot of `memory_context` appear
only with `vault.yaml` `runtime.context.current: true`. The slot holds short-lived context (an
active ticket, a meeting, the current task): memories tagged `current` that are valid now
(`valid_from` / `valid_to`, else a TTL of `runtime.context.current_ttl_days`, default 7, from
`created_at`), not superseded, newest first. It is never cached, so an expired memory is never
served. Switched off, `tools/list`, `resources/list` and every pack are byte-identical.

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
- **Adapters keep data on this machine by default.** Every adapter is off until configured,
  and memfw never imports one. By default each one talks only to in-process code or loopback
  services, and no bundled adapter sends a `local-private` memory or attachment to a non-local
  service: whatever may go to another machine (Qdrant `sync`, LangExtract, LightRAG, PageIndex
  trees for a non-local model, GraphRAG's input documents when `cloud: true` points its local
  `graphrag index` run at a non-loopback model) is limited to `public` / `shared`. Local indexes are not
  limited that way: QMD indexes a `memfw projection export`, which can include `local-private`
  memories unless `--skip-privacy` (or the projection policy) leaves them out, so that index stays on this machine (the QMD
  adapter never returns `local-private` hits). The OCR adapter's custom `module:factory`
  engine is outside its local-only guarantee. A non-local model needs `cloud: true` in the
  adapter's settings, a non-loopback service endpoint needs `allow_remote: true`, loopback
  requests never go through `HTTP(S)_PROXY`, and redirects are refused.
- **Attachments are private by default.** Records default to `local-private`, originals live
  outside the vault in private directories, a proxy can never be less private than its
  attachment, HTTP, OKF and projections never serve attachments, and the MCP tools exist only
  with `mcp.attachments: true`.
- **Forgetting is a request, then a check.** `memfw-http/3` `POST /forget` only stages a
  request for a person; hidden and missing targets answer the same 404, also on replay after
  deletion. `memfw forget residue ID` checks, read-only, that a forgotten memory is gone from
  the FTS index and the related, corroboration, usage and (optionally) entity projections, and names the
  rebuild for each store that still has it.
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

**From 0.3.0 to the next release (planned v0.4.0, on `main` now):**

- **Attachments are one-way.** memfw 0.3.0 fails closed on a vault with an `attach` event (every
  command that reads the event log), so upgrade every host that reads the vault before the first
  `memfw attach add`; there is no downgrade to 0.3.0 after it.
- **Event log forward compatibility.** A 0.4.x reader accepts an event kind it does not know only
  when the event carries `details.projection: "none"` (a newer writer's promise that it never
  changes memory state). Such events are kept byte-for-byte, skipped by replay, counted by
  `log verify`, reported by `fsck` and refused by `rollback`; an unmarked unknown kind is still
  refused as in 0.3.0.
- **Rebuild two projections.** The edge projection moves to schema 2 and the corroboration
  projection moves out of the vault. Run `memfw graph rebuild` and `memfw corroboration rebuild`
  on every vault that uses them; an old vault-root `corroboration.sqlite3` is ignored, never
  deleted, and still counts toward the vault's size until you delete it.
- **Stricter evidence.** A new `att:` pointer that does not resolve is refused (0.3.0 stored it
  as an opaque string).
- **HTTP.** Without a `MemFW-Contract: memfw-http/3` header every `memfw-http/2` answer is
  byte-identical.

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
- the same service serves **`memfw-http/3`** beside v2, chosen per request by the
  `MemFW-Contract` header (`/health` lists both in `MemFW-Contracts`). v3 adds `recorded_at` to
  `/search` hits, `why` and `provenance_type` to `/related`, `POST /context` (the
  `memory_context` v2 `core` pack, built read-only; needs the `memfw` package and a configured
  `MEMFW_READONLY_CLEARANCE`) and `POST /forget` (write token only: stages one deletion request
  under `_candidates/forget/`, idempotent per tenant by `request_id`, at most 1000 waiting per
  tenant);
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
- **Ports with swap drills.** `memfw.ports.PORT_VERSIONS` lists twelve ports
  (`PORTS_API_VERSION` 0.3.0). Eleven (`RecallProvider`, `Projector`, `Ingestor`, `Deriver`,
  `Clock`, `IdGen`, `ClaimVerifier`, `ClaimStager`, `ContextSlotProvider`, `UsageSink`,
  `ProjectionStore`) each have at least two implementations that pass the same
  `memfw.ports.testing` check (`tests/test_swap_drills.py`); the twelfth, `ModelBackend`, is
  checked against the extractive and DeepSeek backends (`tests/test_adapter_deepseek.py`).
  Stdlib-only reference implementations live in `memfw.ports.reference`; the Obsidian
  projection implements `Projector`, and the edge projection implements `ProjectionStore`
  (SQLite, with an in-memory reference). `memfw.ports.proxies` defines the attachment proxy
  format and its locators, with `check_proxy_import` for adapters.
- **Recall coordinator.** `memory_search mode=two_stage` (opt-in) routes a query to recall
  providers, resolves every hit against the vault, filters it through a policy (`compat_fts`,
  or `strict` with privacy, status, lifecycle and archive checks), fuses and labels the result,
  with per-provider and total deadlines. Providers never write the vault; only returned hits
  are warmed and access-logged. The other modes are unchanged. Under `strict`, documents that
  carry a `fleet_path` tag are joined to the lifecycle meta-index
  (`status/memory-meta-index.json`), so a retired, superseded or expired entry is dropped (a
  failed join drops it too, reason `unjoined`). `kh:<path>#<heading>` refs from QMD notes or
  PageIndex can resolve to sections of the built-in notes index through `NotesSegmentResolver`
  (not registered by default), with the `notes_search` privacy rules.
- **Recall adapters from configuration.** `vault.yaml` `runtime.recall.adapters` lists
  `{factory: "module:factory", options, weight, timeout_seconds}` entries (at most 8) that
  `two_stage` runs beside FTS. The module name comes only from this file, never from a request.
  A bad entry fails `two_stage` closed with a fixed `RecallAdapterError` code; an adapter that
  cannot serve is reported degraded while the others answer. Without the key nothing is
  imported. See [`docs/ports-and-recall.md`](docs/ports-and-recall.md) §4.1b.
- **Standalone wheels.** `packages/memfw-ports` and `packages/memfw-recall` build the wheels
  `memfw_ports` and `memfw_recall` from memfw's own sources, so an adapter author can run the
  contract kit without installing memfw. CI builds them and smoke-tests them in a clean venv.
  They are **not published to PyPI**; build them locally with
  `uv build --wheel --package memfw-ports` (and `memfw-recall`). Both are version 0.3.0; the
  adapter wheels are listed under [Optional adapters](#-optional-adapters).

Details: [`docs/architecture.md`](docs/architecture.md) and
[`docs/ports-and-recall.md`](docs/ports-and-recall.md).

## 🧩 Optional adapters

Adapters are separate packages under `packages/adapters/`, each built as its own wheel
(`uv build --wheel --package NAME`) and **not published to PyPI**. memfw never imports them.
Each depends on the `memfw-ports` wheel plus at most a few pinned libraries, and pins the
upstream release its contract tests were recorded against; CI never needs the upstream
service. **Every adapter is off by default:** a recall provider runs only when it is listed
under `runtime.recall.adapters`; a deriver or a proxy producer runs only when an operator calls it, and its output reaches the vault
only as staged candidates or an explicit `attach proxy import`.

| Adapter (wheel version) | Port | What it does | Status |
|---|---|---|---|
| `memfw-adapter-deepseek` (0.1.1) | `ModelBackend` | `conv-v2` summary and tags for the conversations worker; needs `conversations.cloud: true` and `DEEPSEEK_API_KEY` | merged |
| `memfw-adapter-bm25s` (0.1.1) | `RecallProvider` | in-process bm25s 0.3.16 ranking with jieba; indexes only its `clearance` (default `public,shared`), hashed tokens outside the vault; `memfw-adapter-bm25s build` | merged |
| `memfw-adapter-qdrant` (0.1.2) | `RecallProvider` | semantic recall over a Qdrant v1.20.x service (REST, stdlib); loopback unless `allow_remote: true`; `memfw-adapter-qdrant sync` sends only `public` / `shared` memories and stores no text | merged |
| `memfw-adapter-qmd` (0.1.0) | `RecallProvider` ×2 | `qmd` (memory projections → memory ids) and `qmd-notes` (notes → `kh:` refs) over a running QMD 2.5.3 MCP server; never returns `local-private` or `unknown` | merged |
| `memfw-adapter-pageindex` (0.1.0) | `RecallProvider` | PageIndex-style tree search over long documents (`kh:` and `att:…#page=` refs); `mode: titles` needs no model; `memfw-pageindex build-md \| import-tree \| list` | merged |
| `memfw-adapter-ocr` (0.1.0) | proxy producer (`ocr` deriver) | turns a PDF or image attachment into a `memfw.proxy` v1 import file through a local engine (an MCP stdio OCR server, `pdftotext`, or a `module:factory`); nothing uploaded; `memfw-ocr-proxy run \| check` | merged |
| `memfw-adapter-langextract` (0.1.0) | `Deriver` (`entities`) | character-grounded entities and relations from `public` / `shared` memories with LangExtract 1.7.2; a memfw `ModelBackend` (a loopback Ollama backend ships with it) inside a per-run token budget | merged |
| `memfw-adapter-lightrag` (0.1.0) | `Deriver` (`graph`, `answer`) | loopback HTTP client of an operator-run `lightrag-server` 1.5.7; sends only `public` / `shared` memories; every kept row's sources must be requested, shareable, current memories | merged |
| `memfw-adapter-graphrag` (0.1.0) | `Deriver` (`graph`, `reports`) | GraphRAG 3.3.0 as an offline batch over a `memfw projection export` generation (`public` / `shared` only); `graphrag index` runs only when called, as a local subprocess against loopback model endpoints unless `cloud: true` | merged |

Deriver outputs are capped at `epistemic: inference` and become memory only through review. A recall
adapter that is down or slow is reported degraded and FTS still answers. Per-adapter details:
[`docs/COMPONENTS.md`](docs/COMPONENTS.md), [`docs/adapter-bm25s.md`](docs/adapter-bm25s.md),
[`docs/adapter-qdrant.md`](docs/adapter-qdrant.md),
[`docs/adapter-langextract.md`](docs/adapter-langextract.md),
[`docs/adapter-lightrag.md`](docs/adapter-lightrag.md),
[`docs/adapter-graphrag.md`](docs/adapter-graphrag.md),
[`docs/qmd-projection.md`](docs/qmd-projection.md) and each package's `README.md`.

## 📊 Evaluation summary

| Signal | Result |
|---|---|
| Corpus processed | 10 conversations · 5,882 turns · 1,986 questions (LoCoMo) |
| Questions with valid evidence | 1,982 |
| Retention policies compared | FIFO, LRU, decay, salience, consolidate-then-forget |
| Overall adjusted contrasts | **0 / 40** significant (4 scopes × 10 pairwise, Holm adjusted) |
| Retrieval ceiling (full-corpus BM25, 128-token) | **36.56%** evidence recall vs. **95.04%** query-conditioned oracle |
| Local Mem0 baseline (n=18) | memfw 66.7% vs. Mem0 62.5% evidence recall @100%; no significant difference |
| Multimodal causal gate (`eval causal --multimodal`, synthetic) | 59 cases: **0** spurious promotions, **0** fact upgrades, **37 / 37** bypass attempts blocked |

These are **bounded negative results**, not quality claims: small samples, single-repeat runs, and
a logical retention projection rather than a physical-vault capacity test. Dream curate is scored
on synthetic duplicate, conflict and stale cases with `python -m eval.dreams`
([`eval/dreams/`](eval/dreams)). `memfw eval context SNAPSHOT` measures the context pack (coverage,
waste, P50/P95 latency and token cost per slot, swept over slot and resident-prefix caps) on a
private copy of a read-only vault snapshot; it reports counts, ratios and timings only and
changes no default. Full evidence and limits: [`docs/evaluation.md`](docs/evaluation.md).

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
| [`docs/ontology.md`](docs/ontology.md) | concept taxonomy (`broader` / `narrower`), the relation-type table, the typed-edge projection and path patterns |
| [`docs/attachments.md`](docs/attachments.md) | attachments: blob store, records, referenced originals, path safety, purge, scan, events and forward compatibility |
| [`docs/attachment-proxies.md`](docs/attachment-proxies.md) | the `memfw.proxy` v1 import format, canonical locators, proxy storage and budget |
| [`docs/cli-mcp-parity.md`](docs/cli-mcp-parity.md) | every capability × CLI / MCP / HTTP, with each gap marked by design or accidental |
| [`docs/obsidian-projection.md`](docs/obsidian-projection.md) | the read-only Obsidian board: layout, clearance, rebuild rules |
| [`docs/COMPONENTS.md`](docs/COMPONENTS.md) | notes index, code retrieval, read-only HTTP service, standalone wheels, the adapters, corroboration, Obsidian projection, relatedness |
| [`docs/adapter-bm25s.md`](docs/adapter-bm25s.md), [`docs/adapter-qdrant.md`](docs/adapter-qdrant.md), [`docs/adapter-langextract.md`](docs/adapter-langextract.md), [`docs/adapter-lightrag.md`](docs/adapter-lightrag.md), [`docs/adapter-graphrag.md`](docs/adapter-graphrag.md), [`docs/qmd-projection.md`](docs/qmd-projection.md) | adapter setup, privacy rules and contract tests (OCR and PageIndex: their package READMEs) |
| [`docs/adapter-lightrag.md`](docs/adapter-lightrag.md) | LightRAG deriver adapter |
| [`docs/adapter-graphrag.md`](docs/adapter-graphrag.md) | GraphRAG deriver adapter |
| [`docs/retrieval-architecture.md`](docs/retrieval-architecture.md) | retrieval design |
| [`docs/architecture.md`](docs/architecture.md) | package layers, import guards, the shrinking allowlist, the composition root, the byte-identical harness |
| [`docs/ports-and-recall.md`](docs/ports-and-recall.md) | port contracts, the recall coordinator, `two_stage`, configured recall adapters, context packs, slots (including `current`) and cache, standalone wheels, writing an adapter |
| [`docs/v1-integration.md`](docs/v1-integration.md), [`docs/READONLY-SERVICE-UPGRADE.md`](docs/READONLY-SERVICE-UPGRADE.md) | federation surface (`memfw-http/2` and `/3`, `/context`, `/forget`) and service upgrades |
| [`docs/evaluation.md`](docs/evaluation.md), [`reports/`](reports), [`results/`](results) | evaluation evidence, the multimodal causal corpus and the C4 context pack evaluation |
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
notes index and code retrieval, the read-only transcript index, the conversation store with 15
readers, settled ingest, sharing and the worker (with an optional, off-by-default DeepSeek
tagging adapter), handoff packs, dream (verify, curate, distill, budget, skills, open-question
candidates), OKF v0.2 export and import, the Obsidian projection and the read-only HTTP service
(`memfw-http/2`). Changes are listed in [`CHANGELOG.md`](CHANGELOG.md).

**On `main`, not yet released (planned for v0.4.0):** the opt-in `current` context slot,
two-stage conversation search with tag filters, attachments (embedded and referenced originals,
detach, relink, purge, scan, text proxies in the `memfw.proxy` v1 format, attachments as
observation evidence in the write gate, proxy search with the opt-in MCP tools
`attachment_search` / `attachment_get`), the typed-edge projection and bounded path patterns
behind the `ProjectionStore` port, the C4 context pack evaluation, `memfw-http/3` with
`/context`, `/forget` requests and `forget residue`, event log forward compatibility, vault
identity for out-of-vault projections (corroboration moved out of the vault), the lifecycle
meta-index and `kh:` note resolver in recall, `runtime.recall.adapters`, the Qwen Code reader
and eight new adapters beside the DeepSeek one (bm25s, Qdrant, QMD, PageIndex, OCR, LangExtract,
LightRAG, GraphRAG), all off by default.

**Upgrade boundary.** `vault.yaml` stays at `schema_version: 1`, and a file without the new
keys keeps its bytes through parse → render. New frontmatter (`epistemic`, `causal`,
`causal_record`, `limitations`, `experience`, `open_question`, `privacy`, a top-level
`sensitivity:`) is written only when set, and memfw 0.2.0 drops it on rewrite, so upgrade every
host that shares a vault in one go. The FTS5 index is a disposable projection and
is **not** migrated in place: the MCP server (`memfw-mcp`) and `memfw index search` rebuild a
stale index before they use it; the read-only HTTP service never writes, so deploy its three
files together and run `memfw index rebuild` once before pointing it at an upgraded vault.

**Boundaries.** Built-in matching is lexical (semantic recall needs an embedder or an adapter) — a paraphrase with no shared word is missed; dream only
stages proposals (there is no `dream apply`: a person applies candidates through `update` /
`supersede`, and an open-question candidate through `dream accept-question`);
the notes, transcript and conversation indexes are derived and separate from the vault;
`two_stage` memory recall and context v2 are MCP-only, except the v2 `core` pack that
`memfw-http/3` `POST /context` serves; attachments are reachable from the CLI and, with
`mcp.attachments: true`, the MCP tools (the HTTP service never serves them); the typed-edge graph has no MCP tool; the read-only HTTP service is
authenticated by read/write tokens and refuses an unconfigured write path; scheduling ships as a printed launchd plist; the evaluation is small. See
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
`ontology`, `attachments` (vault state, events, concepts, attachment records and blobs), `index`,
`retrieval`, `notes`, `code_retrieval` (search), `projections` (the Obsidian board), `edges`
(the typed-edge projection), `corroboration`, `policies`, `budget`, `curation`, `capture`
(retention and review), `history` (transcript index, conversation store, readers), `recall`,
`context`, `conversations`, `handoff`, `dream`, `okf` (capabilities), `runtime` (composition
root), `mcp` and the CLI (interfaces). The standalone wheels and the adapters are built from
`packages/`.

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

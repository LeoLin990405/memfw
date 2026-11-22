# CLI / MCP / HTTP parity

Which capability is reachable from which surface: the `memfw` CLI (Typer
app in `src/memfw/cli.py` and the sub-apps it mounts), the memfw MCP server
(`TOOLS`, `MEMORY_USED_TOOL` and `RESOURCES` in `src/memfw/mcp/schema.py`),
the notes MCP server (`memfw notes serve`, `src/memfw/notes/mcp.py`) and the
read-only HTTP service (`scripts/memfw_readonly_service.py`: `GET /health`,
`POST /search`, `POST /related`, `POST /stage`).

Measured from code, not from older docs. The roles differ on purpose:

* **CLI** is the human operator's surface. It is the only one that writes the
  authoritative vault directly, and the only place for human decisions
  (approvals, candidate apply, open-question create/resolve, privacy
  settings) and operations (fsck, recover, index, budget, tiers, cold
  archive, workers).
* **MCP** is the agent surface: read tools plus proposal-only writes
  (`memory_remember` stages a candidate, `dream_run` stages candidates). It
  never applies a human-only decision.
* **HTTP** is a narrow, read-only service for other machines: search,
  related, a health probe and candidate staging.

A gap is marked **by design** when the missing surface must not have the
capability (human-only decisions stay off MCP/HTTP; agent-only response shapes
stay off the CLI), and **accidental** when nothing argues against it. An
accidental gap is either **filled** in this change or **open** (listed at the
end with why it was not filled here).

## Memories

| Capability | CLI | MCP | HTTP | Gap |
|---|---|---|---|---|
| Create a memory | `add` | — (`memory_remember` stages a candidate) | — (`/stage` stages a candidate) | by design: direct writes are human |
| Read one memory | `show` (Markdown; `--json` new) | `memory_get` | — | HTTP: by design (search/related only) |
| List memories | `list` (TSV; `--json` new) | — | — | MCP: by design (agents search, they don't enumerate) |
| Update fields | `update` | — | — | by design |
| Delete (hard, tombstone) | `delete` | — | — | by design |
| Rollback to a prior version | `rollback` | — | — | by design |
| Supersede | `supersede` | — | — | by design |
| Move layer | `layer-set`, `layer set` | — | — | by design |
| Vault status / counts | `stats`, `tiers status`, `log verify` | `memory_status` | `/health` (liveness only) | none: same data, different shape |
| Related memories | `memory-related` (derived edge DB) | `memory_related` | `/related` | none |
| Stage a candidate | — | `memory_remember` | `/stage` | CLI: by design (a human uses `add`) |

## Open questions (U1)

| Capability | CLI | MCP | HTTP | Gap |
|---|---|---|---|---|
| Create `type: open-question` | `add --type open-question --question … [--conflicts …] [--missing …] [--tenant …]` | — | — | **filled** (was accidental); MCP/HTTP: by design (human only) |
| Edit question / conflicts / missing | `update ID --question/--conflicts/--missing` | — | — | **filled**; MCP/HTTP: by design |
| Resolve with an answer | `update ID --resolved-by ANSWER` | — | — | **filled**; MCP/HTTP: by design |
| Accept / reject a dream-staged question | `dream accept-question`, `dream reject-question` | — | — | by design (human decision) |
| Review queue (corroborated first) | `corroboration queue` | — | — | MCP: by design (review is a human task) |
| Open-question labels on recall hits | — | `memory_search labels=true` (always in `two_stage`) | — | CLI: accidental, open (see search modes) |

The CLI paths run the same U1 gates as `Repository.add/update`: the actor is
`human`; an open question carries no `epistemic`, `causal` (other than
`none`), `causal_record` or `experience`; every `conflicts` id exists in the
question's tenant and is not the question itself; `--resolved-by` needs a
current answer in the same tenant that is not another open question, and sets
`valid_to`. `--dry-run` runs the same checks (the reference checks need an
existing vault) and writes nothing.

## Search and context

| Capability | CLI | MCP | HTTP | Gap |
|---|---|---|---|---|
| FTS search | `index search` (TSV + metrics; `--json` new; may autowarm) | `memory_search mode=fts` | `/search` | none |
| Hybrid / vector / multihop search | — | `memory_search mode=hybrid/vector/multihop` | — (rejected as unavailable) | CLI: accidental, open |
| Two-stage recall with policy | — | `memory_search mode=two_stage policy=…` | — | by design: agent recall shape (MCP only) |
| Corroboration on hits | `corroboration show ID` | `memory_search labels=true` | — | none for one id |
| Context v1 | — | `memory_context` | — | CLI: accidental, open |
| Context v2 pack (slots, `if_version`) | — | `memory_context v=2`, resources `memfw://context/{core,handoff,topics}` | — | CLI: accidental, open |
| Usage signal | — | `memory_used` (listed only when enabled) | — | by design: a per-session agent signal |
| Batch lifecycle lookup of recall hits | `resolve --ids-stdin` | — | — | by design: feeds external recall projections |

## Concepts and relations

| Capability | CLI | MCP | HTTP | Gap |
|---|---|---|---|---|
| List / show concepts | `concepts list`, `concepts show` | — (counts via `memory_context` slot `topics`) | — | MCP: accidental, open |
| Relation table, relation lint | `concepts relations`, `concepts lint-relations` | — | — | by design (operator lint) |
| Propose / merge / import seed | `concepts propose`, `concepts merge`, `concepts import-seed` | — | — | by design (human) |

## Approvals, candidates, dream, experience

| Capability | CLI | MCP | HTTP | Gap |
|---|---|---|---|---|
| Record an agent proposal | `record-proposal` | — | — | MCP: accidental, open (needs agent signing on MCP) |
| Approve a proposal | `approve-proposal`, `review` | — | — | by design (human only) |
| List / apply / expire staged candidates | `candidates`, `candidate-apply`, `candidates-expire` | — | — | by design (human only) |
| Capture confirm / simulate | `capture confirm`, `capture simulate` | — | — | by design |
| Consolidation packet | `consolidate` | — | — | by design |
| Run a dream | `dream run` | `dream_run` (deterministic rules only) | — | none (judges are CLI-only by design) |
| Skill candidates | `dream skills` | — | — | by design (operator) |
| List staged dreams | — | `dream_list` | — | CLI: accidental, open |
| Experience drafts / invalidate | `experience draft`, `experience from-github`, `experience invalidate` | — | — | by design (human review) |

## Privacy

| Capability | CLI | MCP | HTTP | Gap |
|---|---|---|---|---|
| Set a memory's level | `add --privacy`, `update --privacy` (new) | `memory_remember privacy=` (staged) | `/stage privacy` (staged) | CLI `update`: **filled** (was accidental); MCP/HTTP direct change: by design |
| Show / set `legacy_privacy_default` | `privacy legacy-default [LEVEL]` | — | — | by design (operator setting) |
| Show / set `new_privacy_default` | `privacy new-default [LEVEL]` | — | — | by design |
| Clearance on exits | `okf export --clearance`, `project obsidian --clearance`, `notes serve --clearance` | `MEMFW_MCP_CLEARANCE` (server-wide) | `--clearance` / `MEMFW_READONLY_CLEARANCE` | none |

## Attachments (MM1a)

| Capability | CLI | MCP | HTTP | Gap |
|---|---|---|---|---|
| Register an original | `attach add FILE [--embed] [--privacy …]` | — | — | by design for now: a human write; agents get proxy search in MM2b |
| List / show records | `attach list [--json]`, `attach show ID` | — | — | MCP `attachment_search` / `attachment_get` arrive in MM2b; HTTP never returns originals (design H5) |
| Verify records and blobs | `attach verify [ID]…`, `fsck` | — | — | by design (operations) |

## Conversations and history

| Capability | CLI | MCP | HTTP | Gap |
|---|---|---|---|---|
| List sessions | `conversations list` | `conversations_list` | — | none |
| Search turns | `conversations search` | `conversations_search` | — | none |
| Read a range | `conversations get` (`--around` is a store offset) | `conversations_get` (turn indexes only; no `--around`) | — | `--around` is CLI-only |
| Continuation pack | `conversations handoff`, `handoff` | `conversations_handoff` | — | none |
| Worker run / status / schedule | `conversations worker`, `conversations status`, `conversations schedule` | — | — | by design (ops) |
| Re-redact the stored text | `conversations reredact` | — | — | by design (ops, writes the store) |
| Ingest transcripts | `history ingest` | — | — | by design (ops) |
| Transcript index: index / list / search / read | `history index/list/search/read` | `history_list/search/read` | — | MCP index build: by design |
| Registered readers | `history agents` | — | — | by design |

## Projections, export, notes, code

| Capability | CLI | MCP | HTTP | Gap |
|---|---|---|---|---|
| Obsidian board | `project obsidian` | — | — | by design (writes outside the vault, operator) |
| Recall projection export | `projection export` | — | — | by design |
| OKF export / validate / import | `okf export`, `okf validate`, `okf import` | — | — | by design |
| Notes index build / eval / manifests | `notes index`, `notes eval`, `notes shadow`, `notes manifest-*`, `notes related-*build` | — | — | by design (ops) |
| Notes search / get / related / status | `notes search`, `notes related`, `notes status` | notes MCP `notes_search`, `notes_get`, `notes_related`, `notes_status` | — | CLI `notes_get`: covered by reading the file |
| Retrieval feedback / trace | `notes feedback-export` | notes MCP `retrieval_feedback`, `retrieval_trace` | — | none (different direction: agents record, operator exports) |
| Code index / search / map | `code index/search/map` | — | — | MCP: accidental, open |

## Operations

| Capability | CLI | MCP | HTTP | Gap |
|---|---|---|---|---|
| fsck, recover | `fsck`, `recover` | — | — | by design |
| FTS index rebuild | `index rebuild` | — (the Runtime checks the index) | — | by design |
| Event log replay / verify / compact | `log replay`, `log verify`, `log compact` | `memory_status` (log head) | — | by design |
| Budget show / configure | `budget show`, `budget configure` | — | — | by design |
| Forget, tiers, cold archive / restore | `forget run`, `tiers run/status`, `cold-archive`, `cold-restore` | — | — | by design |
| Evaluations | `eval causal` | — | — | by design |

## Machine output (`--json`)

Most read commands print JSON by default. Two already printed text by
default and had a `--json` flag before this change: `eval causal` (a summary;
`--json` prints the full report) and `okf import` (a per-concept table;
`--json` prints the result). The three below printed text and had no flag;
they now take `--json` (their default output is unchanged byte for byte):

| Command | Default | `--json` |
|---|---|---|
| `list` | `id<TAB>tier<TAB>layer<TAB>title[<TAB>stub]` lines; warnings on stderr | `{"memories": [{id, tier, layer, title, stub}], "warnings": [...]}` (warnings also on stderr) |
| `show ID` | the memory file's Markdown bytes | the parsed memory (`MemoryDocument` as JSON) |
| `index search QUERY` | `id<TAB>title` lines, then one metrics JSON line | `{"results": [{id, title}], "warnings": [...], "metrics": {...}}` |

Already JSON (no flag needed): `stats`, `fsck`, `recover`, `log *`, `budget *`,
`tiers status`, `candidates`, `candidates-expire`, `memory-related`, `review`,
`corroboration *`, `concepts list/show/relations/lint-relations`,
`conversations list/search/get/status/worker`, `history *`, `dream *`,
`experience *`, `capture *`, `notes search/status/related`, `code *`,
`okf *`, `projection export`, `project obsidian`, `resolve`,
`privacy legacy-default/new-default`, and every `--dry-run`.
(`okf *` here means `okf export` and `okf validate`; `okf import` is text by
default, see above.) `projection export` and `resolve` print JSON by default
and keep their existing `--json` flag, which is a no-op. `conversations
handoff` and `handoff` choose with `--format`. `conversations schedule`
prints a launchd plist (a generated file, not data).
Write commands that confirm with a line (`init`, `add`, `update`, `delete`,
`rollback`, `index rebuild`) are not read commands and keep their output.

## Help panels

`memfw --help` groups the top-level commands into four panels
(`memfw.cli.HELP_PANELS`; a test fails when a new top-level command has none):

* **Daily**: init, add, list, show, update, delete, rollback, supersede,
  layer-set, layer, stats, concepts, memory-related, handoff, notes, code,
  attach.
* **Approval**: record-proposal, approve-proposal, review, candidates,
  candidate-apply, candidates-expire, capture, consolidate, dream, experience,
  corroboration.
* **Ops**: fsck, recover, index, log, budget, forget, tiers, tier,
  cold-archive, cold-restore, memory-related-build, history, conversations,
  privacy, eval.
* **Export**: okf, projection, project, resolve.

Command names, options and behaviour did not change.

## Open accidental gaps

Not filled here because each needs more than a thin wrapper:

* **CLI search modes** (hybrid, vector, multihop) and **CLI context**
  (`memory_context` v1/v2): both need the Runtime's embedder and app identity
  settings on the CLI (which app the operator reads as for the `handoff`
  slot), i.e. a settings decision, not only a command.
* **CLI `dream list`**: small (`Runtime.staged_dreams`), but it needs a tenant
  option convention shared with `dream run`; next CLI pass.
* **MCP concepts list/show** and **MCP code search**: new agent read tools
  change `tools/list`, which other clients pin; they belong in their own
  change with a schema review.
* **MCP `record-proposal`**: agent proposals over MCP need the agent
  signature flow on that transport.

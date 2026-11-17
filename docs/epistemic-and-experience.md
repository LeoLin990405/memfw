# Epistemic type, causal grade, and experience points

memfw keeps three statements apart, and a fourth that is not a statement about the
world at all:

| what | field | meaning |
| --- | --- | --- |
| association | `causal: association` | two recorded things were seen together |
| causal hypothesis | `causal: hypothesis` | a causal claim whose identification has not been checked |
| verified causal claim | `causal: verified` | a causal claim that passed all checks below |
| experience point | `type: experience-point` | what a human accepted for one task, in one reviewed scope |

`epistemic` records what kind of statement a memory makes: `experience`, `fact`,
`judgment`, or `inference`. The field is optional; a memory written before it existed
parses with no epistemic type.

## Fields

`MemoryDocument` gained five optional fields. None of them is written when it holds its
default, so older files parse and render byte for byte as before:

* `epistemic` — one of `experience`, `fact`, `judgment`, `inference`; absent when unset.
* `causal` — one of `none` (default), `association`, `hypothesis`, `verified`.
* `causal_record` — required for `hypothesis` and `verified`:

  | field | meaning |
  | --- | --- |
  | `treatment`, `outcome` | the two sides of the claim |
  | `confounders` | `known`, `suspected`, `unobserved` lists |
  | `selection` | how units entered the data |
  | `temporal_order` | why the treatment precedes the outcome |
  | `estimand` | ATE, CATE, or a stated alternative |
  | `identification` | `assumptions` claimed, `untested` assumptions still open |
  | `robustness` | refutation/sensitivity checks: `check`, `result`, `failed` |
  | `proposed_by`, `alternatives` | who proposed it, competing explanations |

* `limitations` — document-level list of accepted limits; can satisfy the
  `verified` requirement together with, or instead of, `robustness`.
* `experience` — the reviewed experience record (see
  [experience-points.md](experience-points.md)).

## One write gate for every path

`memfw.core.epistemic.validate_epistemic_write` runs on `Repository.add`,
`Repository.update`, legacy and typed agent proposals, the human decision that applies a
proposal, and dream/curation candidate staging. A write that cannot pass it is rejected
before anything is written, and the error names every missing or contradictory item.

`causal: verified` requires all of:

1. `causal_record.estimand` non-empty;
2. `causal_record.identification` non-empty;
3. **no untested identification assumptions** (`identification.untested` empty) — an open
   assumption means the claim has not finished its identification argument;
4. at least one `evidence` entry that points **outside the vault** (see below);
5. `causal_record.robustness` or `limitations`;
6. no robustness check with `failed: true`;
7. a human writer, or a write covered by a recorded `HUMAN_DECISION`, when the write
   *turns* the memory into `verified` (`decide_proposal` records it; `apply_proposal`
   requires it). A memory that is already `verified` is not frozen: an agent or dream
   may still correct it, append evidence, or demote it. Only the transition into
   `verified` needs a human.

A failed robustness check therefore cannot stay `verified`: the same write is rejected
until the memory is demoted to `hypothesis` with the failure recorded in the record.

### What counts as evidence

`evidence` entries are opaque pointers; the gate reads the prefix. Authority, strongest
first:

| class | prefixes | counts as |
| --- | --- | --- |
| human decision | the write-level `HUMAN_DECISION` flag | overrides the pointer rules for chat/LLM sources |
| raw | `data:`, `dataset:`, `design:`, `experiment:`, `result:`, `test:`, `code:`, `file:`, `sha256:`, `pr:`, `commit:`, `issue:`, `repo:` | replayable raw data or a design |
| link | `http://`, `https://` | points outside the vault, but cannot be verified by the vault |
| chat | `chat:`, `conversation:`, `session:`, `transcript:`, `message:`, `turn:` | narrative only |
| generated | `llm:`, `reflection:`, `reasoning:`, `explanation:`, `dream:`, `distil:`, `summary:` | narrative only |

A prefix with nothing after it (`data:`, `https://`) is not a pointer. Anything else
(including a bare memory id) is not outside-vault evidence. Evidence that only points at
chat text, only at LLM explanations/reflections, or at no raw source at all cannot make a
memory `verified`; it stays `hypothesis`.

**A bare web link is not raw data.** When `source` is a chat source or `provenance` is
LLM-derived, `causal: verified` requires a replayable raw pointer (the `raw` row above)
or a recorded `HUMAN_DECISION`. A link pasted from a chat therefore cannot promote a
claim by itself; the gate says so and names both ways forward.

**Agents and dreams may not promote, but they can edit and demote.** Their proposals are
validated with `human_decision=False`, so a write that turns a memory into
`causal: verified` is refused. A write on a memory that is already `verified` is allowed
only if the verified invariants survive (raw evidence, tested identification, no failed
robustness). A dream confirmation therefore appends its transcript refs to the existing
evidence instead of replacing it, and a candidate that would break the invariants is
skipped and reported (`skipped_candidates`) instead of aborting `memfw dream run`.

## Open questions

A `type: open-question` memory says what is *not* known. It never states a fact, so it
carries no `epistemic`, has `causal: none`, and has no `causal_record` or `experience`.
Its block is required for the type and refused on any other type:

```yaml
type: open-question
about: [concept:service-port]        # the existing about field
open_question:
  question: Which port does the service listen on?   # 1-300 characters
  conflicts: [mem-a, mem-b]          # memory ids, deduplicated, at most 20
  missing: the deployed config file  # 0-300 characters
  resolved_by: null                  # the answering memory, once resolved
```

* **Write gate.** Every path (`add`, `update`, rollback, agent proposals, dream
  candidates) runs the same contract. Each `conflicts` id must exist and share the
  question's tenant, and none may be the question itself. For a writer other than a
  human, a missing id and an id in another tenant fail with the same message
  ("missing or not visible").
* **Who may do what.** Creating an open question, changing a memory's `type` to or from
  `open-question`, setting or clearing `resolved_by`, changing an open question's
  `valid_to`, and changing the `conflicts` of one that already has `valid_to` need a
  human actor or an approved proposal; a mechanism or a FULL-permission agent may edit
  an open question's other fields. The dream stages open-question candidates for the conflicts it finds (U3; see
  [dream.md](dream.md)) and a human accepts one with `memfw dream accept-question`;
  `memory_remember` cannot stage one. An agent
  can propose a resolution: an `update` proposal with `open_question` and `valid_to`.
  `valid_to` is proposable only for that: the target must be an open question and the
  proposed block must set `resolved_by`.
* **Resolution.** `Repository.resolve_open_question(question_id, answer_id)` (human only)
  is an in-place update of the question that sets `open_question.resolved_by` and
  `valid_to`. The answer must exist, share the tenant, be current, and not be another
  open question. The question file is kept and its unresolved version stays in
  `.history/`. Recall without `as_of` hides the resolved question (it has `valid_to`); an
  `as_of` earlier than the resolution still returns it, read from the current file, so
  its block already names `resolved_by`. `supersede` of an unresolved question keeps its
  scope like any memory's and carries the block over with `resolved_by` cleared (it is
  also accepted in `overrides`); conflict ids that were deleted or moved to another
  tenant are dropped and listed as `dropped_conflicts` in the SUPERSEDE event.
* **Lint.** `fsck` reports `open_question_dangling_conflict`,
  `open_question_self_conflict`, `open_question_cross_tenant_conflict` (conflict checks
  skip historical versions that have `valid_to`), `open_question_dangling_resolution`
  (also when the answer's supersession chain ends at a deleted memory),
  `open_question_superseded_resolution` (the answer's supersession chain is followed, so
  a revised answer passes; one whose current version is no longer in force does not),
  `open_question_cross_tenant_resolution`, `open_question_resolved_but_current`,
  `open_question_multiple_unresolved` (two unresolved versions without `valid_to` in one
  supersession chain), `open_question_missing_block`, and `open_question_wrong_type` /
  `open_question_invalid_block` for an `open_question` key on a memory of another type
  (a well-formed block / anything else). That key is ignored like an unknown key: the
  memory stays readable and editable, and a read does not rewrite the file.
* **Export and hashes.** `memfw okf export` puts `type: open-question` and the block in
  the `memfw:` envelope. `MemoryDocument.model_dump` leaves out an absent
  `open_question`, so every other memory dumps, and its proposal and agent snapshot
  hashes stay, exactly as before.
* **Compatibility.** Files without the block keep their bytes. memfw 0.2.0 does not
  reject an open question: it reads the file, keeps `type: open-question`, and lists
  `open_question` in `ignored_keys`. A write by 0.2.0 drops the block; this version then
  refuses gated writes to that memory until the block is restored, and `fsck` reports
  `open_question_missing_block`.

* **Recall (U2).** An open question is recalled like any memory and its row says
  `type: open-question`. MCP `memory_search` labels other hits: `open_question:<id>` when an
  unresolved open question visible to the caller is `about` one of the hit's concepts, and
  `conflicted` when one lists the hit in `conflicts`. `mode=two_stage` always adds them to
  `recall_labels`; the other modes add `recall_labels` only with `labels: true`. A question
  from another tenant, or one the active policy drops (for example a private one under
  `policy=strict`), never appears in a label. See
  [ports-and-recall.md §4.1](ports-and-recall.md#41-mcp-memory_search-modetwo_stage). The
  context pack does not include open questions yet (iteration C, default off). A question
  with `valid_to` set is never shown in a label, even for an earlier `as_of`.

## Corroboration (derived)

`memfw corroboration rebuild` writes `corroboration.sqlite3` at the vault root (next to
`index.sqlite3`; never inside `memories/`). It is a rebuildable projection: delete it and
rebuild it and the bytes are identical. It never writes the vault and never changes
`epistemic` or `status`. It is read only for display and for the open-question review
order (U5, below); it changes no ranking.

Per memory it records which (app, session) pairs mention it, from
`conv:APP:SESSION@OFFSET` references in three places:

* the memory's own `evidence`;
* staged conversation-worker claims (`memory_claims`, under `_candidates/dream/`) whose
  claim equals the memory's match key after normalising case, whitespace and trailing
  punctuation. The key is the summary, else the title, and only one of at least 4 tokens
  and 20 characters that no other default-tenant memory has as its title or summary
  (other tenants are never consulted, so their text cannot change the answer).
  The conversation store has no tenant, so claims credit only memories in the default
  tenant `""`. The worker stages a claim once per app (it dedupes on app and claim), so
  repeating a claim in later sessions of the same app adds nothing until the claim text
  differs. Dream review records no rejected state: a rejected or stale candidate keeps
  counting until it is removed from `_candidates/`;
* experience points in the same tenant whose `related` lists it and whose review verdict
  is `accepted` (`failed` is not support).

Only ids, app names, counts and times are stored, no text. The file is created `0600`.

Independence rules:

* the same app in the same session counts once;
* a carrier derived from the memory does not count: its evidence (an experience point's
  or a claim candidate's) cites the memory id (bare, `memory:` or `mem:`), or a candidate
  names it in `memory_ids`;
* a mention in a session where the memory was injected, at or after the injection, does
  not count. Injection is read from `memory_access` events that carry `session_id` (and
  `source`/`agent_id` as the app when present). **Gap:** memfw's retrieval and
  `memory_context` do not record a session today, so an agent restating an injected
  memory is not detected until a producer records one;
* a carrier with a generated provenance (`llm`, `agent`, `dream`, `dream-distilled`, ...)
  does not count, nor does a dream candidate that is not conversation-worker output, nor
  a contradicting evidence ref;
* apps that are not `readable`, or opted out, under `conversation_share` are never
  written to the file.

`memfw corroboration show MEMORY_ID --as-app APP` returns
`{count, apps, sessions, first_seen, last_seen}` trimmed to what `APP` may read: only apps
its `can_read` allows (plus itself) appear in `apps`, `sessions` and `count`; with
`can_read: false`, sharing off, or an opted-out caller, it sees only its own. Hidden apps
are filtered again at read time, so tightening the policy takes effect before the next
rebuild. `first_seen`/`last_seen` use the evidence timestamp, else the carrier's
`created_at` (an upper bound).

### How corroboration is used (U5)

* **Display only, opt-in.** MCP `memory_search` with `labels: true` gives every row, in
  every mode (`two_stage` included), a `corroboration` field: the same
  `{count, apps, sessions, first_seen, last_seen}` as `show`, trimmed to the server's
  `--app` (`MEMFW_APP`). With `can_read: false` an app sees only its own share. Without
  `labels` the output is byte-identical to before. When it cannot be shown the field is
  `{"degraded": CODE}` and the search itself is not degraded: `caller_app_required` (no
  `--app`, or the reserved `operator` name over MCP), `share_policy_invalid`,
  `projection_missing` (run `memfw corroboration rebuild`), `projection_unreadable`.
  The projection can be stale until the next rebuild.
* **Reverse signal: review order.** `memfw corroboration queue [--as-app APP] [--tenant T]`
  lists the unresolved, current open questions (the ones the U2 labels use) in review
  order. A memory in a question's `conflicts` is *corroborated* when its visible
  corroboration names at least 2 distinct apps. Order: more corroborated conflicts first,
  then more support (the sum of their `count`s), then older (`created_at`), then id. Each
  row carries `priority: {corroborated_conflicts, support}`. Corroboration is trimmed to
  `APP` first (default: the operator, every readable app), so a hidden app can never move
  a question; only conflicts the caller can see are looked up. Without the projection the
  order is oldest first and `corroboration.degraded` says why. The order never hides,
  edits or decides a question. The rule is `memfw.recall.priority`.
* **Deferred.** Promotion candidates (`inference` to `fact` after 2 apps, 3 sessions and
  no open question) and ranking weights need evaluation data and approval first; neither
  is implemented.

## Retrieval and export

* `MemoryHybridRetriever` rows and the MCP `memory_search` result rows carry
  `epistemic` and `causal`; the MCP `memory_get` response also returns `causal_record`,
  `limitations`, and `experience`. An answering agent can therefore see that a hit is
  only a hypothesis.
* `memfw okf export` writes the same fields inside the `memfw:` envelope of each concept,
  so the OKF concept schema is unchanged and old bundles keep their bytes.

## CLI

```bash
memfw add --title ... --body ... --epistemic inference \
  --causal hypothesis --causal-record record.json
memfw update MEMORY_ID --causal verified --causal-record verified.json --evidence data:run-42
memfw update MEMORY_ID --causal hypothesis          # demote after a failed check
```

`--causal-record` accepts an inline JSON object or a path to a JSON file. `add` and
`update` validate under `--dry-run` too; neither writes with `--dry-run`.

## What this is not

The record is a schema and a gate, not a causal-inference engine. It cannot tell whether
an analysis is correct; it can only refuse a `verified` claim that omits its identification
argument, its robustness check, or its raw-data evidence, and refuse to let an agent make
one. See `docs/evaluation.md` for the small experiment that measures how often the gate
and the retrieval layer let an association pass as a cause.

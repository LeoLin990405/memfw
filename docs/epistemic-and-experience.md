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

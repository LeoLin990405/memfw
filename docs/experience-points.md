# Experience points

An experience point records **what a human accepted for one task, in one reviewed
scope**. It is written after the task ended and after a human reviewed the result. It is
not a causal proof and it does not authorise automatic reuse; every experience-point body
carries the fixed sentence:

> 仅说明这一次、在此范围内人工认为可接受；不是因果证明，不授权自动复用。

Written into the body by `memfw experience draft`; the write gate refuses a
`type: experience-point` document without it.

An experience point is always `epistemic: experience` and `causal: none`. The gate
enforces both, so a causal conclusion cannot be smuggled in as an experience.

## Record shape

| field | meaning |
| --- | --- |
| `task` | the task the experience is about |
| `conditions` | the conditions under which it applies |
| `actions` | what was done |
| `evidence` | pointers back to the review material |
| `review.reviewer` | who reviewed it (required) |
| `review.scope_reviewed` | what the review actually covered |
| `review.not_reviewed` | what it did not cover |
| `review.verdict` | `accepted` or `failed` (required) |
| `version` | the record's version |
| `counterexamples` | known cases where it does not hold |
| `invalidated_when` | conditions that would make it obsolete |

A failed experience is still worth recording: `verdict: failed`.

## Draft JSON schema

`memfw experience draft --from <file.json>` reads this object. Unknown keys are rejected.

```json
{
  "task": "string, required",
  "conditions": ["string"],
  "actions": ["string"],
  "evidence": ["string"],
  "review": {
    "reviewer": "string, required, non-empty",
    "scope_reviewed": ["string"],
    "not_reviewed": ["string"],
    "verdict": "accepted | failed, required"
  },
  "version": 1,
  "counterexamples": ["string"],
  "invalidated_when": ["string"],
  "title": "optional; defaults to 'Experience point: <task>'",
  "body": "optional extra prose appended after the fixed sections"
}
```

A synthetic example lives at `tests/fixtures/experience_point_example.json`.

## Commands

```bash
# Default is a dry run: the validated document is printed, nothing is written.
memfw experience draft --from tests/fixtures/experience_point_example.json

# Stage a propose candidate under _candidates/experience/ (still not the vault).
memfw experience draft --from my-review.json --apply

# Conductor source adapter (read-only): a merged PR becomes a draft.
memfw experience from-github --repo owner/name --pr 42 --apply

# The conditions changed: stage a pending-revision proposal.
memfw experience invalidate MEMORY_ID --reason "the export format changed"
```

`experience draft` and `experience from-github` never write `memories/` or the event
log. `--apply` publishes one candidate file under `_candidates/experience/` with
`authority: false`; the candidate contains the reviewed record and the exact payload.

Promotion is a separate human action (out of scope for this change). A reviewer can
write an accepted candidate with:

```bash
memfw add --title "<EXPERIENCE_POINT_TITLE>" --body "<EXPERIENCE_POINT_BODY>" \
  --type experience-point --epistemic experience \
  --experience "<EXPERIENCE_POINT_EXPERIENCE_JSON>"
```

The write gate validates the record again at that point.

## Conductor / GitHub adapter

`memfw experience from-github --repo <owner/name> --pr <n>` runs
`gh pr view --json title,body,mergedAt,reviews,commits,files,url,mergedBy,author`
(`mergedBy` is an addition to the minimum field list: without it a merged PR with no
review records has no merger to name). It never writes to GitHub, never reads Conductor's
code or database, and tests use recorded synthetic JSON — no network.

* An **unmerged** PR is refused: an experience point needs a finished task and a human
  verdict, and the merge is the recorded verdict this adapter has.
* `reviewer` comes from the PR review records. With no review records, the reviewer is
  the merger and `scope_reviewed` says the reviewed scope is the merge decision only
  (`not_reviewed` records that no review covered the change itself).
* `evidence` contains `pr:<repo>#<n>` and one `commit:<sha>` per commit, which the causal
  gate classifies as raw pointers.

## Invalidation

`memfw experience invalidate <id> --reason ...` writes a proposal under
`_candidates/experience/` with `action: revise`: it suggests appending the reason to
`invalidated_when` and setting `valid_to`. It does not edit the vault. A supersede still
needs a successor record, which a human writes.

## What it is not

An experience point is not evidence that the action causes the outcome, and it is not a
policy. The gate deliberately prevents `causal: verified` on the same document, so a
retriever can never hand an agent an experience point labelled as a causal conclusion.

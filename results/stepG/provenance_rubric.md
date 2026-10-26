# Step G provenance audit rubric

Scope: public LoCoMo turns only. Goal: check whether a stored memory that is *not*
verbatim source text is nevertheless recorded as if it were the user's own words.

## Definitions

- **Source text**: the LoCoMo turn (`dia_id`) attached to the stored memory via
  metadata (`mem0`) or the deterministic memory-id mapping (`memfw`).
- **Verbatim**: lower-cased, punctuation-stripped memory text (after removing a
  leading `speaker X` prefix) is a contiguous substring of the normalized source
  turn. Setext/label prefixes added by a writer are ignored.
- **Derived/inferred**: any stored memory that is not verbatim under the rule above.

## Rules (applied mechanically, identical to the Day 11 audit rules)

- **R0**: no resolvable source turn id -> `missing_source_link` (not flagged).
- **R1**: verbatim -> `verbatim_source_text` (not flagged).
- **R2**: derived and metadata carries an explicit derived/inferred label
  (keys: derived, inferred, is_inference, source_type, provenance_kind) ->
  `derived_labelled` (not flagged).
- **R3**: derived, unlabelled, and the system records the memory in the user's
  memory namespace / attributes it to the user -> `derived_attributed_to_user_unlabelled` (**FLAGGED**: inferred text stored as the user's).
- **R4**: derived, unlabelled, no user attribution -> `derived_unlabelled` (not flagged).

## Attribution determination per system

- **memfw**: the record has an explicit `provenance` field. `verbatim-public-locomo-turn`
  is a verbatim label; it is not user attribution. A derived memfw record without a
  derived label would be R4 unless its provenance says otherwise.
- **mem0**: `Memory.add(..., user_id=...)` stores every produced memory in the user's
  memory namespace and mem0 exposes no per-memory inference/derived label. Step G also
  passes Mem0's public `prompt=` custom-instructions knob, which asks the extractor to
  emit `"attributed_to": "user"`. In the audited run this request was only partly
  followed: most derived memories are explicitly user-attributed, while a minority are
  explicitly assistant-attributed. Under the **Step-G rule**, a record with no
  `attributed_to` field defaults to the user namespace and therefore counts as R3; only
  an explicit non-user value (`assistant`) falls to R4. Under the **Day-11 rule**, a
  missing attribution is R4. Consequently the R3 flag rate is partly prompt-forced and
  partly model behaviour, not an independent quality measurement.
- **Difference from the Day 11 audit**: Day 11 sampled 30 memories uniformly and applied
  R3 only when `attributed_to == "user"` exactly. Step G's primary `derived_frame` samples
  only inferred memories, and its `all_frame` is a uniform fallback; a `day11_frame`
  (uniform sample, Day-11 R3 rule) is also reported for like-for-like direction checks.

## Outputs

- `provenance_audit.csv`: one row per audited memory, with its frame, classification,
  rule label, flagged boolean, and (truncated) public memory/source text.
- `provenance_summary.json`: denominators and flag counts for all three sampling frames.
  The audit uses run `rep1` only and reports this run id in the summary.

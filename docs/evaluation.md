# Evaluation: bounded evidence and limitations

This document preserves the detailed evaluation evidence summarized in the repository
[`README`](../README.md). Read it as **bounded evidence**, not as a product or quality claim.
All numbers are small-sample or single-repeat results; the linked reports state their power limits,
reproduction steps, and data boundaries. The public repository does not include the LoCoMo corpus.

## LoCoMo evaluation: a bounded negative result

The official local evaluation processed **10 conversations, 5,882 turns, and 1,986 questions**. Of
these, **1,982 questions had valid evidence**. It compared five BM25 retention policies: FIFO, LRU,
decay, salience, and consolidate-then-forget. The analysis used **1,000 paired bootstrap resamples
within each conversation**. Each overall condition/budget scope tested the **10 pairwise policy
comparisons** with Holm adjustment.

No overall pairwise contrast was significant after adjustment in the four overall condition/budget
scopes. This is a lack of detected overall differences in this evaluation; it does not establish
equivalence or prove that the policies have no effect. One category-level contrast passed its
within-category adjustment, but category results were not corrected across all categories and
scopes. Treat it as exploratory.

| Evaluation item | Result | Evidence |
|---|---:|---|
| Conversations / turns / all questions | 10 / 5,882 / 1,986 | [Report: result and data boundary](../reports/day7_report.md#result), [JSON: `sample_count`, `quick_scope`](../reports/LARGE_FILES.md) |
| Questions with valid evidence | 1,982 | [Report: evidence normalization](../reports/day7_report.md#official-data-and-evaluation-boundary) |
| Bootstrap repeats / resampling unit | 1,000 / questions within 10 conversations | [Report: paired intervals](../reports/day7_report.md#evidence-recall-with-paired-95-intervals), [JSON: `bootstrap_repeats`](../reports/LARGE_FILES.md) |
| Overall comparison family | 5 policies; 10 pairwise contrasts per scope; Holm adjusted | [Report: paired intervals and overall contrasts](../reports/day7_report.md#overall-paired-policy-contrasts), [JSON: `bootstrap`](../reports/LARGE_FILES.md) |
| Overall result | 0 significant adjusted contrasts across 4 scopes | [Report: overall contrasts](../reports/day7_report.md#overall-paired-policy-contrasts) |

The report measures evidence recall: the share of gold evidence IDs retrieved within a 128-token
context. It does not measure answer accuracy. This is a logical retention projection, not a
physical-vault capacity test. Consolidation uses deterministic mock proposals and simulated
projection semantics. The separate synthetic capture gate did not establish its proposed
storage-saving target; see the [gate results and definition](../reports/day7_report.md#capture-gate-and-h6).
The benchmark uses no answer-generation model or network access. The full
[report](../reports/day7_report.md#evidence-recall-with-paired-95-intervals) describes these limits and
reproducibility. The public repository does not include the corpus; rerunning the evaluation
requires a local copy supplied with `--data`.

## Findings (Days 10–11)

Both results are small-sample and **not statistically significant**; read them as bounded evidence,
not claims.

- **Why the five forgetting policies looked alike (Day 10).** Budgets did bind, but some policies
  kept identical memory sets (FIFO and LRU retained the same IDs in 10/10 no-access samples at 10%
  and 25%), and full-corpus BM25 recalled only 36.56% of evidence within the 128-token context vs.
  95.04% for a query-conditioned oracle, so retrieval/context is one plausible ceiling. Under
  tighter budgets salience diverged from FIFO (5%: 4.36% vs. 2.23% evidence recall, Holm-adjusted
  exact sign-flip p = 0.0977; 0/10 contrasts significant). This suggests budget pressure is where
  policies separate, but it is unproven. [Report](../reports/day10_null_result_analysis.md)
- **Local Mem0 baseline (Day 11).** Same local model (qwen3.5:9b), embedder (nomic-embed-text),
  top-k 5 and 128-token context, on 18 questions from one public LoCoMo conversation (first 100
  turns). Evidence recall at 100% retention: memfw 66.7% vs. Mem0 62.5%; at 5%: 4.2% vs. 0.0%.
  Answer score at 100%: 0.459 vs. 0.527. No difference is significant (Holm p ≥ 0.98); power is low
  (approx. minimum detectable recall difference 15–22 points).
  [Report](../reports/day11_baseline_comparison.md)
- **Provenance is a design trade-off.** In a 30-memory rule-based audit, all 30 memfw memories were
  verbatim source text; 21/30 Mem0 memories were derived text recorded as `attributed_to=user`
  without an inference label. Mem0 stores third-person summaries by design, which compress and
  resolve context; this is a difference in what is stored, not a Mem0 defect. Rules and counts:
  [reports/day11_baseline_comparison.md](../reports/day11_baseline_comparison.md).
- **Human confirmation was not exercised in Day 11.** memfw's budget enforcement ran as the
  automated mechanism and deleted directly (0 human approvals); Mem0 writes without a confirmation
  step by design. Human-in-the-loop cost remains unmeasured.

## Small local demo

The optional portfolio demo evaluates only the first sample, its first 100 turns, and up to 20
questions whose complete valid evidence set falls within those turns. It compares five production
retention policies at a 25% canonical-body budget with a no-forgetting reference, using production
BM25 retrieval. The result is a small subset demonstration, not a full benchmark or a statistical
claim. Consolidation is a deterministic mock-proposal projection; it does not invoke a
human-approval command. The script requires an explicit local JSON path, rejects known pilot-vault
paths, stores its temporary vault outside the project, and writes aggregate counts only.

```sh
python scripts/portfolio_demo.py \
  --data /path/to/local/locomo.json \
  --table \
  --output /tmp/memfw-demo.json \
  --receipt /tmp/memfw-demo-runtime.json
```

The JSON result is deterministic for the same input. On the public first-sample run documented in
the [demo report](../reports/portfolio_demo_report.md), it processed 100 turns and 20 questions in 1.04
seconds on the reporting machine; this timing is machine-specific, not a performance guarantee. The
report includes all six aggregate rows and a repeat-run byte-identity check. The optional runtime
receipt is separate. The demo does not copy the corpus into the repository and makes no model or
network calls. See the [three-minute recording outline](demo-recording.md).

## Step G experiment

[`results/stepG/REPORT.md`](../results/stepG/REPORT.md) is a reproducible, bounded comparison on the
public LoCoMo corpus: one fixed conversation window (`conv-49`, first 100 turns), the 18 questions
defined by the Day 11 manifest, and seed `20261021`. It is an experiment artifact, not a product
claim; read the report's limitations before citing numbers.

- **Arms:** `memfw` (verbatim writes + shared-embedding cosine retrieval), `mem0_stock`
  (stock Mem0 `add(infer=True)`; same default extraction prompt as Day 11, but the context window
  differs — see the report's reproduction section), and `mem0_custom` (a sensitivity arm with
  schema-compatibility extraction instructions).
- **Same answer model and embedder for every arm:** `qwen3.5:9b` + `nomic-embed-text`,
  temperature 0, top-k 5, 128-token context budget.
- **Judging:** primary judge is a different local model (`qwen3.5:4b`); a same-model judge is kept
  as a diagnostic.
- **Bounded result on this subset:** no statistically detectable answer-quality difference between
  the arms (paired exact tests, n=18, MDE ≈ 28 pp); `memfw` writes cost orders of magnitude less;
  Mem0 records derived text as user memories without inference labels in the observed samples.
  An exploratory expanded question set (41 questions in the same window) is reported separately.
- **Expanded check:** an exploratory 41-question set inside the same window (tighter MDE, still no
  significant differences) is included in the report.
- **Cross-framework check:** the same fixed subset is replayed for additional open-source memory
  frameworks (LangMem, MemTensor MemOS in its local `general_text` mode, plus the existing Mem0
  arms) with an identical answerer/embedder/judge. Framework arms are exploratory single-repeat
  runs; see [`results/stepG/frameworks/FRAMEWORK_COMPARISON.md`](../results/stepG/frameworks/FRAMEWORK_COMPARISON.md).
- **Data analysis:** per-category and per-question breakdowns, retrieval-vs-answer bottlenecks, judge
  disagreements and the adversarial-category failure mode are recomputed from the raw artifacts in
  [`results/stepG/ANALYSIS.md`](../results/stepG/ANALYSIS.md) (`python eval/stepG_analysis.py`).
- **Architecture iteration:** [`results/stepG/arch/ARCH_SUMMARY.md`](../results/stepG/arch/ARCH_SUMMARY.md)
  reports ablations (speaker/date-tagged context, abstention/date/count rules, MMR and hybrid
  retrieval) on the same protocol (`python eval/stepG_arch.py`). `memfw_v6`/`memfw_v6_hybrid` have
  higher point estimates than the frozen v1 and the Mem0/LangMem/MemOS arms, but the differences are
  mostly not statistically significant at n=18/41 — read the caveats.
- **Multi-conversation validation:** [`results/stepG/multiconv/MULTICONV_REPORT.md`](../results/stepG/multiconv/MULTICONV_REPORT.md)
  extends the same protocol to all ten public LoCoMo conversations (first 100 turns each, 313
  questions) with conversation-cluster bootstrap, testing whether the architecture gains generalise
  beyond the single frozen conversation (`python eval/stepG_multiconv.py`).
- **Reproduce:** `python eval/stepG_pipeline.py --stage all` (local Ollama models required; see the
  report for the exact environment). Implementation: [`eval/stepG_runner.py`](../eval/stepG_runner.py).

## Causal promotion experiment (synthetic)

`memfw eval causal --json` measures whether an association can be promoted to a causal
conclusion. It is not a benchmark of causal-inference correctness: it exercises the write
gate and the retrieval labels on synthetic materials that are known by construction.

Materials live in `src/memfw/eval_data/causal_eval/` (shipped as package data), one JSON file each, in four groups of
two so that each rule is exercised more than once:

1. `identified` — two correct analyses with a complete identification argument, robustness
   checks, and replayable evidence; the gate must accept both;
2. `confounded` — an unadjusted confounder and a pure Simpson's-paradox reversal; both are
   refused because an identification assumption is still untested;
3. `narrative` — a chat-only "because … therefore …" memory and a structurally complete
   record whose only evidence is a chat-share URL; both are refused on the evidence rule,
   not on a structural check;
4. `malformed` — an empty evidence pointer (`data:`) and a whitespace-only identification
   assumption; both are refused, and the error names the missing item.

Two layers are reported separately:

* **write layer** — each material is written through the real `Repository.add` path with
  `causal: verified`. `false_accepts` counts materials the gate accepted although they do
  not deserve `verified`; `false_rejects` counts materials it refused although they do;
  `spurious_promotions` counts writes that ended up labelled `verified` when they should
  have been capped. The gate should report zero false accepts and zero false rejects.
* **retrieval layer** — correctly labelled documents are searched with the local FTS
  retriever (no model, no network). `missing_labels` counts hits without an `epistemic`/
  `causal` label, and `hypothesis_as_conclusion` counts hypothesis-level hits that a
  deterministic answer template placed in its conclusion section. It should be zero.

The experiment is synthetic and small; it shows the gate and the labels behave as
specified on these cases, not that they generalise to real arguments.

## C4 context pack evaluation

`memfw eval context SNAPSHOT` measures the session-start context pack
(`memory_context` v2, iteration C) on a **read-only vault snapshot**, so the
defaults (2000-token budget, per-slot caps, a resident-prefix cap) can be set
from data. It changes no default; any change still needs the report and an
explicit approval.

**What it measures**, per slot (`core`, `handoff`, `topics`, `current`) and
for the pack. `current` (U6) is measured only when the snapshot's `vault.yaml`
enables it (`runtime.context.current: true`) or `--current` simulates that
switch; the report's `settings` records `current_enabled` and
`current_simulated`. Otherwise `current` is one `available: false` row, as
`memory_context` refuses the slot then.

- *coverage*: of the memories a session's searches returned, the share already
  in the opening pack. `core` covers a memory by carrying it (`direct`);
  `topics` covers the memories about a listed concept and `handoff` those whose
  `evidence` cites a listed session (`pointer`: the pack pointed at them).
  Sweep rows give `coverage` (every resident slot) and `coverage_direct`
  (memory entries only).
- *waste*: of the pack's items, the share a session never used. "Used" is a
  C5 `memory_used` signal of the session when a usage store is given
  (`waste_basis: memory_used`), otherwise it is estimated as "returned by one of
  the session's searches" (`estimated_from_hits`).
- *latency*: P50/P95 (nearest rank) over `--repeats` builds of each slot's real
  provider (`Runtime.context_slot_provider`). The timings are *cold*: the
  evaluation's runtime has no C3 context cache, so every `core` build reads the
  vault (`settings.latency: cold`); a pack served from a warm cache can be
  faster.
- *token cost*: each slot's `used_tokens_est`.

**Where sessions come from**, in this order: the snapshot's `memory_access`
events of search operations (grouped by `details.session_id` when present,
otherwise split after `--session-gap-minutes` of silence, default 30); the
usage store's `memory_used` signals (one session per `session_ref`); or a
synthetic query set (`--sessions` x `--queries` queries drawn with `--seed`
from the visible memories and run through `memory_search(mode=fts)`). The
report's `basis` names the source. The pack is built from the snapshot as it
is now, not as it was when each session started: read old sessions as an
approximation.

**The sweep.** Each slot is built at every cap of `--slot-caps` (default
500,1000,2000,4000; `--slot-cap NAME=CAP,...` sets one slot's list), and every
combination is scored against every `--resident-caps` value (default
1000,2000,4000,6000). The resident prefix is *simulated*: slots are taken in
order while their tokens fit under the cap; the first that does not fit and all
later ones are demoted (they would become a catalogue line plus a
`memory_search(mode=two_stage)` hint) and add neither cost nor coverage. A slot
that is degraded (for example `handoff` without `--conversations`:
`unavailable`) costs and covers nothing; a slot the build does not know is one
`available: false` row and is left out of the sweep.

**Read-only, by construction.** The snapshot is never opened in place. The
command refuses a path without `vault.yaml`, a snapshot with an interrupted
write (`.pending.json`, `.event-append.pending.json`: reading it would need
`memfw recover`, which writes), a snapshot that contains a symlink (it could
point outside the copy), an `--out` inside the snapshot and a store inside it. It copies the snapshot (without its search index and lock file) to
a private temporary directory, runs everything there and deletes it.
`--conversations` and `--usage` are never opened in place either: each is
copied with its `-wal`, `-shm` and `-journal` files (when present) into the
private directory, and the copy is opened `mode=ro`, so a WAL-mode store gets
no sidecar beside it. The snapshot, both stores and their sidecar paths are
fingerprinted (path, size, mtime) before and after; any change fails the run
with no report.

**Privacy.** Visibility is the MCP server's: `--tenant`, `--source`,
`--clearance` and `--app` (else `MEMFW_TENANT`, `MEMFW_ALLOWED_SOURCES`,
`MEMFW_MCP_CLEARANCE`, `MEMFW_APP`; no embedder is ever loaded), the
`--policy` (`compat_fts` or `strict`), and `conversation_share` for `handoff`.
`handoff` reads model summaries only from an explicit `--state FILE` (a copy of
the conversation worker state, copied and fingerprinted like the stores);
without it, none: the worker state `vault.yaml` names is never read.
A hit the caller may not see is not counted (only `hits_dropped`). The report
holds counts, ratios, token estimates, timings, slot names and caps: no body,
title, query, memory or session id, path or tenant name.

**Deterministic.** The same snapshot, stores, options and seed give the same
report except the `latency_ms` fields. `--now` fixes the policy instant
(default: the snapshot's latest memory or event time).

```sh
# Copy the vault first; the command never writes the copy either.
rsync -a --exclude index.sqlite3 /path/to/vault/ /tmp/vault-snapshot/
memfw eval context /tmp/vault-snapshot \
  --conversations /tmp/conversations-copy.sqlite \
  --usage /tmp/usage-copy.sqlite3 \
  --clearance public,shared --app claude --policy compat_fts \
  --out /tmp/c4-report
# -> /tmp/c4-report/context-eval.json and context-eval.md
```

The tests (`tests/test_context_eval.py`) check every number on a hand-worked
synthetic fixture (`tests/context_eval_fixtures.py`) plus invariants on a
generated one; none of it is real data.

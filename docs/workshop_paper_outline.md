# Workshop paper outline (draft)

All numbers below are copied from `reports/day10_null_result_analysis.md` and `reports/day11_baseline_comparison.md`. Nothing is estimated.

## Title candidates

1. Forgetting Is Hard to Measure: A Null Result on Retention Policies for Personal Agent Memory
2. When Does Forgetting Matter? Budget Pressure, Retrieval Ceilings, and Retention Policies on LoCoMo
3. Verbatim or Summarised? Provenance Trade-offs in Local Personal Agent Memory
4. memfw: A Consent-First, Local Memory Framework and What LoCoMo Can (Not) Tell Us About Forgetting

## Abstract (draft, ~200 words)

Long-term memory for personal agents must decide what to keep and what to forget, yet common benchmarks rarely stress forgetting. We present memfw, a local-first memory framework in which writes keep the user's verbatim text, inferred content is never presented as user speech, and destructive operations can be routed through human approval. On the public LoCoMo benchmark (10 conversations, 1,982 questions with valid evidence), five retention policies (FIFO, LRU, decay, salience, consolidate-then-forget) showed no significant overall difference. Diagnostics suggest why: some policies retain identical memory sets, and retrieval within a 128-token context caps evidence recall (36.56% for BM25 vs. 95.04% for a query-conditioned oracle). Under a 5% budget, salience doubled FIFO's evidence recall (4.36% vs. 2.23%), but this did not survive correction (p = 0.0977). A small local comparison with Mem0 using the same 9B model and embedder found no significant differences on 18 questions, but a clear provenance contrast: 30/30 memfw memories were verbatim, while 21/30 Mem0 memories were derived text attributed to the user without an inference label — a design trade-off rather than a defect. We argue that forgetting needs dedicated, budget-stressed, provenance-aware evaluation.

## Contributions

1. An open, local-first memory framework with explicit provenance and approval-gated destructive operations.
2. A carefully reported null result on five retention policies over full LoCoMo, with power analysis and diagnostics (identical retention sets, retrieval ceiling).
3. Evidence that differences appear only under budget pressure (suggestive, not significant).
4. A same-model local comparison with Mem0 and a rule-based provenance audit.

## Research questions

- RQ1: Do retention policies differ in evidence recall on LoCoMo under realistic budgets?
- RQ2: If not, is it because policies make the same choices, because retrieval caps recall, or because the benchmark lacks forgetting pressure?
- RQ3: How do verbatim-with-provenance and LLM-summarised memory compare in recall, cost, and provenance?

## Method

- Data: public LoCoMo (SHA-256 `79fa87e9…`), official category mapping.
- Metric: evidence-ID recall within a 128-token context (project tokenizer); Day 11 adds LoCoMo-style answer token F1 / adversarial abstention.
- Day 10: five policies, budgets 1/5/10/25%, two access conditions; exact sign-flip tests over 10 conversation clusters with Holm correction; cluster MDE at 80% power.
- Day 11: one conversation (conv-49), first 100 turns, 18 seeded questions; qwen3.5:9b + nomic-embed-text via local Ollama; top-k 5; budgets 100% and 5%; paired bootstrap (2,000) with Holm correction; 30-memory rule-based provenance audit per system.

## Tables (real numbers only)

**Table 1. Day 10, keyness_only condition (FIFO vs. salience).**

| Budget | FIFO | Salience | Δ (pp) | 95% CI (pp) | Holm p | Cluster MDE80 (pp) |
|---|---:|---:|---:|---|---:|---:|
| 1% | 0.21% | 0.89% | +0.67 | [+0.32, +1.06] | 0.2344 | 0.84 |
| 5% | 2.23% | 4.36% | +2.13 | [+1.21, +3.11] | 0.0977 | 2.48 |
| 10% | 5.99% | 7.16% | +1.17 | [-0.21, +2.53] | 1.0000 | 3.02 |
| 25% | 13.01% | 13.55% | +0.53 | [-1.24, +2.25] | 1.0000 | 5.37 |

**Table 2. Day 11, memfw vs. Mem0 (18 questions).**

| System | Retention | Evidence recall | Answer score | Write p50 (s) | Search p50 (s) | Retained memories |
|---|---:|---:|---:|---:|---:|---:|
| Mem0 | 100% | 62.5% | 0.527 | 5.376 | 0.314 | 94 |
| memfw | 100% | 66.7% | 0.459 | 0.032 | 0.372 | 100 |
| Mem0 | 5% | 0.0% | 0.222 | 5.376 | 0.312 | 3 |
| memfw | 5% | 4.2% | 0.180 | 0.032 | 0.331 | 8 |

All paired differences not significant (recall Holm p = 1.000 at both budgets; answer Holm p = 0.982).

**Table 3. Provenance audit (30 sampled memories each).**

| System | Verbatim | Derived, attributed to user, unlabelled | Derived, unlabelled | Novel numbers |
|---|---:|---:|---:|---:|
| memfw | 30 | 0 | 0 | 0 |
| Mem0 | 0 | 21 | 9 | 8 |

## Threats to validity

- Day 11 has 18 questions from one conversation: very low power; category rows are anecdotal.
- Question eligibility (evidence in first 100 turns) biases the Day 11 subset.
- Salience was chosen for Day 11 after seeing Day 10 results.
- memfw retrieval in Day 11 is an aligned embedding layer, not its native BM25/FTS.
- Mem0's 5% condition uses FIFO deletion that we added; Mem0 has no budgeted forgetting.
- One local 9B model with thinking disabled; hosted or larger models may change Mem0 results.
- Evidence recall is not answer accuracy; adversarial abstention credit rewards "unknown".
- No human approvals were exercised (Day 11 deletions were automated); human cost unmeasured.
- Provenance audit is rule-based, not human-judged, and does not assess summary correctness.

## Next experiments

1. Larger LoCoMo subset (all 10 conversations, more questions) for the Mem0 comparison, sized from the observed MDE.
2. A forgetting-sensitive benchmark: long histories exceeding budget, superseded/contradictory facts, questions whose answer depends on what was dropped.
3. Human-confirmation-in-the-loop run: real approvals for writes and deletions, measuring time and disagreement.
4. Native memfw retrieval vs. aligned embedding retrieval ablation.

## Candidate venues (checked 2026-10-21)

| Venue | Status | Notes |
|---|---|---|
| AgentArch 2027 (agentarch.org) | Verified page exists | Covers agent memory/state; deadline listed as 2026-12-20. |
| FrontierIR @ AAAI 2027 (frontier-ir-workshop.github.io) | Verified page exists | Memory retrieval for agents; deadline listed as 2026-11-20 (tight). |
| HAXD 2027 (haxd-conference.org) | Verified CFP exists | Track 5 user-controllable memory, provenance; submission 2027-02-20. |
| CHI 2027 workshops (Pittsburgh, May 10–14, 2027) | Conference verified; specific memory workshop unverified | Watch for workshop CFPs. |
| ACL 2027 workshops (Kyoto, Aug 18–19, 2027) | Conference verified; specific workshop unverified | ARR deadline 2027-01-04 for main track. |
| PALM / MemAgents successors in 2027 | Unverified | PALM ran at NeurIPS 2026; MemAgents at ICLR 2026 — check for 2027 editions. |

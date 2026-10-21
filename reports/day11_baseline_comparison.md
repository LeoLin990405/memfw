# Day 11: memfw vs Mem0 local baseline on public LoCoMo

Status: **completed**. Runtime: 11.7 min.

## Configuration

- Data: public LoCoMo `conv-49` (SHA-256 `79fa87e90f040813…`), first 100 turns, 18 fixed questions, seed 20261021.
- Shared answer/extraction model: `qwen3.5:9b` (digest `56671c2ab9385f9c`), Ollama loopback, think=false, temperature 0, num_ctx 8192.
- Shared embedder: `nomic-embed-text` (digest `0a109f422b47e3a3`).
- top-k 5; context budget 128 tokens (project tokenizer, greedy whole-item fit, same as eval_locomo).
- Retention budgets: [100, 5]% of raw turn UTF-8 bytes.
- memfw: verbatim writes, `salience` forgetting policy (the strongest arm in Day 10 — chosen after seeing Day 10, a possible selection bias), retrieval = cosine top-k over the shared embedder.
- Mem0: `mem0ai` 2.2.1, `Memory.from_config` with Ollama LLM + Ollama embedder + on-disk Qdrant, `add(infer=True)` one turn per call, `search(threshold=0.0)`. Mem0 has no capacity-based forgetting, so the same byte budget is applied by FIFO deletion of its oldest stored memories (by created_at). Session date is prefixed to each turn because OSS `add` rejects the timestamp argument.
- Python 3.11.16, macOS-26.7.1-arm64-arm-64bit; MEM0_TELEMETRY=False; no external API calls.

Command:

```sh
scripts/day11_baseline_comparison.py --run
```

## Results

Evidence recall is the project's existing metric (share of gold evidence turn ids whose memories fit in the context budget). Answer score = LoCoMo-style token F1 (categories 1–4) / abstention credit (category 5) from the shared answer model.

| System | Retention | Evidence recall [95% CI] | Answer score [95% CI] | Write p50/p95 (s) | Search p50/p95 (s) | Retained memories | Text bytes | On-disk bytes |
|---|---:|---|---|---|---|---:|---:|---:|
| mem0 | 100% | 62.5% [42.9%, 82.2%] | 0.527 [0.320, 0.739] | 5.376/8.972 | 0.314/0.331 | 94 | 11585 | 836182 |
| memfw | 100% | 66.7% [50.0%, 84.2%] | 0.459 [0.262, 0.664] | 0.032/0.320 | 0.372/0.390 | 100 | 11067 | 406833 |
| mem0 | 5% | 0.0% [0.0%, 0.0%] | 0.222 [0.056, 0.444] | 5.376/8.972 | 0.312/0.329 | 3 | 342 | 873046 |
| memfw | 5% | 4.2% [0.0%, 14.3%] | 0.180 [0.013, 0.359] | 0.032/0.320 | 0.331/0.345 | 8 | 534 | 129225 |

Write latency is per-turn ingest (Mem0: LLM extraction + update decision + embedding + store; memfw: vault write + one embedding) and is measured once, so it is the same for both retention rows.

### Paired comparisons (memfw − Mem0), Holm-corrected across the two budgets

| Metric | Retention | Difference [95% CI] | p (raw) | p (Holm) | Approx. MDE | Significant? |
|---|---:|---|---:|---:|---:|---|
| Evidence recall | 100pct | 4.2% [-8.3%, 20.8%] | 0.795 | 1.000 | 22.4% | no |
| Evidence recall | 5pct | 4.2% [0.0%, 14.3%] | 0.684 | 1.000 | 15.6% | no |
| Answer score | 100pct | -0.068 [-0.262, 0.111] | 0.491 | 0.982 | 0.280 | no |
| Answer score | 5pct | -0.042 [-0.167, 0.039] | 0.698 | 0.982 | 0.162 | no |

With 18 questions from one conversation, statistical power is low: the approximate minimum detectable difference (paired, α=0.05, power 0.8) is shown per row; differences smaller than that cannot be distinguished from noise.

### By category (exploratory, 2–4 questions each)

| Category | Retention | mem0 recall | memfw recall | mem0 answer | memfw answer |
|---|---:|---:|---:|---:|---:|
| 1 multi_hop (n=4) | 100pct | 70.0% | 60.0% | 0.545 | 0.448 |
| 2 temporal (n=4) | 100pct | 100.0% | 100.0% | 0.393 | 0.143 |
| 3 open_domain (n=2) | 100pct | 0.0% | 0.0% | 0.000 | 0.000 |
| 4 single_hop (n=4) | 100pct | 50.0% | 75.0% | 0.432 | 0.725 |
| 5 adversarial (n=4) | 100pct | 50.0% | 75.0% | 1.000 | 0.750 |
| 1 multi_hop (n=4) | 5pct | 0.0% | 0.0% | 0.000 | 0.000 |
| 2 temporal (n=4) | 5pct | 0.0% | 0.0% | 0.000 | 0.000 |
| 3 open_domain (n=2) | 5pct | 0.0% | 0.0% | 0.000 | 0.000 |
| 4 single_hop (n=4) | 5pct | 0.0% | 0.0% | 0.000 | 0.059 |
| 5 adversarial (n=4) | 5pct | 0.0% | 25.0% | 1.000 | 0.750 |

## Provenance audit (rule-based, public data only)

- R0: memory without a resolvable source turn id -> missing_source_link.
- R1: lower-cased, punctuation-stripped memory text (after removing a leading speaker prefix) is a contiguous substring of its source turn -> verbatim_source_text.
- R2: otherwise derived. If metadata carries an explicit derived/inferred label (keys ['derived', 'inferred', 'is_inference', 'source_type', 'provenance_kind']) -> derived_labelled.
- R3: derived, unlabelled, and the system records it as attributed_to='user' -> derived_attributed_to_user_unlabelled (FLAGGED: inferred text stored as the user's).
- R4: derived, unlabelled, no user attribution -> derived_unlabelled.
- Auxiliary: novel_numbers = memory contains digit-bearing tokens absent from the source turn (e.g., dates resolved by the extractor).

| System | Sampled | Label counts | Flagged (R3) | Novel numbers |
|---|---:|---|---:|---:|
| mem0 | 30 | derived_attributed_to_user_unlabelled=21, derived_unlabelled=9 | 21 | 8 |
| memfw | 30 | verbatim_source_text=30 | 0 | 0 |

Reading this fairly: Mem0 memories are third-person summaries and Mem0 does not claim they are quotations; R3 counts that they are stored with `attributed_to=user` and no machine-readable marker separating extracted/inferred text from what the user actually said. The rule check is automatic, not a human judgement, and does not measure whether a summary is correct.

Mem0 add events: {'ADD': 94}; add errors: 0; retried adds: 0. Ingest wall time: 9.7 min.

## Human confirmation (design property)

- memfw: Explicit add stores the user's verbatim text; staged full-capture candidates require keep/discard via `memfw capture confirm`. Forgetting can be routed through approval proposals, but in this benchmark budget enforcement ran as the mechanism actor and deleted directly (see retention_detail for the proposal count), so no human step was exercised.
- Mem0: add(infer=True) lets the LLM extract, ADD, UPDATE or DELETE memories automatically; mem0ai 2.2.1 OSS has no built-in human confirmation step.

## Limitations

- One public conversation, first 100 turns, 18 questions: very low power; category rows are anecdotal.
- The question set requires all evidence within the first 100 turns (selection bias).
- memfw's retrieval here is an aligned embedding layer added for this comparison, not memfw's native FTS/BM25 retrieval; the comparison isolates storage/forgetting behaviour under a shared retriever.
- Evidence recall for Mem0 credits a memory to the turn whose add() call created it (metadata dia_id); memories merged by UPDATE keep that single link, which can under- or over-credit Mem0.
- Mem0 FIFO eviction is our addition (Mem0 has no budgeted forgetting). How many memories survive a byte budget depends on memory length (Mem0 summaries add resolved dates and names; see the text-bytes column).
- Mem0's on-disk size does not shrink after deletes (Qdrant segments and the history database keep tombstones), so on-disk bytes at 5% are not a retained-size measure.
- Category 5 abstention credit rewards answering 'unknown'; an empty or irrelevant context can therefore score well on adversarial questions.
- A single 9B local model was used for extraction and answering; results with larger or hosted models (Mem0's default) may differ. Think mode was disabled for speed.
- No human-review step was exercised for either system, so its cost is not measured.

## Implications for the workshop paper

- Report this as a small, local, same-model sanity comparison, not a leaderboard result. Only differences marked significant above may be stated as differences.
- The provenance audit is the most distinctive qualitative contrast: verbatim storage plus explicit provenance (memfw) vs. LLM-extracted summaries attributed to the user (Mem0). Frame it as a design trade-off — Mem0's extraction compresses and may generalise better — not as a defect.
- The 5% condition connects to Day 10: budget pressure is where forgetting policy matters; a larger, forgetting-sensitive benchmark is needed before any claim.

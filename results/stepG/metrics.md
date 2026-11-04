# Step G metrics

All numbers are computed from `results/stepG/runs/*/*/questions.jsonl`.

## Arms

memfw, mem0_stock, mem0_custom, mem0_stock_day11ctx

## Answer quality (per-question mean across repeats)

| arm | answer_score | token_f1(cat1-4) | recall(post-fit macro) | recall(post-fit micro) | judge:primary | judge:same_model | questions |
|---|---|---|---|---|---|---|---|
| memfw | 0.4591 | 0.3759 | 69.4% | 66.7% | 44.4% | 38.9% | 18 |
| mem0_stock | 0.4393 | 0.2791 | 65.7% | 66.7% | 38.9% | 38.9% | 18 |
| mem0_custom | 0.4652 | 0.3123 | 81.5% | 83.3% | 44.4% | 44.4% | 18 |
| mem0_stock_day11ctx | 0.5050 | 0.3636 | 75.9% | 79.2% | 50.0% | 50.0% | 18 |

## Efficiency

| arm | ingest_s | write_mean_s | write_p50_s | write_p95_s | retrieval_p50_s | retrieval_p95_s | answer_p50_s | prompt_tokens | completion_tokens | memories | storage_bytes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| memfw | 2.20 | 0.0219 | 0.0193 | 0.0226 | 0.4027 | 0.4142 | 1.2398 | 11484 | 267 | 100.0 | 99633 |
| mem0_stock | 1004.45 | 10.0442 | 10.0720 | 12.6570 | 0.3066 | 0.3260 | 1.2334 | 2716128 | 17667 | 79.0 | 709206 |
| mem0_custom | 999.73 | 9.9970 | 9.5281 | 12.5154 | 0.3033 | 0.3073 | 1.2312 | 2744967 | 11994 | 50.0 | 459350 |
| mem0_stock_day11ctx | 526.68 | 5.2665 | 5.1039 | 7.4684 | 0.3041 | 0.3393 | 1.2377 | 1239423 | 16893 | 87.0 | 778838 |

## Paired comparisons

### memfw vs mem0_stock（primary）

| 指标 | A 均值 | B 均值 | 均值差(A-B) | 95% CI | 精确置换 p | Wilcoxon p | Cohen dz | MDE |
|---|---|---|---|---|---|---|---|---|
| answer_score | 0.4591 | 0.4393 | 0.0198 | [-0.1678, 0.2156] | 0.8867 | 0.9414 | 0.0458 | 0.2851 |
| token_f1(cat1-4) | 0.3759 | 0.2791 | 0.0969 | [-0.0844, 0.3150] | 0.4141 | 0.7422 | 0.2441 | 0.2970 |
| evidence_recall(post-fit macro) | 0.6944 | 0.6574 | 0.0370 | [-0.1481, 0.2222] | 1.0000 | 1.0000 | 0.0869 | 0.2812 |
| judge_primary | 0.4444 | 0.3889 | 0.0556 | [-0.1111, 0.2778] | 1.0000 | 1.0000 | 0.1335 | 0.2747 |
| judge_same_model | 0.3889 | 0.3889 | 0.0000 | [-0.2222, 0.2222] | 1.0000 | 1.0000 | 0.0000 | 0.3201 |

### memfw vs mem0_custom（sensitivity）

| 指标 | A 均值 | B 均值 | 均值差(A-B) | 95% CI | 精确置换 p | Wilcoxon p | Cohen dz | MDE |
|---|---|---|---|---|---|---|---|---|
| answer_score | 0.4591 | 0.4652 | -0.0061 | [-0.1914, 0.1893] | 0.9688 | 0.6797 | -0.0141 | 0.2843 |
| token_f1(cat1-4) | 0.3759 | 0.3123 | 0.0636 | [-0.1168, 0.2837] | 0.5547 | 1.0000 | 0.1583 | 0.3006 |
| evidence_recall(post-fit macro) | 0.6944 | 0.8148 | -0.1204 | [-0.3241, 0.0741] | 0.3125 | 0.3125 | -0.2769 | 0.2868 |
| judge_primary | 0.4444 | 0.4444 | 0.0000 | [-0.2222, 0.2222] | 1.0000 | 1.0000 | 0.0000 | 0.3201 |
| judge_same_model | 0.3889 | 0.4444 | -0.0556 | [-0.2778, 0.1667] | 1.0000 | 1.0000 | -0.1030 | 0.3559 |

### mem0_stock vs mem0_custom（configuration effect）

| 指标 | A 均值 | B 均值 | 均值差(A-B) | 95% CI | 精确置换 p | Wilcoxon p | Cohen dz | MDE |
|---|---|---|---|---|---|---|---|---|
| answer_score | 0.4393 | 0.4652 | -0.0259 | [-0.0593, -0.0023] | 0.1250 | 0.1250 | -0.3960 | 0.0431 |
| token_f1(cat1-4) | 0.2791 | 0.3123 | -0.0333 | [-0.0759, -0.0029] | 0.1250 | 0.1250 | -0.4562 | 0.0546 |
| evidence_recall(post-fit macro) | 0.6574 | 0.8148 | -0.1574 | [-0.3241, -0.0185] | 0.1250 | 0.1250 | -0.4690 | 0.2215 |
| judge_primary | 0.3889 | 0.4444 | -0.0556 | [-0.1667, 0.0000] | 1.0000 | 1.0000 | -0.2357 | 0.1556 |
| judge_same_model | 0.3889 | 0.4444 | -0.0556 | [-0.1667, 0.0000] | 1.0000 | 1.0000 | -0.2357 | 0.1556 |

### memfw vs mem0_stock_day11ctx（Day-11 context replication）

| 指标 | A 均值 | B 均值 | 均值差(A-B) | 95% CI | 精确置换 p | Wilcoxon p | Cohen dz | MDE |
|---|---|---|---|---|---|---|---|---|
| answer_score | 0.4591 | 0.5050 | -0.0459 | [-0.2713, 0.1747] | 0.7422 | 0.7422 | -0.0913 | 0.3320 |
| token_f1(cat1-4) | 0.3759 | 0.3636 | 0.0124 | [-0.2397, 0.2727] | 0.8906 | 0.9844 | 0.0244 | 0.3792 |
| evidence_recall(post-fit macro) | 0.6944 | 0.7593 | -0.0648 | [-0.2963, 0.1667] | 0.6562 | 0.6562 | -0.1275 | 0.3356 |
| judge_primary | 0.4444 | 0.5000 | -0.0556 | [-0.2778, 0.1667] | 1.0000 | 1.0000 | -0.1030 | 0.3559 |
| judge_same_model | 0.3889 | 0.5000 | -0.1111 | [-0.3889, 0.1667] | 0.6875 | 0.6875 | -0.1906 | 0.3847 |

### mem0_stock vs mem0_stock_day11ctx（context-window effect）

| 指标 | A 均值 | B 均值 | 均值差(A-B) | 95% CI | 精确置换 p | Wilcoxon p | Cohen dz | MDE |
|---|---|---|---|---|---|---|---|---|
| answer_score | 0.4393 | 0.5050 | -0.0657 | [-0.1838, 0.0318] | 0.3125 | 0.3750 | -0.2691 | 0.1612 |
| token_f1(cat1-4) | 0.2791 | 0.3636 | -0.0845 | [-0.2323, 0.0405] | 0.3125 | 0.3750 | -0.3059 | 0.2067 |
| evidence_recall(post-fit macro) | 0.6574 | 0.7593 | -0.1019 | [-0.2963, 0.0926] | 0.4375 | 0.4375 | -0.2357 | 0.2852 |
| judge_primary | 0.3889 | 0.5000 | -0.1111 | [-0.3333, 0.1111] | 0.6250 | 0.6250 | -0.2357 | 0.3111 |
| judge_same_model | 0.3889 | 0.5000 | -0.1111 | [-0.3333, 0.1111] | 0.6250 | 0.6250 | -0.2357 | 0.3111 |

## Limits

- Small fixed subset: one public LoCoMo conversation, first 100 turns, 18 questions; results do not represent full LoCoMo.
- Single dataset (LoCoMo) and a single local answer model; absolute scores are model-dependent.
- All retention is 100% for the primary comparison; forgetting is not exercised in this formal run.
- memfw stores verbatim turns by design in this protocol; Mem0 arms run extraction pipelines, so write costs are not symmetric.
- Mean token-F1 and paired token-F1 exclude category-5 adversarial questions (abstention rule instead).
- Two evidence-recall definitions are reported: pre-fit top-k and post-fit (128-token context). The post-fit micro value matches the Day 11 metric definition; the macro value is the paired-analysis unit.
- Primary judge is a different model (qwen3.5:4b); the same-model judge is a sensitivity diagnostic, not ground truth.
- Provenance audit uses run rep1 only; mem0_custom's custom instructions request attributed_to='user', so its R3 rate is partly prompt-forced (mem0_stock is not prompt-forced).
- Paired tests use 18 fixed-subset pairs; the minimum detectable difference is reported. Secondary arms/tests are exploratory and not family-corrected.
- Memory-text form differs by protocol: memfw stores verbatim turns (speaker prefix, long sentences) while Mem0 stores extracted short statements; under the same 128-token budget the information density differs, so recall/answer gaps partly reflect this asymmetry rather than retrieval quality alone.
- Model identity is evidenced by env.txt digests, not by a cryptographic receipt from the model server.

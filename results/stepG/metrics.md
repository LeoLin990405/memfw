# Step G metrics

All numbers are computed from `results/stepG/runs/*/*/questions.jsonl`.

## Answer quality (per-question mean across repeats)

| system | answer_score | token_f1 | judge_accuracy | questions |
|---|---|---|---|---|
| memfw | 0.4591 | 0.3759 | 38.9% | 69.4% | 18 |
| mem0 | 0.4652 | 0.3123 | 44.4% | 81.5% | 18 |

## Efficiency

| system | ingest_s | write_mean_s | write_p50_s | write_p95_s | retrieval_p50_s | retrieval_p95_s | prompt_tokens | completion_tokens | memories | storage_bytes |
|---|---|---|---|---|---|---|---|---|---|---|
| memfw | 2.20 | 0.0219 | 0.0193 | 0.0226 | 0.4027 | 0.4142 | 11484 | 267 | 100.0 | 99633 |
| mem0 | 999.73 | 9.9970 | 9.5281 | 12.5154 | 0.3033 | 0.3073 | 2744967 | 11994 | 50.0 | 459350 |

## Paired comparison (primary metric: answer_score)

| n pairs | memfw | mem0 | mean diff | 95% CI | exact p | Wilcoxon p | Cohen dz | rank-biserial | MDE |
|---|---|---|---|---|---|---|---|---|---|
| 18 | 0.4591 | 0.4652 | -0.0061 | [-0.1914 , 0.1893] | 0.9688 | 0.6797 | -0.0141 | -0.1778 | 0.2843 |

### Secondary paired comparisons

| metric | memfw | mem0 | mean diff | 95% CI | exact p | Wilcoxon p | Cohen dz | MDE |
|---|---|---|---|---|---|---|---|---|
| token_f1(cat1-4) | 0.3759 | 0.3123 | 0.0636 | [-0.1168, 0.2837] | 0.5547 | 1.0000 | 0.1583 | 0.3006 |
| LLM judge(per-question) | 0.3889 | 0.4444 | -0.0556 | [-0.2778, 0.1667] | 1.0000 | 1.0000 | -0.1030 | 0.3559 |
| evidence_recall(post-fit macro) | 0.6944 | 0.8148 | -0.1204 | [-0.3241, 0.0741] | 0.3125 | 0.3125 | -0.2769 | 0.2868 |

## Limits

- Small fixed subset: one public LoCoMo conversation, first 100 turns, 18 questions; results do not represent full LoCoMo.
- Single dataset (LoCoMo) and a single local model; absolute scores are model-dependent.
- All retention is 100% for the primary comparison; forgetting is not exercised in this formal run.
- memfw stores verbatim turns by design in this protocol; Mem0 runs its extraction/update pipeline (infer=True), so write costs are not symmetric.
- The judge uses the same local model as the answerer; judge accuracy is a secondary diagnostic, not ground truth.
- Paired tests have 18 pairs; the minimum detectable difference is reported and is larger than several observed gaps.
- mean_token_f1 and the paired token-F1 test exclude category-5 adversarial questions, whose gold answer is a trick answer; they use the abstention rule.
- Two evidence-recall definitions are reported: pre-fit top-k macro recall and post-fit (128-token context) macro/micro recall; only the post-fit micro value matches the Day 11 metric definition.
- Provenance audit uses run rep1 only; mem0's custom extraction instructions force attributed_to='user', so its R3 flag is by construction (rubric R3).
- Four paired tests are reported without family correction; treat secondary tests as exploratory.
- Model identity is evidenced by env.txt digests, not by a cryptographic receipt from the model server.

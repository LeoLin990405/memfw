# Day 10: Why the five LoCoMo retention policies showed no clear difference

## Finding

The Day 7 result is a bounded failure to detect an overall policy difference on this corpus and these retrieval settings. It does not establish equivalence. FIFO and LRU retained identical IDs in 10/10 no-access samples at 10% and 10/10 at 25%. Other policy pairs ranged from strong overlap to low overlap, which may contribute to some small observed gaps. The benchmark has only ten conversation clusters, which limits sensitivity but does not make point estimates close. The score is evidence-ID recall, not answer accuracy.
Even before retention, the default full-corpus BM25 retrieval recalled 36.56% of evidence IDs within the fixed 128-token context (1,031/2,820); the query-conditioned oracle reached 95.04% under the same context. This points to a retrieval/context ceiling as one possible limit on policy separation; it does not explain or prove a zero policy effect.

Category 3 has 92 valid questions, which makes its category-specific estimates especially imprecise.

## Dataset and category mix

Public local input SHA-256: `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`. The input remains outside the repository. The analysis uses 10 conversations, 5,882 turns, and 1,982 questions with valid evidence (2,820 evidence IDs). Official category mapping: 1 multi-hop, 2 temporal, 3 open-domain, 4 single-hop, 5 adversarial. Category results are exploratory because of sparse counts and multiple comparison families.

| Category | Valid questions | Evidence IDs |
|---|---:|---:|
| 1 multi-hop | 282 | 882 |
| 2 temporal | 321 | 375 |
| 3 open-domain | 92 | 208 |
| 4 single-hop | 841 | 895 |
| 5 adversarial | 446 | 460 |

## Category outcomes and power

For each of 10%, 25%, 5%, and 1%, the paired comparison uses the five BM25 policy arms. The JSON companion gives each category’s recall, paired delta and interval, within-category Holm p-value, and an additional Holm correction across all 50 category contrasts per condition/budget. This larger correction is conservative; no category is treated as confirmatory. The official category mapping is documented in the [LoCoMo evaluator](https://github.com/snap-research/locomo/blob/main/task_eval/evaluation.py#L186-L202) and its [official repository category mapping discussion](https://github.com/snap-research/locomo/issues/29).

Power is a normal paired-influence MDE approximation at 80% power. It reports question-independence and conversation-cluster standard errors, conditional on these ten conversations. The cluster Bonferroni MDE uses alpha=.05/10 for ten overall policy contrasts. Identical question outcomes have an unidentifiable MDE (null in JSON), not a true MDE of zero. With only ten clusters, the cluster approximation is itself uncertain. Existing bootstrap p-values are legacy sign-tail diagnostics and are not null-centered tests; intervals and p-values condition on these ten conversations.

| Condition/budget | FIFO recall | Salience recall | Salience − FIFO (pp) | 95% paired CI (pp) | Exact sign-flip Holm p | Significant overall contrasts / 10 | 5-policy recall range (pp) | Cluster MDE80 (pp) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| keyness_only/10% | 5.99% | 7.16% | +1.17 | [-0.21, +2.53] | 1.0000 | 0/10 | 1.17 | 3.02 |
| keyness_only/25% | 13.01% | 13.55% | +0.53 | [-1.24, +2.25] | 1.0000 | 0/10 | 0.64 | 5.37 |
| keyness_only/1% | 0.21% | 0.89% | +0.67 | [+0.32, +1.06] | 0.2344 | 0/10 | 0.67 | 0.84 |
| keyness_only/5% | 2.23% | 4.36% | +2.13 | [+1.21, +3.11] | 0.0977 | 0/10 | 2.13 | 2.48 |
| keyness_plus_shared_access/10% | 5.99% | 7.09% | +1.10 | [-0.21, +2.35] | 1.0000 | 0/10 | 1.49 | 2.48 |
| keyness_plus_shared_access/25% | 13.01% | 14.54% | +1.52 | [-0.10, +3.16] | 1.0000 | 0/10 | 1.70 | 4.24 |
| keyness_plus_shared_access/1% | 0.21% | 0.71% | +0.50 | [+0.14, +0.84] | 0.5000 | 0/10 | 0.67 | 0.82 |
| keyness_plus_shared_access/5% | 2.23% | 3.58% | +1.35 | [+0.46, +2.21] | 0.7031 | 0/10 | 1.35 | 2.27 |

The exact two-sided sign-flip test works on the ten conversation-level paired hit-count differences, under the assumption that strategy labels are exchangeable under the null. Holm correction covers the 10 overall contrasts within each condition/budget; the count column reports how many pass .05 in that family. No cross-scope overall family is claimed. The intervals and legacy percentile-derived sign-tail p-values resample questions within the same ten conversations and are descriptive diagnostics. Cluster-robust estimates expose the limitation from having only ten clusters. MDE is a sensitivity scale under the stated normal approximation, not observed power and not evidence that smaller effects are absent.

The 5-policy recall range column compares how separated the arms are at each budget. A tighter cap can widen gaps, leave gaps similar, or collapse all arms toward a low-recall floor; no monotonic increase is assumed.

## Category recall detail

The table reports evidence-ID recall by the five official question types for every condition and budget.

| Condition/budget | Type | Questions | Evidence IDs | FIFO | LRU | Decay | Salience | Consolidate |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| keyness_only/10% | 1 multi-hop | 282 | 882 | 2.72% | 2.72% | 2.83% | 3.97% | 2.72% |
| keyness_only/10% | 2 temporal | 321 | 375 | 6.13% | 6.13% | 5.60% | 7.47% | 6.13% |
| keyness_only/10% | 3 open-domain | 92 | 208 | 4.81% | 4.81% | 4.81% | 6.25% | 4.81% |
| keyness_only/10% | 4 single-hop | 841 | 895 | 7.93% | 7.93% | 8.27% | 9.05% | 7.93% |
| keyness_only/10% | 5 adversarial | 446 | 460 | 8.91% | 8.91% | 9.35% | 9.78% | 8.91% |
| keyness_only/25% | 1 multi-hop | 282 | 882 | 4.42% | 4.42% | 4.76% | 6.35% | 4.42% |
| keyness_only/25% | 2 temporal | 321 | 375 | 13.60% | 13.60% | 13.60% | 15.20% | 13.60% |
| keyness_only/25% | 3 open-domain | 92 | 208 | 5.29% | 5.29% | 4.81% | 9.13% | 5.29% |
| keyness_only/25% | 4 single-hop | 841 | 895 | 19.33% | 19.33% | 18.55% | 17.65% | 19.33% |
| keyness_only/25% | 5 adversarial | 446 | 460 | 20.22% | 20.22% | 20.65% | 20.00% | 20.22% |
| keyness_only/1% | 1 multi-hop | 282 | 882 | 0.11% | 0.11% | 0.45% | 0.79% | 0.11% |
| keyness_only/1% | 2 temporal | 321 | 375 | 0.00% | 0.00% | 0.00% | 0.27% | 0.00% |
| keyness_only/1% | 3 open-domain | 92 | 208 | 0.48% | 0.48% | 0.00% | 0.48% | 0.48% |
| keyness_only/1% | 4 single-hop | 841 | 895 | 0.34% | 0.34% | 0.67% | 1.23% | 0.34% |
| keyness_only/1% | 5 adversarial | 446 | 460 | 0.22% | 0.22% | 0.65% | 1.09% | 0.22% |
| keyness_only/5% | 1 multi-hop | 282 | 882 | 1.25% | 1.25% | 1.25% | 1.93% | 1.25% |
| keyness_only/5% | 2 temporal | 321 | 375 | 3.20% | 3.20% | 1.87% | 3.73% | 3.20% |
| keyness_only/5% | 3 open-domain | 92 | 208 | 2.40% | 2.40% | 3.37% | 4.33% | 2.40% |
| keyness_only/5% | 4 single-hop | 841 | 895 | 2.79% | 2.79% | 3.80% | 5.81% | 2.79% |
| keyness_only/5% | 5 adversarial | 446 | 460 | 2.17% | 2.17% | 3.70% | 6.74% | 2.17% |
| keyness_plus_shared_access/10% | 1 multi-hop | 282 | 882 | 2.72% | 2.61% | 2.27% | 4.65% | 2.72% |
| keyness_plus_shared_access/10% | 2 temporal | 321 | 375 | 6.13% | 6.13% | 5.60% | 7.73% | 6.13% |
| keyness_plus_shared_access/10% | 3 open-domain | 92 | 208 | 4.81% | 3.37% | 3.37% | 5.77% | 4.81% |
| keyness_plus_shared_access/10% | 4 single-hop | 841 | 895 | 7.93% | 7.82% | 8.16% | 8.83% | 7.93% |
| keyness_plus_shared_access/10% | 5 adversarial | 446 | 460 | 8.91% | 7.61% | 8.48% | 8.48% | 8.91% |
| keyness_plus_shared_access/25% | 1 multi-hop | 282 | 882 | 4.42% | 4.20% | 4.42% | 6.46% | 4.42% |
| keyness_plus_shared_access/25% | 2 temporal | 321 | 375 | 13.60% | 14.13% | 13.07% | 17.33% | 13.60% |
| keyness_plus_shared_access/25% | 3 open-domain | 92 | 208 | 5.29% | 3.85% | 4.33% | 8.17% | 5.29% |
| keyness_plus_shared_access/25% | 4 single-hop | 841 | 895 | 19.33% | 19.11% | 19.78% | 19.78% | 19.33% |
| keyness_plus_shared_access/25% | 5 adversarial | 446 | 460 | 20.22% | 20.22% | 19.35% | 20.43% | 20.22% |
| keyness_plus_shared_access/1% | 1 multi-hop | 282 | 882 | 0.11% | 0.11% | 0.34% | 0.45% | 0.11% |
| keyness_plus_shared_access/1% | 2 temporal | 321 | 375 | 0.00% | 1.07% | 0.27% | 0.53% | 0.00% |
| keyness_plus_shared_access/1% | 3 open-domain | 92 | 208 | 0.48% | 0.96% | 0.48% | 0.48% | 0.48% |
| keyness_plus_shared_access/1% | 4 single-hop | 841 | 895 | 0.34% | 0.56% | 1.56% | 1.01% | 0.34% |
| keyness_plus_shared_access/1% | 5 adversarial | 446 | 460 | 0.22% | 1.09% | 1.30% | 0.87% | 0.22% |
| keyness_plus_shared_access/5% | 1 multi-hop | 282 | 882 | 1.25% | 1.36% | 1.81% | 2.04% | 1.25% |
| keyness_plus_shared_access/5% | 2 temporal | 321 | 375 | 3.20% | 2.93% | 2.40% | 4.53% | 3.20% |
| keyness_plus_shared_access/5% | 3 open-domain | 92 | 208 | 2.40% | 2.88% | 2.40% | 3.37% | 2.40% |
| keyness_plus_shared_access/5% | 4 single-hop | 841 | 895 | 2.79% | 4.36% | 4.58% | 4.47% | 2.79% |
| keyness_plus_shared_access/5% | 5 adversarial | 446 | 460 | 2.17% | 4.35% | 4.57% | 4.13% | 2.17% |

## Did the budget or forgetting pressure activate?

Each ratio is a per-conversation UTF-8 body-byte cap, independently applied to both signal conditions. Counts and bytes below sum separate sample stores; they are not one pooled vault. A trigger means fewer turns retained than existed at intake. The 1% and 5% runs include all five policies, both conditions, all questions, and fixed seed.

| Budget | Triggered stores / sample-condition (policy counts) | Retained bytes summed across 5 policies and 2 conditions | Pairwise retained-ID Jaccard (mean range) |
|---|---|---|---|
| 10% | fifo_bm25: 20/20, lru_bm25: 20/20, decay_bm25: 20/20, salience_bm25: 20/20, consolidate_then_forget_bm25: 20/20 | 719,149 summed across policies and both conditions | 0.042–0.997 |
| 25% | fifo_bm25: 20/20, lru_bm25: 20/20, decay_bm25: 20/20, salience_bm25: 20/20, consolidate_then_forget_bm25: 20/20 | 1,810,621 summed across policies and both conditions | 0.126–0.999 |
| 1% | fifo_bm25: 20/20, lru_bm25: 20/20, decay_bm25: 20/20, salience_bm25: 20/20, consolidate_then_forget_bm25: 20/20 | 63,211 summed across policies and both conditions | 0.006–1.000 |
| 5% | fifo_bm25: 20/20, lru_bm25: 20/20, decay_bm25: 20/20, salience_bm25: 20/20, consolidate_then_forget_bm25: 20/20 | 356,688 summed across policies and both conditions | 0.020–1.000 |

High Jaccard means the strategies often kept the same turns. The tighter caps may push several strategies to a floor where almost no useful turns remain, so a lower budget does not guarantee a larger measured gap. Mean retained body bytes per turn are in the JSON companion; different retained counts at the same byte cap may reflect turn length, but this is not a causal explanation. Full per-policy bytes, counts, trigger counts and pairwise Jaccards are also in the JSON companion.

## Per-sample retention detail

Each row is one conversation × signal condition × byte-budget store. `Retained turns / bytes` is shown separately for every policy. A budget trigger means the policy retained fewer turns than the sample intake count. These are independent sample-local capacities, not pooled budgets.

| Sample | Intake turns | Condition | Budget | Capacity (B) | FIFO n/B | LRU n/B | Decay n/B | Salience n/B | Consolidate n/B |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|
| conv-26 | 419 | keyness_only | 10% | 5,770 | 46 / 5,727 (triggered) | 46 / 5,727 (triggered) | 42 / 5,714 (triggered) | 29 / 5,708 (triggered) | 46 / 5,727 (triggered) |
| conv-26 | 419 | keyness_plus_shared_access | 10% | 5,770 | 46 / 5,727 (triggered) | 39 / 5,757 (triggered) | 39 / 5,742 (triggered) | 28 / 5,745 (triggered) | 46 / 5,727 (triggered) |
| conv-30 | 369 | keyness_only | 10% | 4,359 | 34 / 4,229 (triggered) | 34 / 4,229 (triggered) | 34 / 4,168 (triggered) | 22 / 4,095 (triggered) | 34 / 4,229 (triggered) |
| conv-30 | 369 | keyness_plus_shared_access | 10% | 4,359 | 34 / 4,229 (triggered) | 43 / 4,341 (triggered) | 38 / 4,203 (triggered) | 21 / 3,994 (triggered) | 34 / 4,229 (triggered) |
| conv-41 | 663 | keyness_only | 10% | 8,974 | 72 / 8,963 (triggered) | 72 / 8,963 (triggered) | 71 / 8,909 (triggered) | 48 / 8,962 (triggered) | 72 / 8,963 (triggered) |
| conv-41 | 663 | keyness_plus_shared_access | 10% | 8,974 | 72 / 8,963 (triggered) | 67 / 8,961 (triggered) | 63 / 8,956 (triggered) | 46 / 8,809 (triggered) | 72 / 8,963 (triggered) |
| conv-42 | 629 | keyness_only | 10% | 7,185 | 60 / 7,175 (triggered) | 60 / 7,175 (triggered) | 53 / 7,124 (triggered) | 41 / 7,012 (triggered) | 60 / 7,175 (triggered) |
| conv-42 | 629 | keyness_plus_shared_access | 10% | 7,185 | 60 / 7,175 (triggered) | 56 / 7,153 (triggered) | 51 / 6,982 (triggered) | 43 / 7,162 (triggered) | 60 / 7,175 (triggered) |
| conv-43 | 680 | keyness_only | 10% | 8,631 | 79 / 8,605 (triggered) | 79 / 8,605 (triggered) | 77 / 8,551 (triggered) | 38 / 8,451 (triggered) | 79 / 8,605 (triggered) |
| conv-43 | 680 | keyness_plus_shared_access | 10% | 8,631 | 79 / 8,605 (triggered) | 81 / 8,533 (triggered) | 66 / 8,527 (triggered) | 41 / 8,445 (triggered) | 79 / 8,605 (triggered) |
| conv-44 | 675 | keyness_only | 10% | 8,022 | 72 / 7,952 (triggered) | 72 / 7,952 (triggered) | 57 / 7,990 (triggered) | 39 / 7,864 (triggered) | 72 / 7,952 (triggered) |
| conv-44 | 675 | keyness_plus_shared_access | 10% | 8,022 | 72 / 7,952 (triggered) | 59 / 7,860 (triggered) | 55 / 7,976 (triggered) | 41 / 8,007 (triggered) | 72 / 7,952 (triggered) |
| conv-47 | 689 | keyness_only | 10% | 8,100 | 74 / 7,860 (triggered) | 74 / 7,860 (triggered) | 74 / 8,090 (triggered) | 45 / 8,017 (triggered) | 74 / 8,088 (triggered) |
| conv-47 | 689 | keyness_plus_shared_access | 10% | 8,100 | 74 / 7,860 (triggered) | 72 / 8,049 (triggered) | 71 / 8,100 (triggered) | 44 / 8,029 (triggered) | 74 / 8,088 (triggered) |
| conv-48 | 681 | keyness_only | 10% | 7,325 | 72 / 7,320 (triggered) | 72 / 7,320 (triggered) | 58 / 7,166 (triggered) | 32 / 7,305 (triggered) | 72 / 7,320 (triggered) |
| conv-48 | 681 | keyness_plus_shared_access | 10% | 7,325 | 72 / 7,320 (triggered) | 65 / 7,279 (triggered) | 56 / 7,273 (triggered) | 34 / 7,134 (triggered) | 72 / 7,320 (triggered) |
| conv-49 | 509 | keyness_only | 10% | 6,247 | 48 / 6,222 (triggered) | 48 / 6,222 (triggered) | 42 / 6,207 (triggered) | 31 / 6,155 (triggered) | 48 / 6,222 (triggered) |
| conv-49 | 509 | keyness_plus_shared_access | 10% | 6,247 | 48 / 6,222 (triggered) | 55 / 6,082 (triggered) | 51 / 6,006 (triggered) | 39 / 6,188 (triggered) | 48 / 6,222 (triggered) |
| conv-50 | 568 | keyness_only | 10% | 8,074 | 59 / 8,029 (triggered) | 59 / 8,029 (triggered) | 52 / 7,885 (triggered) | 40 / 7,824 (triggered) | 59 / 8,029 (triggered) |
| conv-50 | 568 | keyness_plus_shared_access | 10% | 8,074 | 59 / 8,029 (triggered) | 56 / 7,929 (triggered) | 53 / 7,892 (triggered) | 40 / 7,972 (triggered) | 59 / 8,029 (triggered) |
| conv-26 | 419 | keyness_only | 25% | 14,426 | 104 / 14,370 (triggered) | 104 / 14,370 (triggered) | 98 / 14,423 (triggered) | 72 / 14,299 (triggered) | 104 / 14,370 (triggered) |
| conv-26 | 419 | keyness_plus_shared_access | 25% | 14,426 | 104 / 14,370 (triggered) | 103 / 14,357 (triggered) | 100 / 14,343 (triggered) | 83 / 14,349 (triggered) | 104 / 14,370 (triggered) |
| conv-30 | 369 | keyness_only | 25% | 10,899 | 101 / 10,843 (triggered) | 101 / 10,843 (triggered) | 95 / 10,775 (triggered) | 61 / 10,834 (triggered) | 101 / 10,843 (triggered) |
| conv-30 | 369 | keyness_plus_shared_access | 25% | 10,899 | 101 / 10,843 (triggered) | 100 / 10,822 (triggered) | 95 / 10,830 (triggered) | 75 / 10,776 (triggered) | 101 / 10,843 (triggered) |
| conv-41 | 663 | keyness_only | 25% | 22,435 | 172 / 22,329 (triggered) | 172 / 22,329 (triggered) | 172 / 22,336 (triggered) | 126 / 22,343 (triggered) | 172 / 22,329 (triggered) |
| conv-41 | 663 | keyness_plus_shared_access | 25% | 22,435 | 172 / 22,329 (triggered) | 173 / 22,350 (triggered) | 168 / 22,430 (triggered) | 135 / 22,278 (triggered) | 172 / 22,329 (triggered) |
| conv-42 | 629 | keyness_only | 25% | 17,962 | 154 / 17,885 (triggered) | 154 / 17,885 (triggered) | 152 / 17,955 (triggered) | 108 / 17,950 (triggered) | 154 / 17,885 (triggered) |
| conv-42 | 629 | keyness_plus_shared_access | 25% | 17,962 | 154 / 17,885 (triggered) | 155 / 17,949 (triggered) | 148 / 17,893 (triggered) | 117 / 17,922 (triggered) | 154 / 17,885 (triggered) |
| conv-43 | 680 | keyness_only | 25% | 21,577 | 185 / 21,570 (triggered) | 185 / 21,570 (triggered) | 181 / 21,555 (triggered) | 115 / 21,540 (triggered) | 185 / 21,570 (triggered) |
| conv-43 | 680 | keyness_plus_shared_access | 25% | 21,577 | 185 / 21,570 (triggered) | 182 / 21,313 (triggered) | 177 / 21,512 (triggered) | 118 / 21,542 (triggered) | 185 / 21,570 (triggered) |
| conv-44 | 675 | keyness_only | 25% | 20,056 | 161 / 19,995 (triggered) | 161 / 19,995 (triggered) | 161 / 20,031 (triggered) | 115 / 20,029 (triggered) | 161 / 19,995 (triggered) |
| conv-44 | 675 | keyness_plus_shared_access | 25% | 20,056 | 161 / 19,995 (triggered) | 163 / 20,023 (triggered) | 159 / 19,899 (triggered) | 120 / 19,861 (triggered) | 161 / 19,995 (triggered) |
| conv-47 | 689 | keyness_only | 25% | 20,250 | 177 / 20,228 (triggered) | 177 / 20,228 (triggered) | 171 / 20,121 (triggered) | 118 / 20,242 (triggered) | 176 / 20,213 (triggered) |
| conv-47 | 689 | keyness_plus_shared_access | 25% | 20,250 | 177 / 20,228 (triggered) | 177 / 20,159 (triggered) | 175 / 20,211 (triggered) | 133 / 20,104 (triggered) | 176 / 20,213 (triggered) |
| conv-48 | 681 | keyness_only | 25% | 18,314 | 180 / 18,306 (triggered) | 180 / 18,306 (triggered) | 159 / 18,231 (triggered) | 96 / 18,311 (triggered) | 180 / 18,306 (triggered) |
| conv-48 | 681 | keyness_plus_shared_access | 25% | 18,314 | 180 / 18,306 (triggered) | 167 / 18,209 (triggered) | 157 / 18,187 (triggered) | 113 / 18,244 (triggered) | 180 / 18,306 (triggered) |
| conv-49 | 509 | keyness_only | 25% | 15,618 | 128 / 15,602 (triggered) | 128 / 15,602 (triggered) | 125 / 15,614 (triggered) | 83 / 15,615 (triggered) | 128 / 15,602 (triggered) |
| conv-49 | 509 | keyness_plus_shared_access | 25% | 15,618 | 128 / 15,602 (triggered) | 127 / 15,567 (triggered) | 125 / 15,392 (triggered) | 112 / 15,616 (triggered) | 128 / 15,602 (triggered) |
| conv-50 | 568 | keyness_only | 25% | 20,187 | 143 / 20,068 (triggered) | 143 / 20,068 (triggered) | 137 / 20,158 (triggered) | 100 / 19,969 (triggered) | 143 / 20,068 (triggered) |
| conv-50 | 568 | keyness_plus_shared_access | 25% | 20,187 | 143 / 20,068 (triggered) | 144 / 20,121 (triggered) | 139 / 20,093 (triggered) | 111 / 19,988 (triggered) | 143 / 20,068 (triggered) |
| conv-26 | 419 | keyness_only | 1% | 577 | 5 / 498 (triggered) | 5 / 498 (triggered) | 2 / 267 (triggered) | 1 / 418 (triggered) | 5 / 498 (triggered) |
| conv-26 | 419 | keyness_plus_shared_access | 1% | 577 | 5 / 498 (triggered) | 3 / 563 (triggered) | 3 / 484 (triggered) | 2 / 547 (triggered) | 5 / 498 (triggered) |
| conv-30 | 369 | keyness_only | 1% | 435 | 7 / 430 (triggered) | 7 / 430 (triggered) | 1 / 333 (triggered) | 1 / 333 (triggered) | 7 / 430 (triggered) |
| conv-30 | 369 | keyness_plus_shared_access | 1% | 435 | 7 / 430 (triggered) | 5 / 380 (triggered) | 5 / 373 (triggered) | 3 / 284 (triggered) | 7 / 430 (triggered) |
| conv-41 | 663 | keyness_only | 1% | 897 | 6 / 803 (triggered) | 6 / 803 (triggered) | 6 / 815 (triggered) | 3 / 779 (triggered) | 6 / 803 (triggered) |
| conv-41 | 663 | keyness_plus_shared_access | 1% | 897 | 6 / 803 (triggered) | 8 / 887 (triggered) | 6 / 746 (triggered) | 3 / 601 (triggered) | 6 / 803 (triggered) |
| conv-42 | 629 | keyness_only | 1% | 718 | 7 / 659 (triggered) | 7 / 659 (triggered) | 4 / 636 (triggered) | 4 / 676 (triggered) | 7 / 659 (triggered) |
| conv-42 | 629 | keyness_plus_shared_access | 1% | 718 | 7 / 659 (triggered) | 5 / 649 (triggered) | 7 / 701 (triggered) | 3 / 540 (triggered) | 7 / 659 (triggered) |
| conv-43 | 680 | keyness_only | 1% | 863 | 6 / 736 (triggered) | 6 / 736 (triggered) | 6 / 832 (triggered) | 3 / 819 (triggered) | 6 / 736 (triggered) |
| conv-43 | 680 | keyness_plus_shared_access | 1% | 863 | 6 / 736 (triggered) | 5 / 697 (triggered) | 6 / 795 (triggered) | 3 / 808 (triggered) | 6 / 736 (triggered) |
| conv-44 | 675 | keyness_only | 1% | 802 | 5 / 602 (triggered) | 5 / 602 (triggered) | 3 / 492 (triggered) | 3 / 663 (triggered) | 5 / 602 (triggered) |
| conv-44 | 675 | keyness_plus_shared_access | 1% | 802 | 5 / 602 (triggered) | 6 / 706 (triggered) | 6 / 753 (triggered) | 3 / 777 (triggered) | 5 / 602 (triggered) |
| conv-47 | 689 | keyness_only | 1% | 810 | 9 / 791 (triggered) | 9 / 791 (triggered) | 5 / 575 (triggered) | 2 / 435 (triggered) | 9 / 791 (triggered) |
| conv-47 | 689 | keyness_plus_shared_access | 1% | 810 | 9 / 791 (triggered) | 9 / 770 (triggered) | 5 / 765 (triggered) | 3 / 674 (triggered) | 9 / 791 (triggered) |
| conv-48 | 681 | keyness_only | 1% | 732 | 7 / 710 (triggered) | 7 / 710 (triggered) | 2 / 563 (triggered) | 2 / 639 (triggered) | 7 / 710 (triggered) |
| conv-48 | 681 | keyness_plus_shared_access | 1% | 732 | 7 / 710 (triggered) | 4 / 570 (triggered) | 6 / 711 (triggered) | 2 / 505 (triggered) | 7 / 710 (triggered) |
| conv-49 | 509 | keyness_only | 1% | 624 | 6 / 546 (triggered) | 6 / 546 (triggered) | 2 / 542 (triggered) | 3 / 569 (triggered) | 6 / 546 (triggered) |
| conv-49 | 509 | keyness_plus_shared_access | 1% | 624 | 6 / 546 (triggered) | 5 / 622 (triggered) | 4 / 496 (triggered) | 4 / 547 (triggered) | 6 / 546 (triggered) |
| conv-50 | 568 | keyness_only | 1% | 807 | 5 / 671 (triggered) | 5 / 671 (triggered) | 4 / 789 (triggered) | 3 / 725 (triggered) | 5 / 671 (triggered) |
| conv-50 | 568 | keyness_plus_shared_access | 1% | 807 | 5 / 671 (triggered) | 5 / 783 (triggered) | 4 / 727 (triggered) | 3 / 620 (triggered) | 5 / 671 (triggered) |
| conv-26 | 419 | keyness_only | 5% | 2,885 | 19 / 2,822 (triggered) | 19 / 2,822 (triggered) | 16 / 2,849 (triggered) | 11 / 2,765 (triggered) | 19 / 2,822 (triggered) |
| conv-26 | 419 | keyness_plus_shared_access | 5% | 2,885 | 19 / 2,822 (triggered) | 20 / 2,732 (triggered) | 19 / 2,818 (triggered) | 14 / 2,680 (triggered) | 19 / 2,822 (triggered) |
| conv-30 | 369 | keyness_only | 5% | 2,179 | 23 / 2,138 (triggered) | 23 / 2,138 (triggered) | 12 / 2,078 (triggered) | 10 / 2,030 (triggered) | 23 / 2,138 (triggered) |
| conv-30 | 369 | keyness_plus_shared_access | 5% | 2,179 | 23 / 2,138 (triggered) | 19 / 2,139 (triggered) | 21 / 2,065 (triggered) | 11 / 2,129 (triggered) | 23 / 2,138 (triggered) |
| conv-41 | 663 | keyness_only | 5% | 4,487 | 38 / 4,447 (triggered) | 38 / 4,447 (triggered) | 34 / 4,478 (triggered) | 22 / 4,416 (triggered) | 38 / 4,447 (triggered) |
| conv-41 | 663 | keyness_plus_shared_access | 5% | 4,487 | 38 / 4,447 (triggered) | 36 / 4,454 (triggered) | 32 / 4,321 (triggered) | 22 / 4,431 (triggered) | 38 / 4,447 (triggered) |
| conv-42 | 629 | keyness_only | 5% | 3,592 | 31 / 3,544 (triggered) | 31 / 3,544 (triggered) | 20 / 3,490 (triggered) | 18 / 3,308 (triggered) | 31 / 3,544 (triggered) |
| conv-42 | 629 | keyness_plus_shared_access | 5% | 3,592 | 31 / 3,544 (triggered) | 27 / 3,434 (triggered) | 25 / 3,446 (triggered) | 21 / 3,358 (triggered) | 31 / 3,544 (triggered) |
| conv-43 | 680 | keyness_only | 5% | 4,315 | 43 / 4,299 (triggered) | 43 / 4,299 (triggered) | 41 / 4,224 (triggered) | 17 / 4,304 (triggered) | 43 / 4,299 (triggered) |
| conv-43 | 680 | keyness_plus_shared_access | 5% | 4,315 | 43 / 4,299 (triggered) | 41 / 4,247 (triggered) | 33 / 4,188 (triggered) | 21 / 4,250 (triggered) | 43 / 4,299 (triggered) |
| conv-44 | 675 | keyness_only | 5% | 4,011 | 29 / 3,931 (triggered) | 29 / 3,931 (triggered) | 27 / 4,004 (triggered) | 19 / 4,009 (triggered) | 29 / 3,931 (triggered) |
| conv-44 | 675 | keyness_plus_shared_access | 5% | 4,011 | 29 / 3,931 (triggered) | 30 / 3,923 (triggered) | 24 / 3,912 (triggered) | 19 / 3,978 (triggered) | 29 / 3,931 (triggered) |
| conv-47 | 689 | keyness_only | 5% | 4,050 | 39 / 4,039 (triggered) | 39 / 4,039 (triggered) | 37 / 4,002 (triggered) | 19 / 3,962 (triggered) | 39 / 4,039 (triggered) |
| conv-47 | 689 | keyness_plus_shared_access | 5% | 4,050 | 39 / 4,039 (triggered) | 39 / 3,913 (triggered) | 35 / 4,044 (triggered) | 19 / 3,891 (triggered) | 39 / 4,039 (triggered) |
| conv-48 | 681 | keyness_only | 5% | 3,662 | 35 / 3,658 (triggered) | 35 / 3,658 (triggered) | 19 / 3,588 (triggered) | 15 / 3,631 (triggered) | 35 / 3,658 (triggered) |
| conv-48 | 681 | keyness_plus_shared_access | 5% | 3,662 | 35 / 3,658 (triggered) | 32 / 3,622 (triggered) | 29 / 3,644 (triggered) | 15 / 3,630 (triggered) | 35 / 3,658 (triggered) |
| conv-49 | 509 | keyness_only | 5% | 3,123 | 22 / 2,978 (triggered) | 22 / 2,978 (triggered) | 21 / 3,103 (triggered) | 14 / 3,054 (triggered) | 22 / 2,978 (triggered) |
| conv-49 | 509 | keyness_plus_shared_access | 5% | 3,123 | 22 / 2,978 (triggered) | 24 / 3,080 (triggered) | 26 / 3,069 (triggered) | 20 / 3,120 (triggered) | 22 / 2,978 (triggered) |
| conv-50 | 568 | keyness_only | 5% | 4,037 | 29 / 3,959 (triggered) | 29 / 3,959 (triggered) | 28 / 3,983 (triggered) | 19 / 4,010 (triggered) | 29 / 3,959 (triggered) |
| conv-50 | 568 | keyness_plus_shared_access | 5% | 4,037 | 29 / 3,959 (triggered) | 29 / 4,000 (triggered) | 28 / 3,994 (triggered) | 17 / 3,813 (triggered) | 29 / 3,959 (triggered) |

## Limits and reproducibility

The experiment authorization metadata was checked read-only with `./.venv/bin/python scripts/approval_metadata_status.py --approval-sha256 25ea0d719e5f4e68baa1c87c5ddfb43e1e3e760b79943f34984d751b65fc1d23`: two matching approval events were found and the sidecar matched that SHA-256. The current query snapshot content was not revalidated. The previous approval-info display was hard-coded false and was corrected in user commit `f02e490`.

No LLM inference or answer generation was used. Consolidation still uses deterministic mock proposals and simulated approvals. The evaluator measures retrieval of gold evidence IDs; it does not measure whether a generated answer is correct. The 10/25% results reuse the frozen Day 7 aggregate with per-question outcomes; 1/5% reruns the deterministic local evaluator. The new pressure results show all strategies were already budget-constrained at 10% and 25%, so 'budget never triggered' does not explain the null result. Tighter caps may create a floor with little evidence left. No user-level or general-population claim follows from this benchmark.

Pressure result SHA-256: `d7f5de39844b127251d17be15e5bea93f77dc621dc922b3faa3e8edac6e7b002`. Seed: `20261018`. Bootstrap repeats: 1000. New pressure evaluation wall time: 150.79 s. Command: `./.venv/bin/python scripts/day10_null_analysis.py --data <MEMFW_DATA_DIR>/locomo10.json --history reports/day7_official.json --pressure-output reports/day10_pressure.json --output reports/day10_null_result_analysis.md --seed 20261018 --bootstrap-repeats 1000`.

Numeric tables and complete paired contrasts: [day10_null_result_analysis.json](day10_null_result_analysis.json).

## Final validation

The final repository gate passed on 2026-10-21: 449 tests passed in 181.15 s. Ruff checks passed, Ruff formatting covered 141 files, and strict mypy covered 121 files. Reproduce with `./.venv/bin/pytest -q`, `./.venv/bin/ruff check .`, `./.venv/bin/ruff format --check .`, and `./.venv/bin/mypy --strict src tests scripts`. The final pressure result remains bound to SHA-256 `d7f5de39844b127251d17be15e5bea93f77dc621dc922b3faa3e8edac6e7b002`.

## Local repository status

A local `git add` attempt was blocked by the environment: Git could not create `.git/index.lock` (`Operation not permitted`). The Day 10 files remain unstaged; no commit or push occurred.

# Step G expanded question set (exploratory)

All 41 eligible questions inside the same 100-turn window; reuses the ingested stores. Not a replacement for the fixed 18-question subset.

| arm | answer_score | token_f1(cat1-4) | recall(macro) | recall(micro) | questions |
|---|---|---|---|---|---|
| mem0_custom | 0.5535 | 0.4417 | 79.7% | 81.2% | 41 |
| mem0_stock | 0.4962 | 0.3660 | 75.2% | 75.0% | 41 |
| mem0_stock_day11ctx | 0.5168 | 0.3932 | 73.6% | 75.0% | 41 |
| memfw | 0.5220 | 0.4324 | 67.1% | 64.6% | 41 |

Judge（qwen3.5:4b）：mem0_custom:56.1%, mem0_stock:51.2%, mem0_stock_day11ctx:56.1%, memfw:53.7%

## memfw vs mem0_stock（primary）

| 指标 | A 均值 | B 均值 | 均值差 | 95% CI | 精确置换 p | Wilcoxon p | Cohen dz | MDE |
|---|---|---|---|---|---|---|---|---|
| answer_score | 0.5220 | 0.4962 | 0.0258 | [-0.0962, 0.1482] | 0.6809 | 0.8798 | 0.0639 | 0.1766 |

## memfw vs mem0_custom（sensitivity）

| 指标 | A 均值 | B 均值 | 均值差 | 95% CI | 精确置换 p | Wilcoxon p | Cohen dz | MDE |
|---|---|---|---|---|---|---|---|---|
| answer_score | 0.5220 | 0.5535 | -0.0314 | [-0.1232, 0.0622] | 0.5340 | 0.2355 | -0.1019 | 0.1349 |

## mem0_stock vs mem0_custom

| 指标 | A 均值 | B 均值 | 均值差 | 95% CI | 精确置换 p | Wilcoxon p | Cohen dz | MDE |
|---|---|---|---|---|---|---|---|---|
| answer_score | 0.4962 | 0.5535 | -0.0573 | [-0.1376, 0.0066] | 0.1597 | 0.2002 | -0.2355 | 0.1063 |

## memfw vs mem0_stock_day11ctx（Day-11 context replication）

| 指标 | A 均值 | B 均值 | 均值差 | 95% CI | 精确置换 p | Wilcoxon p | Cohen dz | MDE |
|---|---|---|---|---|---|---|---|---|
| answer_score | 0.5220 | 0.5168 | 0.0052 | [-0.1086, 0.1203] | 0.9209 | 0.9902 | 0.0139 | 0.1639 |

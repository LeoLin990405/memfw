# memfw 多对话扩展验证（public LoCoMo，10 conversations）

> 由 `eval/stepG_multiconv.py` 生成；只使用公开 LoCoMo 数据，同一 answerer/embedder/judge/top-k/上下文预算。每臂 1 次重复（temperature=0；固定子集已验证重复逐字节一致）。

数据：10 个 conversation × 前 100 轮，共 311 道有完整 gold evidence 的题。

| arm | answer_score | token_f1 | recall micro | judge | write P50 | memories | questions |
|---|---|---|---|---|---|---|---|
| memfw | 0.4383 | 0.3001 | 61.0% | 46.3% | n/a | 100.0 | 311 |
| memfw_v6_hybrid | 0.5179 | 0.4277 | 62.7% | 53.1% | n/a | 100.0 | 311 |
| memfw_v8 | 0.5411 | 0.4618 | 62.7% | 55.6% | n/a | 100.0 | 311 |
| mem0_stock | 0.5469 | 0.4571 | 67.3% | 56.0% | n/a | 82.8 | 209 |
| memoryos | 0.5049 | 0.4067 | 64.0% | 53.7% | n/a | 100.0 | 311 |

## 配对检验（question-level）

| comparison | answer 均值差 | 95% bootstrap CI | 精确置换 p | Wilcoxon p | MDE |
|---|---|---|---|---|---|
| memfw_v6_hybrid vs memfw | 0.0796 | [0.0348, 0.1222] | 0.0007 | 0.0000 | 0.0624 |
| memfw_v6_hybrid vs mem0_stock | -0.0025 | [-0.0617, 0.0582] | 0.9358 | 0.6306 | 0.0860 |
| memfw_v6_hybrid vs memoryos | 0.0130 | [-0.0281, 0.0528] | 0.5264 | 0.1070 | 0.0577 |
| memfw_v8 vs memfw | 0.1028 | [0.0563, 0.1484] | 0.0001 | 0.0000 | 0.0659 |
| memfw_v8 vs mem0_stock | 0.0137 | [-0.0431, 0.0719] | 0.6532 | 0.4841 | 0.0832 |
| memfw_v8 vs memoryos | 0.0362 | [-0.0053, 0.0742] | 0.0728 | 0.0308 | 0.0564 |

## 对话级稳健性（cluster bootstrap + 胜负计数）

| comparison | cluster 均值差 | 对话 cluster 95% CI | wins/losses/ties |
|---|---|---|---|
| memfw_v6_hybrid vs memfw | 0.0796 | [0.0341, 0.1198] | 90/41/180 |
| memfw_v6_hybrid vs mem0_stock | -0.0025 | [-0.0347, 0.0248] | 51/57/101 |
| memfw_v6_hybrid vs memoryos | 0.0130 | [-0.0443, 0.0643] | 72/53/186 |
| memfw_v8 vs memfw | 0.1028 | [0.0483, 0.1564] | 90/38/183 |
| memfw_v8 vs mem0_stock | 0.0137 | [-0.0300, 0.0514] | 54/46/109 |
| memfw_v8 vs memoryos | 0.0362 | [-0.0165, 0.0918] | 69/45/197 |

## 说明与 caveat

- 同一 conversation 内的题目不独立；同时报告 question-level 检验与按 conversation 的 cluster bootstrap。
- 框架臂（Mem0 stock、MemOS general_text）各 1 次重复；LangMem 因单轮写入成本过高未纳入多对话扩展，
固定 18 题对比已显示其显著落后。
- 所有题库/答案均来自公开 LoCoMo；不提交原始数据文件。
- 结论以 pooled 与 per-conversation 的一致性为准，而不是单个子集的点估计。
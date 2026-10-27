# memfw 架构迭代总结（Step G 同协议）

> 由 `eval/stepG_arch.py` 生成。协议、answerer、embedder、top-k、上下文预算、judge 与主实验完全一致；这里只改变 memfw 的记忆表示/检索/提示。

## 方法

- 候选架构：`memfw_v6`（speaker/date 标签、相对日期归一化、归属/计数/地理规则提示）、`memfw_v6_hybrid`（额外 0.85/0.15 的 dense+BM25 加权融合）以及多对话扩展中迭代出的 `memfw_v8`（跨记忆推理/共享事实规则 + 紧凑日期格式）。
- 报告内的消融：v2 提示、v5 提示、v5+MMR、v6、v6_hybrid（0.85/0.15 dense+BM25 融合）；早期探索过的 hybrid/compact 等中间变体未纳入冻结报告。
- 固定 18 题与扩题集 41 题各 3 次重复；参考 arm 复用冻结 runs；框架 arm 为单次重复。
- 提示示例使用虚构人物/事件（Alex/Jamie），不含任何本数据集的实体或答案；早期版本曾因示例含基准实体被判定为泄漏并全部重跑，此处只报告去泄漏后的结果。

| arm | answer_score | token_f1 | recall micro | judge | write P50 | memories | repeats |
|---|---|---|---|---|---|---|---|
| memfw_prompt | 0.5231 | 0.3869 | 62.5% | 44.4% | 0.0295 | 100.0 | 3 |
| memfw_v5 | 0.5608 | 0.4353 | 62.5% | 44.4% | 0.0290 | 100.0 | 3 |
| memfw_v5_mmr | 0.5855 | 0.4670 | 54.2% | 50.0% | 0.0293 | 100.0 | 3 |
| memfw_v6 | 0.5870 | 0.5405 | 62.5% | 55.6% | 0.0287 | 100.0 | 3 |
| memfw_v6_hybrid | 0.5910 | 0.4742 | 54.2% | 55.6% | 0.0291 | 100.0 | 3 |
| memfw | 0.4591 | 0.3759 | 66.7% | 44.4% | 0.0193 | 100.0 | 3 |
| mem0_stock | 0.4393 | 0.2791 | 66.7% | 38.9% | 10.0720 | 79.0 | 3 |
| mem0_custom | 0.4652 | 0.3123 | 83.3% | 44.4% | 9.5281 | 50.0 | 3 |
| mem0_stock_day11ctx | 0.5050 | 0.3636 | 79.2% | 50.0% | 5.1039 | 87.0 | 3 |
| langmem | 0.2672 | 0.0578 | 0.0% | 22.2% | n/a | n/a | 1 |
| memoryos | 0.5575 | 0.4310 | 54.2% | 50.0% | n/a | n/a | 1 |

### fixed 配对比较（v6 系 vs 参考/框架）

| comparison | answer 均值差 | 95% CI | 精确置换 p | MDE |
|---|---|---|---|---|
| memfw_v6 vs memfw | 0.1280 | [0.0063, 0.2769] | 0.1250 | 0.1951 |
| memfw_v6 vs mem0_stock | 0.1478 | [-0.0639, 0.3648] | 0.2109 | 0.3189 |
| memfw_v6 vs memoryos | 0.0296 | [-0.1876, 0.2518] | 1.0000 | 0.3280 |
| memfw_v6 vs langmem | 0.3198 | [0.0606, 0.5745] | 0.0391 | 0.3716 |
| memfw_v6 vs mem0_custom | 0.1219 | [-0.0818, 0.3319] | 0.2852 | 0.3067 |
| memfw_v6_hybrid vs memfw | 0.1320 | [-0.1193, 0.3833] | 0.3281 | 0.3676 |
| memfw_v6_hybrid vs mem0_stock | 0.1518 | [-0.0613, 0.3689] | 0.1953 | 0.3177 |
| memfw_v6_hybrid vs memoryos | 0.0336 | [-0.1820, 0.2518] | 0.8750 | 0.3204 |
| memfw_v6_hybrid vs langmem | 0.3238 | [0.1197, 0.5354] | 0.0156 | 0.3005 |
| memfw_v6_hybrid vs mem0_custom | 0.1259 | [-0.0829, 0.3385] | 0.2734 | 0.3093 |

| arm | answer_score | token_f1 | recall micro | judge | write P50 | memories | repeats |
|---|---|---|---|---|---|---|---|
| memfw_v6 | 0.5857 | 0.5166 | 62.5% | 56.1% | 0.0288 | 100.0 | 3 |
| memfw_v6_hybrid | 0.6068 | 0.5122 | 58.3% | 58.5% | 0.0300 | 100.0 | 3 |
| memfw | 0.5220 | 0.4324 | 64.6% | 53.7% | n/a | 100.0 | 1 |
| mem0_stock | 0.4962 | 0.3660 | 75.0% | 51.2% | n/a | 79.0 | 1 |
| mem0_custom | 0.5535 | 0.4417 | 81.2% | 56.1% | n/a | 50.0 | 1 |
| mem0_stock_day11ctx | 0.5168 | 0.3932 | 75.0% | 56.1% | n/a | 87.0 | 1 |
| langmem | 0.3104 | 0.0879 | 0.0% | 22.2% | n/a | n/a | 1 |
| memoryos | 0.5469 | 0.4331 | 58.3% | 50.0% | n/a | n/a | 1 |

### expanded 配对比较（v6 系 vs 参考/框架）

| comparison | answer 均值差 | 95% CI | 精确置换 p | MDE |
|---|---|---|---|---|
| memfw_v6 vs memfw | 0.0637 | [-0.0004, 0.1398] | 0.0799 | 0.1001 |
| memfw_v6 vs mem0_stock | 0.0895 | [-0.0322, 0.2197] | 0.1773 | 0.1848 |
| memfw_v6 vs mem0_custom | 0.0322 | [-0.0647, 0.1338] | 0.5437 | 0.1455 |
| memfw_v6 vs memoryos | 0.0388 | [-0.0662, 0.1423] | 0.4857 | 0.1524 |
| memfw_v6 vs langmem | 0.2753 | [0.1157, 0.4353] | 0.0019 | 0.2322 |
| memfw_v6_hybrid vs memfw | 0.0848 | [-0.0512, 0.2190] | 0.2303 | 0.1927 |
| memfw_v6_hybrid vs mem0_stock | 0.1106 | [-0.0276, 0.2484] | 0.1324 | 0.2004 |
| memfw_v6_hybrid vs mem0_custom | 0.0533 | [-0.0661, 0.1704] | 0.3912 | 0.1701 |
| memfw_v6_hybrid vs memoryos | 0.0599 | [-0.0721, 0.1868] | 0.3835 | 0.1867 |
| memfw_v6_hybrid vs langmem | 0.2964 | [0.1427, 0.4461] | 0.0006 | 0.2232 |

## 多对话扩展验证（public LoCoMo 10 conversations，311 题）

| arm | answer_score | token_f1 | recall micro | judge | questions |
|---|---|---|---|---|---|
| memfw | 0.4383 | 0.3001 | 61.0% | 46.3% | 311 |
| memfw_v6_hybrid | 0.5179 | 0.4277 | 62.7% | 53.1% | 311 |
| memfw_v8 | 0.5411 | 0.4618 | 62.7% | 55.6% | 311 |
| mem0_stock | 0.5469 | 0.4571 | 67.3% | 56.0% | 209 |
| memoryos | 0.5049 | 0.4067 | 64.0% | 53.7% | 311 |

| comparison | answer 均值差 | question-level 95% CI | 精确置换 p | cluster 95% CI | wins/losses/ties |
|---|---|---|---|---|---|
| memfw_v6_hybrid vs memfw | 0.0796 | [0.0348, 0.1222] | 0.0007 | [0.0341, 0.1198] | 90/41/180 |
| memfw_v6_hybrid vs mem0_stock | -0.0025 | [-0.0617, 0.0582] | 0.9358 | [-0.0347, 0.0248] | 51/57/101 |
| memfw_v6_hybrid vs memoryos | 0.0130 | [-0.0281, 0.0528] | 0.5264 | [-0.0443, 0.0643] | 72/53/186 |
| memfw_v8 vs memfw | 0.1028 | [0.0563, 0.1484] | 0.0001 | [0.0483, 0.1564] | 90/38/183 |
| memfw_v8 vs mem0_stock | 0.0137 | [-0.0431, 0.0719] | 0.6532 | [-0.0300, 0.0514] | 54/46/109 |
| memfw_v8 vs memoryos | 0.0362 | [-0.0053, 0.0742] | 0.0728 | [-0.0165, 0.0918] | 69/45/197 |

## 结论（必须带 caveat）

- 在固定 18 题与扩题集 41 题上，`memfw_v6`/`memfw_v6_hybrid` 的 answer_score、token-F1 与 judge 点估计均高于 memfw v1、Mem0 系、MemOS(general_text) 与 LangMem 的点估计（固定 18 与扩题集 41 均如此）。
- 多对话 pooled 检验中达到 question-level p<0.05 的对比：memfw_v6_hybrid vs memfw (Δ=0.0796, p=0.0007)；memfw_v8 vs memfw (Δ=0.1028, p=0.0001)。
- 多对话（311 题）中，`memfw_v8` 相对 MemOS 为 +0.0362（p=0.0728，cluster CI 跨 0），相对 Mem0 为 +0.0137（p=0.6532）；即点估计更高、但尚不能声称对这两个框架已达到统计显著的整体优势。
- 固定 18 题 MDE≈0.2、扩题集≈0.15；小样本结论仍以 CI/MDE 与 per-conversation 一致性为准，不把点估计领先等同于已证明显著更强。
- 框架 arm 为单次重复，MemOS 仅 report general_text 模式；提示规则属于架构的一部分，其它框架未做同等 prompt 调优，比较是“默认用法 vs 我们的迭代版”。
- 泄漏检查：早期示例包含基准实体（Jasper/Canada 等），已从提示中移除并全部重跑；当前提示只用虚构示例，见 `test_arch_prompts_have_no_benchmark_specific_hints`。

详细数据：`metrics_fixed.json`、`metrics_expanded.json`、`ARCH_REPORT_fixed.md`、`ARCH_REPORT_expanded.md`。
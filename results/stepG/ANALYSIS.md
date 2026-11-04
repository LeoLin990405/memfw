# Step G 数据分析（从冻结产物重算）

> 本文件由 `eval/stepG_analysis.py` 生成，只读取 `results/stepG/` 下的原始 JSONL/JSON；与 `REPORT.md` 互补，不替代主报告。

## 1. 数据与口径

- 数据：公开 LoCoMo，固定子集 `conv-49` 前 100 轮 + 18 题（seed 20261021）。
- 主臂：memfw / mem0_stock / mem0_custom / mem0_stock_day11ctx，每臂 3 次重复；
  框架臂：LangMem / MemOS（general_text）各 1 次重复（exploratory）。
- 分析单元：每题跨重复均值；主 table 的 n=18，扩展集 n=41；分题型样本很小，结论只作描述性。
- 所有数字由 `eval/stepG_analysis.py` 从 `results/stepG/` 原始文件重算，不手填。

- metrics：answer_score 主比较 p=0.8867，MDE=0.2851；重复输出完全确定（见 §11）。

## 2. 分题型表现（固定 18 题，跨重复均值）

| arm | cat1 | cat2 | cat3 | cat4 | cat5 | judge 原始正确率 |
|---|---|---|---|---|---|---|
| memfw | 0.4479 | 0.1429 | 0.0000 | 0.7250 | 0.7500 | 44.4% |
| mem0_stock | 0.5455 | 0.2063 | 0.0000 | 0.2250 | 1.0000 | 38.9% |
| mem0_custom | 0.5800 | 0.2779 | 0.0000 | 0.2353 | 1.0000 | 44.4% |
| mem0_stock_day11ctx | 0.4907 | 0.3714 | 0.0000 | 0.4103 | 1.0000 | 50.0% |

- 各题型 n：cat1:4, cat2:4, cat3:2, cat4:4, cat5:4。
- 答案分对多要素答案只给部分 token-F1；cat5 的 judge 只按语义判，不按 abstention，见 §5。

## 3. 逐题账本（memfw − mem0_stock）

memfw 赢 4 题 / 输 5 题 / 平 9 题。

| cat | question | memfw | mem0_stock | mem0_custom | mem0_stock_day11ctx | memfw−stock |
|---|---|---|---|---|---|---|
| 1 | How many Prius has Evan owned? | 1.0000 | 1.0000 | 1.0000 | 1.0000 | +0.00 |
| 1 | How many roadtrips did Evan take in May 2023? | 0.0000 | 0.0000 | 0.0000 | 0.0000 | +0.00 |
| 1 | What motivates Evan to take care of his health? | 0.1250 | 0.1818 | 0.3200 | 0.2963 | -0.06 |
| 1 | Where has Evan been on roadtrips with his family? | 0.6667 | 1.0000 | 1.0000 | 0.6667 | -0.33 |
| 2 | When did Evan go to Jasper with his family? | 0.2857 | 0.0000 | 0.2500 | 0.2000 | +0.29 |
| 2 | When did Sam first go to the doctor and find out he had a  | 0.0000 | 0.3636 | 0.4000 | 1.0000 | -0.36 |
| 2 | When did Sam's friends mock him for being overweight? | 0.2857 | 0.4615 | 0.4615 | 0.2857 | -0.18 |
| 2 | Which hobby did Sam take up in May 2023? | 0.0000 | 0.0000 | 0.0000 | 0.0000 | +0.00 |
| 3 | What electronic device could Evan gift Sam to help him kee | 0.0000 | 0.0000 | 0.0000 | 0.0000 | +0.00 |
| 3 | Which country was Evan visiting in May 2023? | 0.0000 | 0.0000 | 0.0000 | 0.0000 | +0.00 |
| 4 | What advice did Evan give Sam about finding a passion? | 0.0000 | 0.0000 | 0.0000 | 0.7000 | +0.00 |
| 4 | What did Evan find relaxing about his road trip to Jasper? | 1.0000 | 0.9000 | 0.9412 | 0.9412 | +0.10 |
| 4 | What does the smartwatch help Evan with? | 0.9000 | 0.0000 | 0.0000 | 0.0000 | +0.90 |
| 4 | Where did Evan take his family for a road trip on 24 May,  | 1.0000 | 0.0000 | 0.0000 | 0.0000 | +1.00 |
| 5 | What did Sam find relaxing about his road trip to Jasper? | 1.0000 | 1.0000 | 1.0000 | 1.0000 | +0.00 |
| 5 | What does the smartwatch help Sam with? | 0.0000 | 1.0000 | 1.0000 | 1.0000 | -1.00 |
| 5 | Where did Sam take his family for a road trip on 24 May, 2 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | +0.00 |
| 5 | Why did Sam decide to get the bonsai tree? | 1.0000 | 1.0000 | 1.0000 | 1.0000 | +0.00 |

- 胜负高度集中：cat4 的细节题是 memfw 的主要赢面，cat2 的时间题和 cat5 的归属题是主要输面。

## 4. 检索覆盖与答案瓶颈

| arm | pre-fit macro 覆盖 | ≥1 条 gold 命中 | gold 最佳排名 | 答对但 gold 未进 top5 | 答 0 但 gold 已检索 |
|---|---|---|---|---|---|
| memfw | 69.4% | 42/54 | 1.9 | 3/33 | 12/21 |
| mem0_stock | 65.7% | 39/54 | 1.6 | 3/30 | 12/24 |
| mem0_custom | 81.5% | 45/54 | 1.6 | 3/33 | 15/21 |
| mem0_stock_day11ctx | 75.9% | 42/54 | 1.4 | 6/36 | 12/18 |

- 覆盖率与答案分并不同步：Mem0-custom 覆盖率最高但答案并不最高；“gold 已检索到却答 0”远多于“未检索到却答对”，说明瓶颈主要在答案合成/多跳，而非单纯召回。

## 5. cat5（对抗/归属）专项

LoCoMo cat5 的题目把另一个人的事实问到目标人物头上，正确行为是 abstain。

| question | memfw | mem0_stock | mem0_custom | mem0_stock_day11ctx |
|---|---|---|---|---|
| What did Sam find relaxing about his road trip to Ja | 'unknown' / 1.00 | 'unknown' / 1.00 | 'unknown' / 1.00 | 'unknown' / 1.00 |
| What does the smartwatch help Sam with? | 'It tracks his progress really well' / 0.00 | 'unknown' / 1.00 | 'unknown' / 1.00 | 'unknown' / 1.00 |
| Where did Sam take his family for a road trip on 24  | 'unknown' / 1.00 | 'unknown' / 1.00 | 'unknown' / 1.00 | 'unknown' / 1.00 |
| Why did Sam decide to get the bonsai tree? | 'unknown' / 1.00 | 'unknown' / 1.00 | 'unknown' / 1.00 | 'unknown' / 1.00 |

- memfw 唯一一次 cat5 失误：把 Evan 的 smartwatch 事实答给了 Sam；Mem0 全部 abstain。
- 该行被主 judge 判 correct（gold 文本确实如此），说明 LLM judge 对对抗题会“按字面正确性”放行；judge fallback 对 cat5 abstention 也给分，所以 judge 表里 cat5 全是 100%。**cat5 应以 abstention-aware 的 answer_score 为准。**

## 6. Judge 分析

主 judge 与同模型 judge 逐行一致率：213/216 = 98.6%（不一致集中于同一条 cat5 回答）。

| arm | judge 假阳性（correct 但 score=0） | judge 偏严（incorrect 但 score>0） |
|---|---|---|
| memfw | 3 | 12/33 |
| mem0_stock | 0 | 9/30 |
| mem0_custom | 0 | 9/33 |
| mem0_stock_day11ctx | 0 | 9/36 |

| judge | model | per-arm accuracy（每题跨重复均值） |
|---|---|---|
| primary | qwen3.5:4b | mem0_custom:44.4%, mem0_stock:38.9%, mem0_stock_day11ctx:50.0%, memfw:44.4% |
| same_model | qwen3.5:9b | mem0_custom:44.4%, mem0_stock:38.9%, mem0_stock_day11ctx:50.0%, memfw:38.9% |

- 主 judge（不同模型）与同模型 judge 的排名差异很小：同模型 judge 没有被系统性“偏袒”的证据。
- 但 judge 与 token-F1 的错位明显：部分正确（多要素）常被判错，cat5 的错误归属却被判对；因此 REPORT 同时保留 token-F1、judge 与 abstention 规则三种口径。

## 7. 扩题集（exploratory，n=41）

扩题集与固定 18 题重叠 18 题（真正新增 23 题）；分题型 n 仍然较小。

| arm | cat1 | cat2 | cat3 | cat4 | cat5 | overall |
|---|---|---|---|---|---|---|
| mem0_custom | 0.6640 | 0.2470 | 0.0000 | 0.5433 | 0.9000 | 0.5535 |
| mem0_stock | 0.6364 | 0.2028 | 0.0000 | 0.4225 | 0.9000 | 0.4962 |
| mem0_stock_day11ctx | 0.5926 | 0.2145 | 0.0000 | 0.4864 | 0.9000 | 0.5168 |
| memfw | 0.5583 | 0.0952 | 0.0000 | 0.6503 | 0.8000 | 0.5220 |

- 扩展集上 cat4 仍是 memfw 的优势（n=15），cat2 仍是弱项（n=9），cat1 略输 Mem0；overall 方向与固定 18 题一致但不显著（见 metrics/expanded_metrics.json 的配对检验与 MDE）。

## 8. 效率与规模

| arm | 写入 P50(s) | 写入 P95(s) | 检索 P50(s) | 回答 P50(s) | ingest prompt tokens | 记忆数 | 存储 bytes |
|---|---|---|---|---|---|---|---|
| memfw | 0.0193 | 0.0226 | 0.4027 | 1.2398 | 11484 | 100.0 | 99633 |
| mem0_stock | 10.0720 | 12.6570 | 0.3066 | 1.2334 | 2716128 | 79.0 | 709206 |
| mem0_custom | 9.5281 | 12.5154 | 0.3033 | 1.2312 | 2744967 | 50.0 | 459350 |
| mem0_stock_day11ctx | 5.1039 | 7.4684 | 0.3041 | 1.2377 | 1239423 | 87.0 | 778838 |

- 检索/回答延迟主要由同一 answer 模型决定，各臂接近；主要差异在写入侧。
- memfw 写入比 Mem0-stock 快约 500×、ingest token 少约 236×；但这不是质量优势。

## 9. Provenance 审计

| arm | 记忆总数 | 派生记忆 | day11_frame FLAGGED | attribution 分布 |
|---|---|---|---|---|
| mem0_custom | 50 | 50 | 19/30 | user 31 / assistant 10 / missing 9 |
| mem0_stock | 79 | 79 | 21/30 | user 61 / assistant 17 / missing 1 |
| mem0_stock_day11ctx | 87 | 87 | 18/30 | user 55 / assistant 22 / missing 10 |
| memfw | 100 | 0 | 0/30 | user 0 / assistant 0 / missing 0 |

- Mem0 系派生记忆多数被记为无标注用户记忆（stock 21/30），且几乎不标 missing；这是系统性行为。
- memfw 全部为逐字记忆（派生审计结构性 N/A）。provenance 字段存在不等于答案会用：cat5 的跨 speaker 误答说明 answer prompt/检索还需要 speaker-aware 约束。

## 10. 开源框架对比（exploratory，框架臂 1 次重复）

| arm | answer_score | recall micro | 写入 P50(s) | 记忆数 | 存储 bytes | 重复数 |
|---|---|---|---|---|---|---|
| memfw | 0.4591 | 66.7% | 0.0193 | 100.0 | 99633 | 3 |
| langmem | 0.2672 | 0.0% | 21.5278 | 95.0 | 127372 | 1 |
| memoryos | 0.5575 | 54.2% | 0.0151 | 100.0 | 840277 | 1 |
| mem0_stock | 0.4393 | 66.7% | 10.0720 | 79.0 | 709206 | 3 |

| comparison | 均值差 | 95% CI | 精确置换 p | MDE |
|---|---|---|---|---|
| memfw vs langmem | 0.1919 | [-0.0327, 0.4155] | 0.1211 | 0.3337 |
| memfw vs memoryos | -0.0984 | [-0.2651, 0.0286] | 0.3750 | 0.2212 |

- MemOS(general_text) 与 memfw 架构最接近（原话+向量），题型画像也最像；差异不显著。
- LangMem 的 evidence recall≈0 来自“跨轮摘要丢失逐轮 provenance”，是记忆表示差异，不是检索 bug；写入也最慢。
- 框架默认抽取/更新策略不同、框架臂单次重复；该表只作方向性对比，详见 `frameworks/FRAMEWORK_COMPARISON.md`。

## 11. 可重复性与统计功效

| arm | 3 次重复逐题 answer_score 完全一致 |
|---|---|
| memfw | 18/18 |
| mem0_stock | 18/18 |
| mem0_custom | 18/18 |
| mem0_stock_day11ctx | 18/18 |

- 固定 18 题主比较：diff=0.0198，95% CI [-0.1678, 0.2156]，p=0.8867，MDE=0.2851。
- 扩题集 41 题：p=0.6809，MDE=0.1766，仍不显著。
- 重复输出完全确定（temperature=0），因此重复不提供方差；有效样本是题目数，分题型结论只作描述。

## 12. 结论与 caveat

1. **固定 18 题与扩题集都没有可检测的整体质量差异**；memfw 的优势在成本/延迟/存储与 provenance，不在答案分。
2. **分题型是主要发现**：memfw 的 cat4（细节/多跳）明显更好，cat2（时间归一化）与 cat5（对抗归属）更弱；Mem0-custom/day11ctx 在 cat1/cat2 更稳。
3. **检索覆盖不是唯一瓶颈**：大量 gold 已检索到却答 0 的样本指向答案合成/多跳；cat5 还暴露了答案阶段的 speaker 归属问题。
4. **judge 不能替代任务指标**：cat5 需 abstention-aware 评分；judge 对部分正确偏严。
5. 样本小（分题型 n=2~15）、单一 conversation、框架臂单次重复；下一步应扩充独立题量、加入 speaker-aware/时间归一化约束，并考虑更强的类别感知 judge。

---

生成命令：`python eval/stepG_analysis.py`（数据目录 `$PROJECT_ROOT/results/stepG`；所有数字均为运行产物重算）。

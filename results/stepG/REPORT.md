# Step G：memfw vs Mem0 正式对比实验报告（迭代版）

> 本报告由 `eval/stepG_report.py` 从 `results/stepG/` 下的运行产物自动生成；所有数字均可在标注的来源文件中核对。实验仅使用公开 LoCoMo 数据。

- 迭代内容：新增 **mem0_stock** arm（stock Mem0）作为主比较；**mem0_custom**（schema 兼容 instructions）保留为敏感性 arm；**mem0_stock_day11ctx** 用 Day 11 的 `num_ctx=8192` 复刻 Mem0 抽取，用于逐字节可复现性核对；主 judge 换成不同模型（qwen3.5:4b），同模型 judge 保留为诊断；新增**扩题集**（exploratory）提高统计功效。

## 1. 执行状态

| 步骤 | 状态 | 依据 |
|---|---|---|
| G1 环境 | 完成 | env.txt=True, pip-freeze.txt=True |
| G2 统一模型 | 完成 | llm=qwen3.5:9b, embedding=nomic-embed-text, judge(primary)=qwen3.5:4b, judge(sensitivity)=qwen3.5:9b |
| G3 runner + 单测 | 完成 | pytest_log=True |
| G4 正式运行 | 完成 | arms complete 3×18: 4/4 |
| G5 指标 | 完成 | metrics.json=True |
| G6 provenance | 完成 | arms audited: 4 |
| G7 报告 | 完成 | REPORT.md（本文件） |
| 附加：扩题集 | 完成 | n=41（exploratory, 非固定 18 题子集） |

## 2. 实验设置

| 项目 | 值 |
|---|---|
| 数据 | 公开 LoCoMo `locomo10.json`，SHA-256 `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4` |
| 子集 | `conv-49`，前 100 轮，18 题（`results/stepG/subset.json`） |
| seed | 20261021 |
| arms | `memfw`, `mem0_stock`, `mem0_custom`, `mem0_stock_day11ctx`（每 arm 3 次重复） |
| Answer LLM（各 arm 相同） | `qwen3.5:9b` via ollama，temperature=0.0 |
| Answer embedding（各 arm 相同） | `nomic-embed-text`，dims=768 |
| Primary judge（不同模型） | `qwen3.5:4b`（与 answer 模型不同） |
| Sensitivity judge（同模型） | `qwen3.5:9b`（仅作诊断） |
| 检索 | top_k=5，上下文预算 128 tokens |
| Mem0 配置 | mem0_stock 使用默认抽取提示；mem0_custom 额外传入 schema 兼容的 custom instructions（敏感性 arm）；mem0_stock_day11ctx 为默认提示 + Day 11 的 num_ctx=8192（复现 arm） |
| 与 Day 11 的配置差异 | Day 11 mem0 `num_ctx`=8192；本轮默认 `num_ctx`=16384。mem0_stock 使用本机默认窗口（抽取结果只作近似复现）；mem0_stock_day11ctx 使用 Day 11 的 8192 窗口，用于逐题复现核对。两臂的答案生成 prompt 很短，窗口差异不影响回答阶段 |
| 硬件 | Apple M5 Max, 18 cores, 128 GB unified memory, macOS 26.7.1 (Mac17,7) |
| 总耗时 | 4h50m58s |
| API 花费 | $0.00（本地 Ollama，无外部 API） |
| 模型 digest | nimble:9b=9b953de7a5336756ece1cb1e8632e374b3dbdabe3d02d405cf8291da2d43a131, nomic-embed-text:latest=0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f, qwen3.5:4b=d8b0f5e9760cd1682034f292d7ef72ec46f432149be0df7574bf2d6e92e38c04, qwen3.5:9b=56671c2ab9385f9cfcb404638e32cd62d88e3501d44822208363c010179a3c90, qwen3:4b=359d7dd4bcdab3d86b87d73ac27966f4dbb9f5efdfcc75d34a8764a09474fae7 |

`env.txt`：

```
python_version: 3.11.16
python_executable: $EXTERNAL_PYTHON
platform: macOS-26.7.1-arm64-arm-64bit
machine: arm64
processor: arm
memfw_version: 0.1.0
memfw_module_path: $EXTERNAL_PYTHON
mem0_version: 2.2.1
ollama_server_version: 0.40.3
ollama_local_models: nimble:9b, nomic-embed-text:latest, qwen3.5:4b, qwen3.5:9b, qwen3:4b
ollama_model_digests: nimble:9b=9b953de7a5336756ece1cb1e8632e374b3dbdabe3d02d405cf8291da2d43a131, nomic-embed-text:latest=0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f, qwen3.5:4b=d8b0f5e9760cd1682034f292d7ef72ec46f432149be0df7574bf2d6e92e38c04, qwen3.5:9b=56671c2ab9385f9cfcb404638e32cd62d88e3501d44822208363c010179a3c90, qwen3:4b=359d7dd4bcdab3d86b87d73ac27966f4dbb9f5efdfcc75d34a8764a09474fae7
git_head: 5413127a9944ec942fd9844d7c003dd8e6accc86
git_status_porcelain: M eval/stepG_expanded.py
 M eval/stepG_metrics.py
 M eval/stepG_pipeline.py
 M eval/stepG_report.py
 M eval/stepG_runner.py
 M results/stepG/config.yaml
 M results/stepG/pipeline_status.json
 M results/stepG/run_manifest.json
 M tests/test_stepG_runner.py
?? results/stepG/runs/rep1/mem0_stock_day11ctx/
?? results/stepG/runs/rep2/mem0_stock_day11ctx/
?? results/stepG/runs/rep3/mem0_stock_day11ctx/
model_config: results/stepG/config.yaml

## pip freeze
Jinja2==3.1.6
MarkupSafe==3.0.4
PyYAML==6.0.3
Pygments==2.21.0
SQLAlchemy==2.1.5
annotated-doc==0.0.5
annotated-types==0.8.0
anyio==4.15.1
ast_serialize==0.13.1
backoff==2.2.1
certifi==2026.10.19
charset-normalizer==3.5.2
click==8.5.0
distro==1.9.0
filelock==4.2.1
fsspec==2026.9.0
grpcio==1.84.0
h11==0.16.0
h2==4.4.1
hf-xet==1.7.0
hpack==4.2.0
httpcore2==2.13.1
httpcore==1.0.9
httpx2==2.13.1
httpx==0.28.1
huggingface_hub==2.2.0
hyperframe==6.1.0
idna==3.20
iniconfig==2.3.1
jieba==0.42.1
jiter==0.17.0
librt==0.16.0
markdown-it-py==4.2.0
mdurl==0.1.2
mem0ai==2.2.1
memfw==0.1.0
mlx-lm==0.32.2
mlx-metal==0.32.4
mlx==0.32.4
mypy==2.4.0
mypy_extensions==1.1.0
numpy==2.4.6
ollama==0.6.3
openai==3.35.1
packaging==26.3
pathspec==1.1.1
pluggy==1.7.0
portalocker==3.2.0
posthog==7.73.2
protobuf==6.33.6
pydantic==2.14.1
pydantic_core==2.50.1
pytest==9.1.1
pytz==2026.5
qdrant-client==1.19.1
regex==2026.9.29
requests==2.34.2
rich==15.0.0
ruff==0.17.1
safetensors==0.8.0
sentencepiece==0.2.2
shellingham==1.5.4
sniffio==1.3.1
tokenizers==0.23.3
tqdm==4.70.1
transformers==5.20.1
truststore==0.10.5
typer==0.27.3
types-PyYAML==6.0.12.20260906
typing-inspection==0.4.4
typing_extensions==4.16.0
urllib3==2.8.0
zstandard==0.25.0
```

## 3. 主结果

### 质量（每题跨重复均值；来源 `results/stepG/metrics.json` → `systems.<arm>`）

| arm | answer_score | token_f1（cat1–4） | evidence recall（post-fit macro） | evidence recall（post-fit micro） | judge：primary | judge：same_model | 题数 | 重复数 |
|---|---|---|---|---|---|---|---|---|
| memfw | 0.4591 | 0.3759 | 69.4% | 66.7% | 44.4% | 38.9% | 18 | 3 |
| mem0_stock | 0.4393 | 0.2791 | 65.7% | 66.7% | 38.9% | 38.9% | 18 | 3 |
| mem0_custom | 0.4652 | 0.3123 | 81.5% | 83.3% | 44.4% | 44.4% | 18 | 3 |
| mem0_stock_day11ctx | 0.5050 | 0.3636 | 75.9% | 79.2% | 50.0% | 50.0% | 18 | 3 |

### 效率与规模（来源同上；每轮写入 = 一次 add/写入操作）

| arm | 总写入耗时(s) | 单轮均值(s) | 写入 P50(s) | 写入 P95(s) | 检索 P50(s) | 检索 P95(s) | 回答 P50(s) | prompt tokens | completion tokens | 记忆条数 | 存储字节 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| memfw | 2.20 | 0.0219 | 0.0193 | 0.0226 | 0.4027 | 0.4142 | 1.2398 | 11484 | 267 | 100.0 | 99633 |
| mem0_stock | 1004.45 | 10.0442 | 10.0720 | 12.6570 | 0.3066 | 0.3260 | 1.2334 | 2716128 | 17667 | 79.0 | 709206 |
| mem0_custom | 999.73 | 9.9970 | 9.5281 | 12.5154 | 0.3033 | 0.3073 | 1.2312 | 2744967 | 11994 | 50.0 | 459350 |
| mem0_stock_day11ctx | 526.68 | 5.2665 | 5.1039 | 7.4684 | 0.3041 | 0.3393 | 1.2377 | 1239423 | 16893 | 87.0 | 778838 |

> 注：memfw 逐字写入、无 LLM 抽取调用；Mem0 两个 arm 都跑 `add(infer=True)` 抽取。写入/存储差异同时反映“是否调用 LLM 抽取”和“存储形态”，不能单独解读为工程实现效率。

### LLM judge（每题跨重复均值）

| label | judge model | judged rows | per-arm accuracy（每题跨重复均值） |
|---|---|---|---|
| primary | qwen3.5:4b | 216 | mem0_custom:44.4%, mem0_stock:38.9%, mem0_stock_day11ctx:50.0%, memfw:44.4% |
| same_model | qwen3.5:9b | 216 | mem0_custom:44.4%, mem0_stock:38.9%, mem0_stock_day11ctx:50.0%, memfw:38.9% |

### Provenance 审计（G6；来源 `provenance_summary.json` / `provenance_audit.csv`）

| arm | 记忆总数 | 派生记忆数 | derived_frame (样本/派生/FLAGGED) | all_frame (样本/派生/FLAGGED) | day11_frame (样本/派生/FLAGGED) | 派生样本缺口 |
|---|---|---|---|---|---|---|
| mem0_custom | 50 | 50 | 30 / 30 / 22 | 30 / 30 / 23 | 30 / 30 / 19 | 0 |
| mem0_stock | 79 | 79 | 30 / 30 / 21 | 30 / 30 / 23 | 30 / 30 / 21 | 0 |
| mem0_stock_day11ctx | 87 | 87 | 30 / 30 / 25 | 30 / 30 / 24 | 30 / 30 / 18 | 0 |
| memfw | 100 | 0 | 0 / 0 / 0 | 30 / 0 / 0 | 30 / 0 / 0 | 30 |

- `derived_frame`：每个 arm 最多抽 30 条派生记忆；`FLAGGED` 表示派生文本被记录成用户原话（R3）。
- `all_frame`：30 条均匀抽样（固定分母兜底）；`day11_frame`：Day 11 的均匀抽样 + R3 规则。
- 审计只取 `rep1`；mem0_custom 的 custom instructions 请求 `attributed_to=user`，其 R3 含 prompt 强制成分；mem0_stock 无此强制。

## 4. 显著性检验

### 主比较（固定 18 题）

memfw vs mem0_stock；每题跨重复均值后做配对检验。

| 指标 | A 均值 | B 均值 | 均值差 (A−B) | 95% bootstrap CI | 精确置换 p | Wilcoxon p | Cohen dz | MDE(80%) |
|---|---|---|---|---|---|---|---|---|
| answer_score | 0.4591 | 0.4393 | 0.0198 | [-0.1678, 0.2156] | 0.8867 | 0.9414 | 0.0458 | 0.2851 |
| token_f1（cat1–4） | 0.3759 | 0.2791 | 0.0969 | [-0.0844, 0.3150] | 0.4141 | 0.7422 | 0.2441 | 0.2970 |
| evidence recall（post-fit macro） | 0.6944 | 0.6574 | 0.0370 | [-0.1481, 0.2222] | 1.0000 | 1.0000 | 0.0869 | 0.2812 |
| LLM judge（primary） | 0.4444 | 0.3889 | 0.0556 | [-0.1111, 0.2778] | 1.0000 | 1.0000 | 0.1335 | 0.2747 |
| LLM judge（same_model） | 0.3889 | 0.3889 | 0.0000 | [-0.2222, 0.2222] | 1.0000 | 1.0000 | 0.0000 | 0.3201 |

### memfw vs mem0_custom（sensitivity）

| 指标 | A 均值 | B 均值 | 均值差 (A−B) | 95% bootstrap CI | 精确置换 p | Wilcoxon p | Cohen dz | MDE(80%) |
|---|---|---|---|---|---|---|---|---|
| answer_score | 0.4591 | 0.4652 | -0.0061 | [-0.1914, 0.1893] | 0.9688 | 0.6797 | -0.0141 | 0.2843 |
| token_f1（cat1–4） | 0.3759 | 0.3123 | 0.0636 | [-0.1168, 0.2837] | 0.5547 | 1.0000 | 0.1583 | 0.3006 |
| evidence recall（post-fit macro） | 0.6944 | 0.8148 | -0.1204 | [-0.3241, 0.0741] | 0.3125 | 0.3125 | -0.2769 | 0.2868 |
| LLM judge（primary） | 0.4444 | 0.4444 | 0.0000 | [-0.2222, 0.2222] | 1.0000 | 1.0000 | 0.0000 | 0.3201 |
| LLM judge（same_model） | 0.3889 | 0.4444 | -0.0556 | [-0.2778, 0.1667] | 1.0000 | 1.0000 | -0.1030 | 0.3559 |

### mem0_stock vs mem0_custom（configuration effect）

| 指标 | A 均值 | B 均值 | 均值差 (A−B) | 95% bootstrap CI | 精确置换 p | Wilcoxon p | Cohen dz | MDE(80%) |
|---|---|---|---|---|---|---|---|---|
| answer_score | 0.4393 | 0.4652 | -0.0259 | [-0.0593, -0.0023] | 0.1250 | 0.1250 | -0.3960 | 0.0431 |
| token_f1（cat1–4） | 0.2791 | 0.3123 | -0.0333 | [-0.0759, -0.0029] | 0.1250 | 0.1250 | -0.4562 | 0.0546 |
| evidence recall（post-fit macro） | 0.6574 | 0.8148 | -0.1574 | [-0.3241, -0.0185] | 0.1250 | 0.1250 | -0.4690 | 0.2215 |
| LLM judge（primary） | 0.3889 | 0.4444 | -0.0556 | [-0.1667, 0.0000] | 1.0000 | 1.0000 | -0.2357 | 0.1556 |
| LLM judge（same_model） | 0.3889 | 0.4444 | -0.0556 | [-0.1667, 0.0000] | 1.0000 | 1.0000 | -0.2357 | 0.1556 |

### memfw vs mem0_stock_day11ctx（Day-11 context replication）

| 指标 | A 均值 | B 均值 | 均值差 (A−B) | 95% bootstrap CI | 精确置换 p | Wilcoxon p | Cohen dz | MDE(80%) |
|---|---|---|---|---|---|---|---|---|
| answer_score | 0.4591 | 0.5050 | -0.0459 | [-0.2713, 0.1747] | 0.7422 | 0.7422 | -0.0913 | 0.3320 |
| token_f1（cat1–4） | 0.3759 | 0.3636 | 0.0124 | [-0.2397, 0.2727] | 0.8906 | 0.9844 | 0.0244 | 0.3792 |
| evidence recall（post-fit macro） | 0.6944 | 0.7593 | -0.0648 | [-0.2963, 0.1667] | 0.6562 | 0.6562 | -0.1275 | 0.3356 |
| LLM judge（primary） | 0.4444 | 0.5000 | -0.0556 | [-0.2778, 0.1667] | 1.0000 | 1.0000 | -0.1030 | 0.3559 |
| LLM judge（same_model） | 0.3889 | 0.5000 | -0.1111 | [-0.3889, 0.1667] | 0.6875 | 0.6875 | -0.1906 | 0.3847 |

### mem0_stock vs mem0_stock_day11ctx（context-window effect）

| 指标 | A 均值 | B 均值 | 均值差 (A−B) | 95% bootstrap CI | 精确置换 p | Wilcoxon p | Cohen dz | MDE(80%) |
|---|---|---|---|---|---|---|---|---|
| answer_score | 0.4393 | 0.5050 | -0.0657 | [-0.1838, 0.0318] | 0.3125 | 0.3750 | -0.2691 | 0.1612 |
| token_f1（cat1–4） | 0.2791 | 0.3636 | -0.0845 | [-0.2323, 0.0405] | 0.3125 | 0.3750 | -0.3059 | 0.2067 |
| evidence recall（post-fit macro） | 0.6574 | 0.7593 | -0.1019 | [-0.2963, 0.0926] | 0.4375 | 0.4375 | -0.2357 | 0.2852 |
| LLM judge（primary） | 0.3889 | 0.5000 | -0.1111 | [-0.3333, 0.1111] | 0.6250 | 0.6250 | -0.2357 | 0.3111 |
| LLM judge（same_model） | 0.3889 | 0.5000 | -0.1111 | [-0.3333, 0.1111] | 0.6250 | 0.6250 | -0.2357 | 0.3111 |


## 5. 与 Day 11 的可复现性核对

以 Day 11 报告 JSON（`reports/day11_baseline_comparison.json`）的逐题结果为参照：`memfw` 与 Day 11 `memfw` 同实现；`mem0_stock` 与 Day 11 `mem0` 同为 stock 抽取配置（但本机 16k 上下文会改变抽取）；`mem0_stock_day11ctx` 使用 Day 11 的 `num_ctx=8192`，是用于逐题核对 Mem0 抽取复现的臂；`mem0_custom` 没有 Day 11 对应项（敏感性 arm）。

| arm | Day 11 对应系统 | 逐题 answer_score 相同 | 检索集合 Jaccard | 本次记忆数 | Day 11 记忆数 |
|---|---|---|---|---|---|
| mem0_stock | mem0 | 13/18 | 0.396 | 79 | 94 |
| mem0_stock_day11ctx | mem0 | 12/18 | 0.543 | 87 | 94 |
| memfw | memfw | 18/18 | 0.967 | 100 | 100 |

- 即使对齐 `num_ctx`，day11ctx 也未逐题复现 Day 11：12/18 题分数相同，记忆数 87 vs 94。剩余差异可能来自 mem0/LLM 版本、seed 配置细节与重复运行的非确定性；因此 **跨运行的 Day 11 Mem0 数字只作参考**，实验结论以同一轮内的配对比较为准。

## 6. 扩题集（exploratory）

扩题集：同一 100 轮窗口内全部 41 道有完整 gold evidence 的题（固定 18 题子集之外的 exploratory 补充，不替代主结果）。

| arm | answer_score | token_f1(cat1-4) | recall(macro) | recall(micro) | 题数 |
|---|---|---|---|---|---|
| mem0_custom | 0.5535 | 0.4417 | 79.7% | 81.2% | 41 |
| mem0_stock | 0.4962 | 0.3660 | 75.2% | 75.0% | 41 |
| mem0_stock_day11ctx | 0.5168 | 0.3932 | 73.6% | 75.0% | 41 |
| memfw | 0.5220 | 0.4324 | 67.1% | 64.6% | 41 |

主比较（memfw vs mem0_stock，n=41）：

| 指标 | A 均值 | B 均值 | 均值差 (A−B) | 95% bootstrap CI | 精确置换 p | Wilcoxon p | Cohen dz | MDE(80%) |
|---|---|---|---|---|---|---|---|---|
| answer_score | 0.5220 | 0.4962 | 0.0258 | [-0.0962, 0.1482] | 0.6809 | 0.8798 | 0.0639 | 0.1766 |
| token_f1（cat1–4） | 0.4324 | 0.3660 | 0.0664 | [-0.0736, 0.2184] | 0.3939 | 0.6047 | 0.1563 | 0.2136 |
| evidence recall（post-fit macro） | 0.6707 | 0.7520 | -0.0813 | [-0.2439, 0.0732] | 0.3882 | 0.3877 | -0.1562 | 0.2276 |
| LLM judge（primary） | 0.5366 | 0.5122 | 0.0244 | [-0.0976, 0.1463] | 1.0000 | 1.0000 | 0.0584 | 0.1826 |

> 扩题集复用同一批已写入的记忆，只新增“回答+judge”，用于提高统计功效；属于探索性分析，不做族错误校正。

## 7. 与初步结果的对比

| 项目 | Day 11 初步 | 本轮正式 | 来源 |
|---|---|---|---|
| answer_score memfw | 0.4591 | 0.4591 | metrics.json |
| answer_score mem0（stock） | 0.5267 | 0.4393 | metrics.json |
| evidence recall micro memfw | 66.7% | 66.7% | metrics.json |
| evidence recall micro mem0（stock） | 62.5% | 66.7% | metrics.json |
| 写入 P50 memfw | 0.0323s | 0.0193s | write_turns.jsonl |
| 写入 P50 mem0（stock） | 5.3758s | 10.0720s | write_turns.jsonl |
| provenance FLAGGED（day11_frame） | memfw 0/30；mem0 21/30 | memfw 0/30；mem0_stock 21/30 | provenance_summary.json |

判定（阈值写死在本函数中，结果由数据计算）：

- **确认**：H1 memfw answer_score 与 Day 11 接近（|Δ|≤0.02）（|Δ|=0.0000）。
- **未确认**：H2 mem0_stock answer_score 与 Day 11 接近（|Δ|≤0.02）（|Δ|=0.0874）。
- **未确认**：H3 mem0_stock 逐题复现 Day 11（≥16/18 题分数相同）（13/18）。
- **确认**：H4 memfw 写入 P50 显著低于 mem0_stock（比值 ≥10×）（比值=522.2×）。
- **确认**：H5 主比较未检测到显著差异（精确置换 p>0.05）（p=0.8867）。
- **确认**：H6 memfw post-fit micro recall 复现（|Δ|≤0.05）（|Δ|=0.0000）。
- **确认**：H7 两系统 recall 差距方向与 Day 11 一致（Day11 memfw−mem0=+0.0417；本轮=+0.0000）。
- **确认**：H8 mem0 的派生记忆多数被记为无标注用户记忆（stock arm day11_frame FLAGGED ≥ 半数）（stock 21/30；Day11 21/30）。
- **未确认**：H9 mem0_stock_day11ctx 逐题复现 Day 11（≥16/18 题分数相同）（12/18；记忆数 87 vs 94）。
- **未确认**：H10 mem0_stock_day11ctx answer_score 与 Day 11 接近（|Δ|≤0.02）（|Δ|=0.0217）。
- **配置敏感性**：mem0_custom answer_score=0.4652、记忆数=50.0，与 stock 不同；因此两套 Mem0 数字不能混用。
- **复现解释**：mem0_stock 与 Day 11 mem0 的逐题分数一致率 13/18，记忆数 79 vs 94；本轮 mem0 wrapper 使用 num_ctx=16384，Day 11 使用 8192，抽取 prompt 是否被截断会改变抽取文本，因此 stock arm 是“同默认抽取提示 + 本机协议”的近似复现，而非逐字节复现。
- 正式结论以 §3–§5 与 `metrics.json` 为准；初步数字仅作参照。

## 8. 未完成项与局限

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
- memfw: derived_frame 只有 0 条派生记忆（缺口 30），该 arm 无对象可审计。
- 重复确定性为观测结果（temperature=0），不能代替独立样本方差；judge/模型身份见 §2。
- 运行快照：`env.txt` 的 git_head 是运行开始时 HEAD；实际运行代码由本报告对应的提交保存。

## 9. 复现命令

```sh
cd ~/Documents/Codex/2026-10-18/memfw-stepG-worktree
MEM0_DIR=$PWD/results/stepG/runtime/mem0_home \
  .venv-stepg/bin/python eval/stepG_pipeline.py --stage all

# 只重跑某个阶段
  .venv-stepg/bin/python eval/stepG_pipeline.py --stage metrics
  .venv-stepg/bin/python eval/stepG_pipeline.py --stage provenance
  .venv-stepg/bin/python eval/stepG_expanded.py   # 扩题集（exploratory）
```

## 10. 产物清单

| 文件 | 内容 |
|---|---|
| results/stepG/config.yaml | arms、模型、judge(s)、seed、repeats |
| results/stepG/subset.json | 固定 18 题子集（conv-49 前 100 轮） |
| results/stepG/subset_expanded.json | 扩题集（exploratory） |
| results/stepG/env.txt / pip-freeze.txt | 环境、模型 digest、git HEAD |
| results/stepG/runs/rep*/<arm>/ | 每题 JSONL、每轮写入、记忆导出、run summary |
| results/stepG/judge_*.jsonl | primary（qwen3.5:4b）与 sensitivity（qwen3.5:9b）judge 逐条判定 |
| results/stepG/metrics.json / metrics.md | 质量、效率、规模、配对检验 |
| results/stepG/expanded_metrics.json / md | 扩题集指标（exploratory） |
| results/stepG/provenance_rubric.md / audit.csv / summary.json | provenance 规则、逐条判定、汇总 |
| results/stepG/validation.json | 产物完整性校验 |
| results/stepG/REPORT.md | 本报告 |

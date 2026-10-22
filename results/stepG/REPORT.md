# Step G：memfw vs Mem0 正式对比实验报告

> 本报告由 `eval/stepG_report.py` 从 `results/stepG/` 下的运行产物自动生成；所有数字均可在括号标注的来源文件中核对。实验仅使用公开 LoCoMo 数据。

## 1. 执行状态（G1–G7）

| 步骤 | 状态 | 依据 |
|---|---|---|
| G1 环境 | 完成 | env.txt=True, pip-freeze.txt=True |
| G2 统一模型 | 完成 | llm=qwen3.5:9b, embedding=nomic-embed-text, judge=qwen3.5:9b, smoke_check=env-recorded |
| G3 runner + 单测 | 完成 | runner=True, pytest_log=True |
| G4 正式运行 | 完成 | complete 18-question run summaries: 6/6 |
| G5 指标 | 完成 | metrics.json=True |
| G6 provenance | 完成 | provenance_summary.json=True |
| G7 报告 | 完成 | results/stepG/REPORT.md（本文件） |

## 2. 实验设置

| 项目 | 值 |
|---|---|
| 数据 | 公开 LoCoMo `locomo10.json`，SHA-256 `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4` |
| 子集 | `conv-49`，前 100 轮，18 题（来源 `results/stepG/subset.json`） |
| seed | 20261021 |
| LLM（两系统相同） | `qwen3.5:9b` via ollama @ `http://127.0.0.1:11434`，temperature=0.0 |
| Embedding（两系统相同） | `nomic-embed-text`，dims=768 |
| 检索 | top_k=5，上下文预算 128 tokens |
| 重复次数 | 每系统 3 次（run ids: rep1..rep3） |
| 内存协议 | memfw 仅作为“逐字写入 + 共享 embedding 余弦检索”的基线；Mem0 使用 `add(infer=True)` |
| 硬件 | Apple M5 Max, 18 cores, 128 GB unified memory, macOS 26.7.1 (Mac17,7) |
| 总耗时 | 2h04m22s |
| 模型 digest | nimble:9b=9b953de7a5336756ece1cb1e8632e374b3dbdabe3d02d405cf8291da2d43a131, nomic-embed-text:latest=0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f, qwen3.5:4b=d8b0f5e9760cd1682034f292d7ef72ec46f432149be0df7574bf2d6e92e38c04, qwen3.5:9b=56671c2ab9385f9cfcb404638e32cd62d88e3501d44822208363c010179a3c90, qwen3:4b=359d7dd4bcdab3d86b87d73ac27966f4dbb9f5efdfcc75d34a8764a09474fae7 |
| API 花费 | $0.00（本地 Ollama，无外部 API） |
| 运行环境 | 正式运行使用仓库既有 `.venv`（Python 3.11.16 / mem0ai 2.2.1）；测试使用独立 `.venv-stepg`（worktree 内 editable 安装），依赖版本相同 |

`env.txt`：

```
python_version: 3.11.16
python_executable: /Users/leo/Documents/Codex/2026-10-18/python-keyness-agent-claude-gpt-deepseek/.venv/bin/python
platform: macOS-26.7.1-arm64-arm-64bit
machine: arm64
processor: arm
memfw_version: 0.1.0
memfw_module_path: /Users/leo/Documents/Codex/2026-10-18/python-keyness-agent-claude-gpt-deepseek/src/memfw/__init__.py
mem0_version: 2.2.1
ollama_server_version: 0.40.3
ollama_local_models: nimble:9b, nomic-embed-text:latest, qwen3.5:4b, qwen3.5:9b, qwen3:4b
ollama_model_digests: nimble:9b=9b953de7a5336756ece1cb1e8632e374b3dbdabe3d02d405cf8291da2d43a131, nomic-embed-text:latest=0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f, qwen3.5:4b=d8b0f5e9760cd1682034f292d7ef72ec46f432149be0df7574bf2d6e92e38c04, qwen3.5:9b=56671c2ab9385f9cfcb404638e32cd62d88e3501d44822208363c010179a3c90, qwen3:4b=359d7dd4bcdab3d86b87d73ac27966f4dbb9f5efdfcc75d34a8764a09474fae7
git_head: c55d1febc9ca3b638b53c497d37eb94b23eecbeb
git_status_porcelain: M .gitignore
?? eval/
?? results/
?? tests/test_stepG_runner.py
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

### 质量（每题跨重复均值；来源 `results/stepG/metrics.json` → `systems.<name>`）

| system | answer_score | token_f1（cat1–4） | LLM-judge 正确率（每题跨重复均值） | evidence recall（post-fit macro） | 题数 | 重复数 |
|---|---|---|---|---|---|---|
| memfw | 0.4591 | 0.3759 | 38.9% | 69.4% | 18 | 3 |
| mem0 | 0.4652 | 0.3123 | 44.4% | 81.5% | 18 | 3 |

### 效率与规模（来源同上；写入延迟为每轮 add 的均值/题级检索与回答延迟）

| system | 总写入耗时(s) | 单轮写入均值(s) | 写入 P50(s) | 写入 P95(s) | 检索 P50(s) | 检索 P95(s) | 回答 P50(s) | prompt tokens | completion tokens | 记忆条数 | 存储字节 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| memfw | 2.20 | 0.0219 | 0.0193 | 0.0226 | 0.4027 | 0.4142 | 1.2398 | 11484 | 267 | 100.0 | 99633 |
| mem0 | 999.73 | 9.9970 | 9.5281 | 12.5154 | 0.3033 | 0.3073 | 1.2312 | 2744967 | 11994 | 50.0 | 459350 |

> 注：memfw 采用逐字写入协议，无 LLM 调用开销；Mem0 使用 `add(infer=True)` 跑抽取/更新流水线，因此写入成本天然不对称。memfw 的“存储字节”只统计 vault 目录，内存中的 float32 向量不计入；Mem0 统计 Qdrant + history DB 所在运行目录。

### Provenance 审计（G6；来源 `results/stepG/provenance_summary.json` 与 `provenance_audit.csv`）

| system | 记忆总数 | 派生记忆数 | derived_frame (样本/派生/FLAGGED) | all_frame (样本/派生/FLAGGED) | day11_frame (样本/派生/FLAGGED) | 派生样本缺口 |
|---|---|---|---|---|---|---|
| memfw | 100 | 0 | 0 / 0 / 0 | 30 / 0 / 0 | 30 / 0 / 0 | 30 |
| mem0 | 50 | 50 | 30 / 30 / 22 | 30 / 30 / 23 | 30 / 30 / 19 | 0 |

- `derived_frame`：每个系统最多抽 30 条“派生/推断”记忆（非源轮次逐字文本）；`FLAGGED` 表示派生文本被记录成用户原话（rubric R3）。
- `all_frame`：固定 30 条均匀抽样，作为无派生记忆系统的分母兜底；`day11_frame`：与 Day 11 相同的均匀抽样 + R3 规则，仅用于方向性对照。三个 frame 的分母都在 summary 中明确给出。
- 审计仅使用 `rep1` 的记忆导出（见 `provenance_summary.json` → `audited_run_id`）；mem0 的 custom extraction instructions 强制 `attributed_to=user`，因此其 R3 FLAGGED 属于**构造性质**，不应解读为语言模型的独立测量。

## 4. 显著性检验与局限

主指标为每题 `answer_score`（类别 1–4 为 token F1，类别 5 为弃答得分），先在重复内取均值，再做配对检验。

| 配对题数 | memfw 均值 | mem0 均值 | 均值差 | 95% bootstrap CI | 精确置换 p | Wilcoxon p | Cohen dz | rank-biserial | MDE(80%) |
|---|---|---|---|---|---|---|---|---|---|
| 18 | 0.4591 | 0.4652 | -0.0061 | [-0.1914, 0.1893] | 0.9688 | 0.6797 | -0.0141 | -0.1778 | 0.2843 |

次要指标（同样为 18 题配对、精确检验）：

| 指标 | memfw | mem0 | 均值差 | 95% bootstrap CI | 精确置换 p | Wilcoxon p | Cohen dz | MDE(80%) |
|---|---|---|---|---|---|---|---|---|
| token_f1（cat1–4） | 0.3759 | 0.3123 | 0.0636 | [-0.1168, 0.2837] | 0.5547 | 1.0000 | 0.1583 | 0.3006 |
| LLM judge（每题跨重复均值） | 0.3889 | 0.4444 | -0.0556 | [-0.2778, 0.1667] | 1.0000 | 1.0000 | -0.1030 | 0.3559 |
| evidence recall（post-fit macro） | 0.6944 | 0.8148 | -0.1204 | [-0.3241, 0.0741] | 0.3125 | 0.3125 | -0.2769 | 0.2868 |

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

## 5. 与初步结果的对比

初步结果（来源 `reports/day11_baseline_comparison.json`，Day 11，仅作参考，**不是正式结论**）与本次正式实验使用**同一模型**（`qwen3.5:9b` + `nomic-embed-text`）与**同一固定子集**（conv-49 前 100 轮 / 18 题 / seed 20261021），因此 answer_score 与 **post-fit micro evidence recall** 可以直接对照；但 Mem0 的写入提示（custom extraction instructions）、机器与运行时不同，Mem0 侧数字不是同一配置的严格复现。

| 指标 | 初步结果（Day 11） | 本次正式实验 | 正式值来源 |
|---|---|---|---|
| answer_score memfw | 0.4591 | 0.4591 | metrics.json → systems.memfw.mean_answer_score |
| answer_score mem0 | 0.5267 | 0.4652 | metrics.json → systems.mem0.mean_answer_score |
| evidence recall（post-fit micro）memfw | 66.7% | 66.7% | metrics.json → systems.memfw.micro_evidence_recall_postfit |
| evidence recall（post-fit micro）mem0 | 62.5% | 83.3% | metrics.json → systems.mem0.micro_evidence_recall_postfit |
| 写入 P50 memfw | 0.0323s | 0.0193s | runs/*/memfw/write_turns.jsonl |
| 写入 P50 mem0 | 5.3758s | 9.5281s | runs/*/mem0/write_turns.jsonl |
| provenance FLAGGED（Day-11 同框架：均匀抽样 + R3） | memfw 0/30；mem0 21/30 | memfw 0/30；mem0 19/30 | provenance_summary.json → day11_frame |

按预先设定的判定阈值逐项核对（阈值写死在本函数中，结果由数据计算）：

- **确认**：H1 memfw answer_score 复现（与初步值绝对差 ≤ 0.02）（|Δ|=0.0000）。
- **确认**：H2 mem0 派生记忆多数被记为无标注用户记忆（Day-11 同框架 FLAGGED ≥ 半数）（19/30（初步 21/30））。
- **确认**：H3 memfw 写入显著快于 Mem0（P50 比值 ≥ 10×）（比值=494.0×）。
- **确认**：H4 主指标未检测到显著差异（精确置换 p > 0.05）（p=0.9688）。
- **确认**：H5 memfw post-fit micro recall 与初步值接近（|Δ| ≤ 0.05）（|Δ|=0.0000）。
- **未确认**：H6 两系统 recall 差距方向与初步一致（初步 memfw−mem0=+0.0417；正式=-0.1667）。
- **无法归因**：Mem0 的 answer_score 与初步值差异较大，但 Mem0 在本次运行中使用了 custom extraction instructions，且机器/运行时不同；该差异不能归因于Mem0 或 memfw 的系统设计。
- 正式结论以上表与 §4 的统计结果为准；初步数字仅作为参照，不构成正式结论。

## 6. 未完成项和原因

- memfw 的 derived_frame 只有 0 条派生记忆（缺口 30），无法凑满 30 条；已在 summary 中如实记录。
- 重复确定性：3 个 repeat 的 per-run answer_score 完全相同（[0.459061]），因此“跨重复方差”为 0；这来自 temperature=0 的确定性解码，不能当作独立样本推断方差。
- provenance 审计仅取 `rep1` 的记忆导出；三个 repeat 的写入内容一致，但未对每个 repeat 分别抽样。
- mem0 的 R3 FLAGGED 由 custom extraction instructions 的 `attributed_to=user` 强制产生（构造性质），不是独立测量。
- 数据更新说明：mem0 的最终数字来自 `finalize3` 重跑——修复检索结果 dia_ids 归一化后，重新 ingest 3 个 repeat 并重跑题目/评审；此前试跑的数字未用于本报告。
- 模型同一性由 `env.txt` 中记录的 Ollama model digest 佐证，没有模型服务端的密码学回执；judge 与被测模型相同，属于次要诊断。
- pytest 结果见 `results/stepG/test-results.txt`。
- 测试环境说明：worktree 使用仓库既有 `.venv`（memfw editable 指向主工作树）会导致 2 个 Day5/Day6 测试的 source-root 断言失败，因此测试改用 worktree 内独立 `.venv-stepg`（同一 Python 3.11.16 / mem0ai 2.2.1）。

## 7. 复现命令

```sh
# 0) 一次性环境：本地 Ollama server + 两个模型（均只监听 127.0.0.1）
ollama serve  # OLLAMA_MODELS 指向本机模型目录
ollama pull qwen3.5:9b
ollama pull nomic-embed-text

# 1) 从公开数据到报告的一条命令
python eval/stepG_pipeline.py --stage all

# 可选：只重跑某个阶段
python eval/stepG_pipeline.py --stage metrics
python eval/stepG_pipeline.py --stage provenance
```

## 8. 产物清单

| 文件 | 内容 |
|---|---|
| results/stepG/config.yaml | 实验配置（模型/端点/seed/重复次数） |
| results/stepG/subset.json | 固定子集（100 轮 / 18 题 / seed 20261021） |
| results/stepG/env.txt | Python/OS/mem0/ollama 版本 |
| results/stepG/pip-freeze.txt | 依赖快照 |
| results/stepG/runs/rep*/ | 原始每题 JSONL、每轮写入延迟、记忆导出、run summary |
| results/stepG/metrics.json / metrics.md | 质量、效率、显著性 |
| results/stepG/provenance_rubric.md | provenance 判定规则（先于判定写入） |
| results/stepG/provenance_audit.csv | 逐条抽样判定表 |
| results/stepG/provenance_summary.json | provenance 汇总分母 |
| results/stepG/REPORT.md | 本报告 |

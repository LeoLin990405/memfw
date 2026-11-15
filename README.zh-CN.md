<div align="center">

<img src="docs/assets/banner.svg" alt="memfw banner" width="100%">

# memfw

**给 AI agent 一份你看得懂、能 diff、信得过的长期记忆。**

Markdown 即真相 · 哈希链 JSONL 事件 · 可重建 FTS5 索引 · 先核对再整理 · 所有 agent 共用一个脱敏会话库 · 本地 MCP 服务。

**简体中文** · [**English**](README.md)

<a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue?style=flat-square" alt="License"></a>
<a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.11+"></a>
<a href="#-mcp-工具"><img src="https://img.shields.io/badge/MCP-16_tools-8B5CF6?style=flat-square" alt="MCP tools"></a>
<a href="#-会话库所有-agent-汇到一处"><img src="https://img.shields.io/badge/agent_readers-14-10B981?style=flat-square" alt="14 agent readers"></a>
<a href="#-评估结果摘要"><img src="https://img.shields.io/badge/evaluation-bounded-D97706?style=flat-square" alt="Bounded evaluation"></a>

</div>

## memfw 是什么？

memfw 是一个跑在本机的 AI agent 记忆引擎，主要做三件事：

1. **把记忆存成文件。** 每条记忆都是 vault 文件夹里的一个普通 Markdown 文件；每次改动都追加进一条哈希链事件日志；搜索用的索引随时可以重建。
2. **把 agent 说过的话收拢起来。** 它读取 14 种 agent CLI 和应用各自的会话文件，汇入一个本地、已脱敏的会话库。任何一个 agent 都能搜索、阅读并接手另一个 agent 的工作。
3. **小心地把这些变成更好的记忆。** “做梦”（dream）会拿会话记录去核对记忆，提出确认、改正、合并、经验总结和技能草稿。它只会暂存候选，进不进 vault 由人决定。

agent 通过本地 **MCP 服务**（`memfw-mcp`）使用它，人通过 `memfw` 命令行使用它。

```
agent CLI / 应用 ──reader──► 会话库（本地、已脱敏）
        │                         │
        │ MCP                     ├─► 会话 worker ─► 摘要、episode
        ▼                         ├─► 交接包（换一个 agent 接着做）
     memfw-mcp ──► vault ◄── 人 ◄─┴─► dream：核对 · 整理 · 提炼 · 技能
                    ├─ memories/*.md     （唯一真相）           （只出候选）
                    ├─ events.jsonl      （哈希链审计日志）
                    └─ index.sqlite3     （FTS5，可重建）
```

## 🧭 设计原则

1. **本地优先。** vault、索引和会话库都在你自己的机器上。核心不调用任何模型，也不需要联网；凡是可能连到云端模型的功能，默认关闭，必须显式加 `--allow-cloud`。
2. **以事件为后盾的 Markdown。** Markdown 文件是权威来源。每次状态变化都追加到哈希链 JSONL 事件链里，`memfw log verify` 可以证明它没被动过。SQLite 索引只是派生投影，删了也能重建。
3. **先核对。** 模型提出的任何东西，都要先对照证据核实、再经人批准，才会成为记忆。dream 和会话 worker 只暂存候选，从不直接写 vault。
4. **人 > 机制 > 模型。** 模型可以提议；确定性的机制负责检查预算、来源和状态迁移；内容和锚点记忆最终由人负责。
5. **推断不等于证词。** 模型推断出来的内容，要和用户亲口说的话区分开，并保留来源和不确定性。模型摘要一律标为“未核实”。
6. **只做引擎，不做平台。** memfw 不假设主机、SSH 或远程搜索后端，这些交给嵌入它的应用去处理。

## 🚀 安装与快速开始

**环境要求：** Python 3.11+，推荐使用 [uv](https://docs.astral.sh/uv/)。

```sh
git clone <this-repository> memfw
cd memfw
uv venv .venv && uv sync --frozen --extra dev   # 或：python -m venv .venv && pip install -e '.[dev]'
. .venv/bin/activate
```

装好后有两个命令：`memfw`（命令行）和 `memfw-mcp`（stdio MCP 服务）。

建一个 vault，加一条记忆，再搜出来：

```sh
memfw init ./demo-vault
memfw add --root ./demo-vault \
  --id note-1 --title 'Local demo' --body 'The greenhouse sensor reports 18 degrees.'
memfw index rebuild --root ./demo-vault
memfw index search greenhouse --root ./demo-vault
memfw log verify --root ./demo-vault
```

接入 MCP 客户端时，用 `--app`（或环境变量 `MEMFW_APP`）告诉服务调用方是谁，会话共享规则才能对它生效：

```json
{
  "mcpServers": {
    "memfw": {
      "command": "memfw-mcp",
      "args": ["--root", "/path/to/your/vault", "--app", "claude"]
    }
  }
}
```

Claude Code 可以直接运行 `claude mcp add memfw -- memfw-mcp --root /path/to/vault --app claude`。Codex、Cursor、Gemini、Kimi、Grok、OpenCode 等用同样的命令，换成各自的 `--app` 名字即可（见 [`docs/conversations-share.md`](docs/conversations-share.md)）。

## 🗂️ vault 结构

`memfw init` 建出来的就是一个普通文件夹：

| 路径 | 内容 |
|---|---|
| `vault.yaml` | vault 配置：层、冷热层级、预算、`permissions: propose_then_confirm`、会话记录来源、共享规则 |
| `memories/<id>.md` | **记忆本身**：带 YAML frontmatter 的 Markdown（layer、tier、`about`、`related`、`valid_from` / `valid_to`，可选的 `status` 和 `evidence`） |
| `events.jsonl`、`events.state.json`、`.log/segments/` | 哈希链事件日志及其归档分段 |
| `.history/<id>/` | 保留的旧版本（供 `rollback` 使用） |
| `.proposals/` | agent 的提案，用哈希绑定到确切内容 |
| `.archives/` | 确定性的 `tar.zst` 冷归档（冷层） |
| `_candidates/remember/`、`_candidates/dream/`、`_candidates/skills/` | 等人审核的暂存候选 |
| `index.sqlite3`、`.memfw-history.sqlite` | 派生的 FTS5 索引（可随时删除重建） |

会话库和 worker 状态放在 vault **之外**，默认在 `~/.local/share/memfw/`（见[隐私与权限](#-隐私与权限)）。如果 vault 本身是 git 仓库，记得把 `index.sqlite3` 和 `.memfw-history.sqlite*` 加进 `.gitignore`。

旧版 memfw 写出的索引里没有时间投影。使用索引的写侧命令是惰性重建的：`memfw mcp serve` 和 `memfw index search` 会先检查 schema 世代（`PRAGMA user_version`），过时就在使用前重建，所以从旧版本升级不需要手动操作。只读 HTTP 服务（`scripts/memfw_readonly_service.py`）完全不写盘，因此**不会**重建：它检测到表缺失就降级（没有 `memory_temporal` 时不做时间过滤，没有 `memory_access` 时不返回结果）。升级后如果要用只读服务，先跑一次 `memfw index rebuild`。

## 🧠 记忆相关命令

| 类别 | 命令 |
|---|---|
| 读写 | `add`、`list`、`show`、`update [--dry-run]`、`delete`（只留哈希墓碑）、`rollback`、`supersede` |
| 完整性 | `fsck`、`recover`、`log verify`、`stats` |
| 搜索 | `index rebuild`、`index search`；`concepts`（一个事物一个规范概念，别名可以限定作用域） |
| 审核关口 | `candidates`、`candidate-apply [--apply]`、`candidates-expire`（30 天过期，从不删文件）、`record-proposal`、`approve-proposal`、`review` |
| 保留策略 | `budget`、`forget`（`fifo`、`lru`、`decay`、`salience`、`consolidate_then_forget`）、`tiers` / `tier`（冷热层）、`cold-archive`、`cold-restore`、`layer` / `layer-set`、`consolidate` |
| 关联记忆 | `memory-related-build`、`memory-related` |
| 投影 | `okf export` / `okf validate`（[OKF v0.2](https://github.com/GoogleCloudPlatform/open-knowledge-format) 包）、`notes`（Markdown 笔记索引）、`code`（本地代码检索） |

搜索时每个查询词单独匹配、按 bm25 排序，所以直接输入一整句话也能找到相关记忆。`memory_search` 默认走 FTS5；配置了本地 OpenAI 兼容的 embedding 服务时，可以转交给可选的混合检索器。每个命令都有 `--help`；全局的 `--now` 和 `--id-seed` 可以让运行结果完全确定，方便测试。

## 💬 会话库：所有 agent 汇到一处

`memfw history ingest` 读取各个 agent 自己的会话文件，把规范化后的对话轮次写进同一个本地库（`~/.local/share/memfw/conversations.sqlite`）。它是增量的：没变的会话直接跳过，追加的部分从上次存到的那一轮接着读。每一轮写入前，所有文本字段都要过一遍密钥扫描：一旦命中，整轮扣下不存；其余内容一律脱敏后再写。原始行从不复制，memfw 也从不改动 agent 自己的存储。

**已注册 14 个 reader**（`memfw history agents` 会列出它们，以及本机上实际存在哪些会话目录，包括编排 worker 的隔离 HOME）：

| reader | agent 或应用 | 默认会话目录 |
|---|---|---|
| `claude` | Claude Code | `~/.claude/projects` |
| `codex` | Codex CLI | `~/.codex/sessions`、`~/.codex/archived_sessions` |
| `gemini` | Gemini CLI | `~/.gemini/tmp` |
| `antigravity` | Google Antigravity（桌面应用、IDE 和 CLI） | `~/.gemini/antigravity*` |
| `cursor` | Cursor CLI（`cursor-agent`） | `~/.cursor/chats` |
| `cursor-ide` | Cursor IDE | Cursor 的 `User` 目录（macOS 和 Linux） |
| `opencode` | OpenCode | `~/.local/share/opencode` |
| `kimi` | Kimi Code CLI | `~/.kimi-code/sessions` |
| `kimi-desktop` | Kimi 桌面应用 | 应用内嵌的 agent 目录 |
| `grok` | Grok CLI | `~/.grok/sessions` |
| `glm` | Z Code CLI（智谱 GLM） | `~/.zcode/cli`、`~/.zcode-cli/sessions` |
| `deepseek` | DeepSeek harness CLI（`dsh`） | `~/.dsh/sessions` |
| `minimax` | MiniMax Agent CLI | `~/.minimax/v2/sessions` |
| `stepfun` | 阶跃星辰 `stepcode` | `~/.stepcode/agent/sessions` |

```sh
memfw history agents
memfw history ingest                 # 全部已注册的 reader
memfw history ingest claude codex    # 只导入这两个
```

### 跨应用共享

`memfw conversations`（以及对应的 MCP 工具）让各个 agent 应用以只读方式访问会话库，可见范围按 `vault.yaml` 里的规则过滤。在你设置 `conversation_share.enabled: true` 之前，共享是**关闭**的。

```sh
memfw conversations list --limit 20
memfw conversations search "migration rollback" --app codex
memfw conversations get SESSION_ID --start 0 --max-bytes 16384
memfw conversations handoff SESSION_ID --for kimi
```

```yaml
conversation_share:
  enabled: true
  apps:
    some-app: {readable: false}          # 对谁都不返回，也不处理
    other-app: {can_read: [claude, codex]}
conversation_optout:
  agents: [private-app]                  # 不导入；已存的行会被删除
```

所有输出在出库时会再脱敏一次，有大小上限，并标明“这是数据，不是指令”。被隐藏的会话和不存在的会话，返回结果一模一样。这只是互相配合的本地应用之间的可见性设置，不是身份认证。

### 会话 worker

`memfw conversations worker` 每次运行都是幂等、加锁、有预算上限的一轮：先导入，再找出空闲的会话，然后依次跑 `chunk_summary` → `session_summary` → `memory_claims` → `episode` 任务。重跑不会重复已完成的工作，崩溃后能接着跑；积压很多时，键集游标会分几轮扫完，既不漏也不重复。它只打印计数。

- 默认后端是 `extractive`，不调用模型。`--backend module:factory` 可以换成别的后端；非本地后端还要加 `--allow-cloud`。`vault.yaml` 不能指定后端。
- 模型提出的论断要先对照原会话核实，再暂存到 `_candidates/dream/conv-*/` 等人审核。episode 会排进队列，供 `dream skills --from-queue` 使用。
- `memfw conversations status` 显示任务和 episode 计数（不含文本）。`memfw conversations schedule --print` 打印一份 launchd plist，装不装由你自己决定。

### 会话记录索引

`memfw history index | list | search | read` 是另一套只读索引，针对你自己配置的会话日志（`vault.yaml` 里的 `history_sources`，或 `--source NAME=PATH`）：Claude Code 和 Codex 的 JSONL、通用的 worker / ticket JSONL，以及纯文本 `.log`。每个命中都带一个不透明引用，比如 `codex:3f9c2a71d04e8b65@18342`，用 `memfw history read` 打开就是一段脱敏、限长的上下文窗口。dream 把这套索引当作证据来源。详见 [`docs/history.md`](docs/history.md)。

## 🔁 交接：换一个 agent 接着做

某个 agent 额度用完了，或者任务需要换人做，就在另一个 agent 里开一个新会话，再递给它一份交接包。各家 CLI 的原生会话没法互相恢复，所以交接包是从会话库里拼出来的：

```sh
memfw handoff claude SESSION_ID --for kimi                   # Markdown，直接粘贴
memfw handoff codex SESSION_ID --format json --max-bytes 16384 --cwd ./worktree
```

交接包开头会声明：这是另一个 agent 的工作记录，不是指令。`[system]` 部分完全由代码生成：来源信息、目标（第一条用户消息，或 `--task-file` 的内容）、改过的文件、只读的 `git status` / `diff --stat`、最近几轮对话，以及之前交接包的谱系。只有加了 `--summarize module:factory`，才会多出一段 `[model_summarized: not verified]`；模型回复格式不对就直接丢掉，绝不修补。交接包会再脱敏一次，受 `--max-bytes`（2–64 KiB）限制，超了按固定顺序裁剪，并私密地存在会话库旁边（`--no-save` 只打印不保存）。退出码和 JSON schema（`memfw.handoff/v1`）是给编排系统用的稳定契约。详见 [`docs/handoff.md`](docs/handoff.md)。

## 🌙 做梦（dream）：先核对，再整理

`memfw dream run` 是一次离线、确定性的整理，从不写 vault。结果放在 `_candidates/dream/<dream_id>/`，附一份 `report.json`：

| 步骤 | 产出的提案 |
|---|---|
| **核对**（总会执行） | 对每条被引用的记忆，由会话证据决定 `confirm`、`correct`（只有一个替换值，且绝不是像密钥或账号的值）还是 `mark-disputed`；没有证据就不出候选 |
| **整理** | 在同一概念内：`merge`（内容相同，同义词归一）、`supersede`（同一属性取值不同，包括否定）、`mark-stale`；不同主体之间绝不合并 |
| **提炼** | 至少两个相互独立的事件（引用了不同会话）才能提炼出一条 `type: lesson` 记忆，反例会被点名列出 |
| **预算** | injected 层超过 200 行 / 25 KB 时提出下沉 |

```sh
memfw dream run --root /path/to/vault --dry-run
memfw dream run --root /path/to/vault --focus "deploy" --max-sessions 20
```

记忆多了两个可选字段：`status`（`confirmed | unconfirmed | disputed`）和 `evidence`（会话记录引用）。旧文件照常解析。

### 技能候选

`memfw dream skills` 从在多个会话里反复出现的操作流程中，起草 `SKILL.md`：

- 先剥掉注入的样板文字（harness 提醒、指令文件的回显、在很多会话里一字不差出现的编排模板），只说“继续”的轮次不算新任务；
- 每次工具调用变成步骤签名：程序名加上固定词表里的子命令，主机、路径、分支这类自由参数一概不留；只读查看、裸解释器调用和 agent 自身的调度工具不算步骤；
- episode 按“做了什么”聚类；同一请求的重试或分发副本只算一个会话；一个流程至少要在 `--min-sessions`（默认 3）个独立会话里出现，并且来自至少两个 agent，或分布在至少两天；
- 一个步骤只有在足够多的会话里都出现才保留；只做查看的流程不出候选。

每个候选放在 `_candidates/skills/<id>/` 下，包含 `SKILL.md`（何时使用、前置条件、步骤及其意图、命令、输入和观察到的检查结果）、`evidence.json` 和 `candidate.json`（`approved: false`、`installed: false`，外加置信度）。文本会脱敏，路径和 URL 会遮盖，`skills_distill.private_terms` 里列出的词会变成 `<private>`。它什么都不安装：审核过的 `SKILL.md` 由人自己复制到位。

```sh
memfw dream skills --root /path/to/vault --dry-run --summary   # 只看报告
memfw dream skills --root /path/to/vault --from-queue          # 用 worker 排好的 episode
```

还可以用可选的本地钩子 `--drafter module:function` 润色草稿，它的输出会像证据一样被核实。详见 [`docs/dream.md`](docs/dream.md)。

## 🤖 MCP 工具

`memfw-mcp --root VAULT --app NAME` 通过 stdio 服务一个 vault，不提供直接写 vault 的工具。

| 工具 | 用途 |
|---|---|
| `memory_search` | 搜索 vault（FTS5，可选混合检索）；支持 `mode`、`source`、`include_history`、`as_of` |
| `memory_get` | 按 id 读取一条记忆 |
| `memory_remember` | 把候选暂存到 `_candidates/remember/`，附标签、关联 id 和建议 |
| `memory_related` | 派生的记忆关联边（按租户隔离） |
| `memory_context` | 按 token 预算给出常驻上下文，锚点和流程类记忆优先 |
| `memory_status` | vault 大小、层与层级计数、日志摘要 |
| `history_list`、`history_search`、`history_read` | 会话记录索引：带谱系的会话、带引用的脱敏片段、限长窗口 |
| `conversations_list`、`conversations_search`、`conversations_get` | 共享会话库，按调用方应用的可见性过滤 |
| `conversations_handoff` | 给 agent 应用的轻量交接视图，受共享策略约束 |
| `dream_run` | 跑一次 dream；默认只演练，只用确定性规则，每个租户最多 50 个待审 dream，每分钟最多一次 |
| `dream_list` | 当前租户已暂存的 dream，按时间倒序 |

## 🔒 隐私与权限

- **原始会话记录原地不动。** memfw 只读 agent 的会话文件，从不改写。进入会话库和索引的只有脱敏后的文本；看起来含密钥的轮次整轮扣下，绝不以文本形式存储。
- **两道脱敏。** 入库时脱敏一次，每次读取、交接和生成候选时出库再脱敏一次。邮箱地址和家目录用户名会被遮盖。
- **私密的存放位置。** 会话库、worker 状态和交接目录默认都在 `~/.local/share/memfw/`，目录权限 `0700`，文件权限 `0600`。放在 git 工作区、vault 内部或同步盘（iCloud、Dropbox、Google Drive、OneDrive、Syncthing 等）里会被拒绝；`conversation_store.synced_dirs` 可以再加别的目录。
- **退出与可见性。** `conversation_optout` 让某个 agent 处处被排除并删掉它的数据；`conversation_share.apps` 决定谁能读谁、worker 处理哪些应用。
- **不偷偷调用模型。** 所有模型钩子（`--backend`、`--summarize`、`--judge`、`--drafter`）默认关闭，非本地的还要加 `--allow-cloud`。MCP 的 `dream_run` 不能指定任何要运行的代码。
- **人工关口。** vault 默认 `permissions: propose_then_confirm`：agent 提议，人来落地（`candidate-apply`、`update`、`supersede`）。

## ⬆️ reader 升级顺序

旧版 memfw 的 reader 遇到不认识的 frontmatter 键会直接拒绝，所以带了新字段 `status` 或 `evidence` 的记忆，在旧 reader 那里会“消失”。多个工具共用一个 vault 时，按这个顺序来：

1. **先升级所有 reader：** 每一处 memfw 安装、MCP 服务，以及任何读这个 vault 的服务，让它们忽略未知键（并在 `memfw list` 的警告里列出来）。
2. **再升级 writer：** 负责落地候选的 `memfw` 命令行，以及会话 worker。
3. **最后才落地** dream 候选里的 `status` / `evidence`。

候选文件、报告和 worker 状态只会新增键或表，旧的 reader 和 worker 照常工作（旧 worker 只是忽略扫描游标）。细节见 [`docs/dream.md`](docs/dream.md#memory-fields)。

## 🌐 集成与联邦

memfw 是引擎，编排系统和它保持一定距离地集成：

- **只读 HTTP 服务**（`memfw-http/2` 契约：读写令牌分开、候选走 `/stage`、只开放根路径端点）让其他系统可以联邦检索 memfw，而不必复制或挂载 vault（[`docs/v1-integration.md`](docs/v1-integration.md)、[`docs/READONLY-SERVICE-UPGRADE.md`](docs/READONLY-SERVICE-UPGRADE.md)）；
- Conductor 编排器在额度耗尽自动交接时调用 `memfw handoff … --format json`，它的 worker 通过 MCP 服务使用 memfw（[`docs/handoff.md`](docs/handoff.md)）；
- 共用 vault 的外部记忆 reader 遵循上面的升级顺序。

## 📊 评估结果摘要

| 指标 | 结果 |
|---|---|
| 处理语料 | 10 段对话 · 5,882 轮 · 1,986 个问题（LoCoMo） |
| 有有效证据的问题 | 1,982 |
| 对比的保留策略 | FIFO、LRU、decay、salience、consolidate-then-forget |
| 整体校正后对比 | **0 / 40** 显著（4 个范围 × 10 组两两对比，Holm 校正） |
| 检索上限（全语料 BM25，128 token） | 证据召回 **36.56%**，查询条件下的 oracle 为 **95.04%** |
| 本地 Mem0 基线（n=18） | memfw 66.7%，Mem0 62.5%（@100% 证据召回），无显著差异 |

这些是**有边界的负面结果**，不是质量宣称：样本小、每组只跑一次，而且保留策略用的是逻辑投影，不是真实 vault 的容量测试。dream 的整理步骤用合成的重复、冲突和过期用例打分（`python -m eval.dreams`，见 [`eval/dreams/`](eval/dreams)）。完整证据和局限见 [`docs/evaluation.md`](docs/evaluation.md)。

## 📚 文档地图

| 文档 | 内容 |
|---|---|
| [`docs/history.md`](docs/history.md) | 会话记录索引：来源、格式、谱系、密钥扫描、引用 |
| [`docs/conversations-share.md`](docs/conversations-share.md) | 共享规则、worker、定时运行、在各应用里启用 |
| [`docs/handoff.md`](docs/handoff.md) | 交接包、大小与隐私规则、给编排系统的契约 |
| [`docs/dream.md`](docs/dream.md) | dream 各步骤、匹配规则、记忆字段、技能候选、局限 |
| [`docs/okf.md`](docs/okf.md) | OKF v0.2 导出与校验 |
| [`docs/COMPONENTS.zh-CN.md`](docs/COMPONENTS.zh-CN.md) | 笔记索引、代码检索、只读 HTTP 服务、关联投影 |
| [`docs/retrieval-architecture.md`](docs/retrieval-architecture.md) | 检索架构 |
| [`docs/v1-integration.md`](docs/v1-integration.md)、[`docs/READONLY-SERVICE-UPGRADE.md`](docs/READONLY-SERVICE-UPGRADE.md) | 联邦接口与服务升级 |
| [`docs/evaluation.md`](docs/evaluation.md)、[`reports/`](reports)、[`results/`](results) | 评估证据 |
| [`docs/RELEASE-READINESS.md`](docs/RELEASE-READINESS.md) | 实测检查结果与已知局限 |
| [`docs/legacy-design-notes.md`](docs/legacy-design-notes.md) | 早期设计笔记（历史资料） |

除 `COMPONENTS` 外，`docs/` 下的文档目前只有英文版。

## 📍 现状与边界

memfw 当前版本 **0.2.0**（2026-11-15），Apache-2.0 许可，在单机上离线运行。已经实现：vault 与哈希链事件日志、保留策略与 `10MB` 预算、可重建的 FTS5 索引与读路径（按词匹配、概念别名、多跳）、MCP 服务、章节级 notes 索引与代码检索、只读转录索引、带 14 个 reader 的会话库、共享与 worker、交接包、dream（核对、整理、提炼、预算、技能）、OKF v0.2 导出，以及只读 HTTP 服务（`memfw-http/2`）。逐条变更见 [`CHANGELOG.md`](CHANGELOG.md)。

**升级边界。** `0.2.0` 没有改动 vault 格式：`vault.yaml` 仍是 `schema_version: 1`，`0.1.x` 写出的文件保持原字节——解析再渲染逐字节一致，普通写入也不会再补默认字段。FTS5 索引是可丢弃的投影，**不会就地迁移**，升级后跑一次 `memfw index rebuild`（索引过期时 memfw 也会先重建再搜索）。

**边界。** 匹配是基于词面的（没有共同用词的改写会漏掉）；dream 只暂存候选（没有 `dream apply`，需要用 `update` / `supersede` 落地）；notes、转录和会话索引都是派生索引，与 vault 分开；只读 HTTP 服务用读写 token 鉴权，未配置写路径时一律拒绝；定时运行只提供打印出来的 launchd plist；评估规模较小。详见 [`docs/dream.md`](docs/dream.md#limitations) 和 [`docs/RELEASE-READINESS.md`](docs/RELEASE-READINESS.md)。

## 🛠️ 开发

`dev` extra 会装上运行时、测试工具和 `sqlite-vec`（没有单独的 `test` extra）：

```sh
uv sync --frozen --extra dev
.venv/bin/python -m pytest -q
.venv/bin/python -m mypy src
.venv/bin/python -m ruff check src/memfw tests
```

[`src/memfw`](src/memfw) 下的结构：`core`、`storage`、`log`（vault 状态与事件），`index`、`retrieval`、`notes`、`code_retrieval`（搜索），`policies`、`budget`、`curation`、`capture`（保留与审核），`history`（会话记录索引、会话库、各 reader），`conversations`（共享与 worker），`handoff`、`dream`、`okf`、`mcp`。

## 🤝 参与贡献

欢迎提 issue 和 PR。提 PR 之前请：

1. 跑一遍 `pytest -q`、`mypy src` 和 `ruff check src/memfw tests`；
2. 不要包含个人数据、凭据、私人笔记、真实会话记录或真实记忆；
3. 评估结论要有边界，并附上支撑它的报告或产物链接。

## 📄 许可证

本项目采用 [Apache License 2.0](LICENSE)。`src/memfw/ontology/t2s.py` 中的繁简转换表派生自 [OpenCC](https://github.com/BYVoid/OpenCC)（Apache-2.0），详见 [`NOTICE`](NOTICE)。

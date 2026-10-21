# Historical design notes

> Archived reference from the earlier project README. Some benchmarks and implementation details are historical snapshots; use the current root README and linked reports for current positioning and evidence.

# memfw

**遗忘本身才是真正记住的开始。**  
**Forget well to remember what matters.**

**Markdown 是记忆，SQLite 和向量是目录。**  
**Markdown is memory; SQLite and vectors are catalogs.**

`memfw` is a local-first Python memory vault. Current Markdown files are authoritative. A hash-chained JSONL event stream records metadata and state transitions. SQLite FTS5 is a disposable search projection. The framework does not require an LLM and does not send vault data to a remote service.

`memfw` 是本地优先的 Python 记忆库。当前 Markdown 文件是权威内容，带哈希链的 JSONL 事件流记录元数据与状态变化，SQLite FTS5 是可丢弃的搜索投影。框架不依赖 LLM，也不会把 vault 数据发送到远端。

## Control model / 控制原则

The control order is **human > mechanism > model**. Models may interpret context or propose a change. Deterministic mechanisms validate budgets, provenance fields, state transitions, and permission rules. A person remains responsible for content changes and anchor decisions. A local signature string is audit metadata; it is not a password, cryptographic identity proof, or sandbox.

控制顺序是 **人 > 机制 > 模型**。模型可以理解上下文或提出修改，确定性机制负责预算、来源字段、状态转换和权限规则。内容变更与 anchor 决策仍由人负责。本地 signature 字符串只是审计元数据，不是密码、密码学身份凭证或沙箱。

Memory classification and storage placement are separate axes:

记忆分类和存储位置是相互独立的两条轴：

| Classification / 分类 | Meaning / 含义 |
|---|---|
| `current` | Current memory / 当前记忆 |
| `consolidated` | A memory that has been distilled or integrated / 已沉淀或整合的记忆 |
| `anchor` | Human-pinned memory; protected from forgetting and ordinary mechanism content edits / 人工锚定的记忆；策略不可遗忘，机制不能普通改写其内容 |

| Placement / 存储层 | Meaning / 含义 |
|---|---|
| `hot` | Full Markdown is in the current vault and counted in hot scope / 当前 vault 中保存完整 Markdown，计入 hot 范围 |
| `cold` | Full Markdown is managed in a compressed archive; the current file is a searchable stub / 完整 Markdown 在受管理压缩归档中；当前文件是可搜索的 stub |

An `anchor` is protected by either `layer: anchor` or a non-empty legacy `anchor` value. The two axes do not imply each other: a consolidated memory can be hot or cold, and classification alone does not archive its body. Retrieval may still restore and warm an anchor stub; that placement-only operation preserves its human metadata and does not remove its anchor protection.

`layer: anchor` 或非空的旧式 `anchor` 字段都会保护记忆。三种状态都是记忆层：`consolidated` 指已沉淀或整合的记忆，不等同于单独的 knowledge 层。分类层与存储层互不推导：consolidated 记忆可以是 hot 或 cold；只修改分类不会自动归档正文。检索仍可恢复并预热 anchor stub；这种只改变存储位置的操作会保留人工元数据，不会取消 anchor 保护。

## Deterministic forgetting / 确定性遗忘策略

The selector is a pure function of candidates, limits, and an explicit timezone-aware `now`. It uses stable ID tie-breaks, emits a reason for each candidate, and does nothing when the current set already fits. Anchors are never selected for forgetting. Limits count UTF-8 encoded Markdown body bytes and/or record count; they do not count frontmatter, logs, indexes, archives, or filesystem allocation.

策略选择器只依赖候选项、预算和显式传入的带时区 `now`，是纯函数；并列时按稳定 ID 决胜，每个候选项都有原因。当前集合已满足预算时不做变更。Anchor 永不被策略淘汰。字节预算统计 Markdown 正文的 UTF-8 编码字节数，也可单独限制条数；不包含 frontmatter、日志、索引、归档或文件系统分配量。

The five policies are:

五种策略如下：

| Policy / 策略 | Selection rule / 选择规则 |
|---|---|
| `fifo` | Oldest `created_at` first, then ID. / 按 `created_at` 从旧到新，再按 ID。 |
| `lru` | Oldest recorded access first; use `created_at` when no access event exists, then ID. / 按最近访问时间从旧到新；无访问事件时回退到 `created_at`，再按 ID。 |
| `decay` | Score = `strength × 2^(-age_days / half_life_days) × log2(2 + access_count + reference_count) × (1 + ln(1 + interval_days)/5) × (1 + 0.5 × keyness)`. Defaults: 30-day half-life and threshold 0.20. Scores at or below the threshold rank first. If they are insufficient under budget pressure, higher scores follow in score order with an explicit fallback reason. / 分数由强度、年龄衰减、访问/引用次数、复习间隔与 keyness 共同计算。默认半衰期 30 天、阈值 0.20；低于或等于阈值者优先。预算压力仍未解除时，较高分候选按分数继续淘汰，并标明 fallback 原因。 |
| `salience` | Score = `ln(1 + access_count) + 1.5 × ln(1 + reference_count) + 2 × keyness + 3 × manually_marked`; lowest first, then age and ID. / 分数为访问频率、引用次数、keyness 和人工标记的加权值；低分优先，再按时间和 ID。 |
| `consolidate_then_forget` | A pure selector identifies consolidation pressure. A deterministic mock judge separately proposes bounded near-duplicate pairs; the proposal remains pending until a human approves and explicitly applies it. It never deletes a record directly. Without a suitable pair, it reports an unsatisfied policy instead of deleting a representative or unrelated memory. / 纯策略选择器判断是否有合并压力，确定性 mock judge 独立提出有限的近似重复配对；提议必须经人工批准并显式应用，不会直接删除。没有合适配对时明确报告策略未满足，不删除代表项或无关记忆。 |

The mock judge compares normalized character trigrams with a fixed 0.80 Jaccard threshold. This is deterministic lexical matching, not semantic understanding. It never calls a provider. Provider adapters only accept an explicitly injected transport; no network client is included. A proposal stores its erasable Markdown object separately and binds its payload, target document snapshots, and current raw Markdown hashes. Signatures are opaque audit metadata, not cryptographic authentication.

Mock judge 会用固定 0.80 Jaccard 阈值比较规范化字符三元组。这是确定性词面匹配，不是语义理解，不会调用任何模型服务。Provider adapter 只有在显式注入 transport 后才会工作；仓库不含网络客户端。提议正文位于可擦除 Markdown 对象，绑定 payload、目标文档快照和当前原始 Markdown 哈希。签名只是审计元数据，不是密码学身份验证。

Keyness is an optional, deterministic term-frequency distinctiveness score in `[0, 1]`; it measures how unusual a document's terms are against a reference corpus, not semantic importance. Policy plans use it only when `keyness_enabled: true`. By default each document is compared with the previous `keyness_window` vault documents, ordered by creation time then ID; the default window is 20. You may set `keyness_reference_path` to a local UTF-8 text file as the reference instead. The scorer ignores built-in English and Chinese stopwords and terms appearing fewer than twice in the document; it keeps only overrepresented terms, orders them by G², sums the top `keyness_top_k` (default 5), and maps the sum to `[0, 1]`. `%DIFF` is `null` when a term has zero reference frequency. The scorer uses local Jieba and a simple English tokenizer and makes no network calls or use of ground-truth labels. MLX live adapters also need an explicitly injected transport but do not need `MLX_API_KEY`.

Keyness 是 `[0, 1]` 范围内可选的确定性词频差异度分数，表示文档词项相对参考语料有多独特，不表示语义重要性。只有设置 `keyness_enabled: true` 后，策略计划才会使用它。默认情况下，每条文档与按创建时间和 ID 排序的前 20 条 vault 文档比较；`keyness_window` 可更改窗口大小。也可用 `keyness_reference_path` 指向本地 UTF-8 文本文件。评分会忽略内置中英文停用词及文档内出现少于两次的词，只保留相对参考语料过度代表的词项，按 G² 排序后累加前 `keyness_top_k` 项（默认 5），再映射到 `[0, 1]`。参考语料词频为零时，`%DIFF` 为 `null`。评分使用本地 Jieba 与简单英文分词器，不联网，也不使用评测标签。MLX live adapter 仍要求显式注入 transport，但不要求 `MLX_API_KEY`。

Budget profiles use decimal units: **1 MB = 1,000,000 bytes**, 10 MB = 10,000,000 bytes, and 100 MB = 100,000,000 bytes. `budget_mode: body` limits UTF-8 memory-body bytes and record count; `budget_mode: total` instead drives enforcement from allocated vault filesystem blocks (`st_blocks × 512` where available), including Markdown metadata, event history, archives, indexes, and other files. Total mode applies the configured policy until the measured target fits or returns a named floor such as `anchor_floor`, `metadata_floor`, `policy_exhausted`, or `approval_required`; the append-only audit log itself can contribute to a non-removable physical floor. Day 2 body/count measurements and Day 3 physical-byte measurements are reported separately and are not interchangeable.

预算档位使用十进制单位：**1 MB = 1,000,000 字节**，10 MB 和 100 MB 以此类推。`budget_mode: body` 限制 UTF-8 正文和条数；`budget_mode: total` 则根据 vault 的文件系统分配块（系统支持时为 `st_blocks × 512`）触发遗忘，计入 Markdown 元数据、事件历史、归档、索引和其他文件。total 模式会按配置策略持续执行，直到实测满足目标或返回 `anchor_floor`、`metadata_floor`、`policy_exhausted`、`approval_required` 等明确原因；只追加审计日志本身也可能形成不可移除的物理下限。Day 2 正文/条数测量与 Day 3 物理字节测量分开报告，不能互换。

The reproducible 500-record, 30-day experiment runs all five production selectors at all three limits, without an LLM. The report includes retained count/bytes, forgotten anchors, satisfaction, and consolidation proposal counts: [Markdown results](../reports/day2_policy_experiment.md) · [JSON manifest and results](../reports/day2_policy_experiment.json). The 1 MB consolidation result is explicitly unsatisfied: exact duplicate removal leaves 330 representative records and about 9.98 MB, so the policy does not pretend that the target was met. The other policies can satisfy the 1 MB budget in this generated fixture; all policies retain 500 records under 100 MB.

This Day 2 artifact measures pure selector plans on a 500-record fixture. It predates Day 3 human-reviewed merge orchestration and is not the end-to-end production workflow benchmark.

可复现实验使用生产策略选择器处理 500 条、跨度 30 天的数据，不调用 LLM，并比较五种策略与三个预算档位。报告包含保留条数/字节、被遗忘 anchor、预算满足状态和合并提议数：[Markdown 结果表](../reports/day2_policy_experiment.md) · [JSON 参数与结果](../reports/day2_policy_experiment.json)。1 MB 下的 consolidate 策略明确标记为未满足：移除精确重复后仍保留 330 条代表、约 9.98 MB，因此不会假装达到目标。该合成数据中其余策略能满足 1 MB；100 MB 下所有策略均保留 500 条。

该 Day 2 产物使用 500 条 fixture 测量纯策略计划，早于 Day 3 的人工复核合并编排；它不是端到端生产流程基准。

The Day 3 end-to-end experiment runs the production budget engine on cloned real vaults for all five policies and three total-physical-byte profiles, then compares two real capture-stream vaults: [Markdown report](../reports/day3_experiment.md) · [JSON results and manifest](../reports/day3_experiment.json). It uses seed `20261018`, 1,000 synthetic records across seven themes and 30 days, 100 access events, and ten protected anchors. All fifteen policy/profile runs preserve all ten anchors. Every 1 MB run is explicitly unsatisfied at `anchor_floor`; all 10 MB and 100 MB runs satisfy their measured limits. In this machine's run, always-on capture used 17,260,544 allocated bytes with 100% duty cycle and missed no important input. Intensity capture at threshold `0.65` used 7,995,392 bytes with 36.1% duty cycle, missed 269 important inputs, and caused 13 false wakes among 90 model-triggered wakes. Allocated-byte values depend on filesystem block accounting. This is a deterministic synthetic systems experiment, not evidence of conversational quality on natural users.

| Total profile | Satisfied policies | Final allocated bytes | Anchor loss |
|---|---:|---:|---:|
| 1 MB | 0 / 5 (`anchor_floor`) | 2,527,232–3,092,480 | 0 / 10 |
| 10 MB | 5 / 5 | 9,965,568–9,990,144 | 0 / 10 |
| 100 MB | 5 / 5 | 51,933,184 | 0 / 10 |

At 10 MB, important recall / noise retention were: FIFO 8.8% / 22.2%, LRU 8.6% / 23.4%, decay 19.2% / 13.8%, salience 14.6% / 18.0%, and consolidation 7.5% / 22.2%. These are fixture-specific measurements; inspect the linked full table for every policy/profile row and the capture denominators.

Day 3 端到端实验在克隆的真实 vault 上运行五种生产预算策略和三个 total 物理字节档位，并比较两种真实采集流 vault：[Markdown 报告](../reports/day3_experiment.md) · [JSON 结果与清单](../reports/day3_experiment.json)。实验使用 seed `20261018`，包含 7 个主题、30 天跨度的 1,000 条合成记忆、100 条访问事件和 10 条受保护 anchor。15 组策略/预算结果均保留全部 10 个 anchor。所有 1 MB 运行都明确以 `anchor_floor` 标记为未满足；10 MB 与 100 MB 运行均满足实测限制。在本机运行中，always-on 占用 17,260,544 个分配字节、duty cycle 为 100%，未漏掉重要输入；阈值 `0.65` 的强度采集占用 7,995,392 字节、duty cycle 为 36.1%，漏掉 269 条重要输入，并在 90 次模型唤醒中误唤醒 13 次。分配字节会随文件系统块统计变化。该实验是确定性的合成系统测试，不代表自然用户对话质量。

| Total 档位 | 满足策略数 | 最终分配字节 | Anchor 丢失 |
|---|---:|---:|---:|
| 1 MB | 0 / 5（`anchor_floor`） | 2,527,232–3,092,480 | 0 / 10 |
| 10 MB | 5 / 5 | 9,965,568–9,990,144 | 0 / 10 |
| 100 MB | 5 / 5 | 51,933,184 | 0 / 10 |

10 MB 档位的重要记忆召回率 / 噪声保留率为：FIFO 8.8% / 22.2%、LRU 8.6% / 23.4%、decay 19.2% / 13.8%、salience 14.6% / 18.0%、consolidation 7.5% / 22.2%。这些数据只适用于该 fixture；完整报告包含每个策略/预算组合及采集指标分母。

## Files, recovery, and trust boundaries / 文件、恢复与信任边界

```text
vault/
  vault.yaml                 # validated local configuration
  events.jsonl               # append-only metadata event chain
  events.head                # current event-chain head
  memories/<id>.md           # authoritative current Markdown
  .history/<id>/<sha256>.md  # bounded rollback/replay versions
  .archives/<id>.tar.zst     # cold-only archive of original Markdown
  .proposals/<id>.md         # erasable, hash-bound review objects
  knowledge/cards/<id>.md    # persisted insight/pattern/methodology cards
  .staging/capture-candidates/ # unconfirmed capture candidates, not memories
  .quarantine/               # invalid externally changed Markdown
  index.sqlite3              # disposable FTS5 projection
  .memfw.lock                # inter-process file lock
```

The default `history_keep` is 2 event-referenced Markdown versions per memory, including the current version during normal commits. Schema-3 history deltas store the exact original frontmatter and a content-addressed body hash; a shared body object can remain while the full Markdown version is pruned. Legacy schema-1 deltas retain and verify their `base_hash` dependency. Rollback accepts only hashes among the most recent `max(history_keep, archive_keep_history)` event references; a retained body object does not create another rollback slot. `archive_keep_history` separately defaults to one pre-stub Markdown version beside the cold stub; zero is supported. These retained copies trade storage for rollback availability. Pruning older versions limits rollback: if a version was pruned, that rollback is unavailable. Cold archiving stores the original Markdown in a managed `tar.zst`, replaces the current file with a summary stub, records the original raw Markdown SHA-256 as `archived_hash`, and prunes history according to `archive_keep_history`. `cold-restore` verifies the archived bytes and restores the exact original Markdown, removes the restored duplicate from the archive, and prunes byte-identical history copies; `--autowarm` then performs a separate tier change to `hot`. A rollback to a historical cold stub is allowed only while its referenced archive member still exists and matches `archived_hash`; after restore consumes that member, rollback fails closed instead of recreating an unusable stub.

默认 `history_keep` 为每条记忆保留 2 个事件引用的 Markdown 版本，正常提交时包含当前版本。Schema 3 历史差量保存原始 frontmatter 和内容寻址正文哈希；完整 Markdown 版本被裁剪后，共享正文对象仍可能保留。旧 schema 1 差量继续保留并验证其 `base_hash` 依赖。Rollback 只接受最近 `max(history_keep, archive_keep_history)` 个事件引用中的哈希；保留正文对象不会增加 rollback 槽位。`archive_keep_history` 独立配置，默认在 cold stub 旁保留一份 stub 前 Markdown，也支持设为 0。保留副本会牺牲存储空间以换取 rollback 可用性；被裁剪的版本无法再 rollback。冷归档把原文存入受管理的 `tar.zst`，将当前文件替换为摘要 stub，把原文 SHA-256 记为 `archived_hash`，并按 `archive_keep_history` 修剪历史。`cold-restore` 校验归档字节后精确恢复原 Markdown，从归档中移除该原文，并修剪重复历史副本；`--autowarm` 随后单独追加 tier 到 `hot` 的变更。只有引用的归档成员仍存在且匹配 `archived_hash` 时，才能 rollback 到历史 cold stub；restore 移除该成员后，rollback 会失败关闭，不会重建不可用的 stub。

Repository mutations use a shared file lock and a pending transaction manifest. If a process stops with a pending transaction, ordinary commands refuse to proceed until explicit `memfw recover` reconciles it. Log compaction commits its archive catalog before truncating active events; recovery verifies all logical segments, snapshots, and the canonical active tail before removing unreferenced data. Multiple logical segments are packed in one physical Zstandard container. Hard deletion commits a durable DELETE tombstone before erasing managed content; archived events remain metadata-only and auditable. Compaction retains full metadata history, so total event storage can continue to grow over time. `memfw log compact --root ./vault --dry-run` previews compaction; use `--apply` to archive old metadata events while keeping the configured recent tail (`log_keep_recent`, default 100). `memfw log verify --root ./vault` reports the verification level: active events and legacy archived payloads can be fully checked, while summary archives verify per-member hashes, snapshots, and the catalog chain but cannot recheck discarded original event payloads. `memfw log replay --root ./vault` projects committed terminal hashes without repairing files. `memfw fsck` registers valid outside edits, quarantines invalid Markdown, and records missing files as external deletions. These checks detect inconsistency; they cannot prove that a local administrator or process with filesystem access did not rewrite both data and hashes. The event chain is not signed by an external trust root.

Day-one vaults that lack the current event-chain fields or cold-stub `archived_hash` have no automatic legacy reader or migration path. Current readers fail closed on incompatible data; verification covers newly created and current-schema vaults, not lossless in-place upgrade of older vaults.

Repository 变更使用共享文件锁和 pending 事务清单。若进程中断留下 pending 状态，普通命令会拒绝继续，需显式运行 `memfw recover` 对账。日志压缩先提交归档目录，再截断活动事件；恢复时会先验证全部逻辑段、快照和规范活动尾部，再删除未引用数据。多个逻辑段打包在一个物理 Zstandard 容器中。硬删除会先提交持久化 DELETE 墓碑，再擦除受管理内容；归档事件仍以仅含元数据、可审计的形式保留。压缩保留完整元数据历史，因此事件总存储量会继续随时间增长。`memfw log compact --root ./vault --dry-run` 预览压缩；使用 `--apply` 将旧元数据事件归档，同时保留最近事件（`log_keep_recent` 默认 100）。`memfw log verify --root ./vault` 会报告验证级别：活动事件和旧式完整事件归档可完整校验；摘要归档会验证每个成员哈希、快照和目录链，但无法重新校验已丢弃的原始事件正文。`memfw log replay --root ./vault` 只投影已提交的终态哈希，不修复文件。`memfw fsck` 会登记格式有效的外部编辑、隔离无效 Markdown，并将缺失文件记录为外部删除。这些检查可以发现不一致，但无法证明拥有文件系统权限的本地管理员或进程没有同时改写数据和哈希。事件链没有外部信任根签名。

缺少当前事件链字段或冷存根 `archived_hash` 的第一天 vault 没有自动旧格式读取器或迁移路径。当前读取器会对不兼容数据失败关闭；验证覆盖新建和当前 schema vault，不承诺旧 vault 原位无损升级。

Agent permissions are local workflow rules in `vault.yaml`: `none`, `propose_then_confirm` (default), or `full`. If `permissions` is absent, a string `agent_writes` value uses the same three modes; legacy `true` maps to `full`, while `false` or a missing field maps to `propose_then_confirm`. An explicit `permissions` value takes precedence. `full` permits an identified agent with non-empty signature metadata to use direct repository CRUD on non-anchor memories. Bulk soft-forget through the curation API is different: an agent can request a plan but cannot apply it. In `none`, even a human-approved agent proposal must be applied by a human; the approving person explicitly invokes the apply operation. `propose_then_confirm` requires a current proposal whose payload hash and target hashes still match. Anchor set/unset is always human-only; a full-mode agent may propose it, but cannot apply it directly. The `anchor_always_hot` option defaults to `true`; an anchor cannot be archived cold under that default. The `signature` value is checked only as opaque metadata and is not cryptographically verified. These rules do not restrict a process that edits Markdown directly.

Agent 权限是 `vault.yaml` 中的本地工作流规则：`none`、默认的 `propose_then_confirm` 或 `full`。若没有 `permissions` 字段，字符串 `agent_writes` 使用相同的三种模式；旧布尔值 `true` 映射为 `full`，`false` 或未设置旧字段映射为 `propose_then_confirm`。显式设置 `permissions` 时以它为准。`full` 允许身份匹配且带非空签名元数据的 agent 通过 repository CRUD 直接修改非 anchor 记忆。批量 soft-forget 则不同：agent 可请求计划，但不能应用。`none` 下即使提议已获人工批准，也必须由 human 显式执行 apply。`propose_then_confirm` 要求当前提议的 payload 与目标哈希仍匹配。设置或取消 anchor 永远只允许 human 执行；full 模式 agent 也只能提出建议。`anchor_always_hot` 默认 `true`，此时 anchor 不能被归档到 cold。`signature` 只检查是否为非空的不透明元数据，不做密码学验证。这些规则无法约束直接编辑 Markdown 的进程。

Copyable example using fields read by the current repository and budget engine:

可直接复制的当前配置示例（字段由 repository 和预算引擎读取）：

```yaml
schema_version: 1
content_format: markdown-frontmatter
default_layer: current
default_tier: hot
permissions: propose_then_confirm
history_keep: 2
archive_keep_history: 1
log_keep_recent: 100
budget_level: 1MB
budget_mode: body
max_count: 500
budget_scope: hot
forget_policy: fifo
forget_action: soft
anchor_always_hot: true
auto_enforce_on_write: false
keyness_enabled: false
keyness_window: 20
keyness_top_k: 5
knowledge_protection_boost: 1.0
# Optional local UTF-8 reference file. Omit to use the rolling vault window.
# keyness_reference_path: /path/to/reference.txt
merge_fallback_policy: fifo
capture:
  high_threshold: 0.7
  low_threshold: 0.3
  standby_seconds: 30
  dormant_seconds: 120
  ring_seconds: 30
```

`max_count` is optional; it limits memory records in the selected scope. Valid `budget_level` values are `1MB`, `10MB`, and `100MB`; `budget_scope` is `hot` or `all`; `forget_policy` is one of the five names above; `forget_action` is `soft` or `hard`. `auto_enforce_on_write: false` keeps writes from invoking budget enforcement automatically.

`max_count` 可省略；设置后限制所选范围内的记忆条数。`budget_level` 支持 `1MB`、`10MB`、`100MB`；`budget_scope` 支持 `hot` 或 `all`；`forget_policy` 使用上文五种策略之一；`forget_action` 为 `soft` 或 `hard`。`auto_enforce_on_write: false` 表示写入时不自动执行预算淘汰。

`keyness_top_k` 是正整数，限制每条记忆汇总的显著过度代表词数。`knowledge_protection_boost` 默认 `1.0`；正值会在五种策略中统一降低被知识卡引用记忆的遗忘优先级，设为 `0` 可关闭。Anchor 仍是绝对保护。经批准的合并会在删除来源前将有效引用重定向到保留记忆，并写入审计历史；引用关系不等于内容已被验证。

`keyness_top_k` must be a positive integer and caps the significant overrepresented terms combined for each memory. `knowledge_protection_boost` defaults to `1.0`; a positive value lowers the forgetting priority of memories cited by knowledge cards across all five policies, and `0` disables this preference. Anchors remain absolutely protected. An approved merge redirects active citations to the retained memory before deleting the source and records the change in audit history; citation does not validate a card's claim.

`budget_mode: body` applies the body-byte/count budget. `budget_mode: total` measures allocated vault bytes after each committed operation and repeats the configured selector until the physical target and count target fit, or it reports an unsatisfied floor. The append-only audit log itself consumes space, so `metadata_floor` can remain even after all eligible memories are removed. Protected anchors report `anchor_floor` and their IDs. `policy_exhausted` means the chosen policy has no eligible action; consolidation can instead report `approval_required` with pending proposal IDs. A soft action reduces hot scope but does not reduce total memory count; choose `budget_scope: hot` when count cooling is the goal. Total-byte measurement is filesystem-specific and the unsatisfied event itself adds a small amount of audit data.

`budget_mode: body` 使用正文字节和条数预算。`budget_mode: total` 会在每个操作提交后测量 vault 文件系统分配量，并重复执行所选策略，直到物理占用与条数目标满足，或明确报告无法满足的底线。只追加审计日志也会占用空间，因此删除所有可淘汰记忆后仍可能遇到 `metadata_floor`。受保护的 anchor 会报告 `anchor_floor` 和阻塞 ID。`policy_exhausted` 表示策略没有可执行目标；合并策略可能报告 `approval_required` 并返回待批准提议 ID。Soft 操作会减少 hot 范围条数，但不会减少总记忆数；若目标是按条数降温，请使用 `budget_scope: hot`。总字节数依赖文件系统；未满足事件本身也会增加少量审计数据。

`RuntimeStats.history_bytes`, `archive_bytes`, and `index_bytes` are legacy logical-size fields; `events_bytes` and `memories_bytes` report allocated bytes. For complete non-overlapping categories, use `measure_logical_breakdown(repository)` or `measure_physical_breakdown(repository)`. Logical maps count file contents; physical maps use filesystem allocation where available and distinguish events, history, memories, anchors, archives, index, and other files.

`RuntimeStats.history_bytes`、`archive_bytes` 和 `index_bytes` 是旧版逻辑大小字段；`events_bytes` 和 `memories_bytes` 表示分配字节。需要完整且互不重叠的分类时，使用 `measure_logical_breakdown(repository)` 或 `measure_physical_breakdown(repository)`。逻辑映射统计文件内容；物理映射在系统支持时统计文件系统分配量，并区分事件、历史、记忆、anchor、归档、索引和其他文件。

## Capture and human review / 采集与人工复核

Capture uses `dormant`, `standby`, and `full` states. A threshold crossing or timeout changes state through a deterministic state machine; an optional bounded ring buffer keeps recent input available for an explicit full-capture transition. Stream simulation requires ordered, timezone-aware signals and a final timestamp record. Full-capture candidates are written to erasable `.staging/capture-candidates/` Markdown and are not formal memories until a human confirms them. Current capture state is not restored as an active device session after process restart; this package replays provided streams and persists staged candidates/events only.

采集状态为 `dormant`、`standby` 和 `full`。阈值变化或超时由确定性状态机处理；可选的有界 ring buffer 为显式进入 full 状态保留近期输入。流模拟要求信号按时间排序、时间带时区，并以结束时间记录收尾。Full capture 候选内容写入可擦除的 `.staging/capture-candidates/` Markdown；人工确认前不会成为正式记忆。进程重启后不会恢复成活动中的设备会话；当前包负责重放提供的流并持久化候选和事件。

```sh
memfw capture simulate --stream ./signals.jsonl --vault ./vault
memfw capture confirm --root ./vault --dry-run
memfw capture confirm --root ./vault --decision <candidate-id>=keep --apply
memfw review --root ./vault
memfw review --root ./vault --proposal-id <proposal-id> --decision approve --payload-hash <sha256>
memfw review --root ./vault --proposal-id <proposal-id> --apply
```

Review defaults to listing pending proposals. Approval binds to the exact payload and current target hashes; stale proposals are rejected. Applying is a separate human action. `--dry-run` previews decisions and applies without writing. Merge review records provenance on the representative before deleting approved duplicates. Hard deletion also erases proposal objects that reference deleted targets, except the currently applied merge object, which is erased after the completion event is committed.

复核命令默认列出待处理提议。批准会绑定确切 payload 与当前目标哈希；过期提议会被拒绝。批准与应用是分开的人工动作。`--dry-run` 只预览，不写入。合并复核先在代表记忆上记录来源，再删除获批重复项。硬删除也会擦除引用已删除目标的提议对象；当前正在应用的合并对象会在完成事件提交后擦除。

`JudgeAgent` is a four-method Protocol (`score_salience`, `find_near_duplicates`, `suggest_layer`, `wake_intensity`). Mock judges provide deterministic implementations. Provider-named adapters have `mock` and guarded `live` modes; live mode uses an explicitly injected transport. Provider environment-key gates remain, except MLX live mode does not require `MLX_API_KEY`. There is no built-in SDK or transport, and tests disable socket access. Proposal creation is not approval: the human can approve, reject, or edit a proposal. Edits create a new human-authored proposal and leave the original rejected. A batch preview such as `memfw review --approve-all --filter type=merge --dry-run` lists exactly what would be approved without changing the vault. `merge_fallback_policy` selects a non-consolidation policy to run after an approved merge if budget pressure remains; it defaults to FIFO and cannot recurse into consolidation.

`JudgeAgent` 是包含四个方法（`score_salience`、`find_near_duplicates`、`suggest_layer`、`wake_intensity`）的 Protocol。Mock judge 提供确定性实现。按 provider 命名的 adapter 支持 `mock` 和受限的 `live` 模式；live 使用显式注入的 transport。除 MLX live 模式不要求 `MLX_API_KEY` 外，其他 provider 的环境 key 限制仍保留。项目没有内置 SDK 或网络传输，测试也会禁用 socket。提议不等于批准：人工可以批准、拒绝或编辑提议。编辑会生成一条人工作者的新提议，原提议保持拒绝状态。批量预览 `memfw review --approve-all --filter type=merge --dry-run` 只列出将批准的项，不改 vault。获批合并后若预算仍有压力，`merge_fallback_policy` 指定一个非 consolidation 策略继续处理；默认 FIFO，且不允许再次递归合并。

Capture controls also accept human override signals. `remember` forces `full` and locks out automatic transitions; `forget` clears the ring and forces `dormant`; `auto` releases the override. In automatic mode, `high_threshold` enters full, `low_threshold` starts the quiet timer, `standby_seconds` (`T`) moves full to standby, and `dormant_seconds` (`T2`) moves standby to dormant. The ring retains only its configured recent window. These controls manage this replay/state-machine implementation; they do not capture system-wide audio or install a background daemon.

采集流还支持人工覆盖。`remember` 强制进入 `full` 并阻止自动转换；`forget` 清空 ring 并强制进入 `dormant`；`auto` 释放人工覆盖。在自动模式中，`high_threshold` 进入 full，`low_threshold` 开始安静计时，`standby_seconds`（`T`）使 full 转 standby，`dormant_seconds`（`T2`）使 standby 转 dormant。Ring 只保留配置的近期窗口。当前能力是流重放与状态机，不会采集系统级音频，也不会安装后台守护进程。

## Search and indexes / 搜索与索引

`index.sqlite3` contains a raw searchable metadata projection, including `archive_ref` and `archived_hash`, plus FTS5 search columns. It is not the authority for memory content. Rebuild is atomic and shares the repository file lock. Trigram FTS5 is preferred. On SQLite builds without the trigram tokenizer, the index uses the declared `jieba` dependency to segment text for FTS5 `unicode61`; it does not silently switch to an improvised tokenizer. A two-character query uses substring matching over title, body, summary, and tags; a one-character query returns no results. Rebuilding replaces an older index schema and remains safe because SQLite is disposable.

`index.sqlite3` 保存原始可搜索元数据投影（包括 `archive_ref` 和 `archived_hash`）及 FTS5 搜索列，但不是记忆内容的权威来源。重建使用原子替换并共享 repository 文件锁。优先使用 trigram FTS5；系统 SQLite 不支持 trigram 时，使用声明依赖 `jieba` 分词后写入 FTS5 `unicode61`，不会静默改用临时自制 tokenizer。两字查询会在标题、正文、摘要和标签中做子串匹配；一字查询不返回结果。索引可丢弃，因此重建可替换旧 schema。

The index API is read-only during search. The retrieval layer may rebuild a missing index, record access metadata, and autowarm cold hits. Cold retrieval indexes only the current stub's summary, title, and tags; it does **not** full-text search the archived body while that body remains cold. A matching stub can trigger autowarm, after which the full body is restored. `cold-restore` alone restores the exact raw Markdown bytes and archive-time metadata. Autowarm instead verifies those archived bytes, restores their body into the latest stub metadata (including human-edited anchor, tags, and strength), records `memory_restore`, then records a separate `tier_change` to hot. An anchor does not block this placement-only warm operation. Search returns IDs, not a promise that a result is safe or authorized for every caller.

Runtime duration, cold-restore latency, and allocated physical bytes are measurements, not deterministic persisted memory state. Retrieval timing is injectable for tests. Retrieval metrics are kept in memory and are not written to the event stream or Markdown; access itself is separately recorded as metadata.

索引 search API 本身只读。检索层可以重建缺失索引、记录访问元数据，并对 cold 命中执行 autowarm。Cold 检索只索引当前 stub 的摘要、标题和标签；stub 仍为 cold 时，**不会对归档正文做全文搜索**。stub 命中后可以触发 autowarm，再恢复完整正文。单独运行 `cold-restore` 会精确恢复归档时的原始 Markdown 字节和元数据。Autowarm 则先校验归档字节，把原文正文恢复到最新 stub 元数据上（保留人工编辑的 anchor、tags、strength 等），先记录 `memory_restore`，再单独记录 tier 变为 hot 的 `tier_change`。Anchor 不会阻止这种只改变存储位置的预热。检索只返回 ID，不代表结果对所有调用者都已授权或安全。

运行时耗时、cold 恢复延迟和物理分配字节数是测量值，不是确定性持久化记忆状态。检索计时器可注入以便测试。检索指标只保存在内存，不写入事件流或 Markdown；访问本身会作为独立元数据事件记录。

## Source map / 源码目录

```text
src/memfw/
  core/          models, Markdown repository, permissions and errors
  policies/      five pure forgetting selectors
  budget/        UTF-8 body/count limits and total physical-byte profile selection
  curation/      one-time policy ranking, judged merge review, and iterative budget enforcement
  layers/        current/consolidated/anchor transitions
  tiers/         hot/cold placement plans
  storage/       deterministic managed tar.zst archives
  index/         disposable SQLite FTS5 projection
  retrieval/     indexed lookup, access events and cold autowarm
  log/           event serialization and hash verification
  stats/         policy and runtime measurements
  permissions/   local actor/approval rules
  agents/        judge Protocols, deterministic mock judges, proposal models
  capture/       state machine, stream replay and human confirmation
  knowledge/     insight/pattern/methodology card types
  export/        export interface
  cli.py         Typer command line
```

Knowledge cards are stored as Markdown with YAML frontmatter under `knowledge/cards/`. `Repository.create_knowledge_card(card_id, kind, statement, source_refs)` accepts `insight`, `pattern`, or `methodology` and verifies that each source ID currently resolves in the repository. Hard deletion of a source marks dependent cards as missing but does not erase their statements; a soft archive keeps the source resolvable. Reads also check the stored file hash, so manual edits are surfaced as integrity errors. Source existence does not prove that a source supports a card's statement. There is no knowledge-card CLI command.

知识卡片以带 YAML frontmatter 的 Markdown 保存于 `knowledge/cards/`。`Repository.create_knowledge_card(card_id, kind, statement, source_refs)` 支持 `insight`、`pattern`、`methodology`，并在创建时确认每个 source ID 当前都能在 repository 中解析。硬删除来源后，依赖卡片会标记为 missing，但不会擦除卡片陈述；软归档仍可解析。读取时也会校验保存文件的哈希，因此手工编辑会作为完整性错误暴露。来源存在不等于来源足以支持卡片陈述。目前没有知识卡片 CLI 命令。

For example, create a memory first and use its returned ID as the source reference:

```python
source = repo.add(title="Observed behavior", body="The retry succeeds after reopening.")
repo.create_knowledge_card(
    card_id="retry-pattern",
    kind="pattern",
    statement="Reopening the connection allows this retry to succeed.",
    source_refs=[source.id],
)
```

例如，先创建一条记忆，再把返回的 ID 作为来源：

```python
source = repo.add(title="Observed behavior", body="The retry succeeds after reopening.")
repo.create_knowledge_card(
    card_id="retry-pattern",
    kind="pattern",
    statement="Reopening the connection allows this retry to succeed.",
    source_refs=[source.id],
)
```

## Day 4 keyness and evaluation / Day 4 keyness 与评测

The Day 4 generator interleaves content themes across timestamps and writes evaluation labels to a separate file. The keyness scorer and policies receive memory content and metadata only; they do not receive those labels. The local LoCoMo adapter consumes a provided JSON file and makes no remote calls. Run the quick matrix with `python scripts/day4_quick.py --count 200 --seed 20261018`. Day 3's historical generator placed themes in time blocks, so its topic comparisons are confounded with recency and should remain historical runtime evidence rather than a clean keyness result.

Day 4 生成器会将不同内容主题交错分布在时间线上，并把评测标签写入独立文件。Keyness 评分器和策略只接收记忆内容与元数据，不会接收这些标签。本地 LoCoMo adapter 读取用户提供的 JSON，不进行远程调用。可运行 `python scripts/day4_quick.py --count 200 --seed 20261018` 执行快速矩阵。Day 3 历史生成器按时间块分配主题，因此主题比较与近期性混杂；这些结果只作为历史运行时证据，不应解释为干净的 keyness 结果。

The bounded Day 4 quick report measured 131,072 allocated bytes at threshold `.85`, or 13.85% of the same stream’s 946,176-byte always-on baseline. This meets H6 (at most 20%) in that setup, while capturing only 11 of 101 assigned-important records (10.89% recall); it does not establish a useful general policy. The report is at [Day 4 results](../reports/day4_report.md). Tail-fit outputs remain partial: stored references were all zero and seven-theme frequencies were insufficient; access-count KS is descriptive, with no bootstrap p-value or power-law claim.

H6 快速评测报告在阈值 `.85` 下测得 131,072 个分配字节，占同一输入流 always-on 基线 946,176 字节的 13.85%。该设置满足 H6（不超过 20%），但只捕获了 101 条评测指定重要记录中的 11 条（召回率 10.89%）；这不能证明该策略普遍有效。报告见 [Day 4 结果](../reports/day4_report.md)。Tail-fit 仍是部分结果：存储引用计数全为零，七个主题频数样本不足；access-count KS 仅为描述值，没有 bootstrap p 值，也不支持幂律结论。

## Day 5 storage, protection, and statistical evaluation / Day 5 存储、保护与统计评测

Schema-3 history preserves the original frontmatter bytes and references a content-addressed body, so pruning a full Markdown version does not necessarily remove a shared body. Schema-1 `base_hash` dependencies remain supported. Rollback is still limited by retained event references; a body dependency is not an extra rollback target. Log compaction commits the archive catalog before truncating the active log. Multiple logical segments share one physical Zstandard container; recovery verifies all segment members, snapshots, and the canonical active tail before removing unreferenced data. `memfw log verify` reports its verification level: summary archives verify per-member hashes, snapshots, and the catalog chain, but cannot recheck discarded original event payloads. Capture staging uses a pending manifest; recovery reconciles interrupted writes and rejects valid lines that were never committed.

Schema-3 历史保留原始 frontmatter 字节，并引用内容寻址正文；因此裁剪完整 Markdown 版本后，共享正文仍可能保留。旧 schema-1 `base_hash` 依赖仍受支持。Rollback 仍由保留的事件引用限制；正文依赖不增加 rollback 目标。日志压缩会先提交归档目录，再截断活动日志。多个逻辑段共用一个物理 Zstandard 容器；恢复时先验证全部段成员、快照和规范活动尾部，再清理未引用数据。`memfw log verify` 会报告验证级别：摘要归档会校验成员哈希、快照和目录链，但无法重新校验已丢弃的原始事件正文。采集暂存使用 pending manifest；恢复会对账中断写入，并拒绝从未提交的有效日志行。

Knowledge-card references add a configurable priority preference across FIFO, LRU, decay, salience, and consolidation; anchors remain absolute. On an approved merge, active citations are repointed to the surviving memory before source deletion. A missing source is marked in card metadata, but this does not erase or fact-check the card statement.

知识卡来源会在 FIFO、LRU、decay、salience 和 consolidate 五种策略中获得可配置的优先保护；anchor 仍是绝对保护。合并获批后，活动引用会在来源删除前重定向到保留记忆。来源缺失会记录在卡片元数据中，但不会擦除卡片陈述或验证其事实性。

The heavy-tail experiment uses a discrete power-law model with Hurwitz-zeta normalization, numerical maximum likelihood, KS-based `xmin` selection, and a semiparametric bootstrap that refits the tail. It compares a discretized lognormal on the same tail support with a Vuong likelihood-ratio test. Small samples and zero-count series return an insufficient-sample status. These are model diagnostics, not proof that a process follows either distribution; numerical approximations and test limitations are stated in the report.

重尾实验使用 Hurwitz-zeta 归一化的离散幂律模型、数值最大似然估计、基于 KS 的 `xmin` 选择，以及会重新拟合尾部的半参数 bootstrap。它在相同尾部支持集上与离散化对数正态分布比较，并使用 Vuong 似然比检验。小样本和全零序列会返回样本不足状态。这些结果是模型诊断，不能证明过程服从任一分布；报告会注明数值近似和检验限制。

The Day 5 evaluation keeps generated truth labels separate from generator output and policy inputs. It exercises all five policies with self-made local fixtures. The official ten-sample dataset was not downloaded or evaluated because network access was denied. Synthetic results do not establish natural-user effectiveness; see [Day 5 report](../reports/day5_report.md) for the frozen commands, measurements, and limits.

The 181-record regression fixture at 983,040 allocated bytes is a storage invariant check, not the formal access-bearing experiment. The latter uses the corrected 1,000-record fixture with logged accesses and is reported separately.

Day 5 评测将生成真值标签与生成器输出及策略输入分开，并使用自制本地 fixture 测试五种策略。官方十样本数据集因网络访问被拒，未下载、未评测。合成结果不能证明真实用户效果；冻结后的命令、测量值和限制见 [Day 5 报告](../reports/day5_report.md)。

181 条记录、983,040 个分配字节的回归 fixture 用于检查存储不变量，不是正式的带访问记录实验。正式实验使用修正后的 1,000 条、带访问日志的 fixture，并在报告中单独呈现。

The corrected access-bearing matrix took 1,210.241517 seconds. At 1 MB, the five policies retained 119–121 records under the byte limit, below the approximate 200-record goal; at 10 MB, all retained 1,000 records and capacity was inactive. H6 passed in 0 of 16 capture cells. The 1,000-record access-tail fit used 200 bootstrap replicates (`xmin=3`, `n=238`, `alpha=2.3761`, goodness-of-fit `p=.0796`); the Vuong comparison was inconclusive (`p=.6382`). These synthetic outcomes do not establish real-user performance or a power-law process.

修正后的带访问记录矩阵耗时 1,210.241517 秒。1 MB 下五种策略保留 119–121 条，均满足字节上限，但低于约 200 条的目标；10 MB 下均保留 1,000 条，容量未形成压力。Capture 的 16 个格子中 H6 通过数为 0。1,000 条访问尾部拟合使用 200 次 bootstrap（`xmin=3`、`n=238`、`alpha=2.3761`、拟合优度 `p=.0796`）；Vuong 比较无结论（`p=.6382`）。这些合成结果不能证明真实用户效果或幂律过程。

## Day 6 append, source protection, and capture accounting / Day 6 追加、来源保护与采集计量

Capture-event append uses a warm repository cursor after its event head, catalog, sidecar, and process file signature are validated. A fresh repository instance or changed log rebuilds the cursor through full-chain validation; full event reads still validate the chain. This is a warm append optimization, not a guarantee that arbitrary cold reads or Markdown transactions are constant-time.

采集事件追加会在校验事件头、目录、sidecar 和进程内文件签名后使用 repository 暖游标。新建 repository 实例或日志变化时，会通过全链校验重建游标；完整事件读取仍会校验整条链。这是暖追加优化，不代表任意冷读取或 Markdown 事务都是常数时间。

The local LoCoMo evaluator measures evidence recall and question-hit fraction against the fixture's exact original turn IDs. A merged representative body does not receive credit for a deleted evidence ID through provenance alone. Day 6 includes the ten provided samples in official LoCoMo format, evaluated locally without answer generation, as well as separate seeded synthetic fixtures. These measurements are not official Mem0 scores and do not establish natural-user effectiveness.

本地 LoCoMo evaluator 按 fixture 中原始 turn ID 精确计算 evidence recall 和 question-hit fraction。合并后的代表记忆不会仅凭来源记录为已删除的 evidence ID 获得命中分。Day 6 在本地评测了已提供的 10 个官方格式 LoCoMo 样本，不生成答案；另外还有独立的固定种子合成 fixture。这些测量不是官方 Mem0 分数，也不能证明真实用户效果。

At session end, `pack_capture_candidates(repository)` verifies each staged candidate against its durable capture event, compresses the canonical JSONL, checks the decoded bytes, atomically installs the pack, and removes the raw WAL. Read-only reopen and recovery keep the packed form. A later append or human confirmation safely unpacks it first. H6 uses packed-stage allocated bytes divided by the same stream's always-on packed-stage allocated bytes; cells are eligible only when important-record recall is at least 70%, and pass when that packed-stage ratio is at most 20%. The all-in allocated-vault ratio is reported separately and does not determine H6. Empty-vault/config bytes and event-log bytes are also reported separately.

会话结束时，`pack_capture_candidates(repository)` 会将每个暂存候选与已持久化的采集事件核对，压缩规范 JSONL，验证解压字节，原子安装压缩包并删除原始 WAL。只读重开和恢复会保持压缩状态；后续追加或人工确认前会安全解包。H6 的分子是压缩暂存包的分配字节，分母是同一输入流 always-on 压缩暂存包的分配字节；重要记录召回率至少 70% 的格子才有资格评估，且该暂存比率不超过 20% 才算通过。全库分配字节比率单独报告，不参与 H6 判定。空库/配置字节和事件日志字节也分别列出。

Deleting a memory cited by a knowledge card requires a current human decision for that single source. A multi-target forget that includes a cited source is rejected before writes; split the review so each cited source is decided separately. For a manual API review, call `proposal = repo.propose_source_deletion(memory_id)`, inspect that one-source proposal, then call `decide_proposal(repo, proposal.proposal_id, "approve", payload_hash=proposal.payload_hash)` and `apply_approved(repo, proposal.proposal_id, actor="human")`. Simulated experiment approvals use the explicit `simulated_approver` actor and are labeled as simulations; they cannot approve cited-source deletion or act on anchors.

删除知识卡引用的记忆，需要针对该单一来源作出当前人工决定。包含被引用来源的多目标遗忘会在写入前被拒绝；应拆分审核，分别处理每个来源。手动 API 审核可调用 `proposal = repo.propose_source_deletion(memory_id)`，检查这条单来源提案，再调用 `decide_proposal(repo, proposal.proposal_id, "approve", payload_hash=proposal.payload_hash)` 和 `apply_approved(repo, proposal.proposal_id, actor="human")`。实验中的模拟批准使用明确的 `simulated_approver` actor 并标记为模拟；它不能批准删除被引用来源，也不能操作 anchor。

The final Day 6 experiment report records the frozen commands, measured outcomes, run identities, and limitations: [Day 6 report](../reports/day6_report.md). It separates the provided local ten-sample LoCoMo evaluation from seeded synthetic systems experiments. Neither establishes natural-user effectiveness or official Mem0 benchmark scores.

Day 6 最终实验报告记录冻结命令、测量结果、运行身份和限制：[Day 6 报告](../reports/day6_report.md)。报告将本地评测的 10 个 LoCoMo 样本与固定种子的合成系统实验分开；两者都不能证明真实用户效果，也不是官方 Mem0 基准分数。

## Day 7 capture gate / Day 7 采集门槛评测

Run the official local evaluation with `.venv/bin/python scripts/eval_locomo.py --day7 --data <MEMFW_DATA_DIR>/locomo10.json --output reports/day7_official.json --quick`. It uses all provided samples and questions, 1,000 paired bootstrap repeats, and a 200-record synthetic capture gate by default; `--gating-count` changes the gate size. The separate deterministic diagnostic is `python scripts/day7_gating.py --count 200 --seed 20261018`; its `--quick` flag caps that diagnostic at 40 records. Both compare a label-only ideal selector passed through the capture state machine, a past-only deterministic keyness/text score, generated capture intensity, and always-on capture. Labels are not passed to the text judge. The oracle reports ideal and state-machine-realized candidates separately because the state machine can overcapture.

运行官方本地评测：`.venv/bin/python scripts/eval_locomo.py --day7 --data <MEMFW_DATA_DIR>/locomo10.json --output reports/day7_official.json --quick`。该命令处理全部已提供样本与问题，使用 1,000 次配对 bootstrap，并默认运行 200 条记录的合成采集门槛；可用 `--gating-count` 调整门槛样本数。独立的确定性诊断命令为 `python scripts/day7_gating.py --count 200 --seed 20261018`；该诊断的 `--quick` 会将记录数限制为 40。两项评测都比较四种路径：仅用于评测的标签理想选择器（仍经过采集状态机）、只看过去文本的确定性 keyness/text 分数、生成的采集强度，以及 always-on。文本评测器不会接收标签。由于状态机可能额外采集，报告会分别列出 oracle 理想选择和实际状态机候选。

The output reports important recall, candidate fraction, persistent-ID fraction, stage and all-in logical/allocated bytes against always-on, plus empty-vault and event-log baseline/growth. H6 eligibility requires recall at least 70%. It reports separate pass flags for packed-stage logical and allocated ratios at or below 20%, with each gap to the threshold. The all-in vault ratio is separate. Peak bytes are sampled during acquisition and packing, including atomic-write temporary files and raw/pack coexistence; promotion after simulated keep confirmation has a separate peak. These are operation-boundary samples, not continuous filesystem tracing.

输出包含重要记忆召回率、候选比例、持久 ID 比例、相对 always-on 的暂存和全库逻辑/分配字节，以及空库与事件日志基线/增长。H6 仅在召回率至少 70% 时有资格评估。报告分别给出压缩暂存包逻辑字节和分配字节比例是否不超过 20%，并列出到阈值的差值；全库比例单独呈现。峰值计量覆盖采集和打包期间的操作采样，包括原子写临时文件以及 raw/pack 共存；模拟确认后提升记忆的峰值另列。这些是操作边界采样，不是连续文件系统跟踪。

Retention byte budgets use the canonical stored `MemoryDocument.body` after the Markdown round trip. Raw input turn-text bytes are reported separately. This is a logical retention budget, not a physical vault-size measurement. `MemoryIndex.ranked_search(query, limit=20, ranking="bm25", candidate_ids=None)` uses BM25 by default; `ranking="count"` selects the legacy hit-count ranker. New cited-source proposals use a semantic source snapshot. Markerless legacy proposals retain raw-Markdown hash checks, and cooling fails closed while a valid legacy proposal would become stale.

保留预算按 Markdown 往返后规范化存储的 `MemoryDocument.body` 计量。原始输入 turn 文本字节数单独报告。这是逻辑保留预算，不是全库物理容量测量。`MemoryIndex.ranked_search(query, limit=20, ranking="bm25", candidate_ids=None)` 默认使用 BM25；`ranking="count"` 使用旧版命中数排序器。新建的来源引用删除提案使用语义来源快照。无该标记的旧提案继续按原始 Markdown 哈希校验；若冷却会使有效旧提案失效，冷却会安全拒绝。

核心模块对应关系：`core` 管理 Markdown 与事件，`policies` 和 `budget` 负责确定性淘汰计划，`curation` 编排计划和应用，`tiers`/`layers` 分别管理存储位置和记忆层，`retrieval`/`index`/`storage` 负责检索、投影和冷归档。

## Quickstart and CLI / 快速开始与命令行

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'

memfw init ./vault
memfw add --root ./vault --id note-1 --title 'First note' --body 'A useful fact.'
memfw list --root ./vault
memfw index rebuild --root ./vault
memfw index search useful --root ./vault
memfw log verify --root ./vault
memfw tier status --root ./vault
memfw layer set note-1 consolidated --root ./vault --dry-run
```

The original aliases `memfw tiers status` and `memfw layer-set <id> <layer>` remain available. Mutating commands that support preview accept `--dry-run` (or `--apply` for an explicit write). This includes `init`, `add`, `update`, `delete`, `rollback`, `index rebuild`, `fsck`, `recover`, `cold-archive`, `cold-restore`, `record-proposal`, `approve-proposal`, `budget configure`, `forget run`, `tiers run`, and both layer-set routes. `forget run` defaults to dry-run; other commands show their default in `--help`. Always review the output before using `--apply` for an irreversible or destructive operation.

原有别名 `memfw tiers status` 和 `memfw layer-set <id> <layer>` 仍可使用。支持预览的变更命令接受 `--dry-run`；显式写入使用 `--apply`。包括 `init`、`add`、`update`、`delete`、`rollback`、`index rebuild`、`fsck`、`recover`、`cold-archive`、`cold-restore`、`record-proposal`、`approve-proposal`、`budget configure`、`forget run`、`tiers run` 以及两种 layer-set 路由。`forget run` 默认 dry-run；其余命令的默认值可用 `--help` 查看。对不可逆或破坏性操作，先审阅预览结果，再使用 `--apply`。

```sh
# Plan first; apply soft forgetting only after review.
memfw forget run --policy fifo --scope hot --action soft --root ./vault --dry-run
memfw forget run --policy fifo --scope hot --action soft --root ./vault --apply

# Inspect body budget separately from physical vault size.
memfw budget show --root ./vault
memfw stats --root ./vault

# Explicit cold archive and exact restore.
memfw cold-archive cold-2026-10 --root ./vault --dry-run
memfw cold-archive cold-2026-10 --root ./vault --apply
memfw cold-restore note-1 --root ./vault --apply
memfw cold-restore note-1 --autowarm --root ./vault --apply
```

`forget run` accepts five policies, `--scope hot|all`, and `--action soft|hard` (`cold|delete` are accepted aliases). Soft forgetting creates an auditable plan, moves selected memories to cold, and archives their originals as stubs. Hard forgetting removes managed current, history, quarantine, and archive copies and leaves a metadata tombstone; filesystem snapshots, backups, and media blocks are outside the guarantee. `scope=all` includes cold bodies by reading their managed archives. `auto_enforce_on_write` defaults to `false`; when explicitly enabled in config, add/update writes can trigger the configured policy/action/scope automatically.

`forget run` 支持五种策略、`--scope hot|all` 和 `--action soft|hard`（也接受 `cold|delete` 别名）。Soft forgetting 先生成可审阅计划，再把选中记忆移入 cold，并将原文归档为 stub。Hard forgetting 会删除受管理的当前文件、历史、隔离文件和归档副本，并保留元数据墓碑；文件系统快照、备份和介质块不在保证范围内。`scope=all` 会从受管理归档读取 cold 正文并纳入预算。配置 `auto_enforce_on_write` 默认 `false`；显式启用后，add/update 可以自动执行配置的策略、动作和范围。

The global deterministic inputs must be repeated for each CLI invocation:

每次 CLI 调用都要重复提供全局确定性输入：

```sh
memfw --now 2026-10-18T12:00:00+00:00 --id-seed replay-1 init ./vault
memfw --now 2026-10-18T12:00:00+00:00 --id-seed replay-1 add --root ./vault --title Note --body Body
memfw --now 2026-10-18T12:00:00+00:00 --id-seed replay-1 update <id> --root ./vault --body Revised
```

`--now` must include a timezone. `--id-seed` makes generated IDs repeatable against the same event history. Stable vault inputs, command order, times, and IDs are required for byte-identical replay. The printed init path naturally differs when comparing different vault directories.

`--now` 必须包含时区。`--id-seed` 会在事件历史相同时稳定生成 ID。要实现字节级一致回放，vault 输入、命令顺序、时间和 ID 必须一致。比较不同 vault 时，init 输出的路径本身会不同。

## Validation / 验证

The final Day 5 validation completed with 269 passing tests in 149.90 seconds. Ruff check passed, format check reported 73 files unchanged, and strict mypy passed on 73 files. The final matrix and capture grid are summarized above; see [Day 5 report](../reports/day5_report.md) for exact commands, measurements, and limits. The separate Day 4 snapshot remains documented in the [Day 4 report](../reports/day4_report.md).

Day 5 最终验证共 269 项通过，耗时 149.90 秒；Ruff check 通过，format check 显示 73 个文件未变，strict mypy 对 73 个文件检查通过。最终矩阵和 capture 网格见上文；具体命令、测量值和限制见 [Day 5 报告](../reports/day5_report.md)。Day 4 的独立快照仍见 [Day 4 报告](../reports/day4_report.md)。

```sh
pytest
ruff format --check src tests scripts
ruff check .
mypy --strict src tests scripts
```

## Roadmap / 路线图

- **Day 1 — durable local vault:** Markdown authority, metadata event chain, versioned rollback, cold archives, fsck/replay, and disposable FTS5 index.
- **Day 2 — deterministic lifecycle controls:** independent layer/tier axes, body/count budgets, five forgetting policies, soft/hard actions, cold restore/autowarm, access-aware retrieval, runtime usage reporting, and the reproducible synthetic policy comparison.
- **Day 3 — capture and review:** deterministic capture stream replay and states, staged candidates with human confirmation, hash-bound mock-judge proposals, human-only apply, and physical-byte budgets with explicit unsatisfied floors.
- **Day 4 — evidence-bounded keyness and knowledge:** optional rolling/local-reference term distinctiveness, persisted source-linked Markdown cards, and a clean synthetic/local LoCoMo evaluation harness. The bounded report is frozen; the full repository suite passed (234 tests in 165.67 seconds), with Ruff and strict mypy passing. Tail-fit is partial and does not establish a power law.

- **第一天——本地持久化：** Markdown 权威内容、元数据事件链、版本回滚、冷归档、fsck/replay 和可丢弃 FTS5 索引。
- **第二天——确定性生命周期控制：** 独立 layer/tier、正文与条数预算、五种遗忘策略、soft/hard 动作、冷恢复/autowarm、访问感知检索、运行时占用统计和可复现实验。
- **第三天——采集与复核：** 确定性采集流与状态机、待确认候选、绑定哈希的 mock judge 提议、人工专属应用，以及带明确未满足原因的物理字节预算。
- **第四天——有证据边界的 keyness 与知识卡：** 可选滚动/本地参考词项差异度、持久化且关联来源的 Markdown 卡片，以及干净合成数据/本地 LoCoMo 评测框架。快速评测报告已冻结；全仓测试通过（234 项，165.67 秒），Ruff 与 strict mypy 也通过。Tail-fit 是部分实现，不能据此认定幂律。

## Day 8 — Local MLX pilot

The staged local pilot runner, offline MLX transport, read-only importer, query/score approval binding, and byte-budget evaluator are documented in the Day 8 report (private pilot report kept locally). The five-record dry-run uses synthetic data only. Live inference remains pending until the local source and compatible model are prepared; no live performance result is claimed.

Day 8 的本地 MLX 试点 runner、离线 transport、人工审批流程和合成数据验证见Day 8 报告 (private pilot report kept locally)。真实数据和模型尚未就绪，因此没有真实推理或性能结果。

### Day 8 continuation — readiness and boundary

The local source and Qwen 4-bit model are now present, and `check-inputs` reports the local MLX runtime as available. The external `mlx-lm 0.32.2` install was verified. The sandbox cannot write to the private vault, so the real import and model stages remain unrun; the attempted optional dependency and offline lock update were rolled back because required MLX metadata was unavailable offline. See the current Day 8 pilot report (private pilot report kept locally) for source counts, exact local commands, approval boundary, and validation.

本地语料和 Qwen 4-bit 模型现已就绪，`check-inputs` 检查到 MLX runtime 可用；已核验外部安装的 `mlx-lm 0.32.2`。当前 sandbox 无法写入私有 vault，因此真实导入和推理仍未运行。离线缺少可验证的 MLX 元数据，故已回滚本轮尝试的可选依赖声明和锁文件更新。语料统计、本地执行命令、审批停点和验证结果见Day 8 续报 (private pilot report kept locally)。

# 检索健康度（`retrieval_health`）

英文版：[retrieval-health.md](retrieval-health.md)

## 是什么

每个检索出口本来就带着各自的降级信号，只是名字五花八门：`degraded` 标记或列表、`errors`、`recall.degraded`、`fallback_reason`、`recall_metrics.dropped`、上下文包槽位的 `degraded` 码。`memfw.retrieval.health` 只读这些已有字段，汇总成一个对象（schema `retrieval-health/1`），放在顶层键 `retrieval_health` 下，回答“这次检索结果有多可信”。

接入的出口：

- `memory_search`：所有模式（`hybrid`、`fts`、`vector`、`multihop`、`two_stage`）；
- `memory_context`：仅 v=2 上下文包。v1 的输出按设计保持不变；`unchanged: true` 的简短回复也不带；
- `conversations_search`：默认模式和 `two_stage`。

保证：

- 只由输出里已有的字段推导，不做额外 I/O，不读 vault；
- 结果、顺序、分数都不变；
- 没有降级时，与之前相比唯一的差别就是多了这一个键；
- 召回协调器（`memfw.recall`）本身没改，`from_recall` 读的是 `RecallLabels.as_dict()` / `RecallMetrics.as_dict()` 的字典；
- 端口契约不变（`PORT_VERSIONS` 不变），这只是输出元数据。

## 字段

| 字段 | 取值 | 含义 |
| --- | --- | --- |
| `schema` | `retrieval-health/1` | 版本号 |
| `mode` | `full` / `degraded` / `fallback` / `empty` | 这次回答走了哪种路径 |
| `confidence` | `high` / `medium` / `low` | 结果可信度 |
| `reasons` | 原因码列表（排序后） | 只能是下文原因码表里的码，未知码直接 `ValueError` |
| `score_kind` | `rank` / `bm25` / `cosine` / `mixed` / `none` | 命中上的分数是什么含义 |
| `note` | 一句中文 | 给人看的摘要 |

判定规则，先命中先定：

1. **没有结果**：`empty` / `low`。`reasons` 里会说明原因：`no_match`、`all_sources_failed` 或 `withheld_only`，另外列出其他信号；
2. **主路径失败、备用路径回答了**：`fallback` / `low`；
3. **有任何降级原因**：`degraded`；如果只剩字面匹配（有 `keyword_only`）为 `low`，否则 `medium`；
4. 其余：`full` / `high`。

informational 原因（`withheld:*`、`access_not_recorded`）会列出来，但**从不**导致降级。

## 原因码

`<name>`、`<n>`、`<category>` 是参数。`<name>` 只允许 `[A-Za-z0-9_.-]`，最长 64 字符，不合规的来源名记为 `unknown`。

| 原因码 | 含义 | 哪些出口会给 |
| --- | --- | --- |
| `semantic_unavailable` | 没有配置 embedder，语义侧没跑 | memory_search、two_stage recall、conversations_search（two_stage） |
| `semantic_off` | 本地性设置不让查询或摘要发给 embedder | memory_search、conversations_search（two_stage） |
| `semantic_partial` | 部分记忆没有向量（来自不在本机的 embedder，被保留下来） | memory_search（含 two_stage） |
| `summaries_unavailable` | 会话摘要/标签读不到 | conversations_search（two_stage） |
| `fts_fallback` | 语义侧什么都没给，结果只来自 FTS | memory_search |
| `keyword_only` | 只有字面（关键词）匹配产出了结果 | memory_search、two_stage recall、conversations_search（two_stage） |
| `expansion_empty` | multihop 扩展没有新增命中，只有第一跳结果 | memory_search（multihop） |
| `route_fallback` | 路由选中的提供方失败，由其余提供方回答 | two_stage recall |
| `qmd_live_read_errors:<n>` | QMD 实时索引有 `<n>` 次读取错误 | 仅客户端（见下文） |
| `source_timeout:<name>` | 来源 `<name>` 超时 | two_stage recall、memory_context |
| `source_busy:<name>` | 来源 `<name>` 忙（达到并发上限），被跳过 | two_stage recall、memory_context |
| `source_error:<name>` | 来源 `<name>` 失败或返回了无效输出 | memory_search、two_stage recall、memory_context、conversations_search（two_stage） |
| `source_unavailable:<name>` | 来源 `<name>` 报告自己不可用 | two_stage recall、memory_context |
| `resolver_error:<name>` | `<name>` 解析器失败，它的候选被丢弃 | two_stage recall |
| `adapters_shadow` | 召回适配器以 shadow 模式运行（不进入结果） | 仅客户端（见下文） |
| `adapters_timeout` | 至少一个召回适配器超时 | two_stage recall |
| `adapters_busy` | 至少一个召回适配器忙 | two_stage recall |
| `withheld:<category>:<n>` | 有 `<n>` 条匹配被策略隐藏（informational） | two_stage recall |
| `access_not_recorded` | 某条返回命中的访问事件或升温失败，命中本身不受影响（informational） | memory_search |
| `no_match` | 没有任何匹配 | 全部出口 |
| `all_sources_failed` | 所有来源都失败，空结果不能说明没有相关内容 | two_stage recall、memory_context |
| `withheld_only` | 所有匹配都被策略隐藏 | two_stage recall |

### withheld 类别

`<category>` 只能是 `privacy`、`tenant`、`retired`、`other`。recall 的丢弃原因按 `withheld_category` 映射：

| recall 丢弃原因 | 类别 |
| --- | --- |
| `privacy` | `privacy` |
| `tenant` | `tenant` |
| `archived`、`superseded`、`expired`、`status` | `retired` |
| 其他（如 `unjoined`） | `other` |

同一类别的计数相加。只在输出**已经暴露计数**的地方给，也就是 two_stage 的 `recall_metrics.dropped`；不会因此泄露更多信息。`conversations_search` 默认模式有意**不**报告被隐藏的条数。

## score_kind

| 值 | 含义 | 出现在 |
| --- | --- | --- |
| `rank` | 融合名次分（weight/(60+rank) 归一化）。只表示顺序，**不是相关度** | memory_search `hybrid`、two_stage、conversations_search two_stage |
| `bm25` | BM25 分 | memory_search `fts`、`multihop`，以及 hybrid/vector 回退到 FTS 时 |
| `cosine` | 余弦相似度 | memory_search `vector` |
| `mixed` | 多种分数混在一起 | 保留值，当前出口不给 |
| `none` | 命中不带分数 | conversations_search 默认模式（按时间倒序的关键词轮次）、memory_context |

## 今天的信号 → 原因码

| 出口 | 已有信号 | 原因码 |
| --- | --- | --- |
| conversations_search two_stage | `degraded: no_embedder` | `semantic_unavailable` |
| | `degraded: semantic_off` | `semantic_off` |
| | `degraded: embedder_error` | `source_error:embedder` |
| | `degraded: no_summaries` | `summaries_unavailable` |
| | 语义侧缺失且没有 tags 候选 | 另加 `keyword_only` |
| memory_search `errors` | `semantic_partial` | `semantic_partial` |
| | `semantic_query_withheld`、`rerank_withheld`、`rewrite_withheld` | `semantic_off` |
| | `access_not_recorded` | `access_not_recorded` |
| | `hybrid unavailable: ...` | `source_error:hybrid`，hybrid/vector 回退到 FTS 时 mode 为 `fallback`，另加 `fts_fallback` |
| | `fts index unavailable...` | `source_error:fts` |
| | `rerank:...`、`rewrite:...`、`trace:...` | `source_error:rerank` / `source_error:rewrite` / `source_error:trace` |
| memory_search hybrid/vector 实际由 `fts` 回答 | 没有 embedding 模型 | `semantic_unavailable` + `keyword_only` |
| | 有模型但语义侧为空 | `fts_fallback` + `keyword_only` |
| memory_search multihop | `degraded: true` | `expansion_empty` |
| 协调器 `recall.degraded` | `timeout` | `source_timeout:<name>` |
| | `busy` | `source_busy:<name>` |
| | `unavailable` | `source_unavailable:<name>` |
| | `error`、`invalid` 及其他 | `source_error:<name>` |
| | 非内置 provider（内置是 `fts`、`hybrid`）超时/忙 | 另加 `adapters_timeout` / `adapters_busy` |
| | `resolvers_degraded` | `resolver_error:<name>` |
| 协调器 `fallback_reason` | `routed_providers_failed` | `route_fallback`，mode `fallback` |
| | `all_providers_failed`，或 `fail_closed` | `all_sources_failed` |
| memory_context v=2 槽位 `degraded` | `unavailable`、`not_found` | `source_unavailable:<slot>` |
| | `timeout` / `busy` / 其他 | `source_timeout:<slot>` / `source_busy:<slot>` / `source_error:<slot>`；所有槽位都降级时视为 `all_sources_failed` |

## 只留给客户端的码

`adapters_shadow` 和 `qmd_live_read_errors:<n>` 在码表里，但 memfw 自己从不给出：它们描述的是客户端（例如 fleet-memory）那一侧的情况。客户端只要有 JSON 就能复用同一套码和规则：

```python
from memfw.retrieval.health import derive

health = derive(
    result_count=len(hits),
    reasons=["adapters_shadow", "qmd_live_read_errors:3"],
    score_kind="rank",
)
```

模块是纯函数（不碰 vault、不做 I/O、不导入 memfw 的其他部分），`derive` 会用 `check_reason` 校验每个码，未知码直接报错。

## 已知局限

- two_stage 的 `keyword_only` 只把 `fts` 视为字面匹配提供方；bm25s 这类同样是字面匹配的适配器不算，所以只有它们回答时不会标 `keyword_only`。
- `errors` 里不认识前缀的自由文本错误统一归为 `source_error:unknown`；conversations two_stage 里不认识的 `degraded` 码也一样。
- `note` 只是给人看的一句话，程序应读 `mode`、`confidence` 和 `reasons`。
- `conversations_search` 的 `two_stage` 保持整个回复的 `max_bytes` 上限：打包会话前先预留这个键两种可能形态（有会话 / 无会话）中较大的那份字节。接近上限时可能比以前少放一个会话或一个 turn 窗口，此时照常由 `truncated` 和 `next_offset` 说明。
- `memory_search` 的 `two_stage`：只有 JSON 的客户端按 `recall.providers` 或 `recall.degraded` 里是否出现 `hybrid` 推断有没有配置 embedder（`from_memory_search`）；memfw 自己的 Runtime 直接传真实配置。
- 暂未覆盖：只读 HTTP 服务的 `POST /context`（`memfw-http/3`，不变）和 `memfw://context/<slot>` MCP 资源。

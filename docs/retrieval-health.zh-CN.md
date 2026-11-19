# 检索健康度（`retrieval_health`）

英文版：[retrieval-health.md](retrieval-health.md)

## 是什么

每个检索出口本来就带着各自的降级信号，只是名字五花八门：`degraded` 标记或列表、`errors`、`recall.degraded`、`fallback_reason`、`recall_metrics.dropped`、上下文包槽位的 `degraded` 码。`memfw.retrieval.health` 只读这些已有字段，汇总成一个对象（schema `retrieval-health/1`），放在顶层键 `retrieval_health` 下，回答“这次检索结果有多可信”。

接入的出口：

- `memory_search`：所有模式（`hybrid`、`fts`、`vector`、`multihop`、`two_stage`）；
- `memory_context`：仅 v=2 上下文包。v1 的输出按设计保持不变；`unchanged: true` 的简短回复也不带；
- `conversations_search`：默认模式和 `two_stage`。

## 保证

- 只由输出里已有的字段推导，不做额外 I/O，不读 vault；
- 结果、顺序、分数都不变；
- 没有降级时，与之前相比唯一的差别就是多了这一个键；
- 召回协调器（`memfw.recall`）本身没改，`from_recall` 读的是 `RecallLabels.as_dict()` / `RecallMetrics.as_dict()` 的字典；
- 端口契约不变（`PORT_VERSIONS` 不变），这只是输出元数据；
- `memfw.retrieval.health` 不导入 memfw 的其他部分：`import memfw.retrieval.health` 只加载 `memfw`、`memfw.retrieval` 和它自己。包根 `memfw` 和 `memfw.retrieval` 的再导出都是惰性的（`from memfw import RelatedIndex`、`from memfw.retrieval import VaultRetriever` 照常可用，但只在用到时才加载）。

## 唯一的权威码表

memfw 的 `REASON_CODES` 是唯一的权威码表。它以数据形式由下面几处承载：

- `export_codes()`：返回码表及相关常量（schema `retrieval-health-codes/1`：`modes`、`confidences`、`score_kinds`、`affects_mode`、`withheld_categories`、`name_pattern`、`empty_order`、`codes`）；
- `export_codes_json()`：同一份数据的 JSON 文本（键排序、两空格缩进）；
- 提交进仓库的 [`docs/retrieval-health-codes.json`](retrieval-health-codes.json)：就是 `export_codes_json()` 的输出，有测试检查它与代码一致。

改码表后用下面的命令重新生成 JSON，并同步更新两份文档的表格：

```sh
python -m memfw.retrieval.health > docs/retrieval-health-codes.json
```

fleet-memory 在升级到 memfw >= 0.5.0 之前自带一份码表副本；升级后改用这些函数。fleet-memory 侧会有跨仓测试，把它的码表与这个 JSON 对比。

## 字段

| 字段 | 取值 | 含义 |
| --- | --- | --- |
| `schema` | `retrieval-health/1` | 版本号 |
| `mode` | `full` / `degraded` / `fallback` / `empty` | 这次回答走了哪种路径 |
| `confidence` | `high` / `medium` / `low` | 结果可信度 |
| `reasons` | 原因码列表（排序后） | 只能是下文原因码表里的码，未知码直接 `ValueError` |
| `score_kind` | `rank` / `bm25` / `cosine` / `mixed` / `keyword` / `none` | 命中上的分数是什么含义（见下文） |
| `note` | 一句中文 | 给人看的摘要 |

每个原因码都有一个 `affects_mode` 类别（见下文原因码表，代码里是 `REASON_CODES[code].affects_mode`，公开函数 `affects_mode(code)` 对具体的码返回它，取值集合是常量 `AFFECTS_MODE`）：

- `yes`：这次回答是 `degraded`；
- `no`：informational，只列出来，**从不**降低 mode 或 confidence；
- `fallback`：主路径失败，由备用路径回答；
- `empty`：只用来解释空结果。

降级由**原因码**承担，而不是由“结果长什么样”承担：例如没有 embedder 时给 `semantic_unavailable`（`yes`），而 `keyword_only` 只是描述结果来自字面匹配（`no`）。这套语义与 fleet-memory 对齐，客户端和 memfw 对同一份 JSON 得出相同的 mode 与 confidence。

## 判定规则

先命中先定：

1. **没有结果**：`empty` / `low`。`reasons` 里**恰好加一个** `empty` 类码，按 `_EMPTY_ORDER` 取第一个适用的：
   1. `all_sources_failed`：所有来源都失败；
   2. `page_empty`：这一页为空，但检索有候选（偏移超出了范围，或这一页放不下；来自 `conversations_search` two_stage）；
   3. `empty_degraded`：有任一 `yes` / `fallback` 类原因（或回退）：没有结果不代表不存在；
   4. `withheld_only`：所有匹配都被策略隐藏；
   5. `no_match`：没有匹配，且所有路径都健康。

   其他信号照常列出；
2. **有 `fallback` 类原因**（主路径失败、备用路径回答了）：`fallback` / `low`；
3. **有 `yes` 类原因**：`degraded` / `medium`（`degraded` 一律是 `medium`）；
4. 其余（只有 `no` 类原因，或没有原因）：`full` / `high`。

`keyword_only`、`adapters_shadow`、`withheld:<category>:<n>`、`access_not_recorded` 都是 informational，从不降低 mode 或 confidence。传给 `derive` 的 `empty` 类码只在结果为空时才起作用；有结果时会被去掉。

单源和多源输出的回退码不同：`fts_fallback`、`grep_fallback` 只用于**单源**输出（整个回答来自这个回退，`fallback`）；在**多源**输出（recall、context）里，某一个源自己走了回退记为 `source_fallback:<name>`（`yes`），整体只是降级。

## 原因码

`<name>`、`<n>`、`<category>` 是参数。`<name>`（provider、来源、槽位、解析器的名字）遵循 `NAME_PATTERN` `[a-z][a-z0-9_.-]{0,63}`：先转小写，不合规则记为 `unknown`。`<n>` 是 1–9 位数字，`<category>` 见下文 withheld 类别。

“由谁给出”一列写的是 memfw 的哪些出口会给这个码；“two_stage recall”指 `memory_search mode=two_stage`。标“客户端（fleet-memory）”的码在码表里，但 memfw 自己从不产生。

| 码 | affects_mode | 含义 | 由谁给出 |
| --- | --- | --- | --- |
| `semantic_unavailable` | `yes` | 没有配置 embedder，语义侧没跑 | memory_search、two_stage recall、conversations_search（two_stage） |
| `semantic_off` | `yes` | 本地性设置不让查询或摘要发给 embedder | memory_search（含 two_stage）、conversations_search（two_stage） |
| `semantic_partial` | `yes` | 部分 provider 返回了不完整的语义结果 | 客户端（fleet-memory） |
| `vectors_partial` | `yes` | 部分记忆没有向量（来自不在本机的 embedder，被保留下来） | memory_search（含 two_stage） |
| `semantic_empty` | `yes` | 语义侧跑了，但什么都没召回 | memory_search |
| `summaries_unavailable` | `yes` | 会话摘要/标签读不到 | conversations_search（two_stage） |
| `expansion_empty` | `yes` | multihop 扩展没有新增命中，只有第一跳结果 | memory_search（multihop） |
| `meta_index_stale` | `yes` | 回答依赖的某个元数据索引已过期 | 客户端（fleet-memory） |
| `qmd_live_read_errors:<n>` | `yes` | QMD 实时索引有 `<n>` 次读取错误 | 客户端（fleet-memory） |
| `source_timeout:<name>` | `yes` | 来源 `<name>` 超时 | two_stage recall、memory_context |
| `source_busy:<name>` | `yes` | 来源 `<name>` 忙（达到并发上限），被跳过 | two_stage recall、memory_context |
| `source_error:<name>` | `yes` | 来源 `<name>` 失败或返回了无效输出 | memory_search、two_stage recall、memory_context、conversations_search（two_stage） |
| `source_unavailable:<name>` | `yes` | 来源 `<name>` 报告自己不可用 | memory_search（fts 的 `index_stale`）、two_stage recall、memory_context |
| `source_fallback:<name>` | `yes` | 多源输出中，来源 `<name>` 自己走了回退 | memory_search two_stage（来自 `degraded_sources`）；其余由客户端给 |
| `resolver_error:<name>` | `yes` | `<name>` 解析器失败，它的候选被丢弃 | two_stage recall |
| `adapters_timeout` | `yes` | 至少一个召回适配器超时 | two_stage recall |
| `adapters_busy` | `yes` | 至少一个召回适配器忙 | two_stage recall |
| `fts_fallback` | `fallback` | 这个单源回答来自 FTS 回退（主路径失败） | memory_search |
| `grep_fallback` | `fallback` | 这个单源回答来自纯文本 grep 回退 | 客户端（fleet-memory） |
| `route_fallback` | `fallback` | 路由选中的提供方失败，由其余提供方回答 | two_stage recall |
| `keyword_only` | `no` | 只有字面（关键词）匹配产出了结果（提示，不降级） | memory_search、two_stage recall、conversations_search（two_stage） |
| `adapters_shadow` | `no` | 召回适配器以 shadow 模式运行（不进入结果） | 客户端（fleet-memory） |
| `withheld:<category>:<n>` | `no` | 有 `<n>` 条匹配被策略隐藏（privacy、tenant、retired、other） | two_stage recall |
| `access_not_recorded` | `no` | 某条返回命中的访问事件或升温失败，命中本身不受影响 | memory_search（含 two_stage） |
| `all_sources_failed` | `empty` | 所有来源都失败，空结果不能说明没有相关内容 | memory_search（含 two_stage）、memory_context |
| `page_empty` | `empty` | 这一页为空但检索有候选（偏移超出范围，或这一页放不下） | conversations_search（two_stage） |
| `empty_degraded` | `empty` | 有路径降级或回退时什么都没返回：不代表不存在 | 全部出口 |
| `withheld_only` | `empty` | 所有匹配都被策略隐藏 | two_stage recall |
| `no_match` | `empty` | 没有任何匹配，且所有路径都健康 | 全部出口 |

### withheld 类别

`<category>` 只能是 `privacy`、`tenant`、`retired`、`other`。recall 的丢弃原因按 `withheld_category` 映射：

| recall 丢弃原因 | 类别 |
| --- | --- |
| `privacy` | `privacy` |
| `tenant` | `tenant` |
| `archived`、`superseded`、`expired`、`status` | `retired` |
| 其他（如 `unjoined`） | `other` |

同一类别的计数相加。只在输出**已经暴露计数**的地方给，也就是 two_stage 的 `recall_metrics.dropped`；不会因此泄露更多信息。`conversations_search` 默认模式有意**不**报告被隐藏的条数：报告计数就等于透露有被隐藏的内容匹配上了。

## score_kind

| 值 | 含义 | 出现在 |
| --- | --- | --- |
| `rank` | 融合名次分（weight/(60+rank) 归一化）。只表示顺序，**不是相关度**，不要拿它设阈值 | memory_search `hybrid`、two_stage、conversations_search two_stage |
| `bm25` | BM25 全文分 | memory_search `fts`、`multihop`，以及 hybrid/vector 回退到 FTS 时 |
| `cosine` | 余弦相似度 | memory_search `vector` |
| `mixed` | 多种分数混在一起 | 保留值，当前出口不给 |
| `keyword` | 字面匹配顺序（如 grep、按时间倒序），没有排序分 | 客户端（如 grep 回退）；memfw 当前出口不给 |
| `none` | 命中不带分数 | conversations_search 默认模式（按时间倒序的关键词轮次）、memory_context |

## 今天的信号 → 原因码

| 出口 | 已有信号 | 原因码 |
| --- | --- | --- |
| conversations_search two_stage | `degraded: no_embedder` | `semantic_unavailable` → `degraded` / `medium` |
| | `degraded: semantic_off` | `semantic_off` |
| | `degraded: embedder_error` | `source_error:embedder` |
| | `degraded: no_summaries` | `summaries_unavailable` |
| | 其他 `degraded` 码 | `source_error:unknown` |
| | 语义侧缺失（前三个之一）且没有 tags 候选 | 另加 `keyword_only` |
| | 没有会话，但 `candidates.total > 0` | 空结果时加 `page_empty` |
| memory_search `errors` | `semantic_partial`（memory_search 内部的错误码：部分记忆没有向量） | `vectors_partial` |
| | `semantic_query_withheld`、`rerank_withheld`、`rewrite_withheld` | `semantic_off` |
| | `access_not_recorded` | `access_not_recorded` |
| | `index_stale`（共享 FTS 读取器：索引需要重建，返回为空） | `source_unavailable:fts` |
| | `hybrid unavailable: ...` | `source_error:hybrid` |
| | `fts index unavailable...` | `source_error:fts` |
| | `rerank:...`、`rewrite:...`、`trace:...` | `source_error:rerank` / `source_error:rewrite` / `source_error:trace` |
| | `unicode61 query terms unavailable...` | `source_error:query_terms` |
| | `alias expansion unavailable...` | `source_error:aliases` |
| | 其他错误文本 | `source_error:unknown` |
| memory_search hybrid/vector 实际由 `fts` 回答 | 语义路径异常（`errors` 里有 `hybrid unavailable: ...`） | `source_error:hybrid` + `fts_fallback` + `keyword_only` → `fallback` / `low` |
| | 本地性设置挡住了语义侧（`semantic_off`） | `semantic_off` + `keyword_only` → `degraded` / `medium` |
| | 没有 embedder（没有 embedding 模型） | `semantic_unavailable` + `keyword_only` → `degraded` / `medium` |
| | 有 embedder，但语义侧没召回 | `semantic_empty` + `keyword_only` → `degraded` / `medium` |
| memory_search hybrid/vector 自己回答 | `degraded: true` 且没有其他错误 | `semantic_empty` + `keyword_only` → `degraded` / `medium` |
| memory_search multihop | `degraded: true` | `expansion_empty` |
| memory_search 非 two_stage，没有结果 | 该模式读的每个源都报错或不可用（hybrid/vector：`hybrid` 和 `fts` 都是；fts/multihop：`fts`） | `all_sources_failed` |
| memory_search two_stage | `degraded_sources: [<name>, ...]`（页面降级的 provider，只在非空时出现） | 每个给 `source_fallback:<name>` |
| memory_search two_stage | 回复标了 `degraded`，但没有 `degraded_sources`，`recall.degraded`、`resolvers_degraded`、`fail_closed` 和 `errors` 也都没有（`degraded_sources` 之前的旧回复） | `source_fallback:unknown` |
| memory_search two_stage | 只因访问事件写入失败而标了 `degraded`（`errors: [access_not_recorded]`） | `access_not_recorded`（提示：`full` / `high`） |
| | `errors` | 同上面 memory_search `errors` 一节 |
| 协调器 `recall.degraded` | `timeout` | `source_timeout:<name>` |
| | `busy` | `source_busy:<name>` |
| | `unavailable` | `source_unavailable:<name>` |
| | `error`、`invalid` 及其他 | `source_error:<name>` |
| | 非内置 provider（内置是 `fts`、`hybrid`）超时/忙 | 另加 `adapters_timeout` / `adapters_busy` |
| | `resolvers_degraded` | `resolver_error:<name>` |
| 协调器其他信号 | 没有配置 embedder | `semantic_unavailable` → `degraded` / `medium` |
| | 回答的 provider 全是 `fts`，且语义侧缺失（没有 embedder，或有 `:hybrid` 来源码） | 另加 `keyword_only` |
| | `recall_metrics.dropped` | `withheld:<category>:<n>` |
| 协调器 `fallback_reason` | `routed_providers_failed` | `route_fallback`，mode `fallback` |
| | `all_providers_failed`，或 `fail_closed` | 空结果时 `all_sources_failed`，否则 mode `fallback` |
| memory_context v=2 槽位 `degraded` | `unavailable`、`not_found` | `source_unavailable:<slot>` |
| | `timeout` / `busy` / 其他 | `source_timeout:<slot>` / `source_busy:<slot>` / `source_error:<slot>`；所有槽位都降级时视为 `all_sources_failed` |

## 只留给客户端的码

`semantic_partial`、`meta_index_stale`、`qmd_live_read_errors:<n>`、`grep_fallback`、`adapters_shadow` 在码表里，但 memfw 自己从不给出；`source_fallback:<name>` memfw 按 `degraded_sources` 给出具体名字，只有旧回复才会是 `source_fallback:unknown`。它们描述的是客户端（例如 fleet-memory）那一侧的情况。客户端用同一套函数推导，词汇就保持一致：

```python
from memfw.retrieval.health import derive

health = derive(
    result_count=len(hits),
    reasons=["adapters_shadow", "qmd_live_read_errors:3", "source_fallback:qmd"],
    score_kind="rank",
)
```

模块是纯函数（不碰 vault、不做 I/O、不导入 memfw 的其他部分），`derive` 会用 `check_reason` 校验每个码，未知码直接报错。只有 JSON 的客户端可以读 `docs/retrieval-health-codes.json`，拿到同一份码表。

## 已知局限

- two_stage 的 `keyword_only`：只有回答的 provider 全是 `fts`（且语义侧缺失）时才加；bm25s 这类同样是字面匹配的适配器参与回答时不会标。反正 `keyword_only` 只是提示（affects_mode `no`），不影响 mode 和 confidence；降级由 `semantic_unavailable`、`source_error:hybrid` 等原因码表达。
- `errors` 里不认识前缀的自由文本错误统一归为 `source_error:unknown`；conversations two_stage 里不认识的 `degraded` 码也一样。回答仍标为降级，但说不出是哪个来源。
- 只有 `degraded_sources` 出现之前的旧回复，标了 `degraded` 却没有任何其他信号时，才给 `source_fallback:unknown`：回复里没说是哪个 provider 走了回退。
- `retrieval_health` 只反映输出里已有的信号；输出没报告的故障这里也看不到。
- `note` 只是给人看的一句话，程序应读 `mode`、`confidence` 和 `reasons`。
- `conversations_search` 的 `two_stage` 保持整个回复的 `max_bytes` 上限：打包会话前只按“有会话”的形态预留这个键的字节。没有会话的回复很小（什么都没追加），它的空结果形态一定放得下。接近上限时可能比以前少放一个会话或一个 turn 窗口，此时照常由 `truncated` 和 `next_offset` 说明。
- `memory_search` 的 `two_stage`：只有 JSON 的客户端按 `recall.providers` 或 `recall.degraded` 里是否出现 `hybrid` 推断有没有配置 embedder（`from_memory_search`）；memfw 自己的 Runtime 直接传真实配置。
- 暂未覆盖：只读 HTTP 服务的 `POST /context`（`memfw-http/3`，不变）和 `memfw://context/<slot>` MCP 资源。

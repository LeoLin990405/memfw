# memfw 组件

基于 memfw 引擎的可选组件，README 会链接到这里。

## Notes MCP 工具

- `notes_search` — FTS/向量/混合 section 检索；
- `notes_related` — 派生的笔记间关联边；
- `notes_get`、`notes_status`；
- `retrieval_feedback`、`retrieval_trace`。

## 笔记索引

`memfw.notes` 是 Markdown 笔记树的派生 section 级索引。它独立于记忆 vault，且绝不写入笔记：

- SQLite FTS5 词法召回（trigram，或 jieba + unicode61 回退）；
- 可选 sqlite-vec 向量召回；
- OpenAI 兼容 embedding 提供方（`HttpEmbeddingProvider`）；
- heading/frontmatter 分块、wikilink、基于 hash 的增量更新；
- RRF 混合检索，可降级为仅 FTS 检索。

```bash
memfw notes index --root ~/notes --db /tmp/notes.sqlite \
  --endpoint http://127.0.0.1:8001/v1/embeddings \
  --model qwen3-embedding --dimension 1024
memfw notes search "corpus collocations" --db /tmp/notes.sqlite \
  --endpoint http://127.0.0.1:8001/v1/embeddings \
  --model qwen3-embedding --dimension 1024
memfw notes status --db /tmp/notes.sqlite
memfw notes serve --root ~/notes --db /tmp/notes.sqlite \
  --endpoint http://127.0.0.1:8001/v1/embeddings \
  --model qwen3-embedding --dimension 1024
```

索引布局、MCP 服务以及检查点/恢复行为见[笔记索引](notes-index.md)。

通过 `pip install 'memfw[notes]'` 安装可选向量扩展。未安装时，同一 API 仍可使用全文检索，并将向量存入回退 BLOB 表。


## 代码检索

`memfw code` 提供面向 Agent 的本地代码检索路径，无需新增服务：

- 按 Python 文件、顶层函数/类分块（标准库 AST，无需 tree-sitter）；
- 词法 FTS + 可选本地 Qwen3-Embedding + 结构化定义/调用方查找；
- RRF 融合；embedding 不可用时回退到词法 + 结构检索；rerank 关闭；
- 轻量级仓库地图：入口文件、目录摘要、最近文件。

```bash
memfw code index --root . --db /tmp/memfw-code.sqlite
memfw code search "where is VaultRetriever defined" --db /tmp/memfw-code.sqlite
memfw code map --db /tmp/memfw-code.sqlite
```

要进行语义代码检索，请在 `index` 和 `search` 命令中添加 `--embedding-url`、`--embedding-model` 和 `--dimension`。代码块、符号和向量都是可丢弃的投影；源文件仍是权威。


## V1 集成状态

只读 MemFW V1 接口与 Conductor 联邦适配器见 [`docs/v1-integration.md`](v1-integration.md)。V1 不替代 Conductor 的提议/批准生命周期，也不会自动写入 MemFW。

Conductor v1（2026-11-07 发布）通过 `memfw-http/2` 契约访问 MemFW。这个契约包括：score、读写分离的 token、`/stage`、只接受根地址、拒绝重定向，以及在不支持 `forget` 时明确说明。



**2026-11-08 记忆库测试后的加固**（PR #15、#16）：

- **租户隔离：** `/related` 和 MCP `memory_related` 按租户隔离。边表会写入 tenant/scope，并在截断之前过滤；返回 ID 或标题前，会再校验一次当前租户。related 的召回理由保留真实来源，分数限定在 [0, 1] 之间。
- **`memory_remember`：** 同一秒写入的候选不再互相覆盖，每条都能批准生效。
- **外发 HTTP：** 不跟随重定向，也不转发 `Authorization`。
- **Notes 索引：** 跳过逃出 notes 根目录的符号链接。
- **重排：** 调用方不能再指定 rerank 端点，只能由服务端配置。
- **只读服务：** `scripts/memfw_readonly_service.py` 可独立运行，不依赖 `import memfw`。[`docs/READONLY-SERVICE-UPGRADE.md`](READONLY-SERVICE-UPGRADE.md) 说明如何把旧的只读部署升级到 `memfw-http/2`，包括 `/health` 契约检查。
- **字段别名：** 输入接受 `valid_until`、`superseded`，作为 `valid_to`、`superseded_by` 的别名；输出一律使用规范名。互相冲突的一对字段会被拒绝；被取代记录的 ID 按字符串精确比较。
- **测试：** #16 之后 `main` 上的全量测试为 652 个通过、2 个跳过（离线运行，不含 live/network 测试）。

**检索：读路径与多跳试点**（PR #18，2026-11-09）：

- **评测基座**（`eval/retrieval_r0/`）：六类合成用例（时间更新、冲突、删除、多租户、污染、多跳），统计功效分析，多跳题按完整证据链计分，并覆盖所有现有检索路径（HTTP `/search`、MCP 默认的混合检索）。
- **统一读路径：**
  - HTTP `/search` 和 MCP 共用同一套实现，结果附带来源和召回理由（`why`）。
  - 租户和 token 绑定，在 SQL 中先按租户过滤再排序。
  - 新鲜度以 Markdown 事实源为准：只检查返回的前 10 条结果，用按文件 stat 做键的缓存，读取前后各比对一次，对不上就判为不新鲜。
- **MCP 多跳试点：** 按 SAG2 的思路在查询时做一轮词项扩展，能区分 ID 和关键词，扩展失败时降级为普通检索。任何入口都不会调用判定模型。
- **合成集上的实测**（每类 160 题，独立运行 3 次）：
  - 多跳召回提升 +0.5；
  - 单跳召回不下降；
  - p95 延迟是单跳的 1.78 倍（上限为 2 倍）。

  这些都是合成数据上的结果，不代表真实使用场景。
- **全量测试：** 709 个通过，2 个跳过。

## 关联性投影

`memfw.related` 从显式 wikilink/`related`、共享 tag/课程、共享实体和词法重叠构建可丢弃的边表：

```bash
memfw notes related-build --notes-db /tmp/notes.sqlite --related-db /tmp/related.sqlite
memfw notes related "Week 02/...md" --related-db /tmp/related.sqlite
memfw memory-related-build --root ./vault --related-db /tmp/related.sqlite
memfw memory-related memory-id --related-db /tmp/related.sqlite

memfw notes related-sections-build --notes-db /tmp/notes.sqlite \
  --related-db /tmp/related.sqlite --max-sections 2000
```

section/条目边带有 tenant/scope 过滤器。可用 `memfw.related_annotation.generate_pairs` 生成 100 对人工标注 CSV，并在填写标签后用 `calibrate` 校准。

MCP 工具 `memory_related` 暴露记忆关联边；配置 related DB 后，可通过 notes MCP 使用 `notes_related`。关联边是派生数据，绝不写入 Markdown，且只为当前记忆建立索引。

可选的 Conductor 写入桥接会把人工批准的提议暂存到远程服务的非权威 `/stage` 端点；它只写入 `_candidates/`，并且仍需 MemFW 人工确认。

语义边是可选能力：在重建命令中添加 `--embedding-url`/`--embedding-model` 即可使用 `sqlite-vec` 进行 KNN。同一次构建中的所有索引源共享同一个 embedder/fingerprint。当前 QMD 覆盖使用本地 4,979 篇笔记的 memfw notes 索引；纯远程 QMD 命中在建立本地向量投影前不会纳入。语义分始终是独立方法，绝不替代显式链接。

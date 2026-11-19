# memfw 组件

基于 memfw 引擎的可选组件，README 会链接到这里。

## Notes MCP 工具

- `notes_search` — FTS/向量/混合 section 检索；
- `notes_related` — 派生的笔记间关联边；
- `notes_get`、`notes_status`；
- `retrieval_feedback`、`retrieval_trace`。

加上 `--clearance` / `MEMFW_NOTES_CLEARANCE`（配合 `--legacy-privacy-default`）后，所有工具只能看到隐私级别在 clearance 范围内的笔记，级别从笔记文件里读取；`notes_status` 也只统计可见的笔记。不设置就不过滤。

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


## 只读 HTTP 服务

`scripts/memfw_readonly_service.py` 提供 `memfw-http/2` 契约（读写分离的 token、`/search`、`/related`、`/stage`、`/health`）。它的读路径是库代码 `memfw.index.readonly`，所以部署时要从同一版本拷三个文件：

1. `scripts/memfw_readonly_service.py`（HTTP 入口）；
2. `src/memfw/index/readonly.py`（查询规则、`search_vault`）；
3. `src/memfw/index/read_path.py`（读路径共用字段）。

两个库文件只依赖标准库和 PyYAML。脚本按顺序加载读路径：先找旁边的 `readonly.py`，再找仓库里的 `src/memfw/index/`，最后找已安装的 `memfw`，并在日志里写明用了哪一个；三者都没有就报错退出，并指出缺了哪些文件。`read_path.py` 带有 `READ_PATH_VERSION`（0.3.0 和 0.4.0 中为 2）；脚本和 `readonly.py` 遇到更旧的副本会拒绝启动，这样只升级了一部分时，启动就会失败，而不是每次 `/search` 都报错。它从不写盘，所以升级后要先跑一次 `memfw index rebuild`。

`--clearance` / `MEMFW_READONLY_CLEARANCE`（例如 `public,shared`）把 `/search` 和 `/related` 限制在这些隐私级别；不设置就不过滤。没有 `privacy` 字段的记忆按 vault 的 `legacy_privacy_default` 处理（没设时是 `local-private`），所以要先设好它，否则旧记忆会全部被挡掉。见 [`READONLY-SERVICE-UPGRADE.md`](READONLY-SERVICE-UPGRADE.md)。

同样这三个文件还提供 `memfw-http/3`，按每个请求的 `MemFW-Contract: memfw-http/3` 头选择；不带这个头时，每个回应都和 v2 逐字节相同。v3 给 `/search` 命中加上 `recorded_at`，给 `/related` 加上 `why` 和 `provenance_type`，并新增 `POST /context`（只读地构建 `memory_context` v2 的 `core` 包；需要装好 `memfw` 包并配置 clearance，否则返回 404，`/health` 里也不列出）和 `POST /forget`（只认写令牌：在 `<vault>/_candidates/forget/` 下记一份删除请求候选，从不真正删除；被隐藏的目标和不存在的目标返回同样的 404；同一租户内按 `request_id` 幂等；每个租户最多 1000 份待审）。`memfw forget requests` 列出这些请求，`memfw forget residue ID` 核查被遗忘的记忆是否已从所有投影里消失。见 [`v1-integration.md`](v1-integration.md)。


## 独立 wheel：`memfw-ports` 与 `memfw-recall`

两个 uv workspace 成员直接用 memfw 自己的 `src/memfw/ports` 和 `src/memfw/recall` 构建 wheel（不复制源码）：

| 分发包 | 导入名 | 内容 |
|---|---|---|
| `memfw-ports`（`packages/memfw-ports`） | `memfw_ports` | 端口协议、值类型、契约测试套件 `memfw_ports.testing`、只依赖标准库的参考实现 |
| `memfw-recall`（`packages/memfw-recall`） | `memfw_recall` | 召回协调器，不含 memfw 内置的 vault 提供方 |

写适配器的人装上它们就能跑契约检查，不必安装 memfw。两者的版本都跟随 `memfw.ports.PORTS_API_VERSION`（目前是 `0.4.0`）。只提供 wheel（没有 sdist，也不支持 editable 安装），并且**没有发布到 PyPI**：

```bash
uv build --wheel --package memfw-ports
uv build --wheel --package memfw-recall
```

CI 的 `standalone-wheels` 任务会构建两者，装进一个没有 memfw 的全新 venv，再跑 `tests/standalone/wheel_smoke.py`。memfw 本身不依赖它们。在 memfw 进程里请用 `memfw.ports`；`memfw_ports` 是另一份拷贝，它的类和 memfw 的类永远不相等。见 [`ports-and-recall.md`](ports-and-recall.md#11-packaging-boundaries)。


## 流水线运行器：`memfw-pipeline`

`packages/memfw-pipeline`（wheel 名 `memfw_pipeline`，版本 0.1.0）是派生产物（准入视图、元数据索引、FTS / bm25s / Qdrant 索引、投影）的 Stage 契约和运行器。一个 stage 只实现 `freshness`、`inputs_digest`、`build`、`verify`、`publish`，以及可选的幂等 `post_publish` 步骤；其余由运行器统一提供：放在目标旁边的单写者锁（`.<target>.lock`，`O_NOFOLLOW`，被占用时先重试几秒，仍被占用则退出码 75）、开始时写 `running` 状态、在目标旁边的临时目录里构建、每个发布出去的产物里自带一份记录（`.memfw-pipeline.json`：输入摘要和计数）、原子交换目录（不支持时退回两次 rename，失败会回滚）、缩水保护（空构建或少于基线一半时退出码 5，除非加 `--allow-shrink`；基线优先取线上产物自带的记录）、新鲜度，以及只含计数和固定原因码的状态文件。输入摘要与线上产物的记录一致时跳过构建，但 post-publish 步骤照样运行。以 root 运行时默认开启路径信任检查。`memfw-pipeline unit` 打印加固过的 systemd 或 launchd 模板供审阅，不会安装任何东西。运行时只用标准库，外加 `memfw-ports` 0.3.0；memfw 本身从不导入它。用 `uv build --wheel --package memfw-pipeline` 构建（没有发布到 PyPI）。这一版只带一个示例 stage，真正的各个 stage 之后再迁移过来。见 [`pipeline.md`](pipeline.md)。


## DeepSeek 打标签适配器

`packages/adapters/memfw-adapter-deepseek`（wheel 名 `memfw_adapter_deepseek`，版本 0.1.1）只用标准库实现了 `ModelBackend` 端口。它给会话 worker 提供 `conv-v2` 格式的输出：每个分块和每个会话一段摘要，外加标签（项目、任务类型、组件、意图、结果）；它不抽取事实或论断。它**默认关闭**：只有 `vault.yaml` 里设置了 `conversations: {backend: "memfw_adapter_deepseek:factory", cloud: true}`，并且提供了 `DEEPSEEK_API_KEY`，worker 才会用它；每一轮都有 token 和费用上限。密钥只会发往服务商的 API 地址。memfw 本身从不导入这个适配器；它单独构建成 wheel（`uv build --wheel --package memfw-adapter-deepseek`），没有发布到 PyPI。见 [`conversations-share.md`](conversations-share.md#deepseek-tagging-adapter)。


## bm25s 召回适配器

`packages/adapters/memfw-adapter-bm25s`（wheel 名 `memfw_adapter_bm25s`，版本 0.1.1）是一个进程内的 `RecallProvider`：用 bm25s 0.3.16（MIT）给记忆排序，中文用 jieba 切词。它**默认关闭**：只有 `vault.yaml` 的 `runtime.recall.adapters` 里写了 `memfw_adapter_bm25s:factory`，MCP `memory_search` 的 `mode=two_stage` 才会用它。它只索引隐私级别在 `clearance` 里的记忆（默认 `public,shared`），索引放在 vault 之外的私有目录（`~/.local/share/memfw/adapters/bm25s/`）。memfw 本身从不导入它。见 [`adapter-bm25s.md`](adapter-bm25s.md)。

## Qdrant 召回适配器

`packages/adapters/memfw-adapter-qdrant`（wheel 名 `memfw_adapter_qdrant`，版本 0.1.2）是一个语义 `RecallProvider`，后端是运维自己运行的 Qdrant v1.20.x 服务（只绑 loopback，遥测关闭：`QDRANT__TELEMETRY_DISABLED=true`）。查询用 memfw 自己的 embedder 向量化，只取回记忆 id。它**默认关闭**；`memfw-adapter-qdrant sync` 只发送 `public`/`shared` 记忆，从不发送 `local-private`，也不在 Qdrant 里存正文。memfw 本身从不导入它。见 [`adapter-qdrant.md`](adapter-qdrant.md)。


## LightRAG 派生适配器

`packages/adapters/memfw-adapter-lightrag`（wheel 名 `memfw_adapter_lightrag`，版本 0.1.0）实现了 `Deriver` 端口，是运维自己运行的 `lightrag-server` 1.5.7（绑定在 `127.0.0.1`，带 API key）的 loopback HTTP 客户端：产出图谱视图和问答视图，其中每一条的来源都必须是请求过的、可共享的、现行的记忆。**默认关闭**；它产出的行只能以暂存候选的形式成为记忆。见 [`adapter-lightrag.md`](adapter-lightrag.md)。

## GraphRAG 派生适配器

`packages/adapters/memfw-adapter-graphrag`（wheel 名 `memfw_adapter_graphrag`，版本 0.1.0）实现了 `Deriver` 端口，把 GraphRAG 3.3.0 当作离线批处理任务：输入是一代 `memfw projection export` 里可共享的文档；只有运维主动调用时，才以子进程方式运行 `graphrag index`（除非 `cloud: true`，只连 loopback 的模型端点）；它输出的 CSV 表再读回来，成为图谱视图和社区报告视图。**默认关闭**；它产出的行只能以暂存候选的形式成为记忆。见 [`adapter-graphrag.md`](adapter-graphrag.md)。

## QMD 召回适配器

`packages/adapters/memfw-adapter-qmd`（wheel 名 `memfw_adapter_qmd`，版本 0.1.1）只用标准库和 PyYAML，对一个正在运行的 [QMD](https://github.com/tobi/qmd)（MIT，锁定 2.5.3）MCP 服务实现了两个 `RecallProvider`：`qmd` 查由 `memfw projection export` 建成的 collection，按投影 manifest 映射回记忆 id；`qmd-notes` 查 Knowledge-Hub 的 collection，返回 `kh:<路径>#<标题>` 引用。两者都只返回引用、QMD 的分数和来源标签；映射不到可见文档的结果一律丢弃（`local-private` 和 `unknown` 永远不返回）；**默认关闭**（要在 `AdaptersConfig` 的 `enabled` 里打开）。端点默认必须是 loopback（除非设 `allow_remote`），loopback 请求不走代理。QMD 不可用或超时时，协调层把它标成降级，由 FTS 回答。memfw 本身从不导入这个适配器。见 [`qmd-projection.md`](qmd-projection.md#qmd-recall-adapter) 和包内 README。

## LangExtract 派生适配器

`packages/adapters/memfw-adapter-langextract`（wheel 名 `memfw_adapter_langextract`，版本 0.1.0）实现了 `Deriver` 端口：用 LangExtract 1.7.2（由运维自行安装）从 `public`/`shared` 记忆里抽取能对应到原文字符位置的实体和关系，模型是 memfw 的 `ModelBackend`（自带一个只连 loopback 的 Ollama 后端），每轮有 token 预算。**默认关闭**；它产出的行只能以暂存候选的形式成为记忆。见 [`adapter-langextract.md`](adapter-langextract.md)。

## Tailnet 主机清单适配器

`packages/adapters/memfw-adapter-tailnet`（wheel 名 `memfw_adapter_tailnet`，版本 0.1.0，只用标准库）为 Tailscale tailnet 的机器列表实现了 `Ingestor` 端口。`memfw-adapter-tailnet scan` 读取 `tailscale status --json`（运维导出的文件，或者运行一次 CLI：固定参数、不经 shell、有截止时间和输出上限、去掉代理环境变量），每台机器只保留名字、MagicDNS 的第一段（tailnet 后缀只存哈希）、tailnet IP、OS、是否在线、最后在线时间和标签，并用哈希过的主机 id 标识（从不读取用户和登录信息、密钥和 endpoint；从别的 tailnet 共享进来的节点默认排除、需显式开启，服务商出口节点一律排除）；对新出现、回来、消失、改名或有其他变化的机器，各在 `_candidates/tailnet/` 下暂存一条只含身份字段的 `host-fact` 候选，没有变化的机器不产生候选，重试只会得到重复项。候选默认是 `local-private`，除非运维另行配置。不含名字的 `report.json` 统计名单内离线超过阈值的机器、一台都匹配不到或匹配到多台的名单条目，以及没有条目担保的机器（从不按机器自报的主机名匹配）；发现结果从不修改这份名单，也不决定数据发往哪里。**默认关闭**；memfw 本身从不导入它。见 [`adapter-tailnet.md`](adapter-tailnet.md)。

## Wiki 页面生成适配器

`packages/adapters/memfw-adapter-wikigen`（wheel 名 `memfw_adapter_wikigen`，版本 0.1.0，只用标准库）为 LLM Wiki 实现了 `Deriver` 端口（kind `wiki_page`）：读取一个只含放行文本（`public`/`shared`）的 `memfw-wiki-job/1` 任务包，产出一页 `memfw-wiki-page/1`。提示词固定页面的节和"每句都带引用"；引用不是任务里某个来源的原样写法、或者违反节、因果、生命周期、冲突规则的句子直接丢掉，从不修补；未决问题/冲突一节和来源列表由程序生成，不由模型写。模型是 memfw 的 `ModelBackend`（自带只连字面 loopback 地址的 Ollama 后端和不跑模型的 stub 后端；非本地后端必须 `cloud: true` 且任务来源全是 `public`）；任务里写明的模型 digest、生成器版本和提示词/采样设置（`temperature`、`seed`、`num_ctx`）必须与 worker 实际运行的一致，并有 token 预算、单次调用超时和整任务截止时间。它跑在装有本地 Ollama 的 worker 机器上，从不在存储主机上跑。**默认关闭**；页面只会成为暂存候选，memfw 本身从不导入它。见 [`adapter-wikigen.md`](adapter-wikigen.md)。

## PageIndex 召回适配器

`packages/adapters/memfw-adapter-pageindex`（wheel 名 `memfw_adapter_pageindex`，版本 0.1.1）是一个面向长文档的 `RecallProvider`，按 PageIndex 风格搜索目录树（标题、节点 id、页码范围、可选的简短摘要；从不含正文）。由 memfw 的 `ModelBackend` 挑选章节，或者用 `mode: titles` 不经模型直接给标题排序。它只返回 `kh:<路径>#<标题>` 和 `att:<id>#page=<n>` 引用。非本地后端需要 `cloud: true`，`local-private` 的目录树从不发给它，每轮有 token 和调用次数预算。目录树用 `memfw-pageindex build-md | import-tree | list` 离线构建或导入，以 `0600` 的 JSON 存在 vault 之外。**默认关闭**；memfw 本身从不导入它。见包内 README。

## OCR 代理适配器

`packages/adapters/memfw-adapter-ocr`（wheel 名 `memfw_adapter_ocr`，版本 0.1.1，只用标准库）把 PDF 或图片附件转成 `memfw.proxy` v1 导入文件：带规范化定位信息的文本片段、`coverage`、`source_sha256`、生成者，以及引擎给出时的逐段置信度。可用的引擎：以子进程方式通过 MCP stdio 调用的 OCR 服务（有截止时间和页数预算，结束时停掉整个进程组）、处理带文字层 PDF 的 `pdftotext`，或一个 `module:factory`（不在“只用本地”的保证范围内）。什么都不上传；只有隐私级别在 `clearance` 内（默认 `public,shared`）的附件才会交给引擎，处理前后都会核对原件的 sha256。`memfw-ocr-proxy run | check` 生成文件，`memfw attach proxy import` 把它存进来。**默认关闭**；memfw 本身从不导入它。见包内 README 和 [`attachment-proxies.md`](attachment-proxies.md)。


## 佐证投影

`memfw corroboration rebuild` 在 vault 之外生成佐证投影（`~/.local/share/memfw/corroboration/<key>.sqlite3`，文件里记着它属于哪个 vault）：每条记忆被哪些相互独立的（应用、会话）提到过，来源是记忆自己的 `conv:` 证据、会话 worker 暂存的论断，以及已接受的经验点。每个应用的每个会话只算一次；本身就引用了这条记忆 id 的来源、模型或 agent 生成的来源，以及被隐藏或已退出共享的应用都不算。只存 id、名称、计数和时间；文件权限 `0600`，每次重建逐字节一致，对 vault 只读不写。

```bash
memfw corroboration rebuild --root ./vault
memfw corroboration show MEMORY_ID --as-app codex --root ./vault
```

`show` 会把结果裁剪到 `--as-app` 可读的应用范围内。MCP `memory_search` 加上 `labels: true` 时，会给每一行加上 `corroboration`（`count`、`apps`、`sessions`、`first_seen`、`last_seen`），按服务端应用的 `can_read` 裁剪；无法给出时返回 `{"degraded": CODE}`。不加 `labels` 时输出不变。`memfw corroboration queue` 按审核顺序列出未解决的待解问题：冲突记忆得到至少两个可见应用佐证的排在前面（只读，默认 `--policy strict`）。见 [`epistemic-and-experience.md`](epistemic-and-experience.md#corroboration-derived)。


## Obsidian 投影

`memfw project obsidian --out DIR [--root VAULT] [--clearance public,shared] [--tenant T]` 生成一个只读的 Obsidian 看板：每条记忆一页，另有待解问题页和概念页；`about`、取代关系、待解问题的冲突与解答、概念的 `broader` / `narrower` 都写成 wikilink；还有 Bases 视图和一份 sha256 清单。它实现的是 `Projector` 端口。输出是确定性的（不含运行时间戳，顺序稳定），可以提交进 git 做 diff。被 clearance 挡掉的记忆不会留下页面、链接、计数或概念页。vault 只读不写，输出目录不能在 vault 里面，也不能是受保护的路径。重建时只动上一次清单里列出的文件。不运行这个命令就什么都不会发生。见 [`obsidian-projection.md`](obsidian-projection.md)。


## 概念层级与关系类型

概念文件可以写 `broader`（上位概念的 oid）；`narrower` 由它推导，从不写入。lint 会报告无效、悬空、已合并或成环的上位概念。带版本号的关系类型表 `src/memfw/ontology/relations.yaml` 列出了 memfw 已经存下的每一种带类型的关系，以及允许的节点类型和隐私/租户规则；`memfw concepts relations` 打印这张表，`memfw concepts lint-relations`（只读）拿它检查 vault。读路径不会顺着 `broader` 走。见 [`ontology.md`](ontology.md)。


## V1 集成状态

只读 MemFW V1 接口与 Conductor 联邦适配器见 [`docs/v1-integration.md`](v1-integration.md)。V1 不替代 Conductor 的提议/批准生命周期，也不会自动写入 MemFW。

Conductor v1（2026-11-07 发布）通过 `memfw-http/2` 契约访问 MemFW。这个契约包括：score、读写分离的 token、`/stage`、只接受根地址、拒绝重定向，以及在不支持 `forget` 时明确说明。



**2026-11-08 记忆库测试后的加固**（PR #15、#16）：

- **租户隔离：** `/related` 和 MCP `memory_related` 按租户隔离。边表会写入 tenant/scope，并在截断之前过滤；返回 ID 或标题前，会再校验一次当前租户。related 的召回理由保留真实来源，分数限定在 [0, 1] 之间。
- **`memory_remember`：** 同一秒写入的候选不再互相覆盖，每条都能批准生效。
- **外发 HTTP：** 不跟随重定向，也不转发 `Authorization`。
- **Notes 索引：** 跳过逃出 notes 根目录的符号链接。
- **重排：** 调用方不能再指定 rerank 端点，只能由服务端配置。
- **只读服务：** `scripts/memfw_readonly_service.py` 是 HTTP 入口，读取逻辑在库模块 `memfw.index.readonly` 里（导入时只用标准库和 PyYAML）。把 `src/memfw/index/` 下的 `readonly.py` 和 `read_path.py` 放在脚本旁边，没装 `memfw` 也能运行。[`docs/READONLY-SERVICE-UPGRADE.md`](READONLY-SERVICE-UPGRADE.md) 说明如何把旧的只读部署升级到 `memfw-http/2`，包括 `/health` 契约检查。
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

语义边是可选能力：在重建命令中添加 `--embedding-url`/`--embedding-model` 即可使用 `sqlite-vec` 进行 KNN。同一次构建中的所有索引源共享同一个 embedder/fingerprint。当前 QMD 覆盖使用本地的 memfw notes 索引；纯远程 QMD 命中在建立本地向量投影前不会纳入。语义分始终是独立方法，绝不替代显式链接。

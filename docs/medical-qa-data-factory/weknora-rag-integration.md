# WeKnora RAG 接入契约

> 来源：`E:\Cursor\WeKnora` v0.10 源码与 `docs/api/`，核对日期 2026-10-20。
> 本文只记录从源码读到的契约，不含推测。

## 端点

### 检索（不做 LLM 合成）

```
POST {base_url}/api/v1/knowledge-search
```

鉴权：`X-API-Key: sk-...`

请求体：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `query` | string | 是 | 检索文本 |
| `knowledge_base_id` | string | 二选一 | 单个知识库 ID |
| `knowledge_base_ids` | string[] | 二选一 | 多个知识库 ID |
| `knowledge_ids` | string[] | 否 | 限定到具体文档 |
| `tag_ids` | string[] | 否 | 单库内的标签过滤 |
| `mentioned_items` | array | 否 | 作用域标签提及 |

必须给出 `knowledge_base_id` 或 `knowledge_base_ids` 之一。

响应 `{success, data: [SearchResult]}`，`SearchResult` 字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 分块 ID，回读用这个 |
| `content` | string | 命中的分块文本 |
| `knowledge_id` | string | 所属文档 ID |
| `chunk_index` | int | 文档内序号 |
| `knowledge_title` | string | 文档标题 |
| `start_at` / `end_at` | int | 源文档中的字符偏移 |
| `seq` | int | 命中排序号 |
| `score` | float | RRF 融合得分，非原始向量相似度 |
| `match_type` | int | 命中通道，见下表 |
| `chunk_type` | string | `text` / `image` 等 |
| `metadata` | object | 自定义元数据 |
| `knowledge_filename` | string | 源文件名 |
| `knowledge_source` | string | `file` / `url` / `manual` |
| `parent_chunk_id` | string | 父分块 ID |
| `sub_chunk_id` | string[] | 子分块 ID |

`match_type` 的数值是线上契约，与服务端 `internal/types/embedding.go` 的 iota 顺序一致，不可重排：

| 值 | 名称 | 含义 |
|---:|---|---|
| 0 | vector | 向量通道 |
| 1 | keyword | 关键词通道 |
| 2 | nearby_chunk | 邻近分块 |
| 3 | history | 历史 |
| 4 | parent_chunk | 父分块 |
| 5 | relation_chunk | 关系分块 |
| 6 | graph | 图数据库 |
| 7 | web_search | 网络搜索 |
| 8 | direct_load | 按 ID 直接加载，未打分 |
| 9 | data_analysis | 分析流水线产出 |

### 分块回读

```
GET {base_url}/api/v1/chunks/by-id/{chunk_id}
```

响应 `{success, data: Chunk}`，`Chunk` 字段（`client/chunk.go`）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 分块 ID |
| `knowledge_id` | string | 所属文档 ID |
| `knowledge_base_id` | string | 所属知识库 ID |
| `content` | string | 分块正文 |
| `chunk_index` | int | 文档内序号 |
| `start_at` / `end_at` | int | 源文档中的字符偏移 |
| `chunk_type` | string | 分块类型 |
| `parent_chunk_id` | string | 父分块 ID |
| `content_hash` | string | 服务端的 hash，检索结果里没有 |

另有 `GET /api/v1/chunks/{knowledge_id}` 按文档分页列出分块（`page`、`page_size`）。

### 错误

HTTP 状态码即分类依据：错误体可能带 `{"code": 1003}` 形式的服务端错误码。

| 状态码 | 含义 |
|---:|---|
| 401 / 403 | 鉴权失败 |
| 404 | 资源不存在 |
| 429 | 限流 |
| 5xx | 服务端错误 |

## 与本项目契约的对应

| 本项目 | WeKnora |
|---|---|
| `LiteratureRetrievalPort.preflight()` | 组合能力与 corpus 快照声明 |
| `LiteratureRetrievalPort.search(request)` | `POST /knowledge-search` |
| `LiteratureRetrievalPort.read_chunk(request)` | `GET /chunks/by-id/:id` |
| `LiteratureSnapshot.snapshot_id` | 部署侧的语料版本标识 |
| `RetrievedChunk.chunk_id` | `SearchResult.id` |
| `RetrievedChunk.document_id` | `SearchResult.knowledge_id` |
| `RetrievedChunk.start_at` / `end_at` | `SearchResult.start_at` / `end_at` |

## 三条硬约束

1. **检索结果没有 `content_hash`。** hash 由本项目从取回的文本计算
   （`literature/providers/weknora.py` 的 `content_hash`），provider 不为自己的文本背书。
2. **检索与回读是两个请求。** 一条结果要成为 gold 文本证据，必须在同一个 snapshot 上
   通过 `read_chunk` 独立回读，且两侧文本的 hash 与字符偏移都一致。
3. **当前 WeKnora adapter 不宣称能固定语料快照。** 这两个端点没有经过验证的
   版本选择/服务端版本证明协议；即使配置了 `snapshot_id` 字符串，能力仍为
   `supports_pinned_snapshot=false`，返回快照为 `snapshot_id=null, pinned=false`。
   普通检索、分块回读及其一致性检查继续可用，但结果仅作 development/context candidate。
   正式证据执行器拒绝用它产出 gold；显式请求 `expected_snapshot_id` 时返回
   `provider_error/snapshot_unavailable`，不会静默忽略版本要求后继续请求。
   后续只有实际接通可核验的固定版本读取，才能开放这项能力，不能靠增加一个配置值放行。

检索返回 `success=false` 或损坏分块分别记录为服务失败/协议失败，不转换为空结果。
合法的空 `data` 数组只表示本次查询未命中，不能推出关系不成立或全库没有资料。

## 部署事实

- 服务默认监听 `http://localhost:8080`，API 前缀 `/api/v1`。
- 知识库必须绑定 embedding model 才可检索；未配置时 `retrieval_ready` 为 false，
  检索返回空。配置方法是 `kb create --embedding-model <m> --chat-model <m>`，
  或事后 `kb config set <kb> --embedding-model <m> --chat-model <m>`。
- Docker Compose 服务见 `E:\Cursor\WeKnora\docker-compose.yml`：app、postgres、redis、
  minio、qdrant/milvus/weaviate、docreader、sandbox、mcp 等。
- CLI 侧另有 `weknora search chunks`（`--kb` 必填、`--limit` 默认 8），其返回形状与
  `knowledge-search` 一致，适合人工排查；正式接入走 HTTP。

## 当前未验证项

本地 WeKnora 服务未运行（Docker daemon 未启动，`docker info` 报
`npipe:////./pipe/docker_engine` 不存在），因此以下尚未做实机验证：

- 对真实知识库执行检索并回读；
- 语料快照的可固定方式与版本标识；
- 许可字段的实际取值；
- 错误码在真实失败下的具体表现。

adapter 的请求与响应形状全部来自上述源码，测试用本地 HTTP server 按同样形状回放。
实机联调是 Step 4 的剩余工作。
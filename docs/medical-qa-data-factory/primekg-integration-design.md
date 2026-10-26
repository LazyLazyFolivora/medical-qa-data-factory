# PrimeKG 与可扩展知识图谱接入设计

> 状态：第一阶段已实现（步骤 1-8 完成；第 9 步真实生成命令与第 10 步覆盖率审计待做）  
> 目标版本：v0.2  
> 更新日期：2026-10-17

## 1. 目标

本设计用于把本地 PrimeKG SQLite 数据库接入 Medical QA Data Factory，同时建立一个不依赖 PrimeKG、SQLite 或特定图谱字段的稳定知识图谱接口。后续接入新的本地图谱、HTTP 图谱服务或 MCP 工具时，不应修改生成流水线和核心领域模型。

第一阶段交付目标：

- 使用只读方式查询本地 PrimeKG；
- 支持实体解析、邻居查询和最多 2-hop 的受限路径查询；
- 将图谱原始结果统一转换为 `KGEvidence`；
- 保存查询参数、调用轨迹、数据库快照和用量信息；
- 明确区分“合法空结果”和“工具调用失败”；
- 保持现有 `DataFactoryPipeline` 与 `KGTool.collect()` 兼容；
- 为后续接入其他图谱保留 provider 注册、能力声明和配置入口。

## 2. 非目标

本阶段不做：

- 修改或清洗原始 PrimeKG 数据库；
- 构建通用图数据库或把 SQLite 迁移到 Neo4j；
- 统一所有图谱的医学本体和关系语义；
- 自动融合多个图谱中的冲突事实；
- 接入文献 RAG、问题生成模型或来源消融；
- 对外提供公共 HTTP API。

先稳定 Python 内部契约；如果未来需要独立部署，再在同一应用服务层外增加 `/api/v1/knowledge-graphs/...`，而不是让 HTTP 模型进入核心端口。

## 3. 当前数据与代码基线

本地 PrimeKG：

- 路径：`E:\Cursor\assumption\datasets\PrimeKG\primekg.sqlite`
- 大小：约 1.63 GiB
- 表：`edges`
- 边数：8,100,498
- 关系种类：30
- `head_type` / `tail_type` 种类：各 10
- 现有索引：`head`、`tail`、`(head, relation)`、`(tail, relation)`

`edges` 字段：

| 字段 | 含义 | 可空 |
|---|---|---:|
| `head` | 头实体显示名称 | 否 |
| `relation` | PrimeKG 原始关系 | 否 |
| `tail` | 尾实体显示名称 | 否 |
| `head_id` | 头实体原生 ID | 是 |
| `tail_id` | 尾实体原生 ID | 是 |
| `head_type` | 头实体类型 | 是 |
| `tail_type` | 尾实体类型 | 是 |
| `source` | 原始来源 | 是 |

现有代码边界：

- `ports.KGTool.collect(seed_entity)` 是流水线使用的高层接口；
- `DataFactoryPipeline` 只消费 `KGCollectionResult`；
- `KGEvidence` 是对外保存的数据契约；
- `DemoKGTool` 把查询、实体构造和证据转换全部放在一个方法中。

`KGTool.collect()` 可以继续作为流水线门面，但不能直接作为多图谱底层接口，因为它没有表达实体歧义、查询方向、关系过滤、分页、路径、provider 能力和结构化失败。

## 4. 架构原则

1. **核心依赖抽象**：流水线依赖端口，不依赖 PrimeKG 或 SQLite。
2. **原始模型与领域模型分离**：provider 返回统一图模型，由应用服务转换为 `KGEvidence`。
3. **provider 身份显式化**：所有实体、边、快照和轨迹都带 `provider_id`。
4. **空结果不是错误**：没有匹配边时返回成功的空集合；连接、SQL、超时和数据损坏使用结构化异常。
5. **有界查询**：任何邻居或路径查询都必须带上限，并返回 `truncated`。
6. **确定性结果**：同一快照和同一查询必须采用稳定排序，便于复现和测试。
7. **原始数据只读**：使用 SQLite read-only URI 和 `PRAGMA query_only=ON`。
8. **快照可追溯**：数据库 SHA256、大小、修改时间、适配器版本和配置摘要进入快照清单。
9. **能力先声明后调用**：不支持路径、反向边或 ID 查询的 provider 应明确声明，不能静默降级。
10. **最小公共语义**：公共接口只统一图结构和查询行为，不强迫所有图谱采用 PrimeKG 关系词表。

## 5. 分层设计

```text
DataFactoryPipeline
        │
        │ KGTool.collect(EntityRef)
        ▼
KnowledgeGraphEvidenceCollector      应用层：查询计划、证据映射、用量记录
        │
        │ KnowledgeGraphProvider
        ▼
Provider Registry                    按 provider_id 选择实现
        │
        ├── PrimeKGSQLiteProvider     本阶段实现
        ├── RemoteGraphProvider       后续 HTTP 图谱
        └── OtherBiomedicalProvider   后续其他图谱
```

分层职责：

- **流水线门面**：保持现有 `KGTool` 兼容，不感知具体 provider。
- **Evidence Collector**：决定从种子查询哪些邻居或路径，将统一边转换成 `KGEvidence`。
- **Provider Port**：定义实体解析和图查询的稳定契约。
- **Provider Adapter**：处理具体数据库结构、SQL、鉴权、重试和原始字段。
- **Registry/Factory**：读取配置并实例化 provider，禁止在 CLI 中到处写 `if provider == ...`。

## 6. 建议的公共契约

建议新增 `src/medical_qa_data_factory/knowledge_graph/`，公共契约使用 Pydantic 模型；provider 协议使用 `Protocol`。

### 6.1 统一图模型

```python
class GraphEntity(ContractModel):
    provider_id: str
    native_id: str | None
    canonical_id: str
    name: str
    entity_type: str | None = None
    aliases: list[str] = []
    metadata: dict[str, Any] = {}


class GraphEdge(ContractModel):
    provider_id: str
    edge_id: str
    head: GraphEntity
    relation: str
    tail: GraphEntity
    source: str | None = None
    metadata: dict[str, Any] = {}
```

`GraphEntity` 和 `GraphEdge` 是适配层与应用层之间的内部契约，不直接成为正式数据集的公共 Schema。正式产物仍使用 `EntityRef` 和 `KGEvidence`。

### 6.2 实体解析

```python
class EntityLookup(ContractModel):
    mention: str
    entity_types: list[str] = []
    namespace: str | None = None
    native_id: str | None = None
    limit: int = Field(default=10, ge=1, le=100)


class EntityCandidate(ContractModel):
    entity: GraphEntity
    match_kind: Literal["id", "exact", "alias", "normalized", "fuzzy"]
    score: float = Field(ge=0, le=1)


class EntityResolution(ContractModel):
    candidates: list[EntityCandidate]
    selected: GraphEntity | None = None
    ambiguous: bool
    trace: GraphQueryTrace
```

第一版 PrimeKG 只要求 `id` 和大小写不敏感的 `exact`；`alias` 和 `fuzzy` 是可选能力，不允许伪装成精确匹配。

### 6.3 邻居与路径查询

```python
class NeighborQuery(ContractModel):
    entity: GraphEntity
    direction: Literal["outgoing", "incoming", "both"] = "both"
    relations: list[str] = []
    target_types: list[str] = []
    limit: int = Field(default=100, ge=1, le=1000)
    cursor: str | None = None


class PathQuery(ContractModel):
    start: GraphEntity
    end: GraphEntity | None = None
    max_hops: int = Field(default=2, ge=1, le=2)
    relations: list[str] = []
    target_types: list[str] = []
    max_paths: int = Field(default=100, ge=1, le=500)


class GraphQueryResult(ContractModel):
    edges: list[GraphEdge]
    trace: GraphQueryTrace
    snapshot: GraphSnapshot
    next_cursor: str | None = None
    truncated: bool = False
```

路径结果第一版可以仍以去重后的 `edges` 返回，同时在 `metadata.path_ids` 中保留路径归属。若后续算法确实依赖有序路径，再以非破坏方式增加 `paths` 字段。

### 6.4 能力、快照与轨迹

```python
class GraphCapabilities(ContractModel):
    entity_lookup_by_id: bool
    entity_lookup_by_name: bool
    alias_lookup: bool
    fuzzy_lookup: bool
    entity_search: bool
    incoming_edges: bool
    outgoing_edges: bool
    path_query: bool
    max_hops: int


class GraphSnapshot(ContractModel):
    provider_id: str
    adapter_version: str
    data_snapshot_id: str
    sha256: str | None
    size_bytes: int | None
    modified_at: datetime | None
    config_hash: str


class GraphQueryTrace(ContractModel):
    query_id: str
    provider_id: str
    operation: str
    normalized_request: dict[str, Any]
    started_at: datetime
    elapsed_ms: int
    rows_examined: int | None = None
    records_returned: int = 0
    truncated: bool = False
```

SHA256 在准备快照时计算一次并写入 manifest，不应在每次查询时重新读取 1.63 GiB 数据库。

### 6.5 Provider 协议

```python
class KnowledgeGraphProvider(Protocol):
    @property
    def provider_id(self) -> str: ...

    def capabilities(self) -> GraphCapabilities: ...

    def healthcheck(self) -> GraphHealth: ...

    def snapshot(self) -> GraphSnapshot: ...

    def resolve_entity(self, query: EntityLookup) -> EntityResolution: ...

    def search_entities(self, query: EntitySearchQuery) -> EntitySearchResult: ...

    def neighbors(self, query: NeighborQuery) -> GraphQueryResult: ...

    def paths(self, query: PathQuery) -> GraphQueryResult: ...

    def close(self) -> None: ...
```

协议不暴露 SQL、表名、HTTP 状态码或 PrimeKG 行结构。provider 可以是 SQLite、本地 Python 库、HTTP 客户端或 MCP 代理。

`search_entities` 的空白查询从 PrimeKG 靠前的有界边窗口提取去重端点，用于工作台默认候选；
非空查询覆盖全表 `head` 和 `tail` 实体，可按名称或原生 ID 检索。PrimeKG 的边表没有独立节点表，
因此全表 substring 查询由 `data/cache/primekg-entities.sqlite` 派生目录承担。该目录按数据库
snapshot id 失效并重建，原始 PrimeKG 数据库保持只读。

## 7. 错误语义

建议定义以下异常：

| 异常 | 含义 | 是否建议重试 |
|---|---|---:|
| `GraphConfigurationError` | 路径、凭据或配置错误 | 否 |
| `GraphUnavailableError` | 数据库或远程服务暂时不可用 | 是 |
| `GraphQueryTimeoutError` | 查询超过时间或资源限制 | 是 |
| `GraphQueryError` | 查询执行失败或返回格式错误 | 视原因 |
| `GraphDataIntegrityError` | 数据库缺表、损坏或字段不符合契约 | 否 |
| `GraphCapabilityError` | 调用了 provider 不支持的能力 | 否 |

以下情况不抛异常：

- 实体没有候选项：`candidates=[]`；
- 实体存在多个候选：`ambiguous=true`，由上层选择或转人工；
- 查询成功但没有边：`edges=[]`；
- 达到查询上限：返回已有结果并设置 `truncated=true`。

流水线目前把所有外部异常都标成 `retryable=true`。实现适配器时应同步修改错误映射，让任务记录使用异常上的稳定 `error_code` 和真实 `retryable`。

## 8. PrimeKG 适配器设计

### 8.1 连接方式

```python
uri = f"file:{database_path.as_posix()}?mode=ro"
connection = sqlite3.connect(uri, uri=True)
connection.execute("PRAGMA query_only = ON")
connection.execute("PRAGMA busy_timeout = 5000")
```

要求：

- 所有 SQL 使用参数绑定；
- 不对原始数据库执行 `CREATE INDEX`、`VACUUM` 或数据修订；
- 如果需要额外实体索引，写入单独的派生 sidecar 数据库；
- 每个查询必须有显式 `LIMIT`；
- 连接由 provider 拥有并显式关闭。

### 8.2 PrimeKG 实体标识

优先使用原始 ID，统一 ID 格式建议为：

```text
primekg:{normalized_entity_type}:{native_id}
```

例如：

```text
primekg:gene-protein:9796
```

当原始 ID 为空时，使用稳定的后备标识：

```text
primekg:{normalized_entity_type}:name-sha256:{digest}
```

后备 ID 必须基于规范化后的实体类型和名称生成，并在 `metadata.identity_fallback=true` 中标记。不能使用运行时 UUID 作为实体 canonical ID。

### 8.3 边标识

当前表没有显式主键。`edge_id` 使用以下字段的规范化值生成稳定 SHA256：

```text
provider_id | head canonical_id | relation | tail canonical_id | source
```

如果数据库中存在完全重复行，它们映射到同一逻辑边；原始重复次数写入 metadata，不能用 SQLite `rowid` 作为跨快照稳定 ID。

### 8.4 查询策略

实体精确解析分别扫描 `head` 和 `tail`，合并并按 canonical ID 去重。邻居查询分别处理出边和入边，再转换成统一方向。

第一版排序键固定为：

```text
relation, head_type, head, tail_type, tail, source, head_id, tail_id
```

这保证同一数据库快照的分页和产物顺序稳定。游标编码最后一条记录的排序键，调用者不得解析游标内部结构。

2-hop 查询必须限制中间节点扩张：

- 第一跳和第二跳分别设置上限；
- 支持关系和目标类型白名单；
- 记录候选扩张数与截断状态；
- 禁止无过滤地把某个高连接度实体全部载入内存。

## 9. 从统一图模型到现有领域模型

`KnowledgeGraphEvidenceCollector` 负责转换：

| 统一图字段 | 领域字段 |
|---|---|
| `GraphEntity.canonical_id` | `EntityRef.canonical_id` |
| `GraphEntity.name` | `EntityRef.canonical_name` |
| `GraphEntity.entity_type` | `EntityRef.entity_type` |
| `GraphEdge.relation` | `KGEvidence.relation` |
| `GraphEdge.edge_id` | `KGEvidence.source_record_id` |
| `GraphEdge.provider_id` | `KGEvidence.source_database` |
| 查询方向 | `KGEvidence.direction` |
| trace 摘要 | `KGEvidence.retrieval_action` |
| provider 原始字段 | `KGEvidence.metadata` |

`retrieval_action` 至少保存：

```json
{
  "provider_id": "primekg",
  "query_id": "kg_query_...",
  "operation": "neighbors",
  "direction": "both",
  "relations": [],
  "target_types": [],
  "limit": 100,
  "truncated": false
}
```

PrimeKG 原始 `source` 字段必须保留，但 `source_database` 固定为 `primekg`，避免把 NCBI 等上游来源误当成图谱 provider。

## 10. 配置和 Provider 注册

建议新增 `config/knowledge_graphs.json`：

```json
{
  "default_provider": "primekg",
  "providers": {
    "primekg": {
      "driver": "primekg_sqlite",
      "database_path": "../../datasets/PrimeKG/primekg.sqlite",
      "read_only": true,
      "default_limit": 100,
      "max_limit": 1000,
      "max_hops": 2,
      "query_timeout_ms": 10000
    }
  }
}
```

路径解析基准必须固定为项目根目录，而不是当前终端目录。允许使用环境变量 `MEDICAL_QA_PRIMEKG_PATH` 覆盖本地路径，但最终解析后的绝对路径和配置哈希必须进入运行记录。

Registry 只根据 `driver` 构造适配器：

```python
PROVIDER_FACTORIES = {
    "primekg_sqlite": build_primekg_provider,
}
```

增加其他图谱时新增 provider 和注册项，不修改 pipeline、collector 或已有 provider。

## 11. CLI 设计

先增加诊断命令，再接入完整生成流程：

```bat
python -m medical_qa_data_factory kg health --provider primekg
python -m medical_qa_data_factory kg resolve --provider primekg --entity aspirin
python -m medical_qa_data_factory kg neighbors --provider primekg --entity aspirin --direction both --limit 20
```

诊断命令输出 JSON，成功返回码为 0。配置错误、provider 不可用、实体歧义和查询错误使用不同的非零返回码，并将稳定 `error.code` 写到 stderr。

完成诊断接口后，新增真实生成命令：

```bat
python -m medical_qa_data_factory run --workspace data/runs/primekg-smoke --task-id aspirin-primekg --seed aspirin --kg-provider primekg
```

在文献和生成模型尚未接入时，不得把该命令产生的数据标为正式 gold；可以使用真实 KG 加 Demo 文献/生成器进行开发冒烟，但 manifest 必须记录混合来源和开发水印。

## 12. 建议文件结构

```text
src/medical_qa_data_factory/
├── knowledge_graph/
│   ├── __init__.py
│   ├── contracts.py          # provider-neutral DTO
│   ├── errors.py             # 稳定异常与 retryable
│   ├── ports.py              # KnowledgeGraphProvider
│   ├── collector.py          # GraphEdge -> KGEvidence
│   ├── registry.py           # provider factory/registry
│   └── providers/
│       ├── __init__.py
│       └── primekg.py        # SQLite 适配器
├── ports.py                  # 暂时保留 KGTool 兼容门面
└── cli.py

tests/
├── fixtures/
│   └── primekg_tiny.sqlite
├── test_kg_contracts.py
├── test_kg_collector.py
├── test_primekg_provider.py
└── test_kg_cli.py
```

`primekg_tiny.sqlite` 由测试代码临时构建或作为极小 fixture 提供，不能在单元测试中读取 1.63 GiB 正式数据库。正式数据库测试使用单独的 integration marker。

## 13. 测试策略

### 单元测试

- 精确名称和原始 ID 解析；
- 头实体、尾实体和双向邻居查询；
- 关系、类型、方向和 limit 过滤；
- 空结果与歧义结果；
- 稳定 entity ID 和 edge ID；
- 确定性排序、游标和 `truncated`；
- SQL 参数绑定，包含引号的实体名不能破坏查询；
- GraphEdge 到 `KGEvidence` 的字段映射；
- provider 能力不足时抛出 `GraphCapabilityError`；
- 数据库不存在、缺表和损坏时的错误分类。

### 集成测试

- 以只读模式打开正式 PrimeKG；
- `healthcheck` 验证表、字段和索引；
- 对固定种子返回可复现结果；
- 查询不会修改数据库大小和修改时间；
- 常见邻居查询在规定时间内完成；
- 数据快照 ID 与 manifest 一致。

集成测试默认不随普通 `pytest` 运行，建议使用：

```bat
python -m pytest tests -q
python -m pytest tests -m primekg_integration -q
```

## 14. 可观测性与用量

每次 provider 调用记录：

- provider、adapter 和 snapshot 版本；
- query ID 和规范化请求摘要；
- 耗时、返回边数、截断状态；
- 图谱关系访问数；
- 错误码、是否可重试；
- 配置哈希，不记录凭据。

`UsageRecord.kg_relation_accesses` 第一版定义为返回给上层的逻辑边数，不使用 SQLite 内部扫描行数代替。若能够可靠获得扫描量，可单独写入 trace。

## 15. 实施顺序

1. 新增 provider-neutral contracts、错误类型和协议。
2. 用 tiny SQLite fixture 实现 `PrimeKGSQLiteProvider` 的 health、resolve 和 neighbors。
3. 实现稳定 ID、排序、limit、trace 和 snapshot。
4. 实现 `KnowledgeGraphEvidenceCollector`，保持 `KGTool.collect()` 兼容。
5. 增加 registry 与 `knowledge_graphs.json`。
6. 增加 `kg health`、`kg resolve`、`kg neighbors` CLI。
7. 使用正式数据库完成只读集成测试。
8. 增加受限 2-hop 查询。
9. 接入真实生成命令并运行单种子 smoke test。
10. 完成 BioHopR 覆盖率审计，再决定实体别名和 sidecar 索引方案。

## 16. 第一阶段验收标准

- [x] pipeline、领域模型和测试代码不直接 import PrimeKG 适配器；
- [x] PrimeKG 数据库只读打开，查询前后文件不发生变化；
- [x] `kg health` 能验证数据库路径、Schema 和快照；
- [x] `kg resolve` 能区分未匹配、唯一匹配和歧义匹配；
- [x] `kg neighbors` 支持方向、关系、类型和数量限制；
- [x] 空结果不被记录为工具失败；
- [x] 工具失败带稳定错误码和正确 `retryable`；
- [x] 所有边具有稳定 entity ID、edge ID、provider 和 provenance；
- [x] 同一快照和查询产生确定性顺序；
- [x] `KGEvidence` 中保存 query ID、查询参数和截断状态；
- [x] 单元测试不依赖正式 1.63 GiB 数据库；
- [x] 正式数据库集成测试可以单独执行；
- [x] 新增第二个假 provider 时不需要修改 pipeline 和公共契约。

### 实现落地记录（2026-10-17）

- 新增 `src/medical_qa_data_factory/knowledge_graph/`：`contracts.py`、`errors.py`、
  `ports.py`、`collector.py`、`registry.py`、`providers/primekg.py`。
- 新增 `config/knowledge_graphs.json`，路径相对项目根目录解析，可用
  `MEDICAL_QA_PRIMEKG_PATH` 覆盖数据库、`MEDICAL_QA_KG_CONFIG` 覆盖配置文件。
- 新增 CLI：`kg providers`、`kg health`、`kg resolve`、`kg neighbors`、`kg paths`。
  返回码：0 成功、1 健康检查失败、2 配置错误、3 不可用、4 超时、5 查询错误、
  6 数据完整性、7 能力不足、8 其他、9 实体歧义、10 实体未匹配。
- `DataFactoryPipeline._error()` 现在优先使用异常的 `code` 和 `retryable`，
  不再把所有外部异常一律标记为可重试。
- PrimeKG 事实：`aspirin` 只以 `exposure / D001241` 出现，且大小写为 `Aspirin`，
  因此默认按大小写不敏感的精确匹配解析。
- 测试：单元测试 117 项通过；真实库集成测试 3 项通过（需显式 marker）。

## 17. 待确认决策

实现前需要确认，但不阻塞 contracts 和 tiny fixture 开发：

1. PrimeKG 中相同名称、不同类型实体的默认选择策略；
2. 第一版是否只接受精确匹配，还是同步构建别名 sidecar；
3. 2-hop 查询允许的最大第一跳/第二跳扩张数；
4. 正式 snapshot manifest 的存放目录和发布方式；
5. 是否保留 PrimeKG 原始关系，或另加一层规范关系 taxonomy；
6. 多图谱并行查询是本项目负责，还是由上层 query planner 负责。

默认建议：第一版只做精确匹配、保留 PrimeKG 原始关系、每个 provider 独立查询；多图谱融合和关系规范化作为后续独立能力，避免阻塞真实 PrimeKG 接入。

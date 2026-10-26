# 真实生成 Agent 开发实施文档

> **历史施工计划，已停止作为需求依据。** 当前产品需求以
> `docs/entity-question-generation-implementation-requirements.md` requirements-v2 为准。
> 本文件仅保留实现思路；其中固定多跳、逐 hop RAG 和复杂 Agent 闭环不得覆盖新需求。

> 面向读者：负责实现 `medical-qa-data-factory` 的开发者或编码 Agent  
> 产品依据：`E:\Cursor\assumption\docs\PRD_多源医学问答数据工厂.md` v0.4  
> 项目目录：`E:\Cursor\assumption\projects\medical-qa-data-factory`  
> 更新日期：2026-10-20  
> 执行模式：direct mode；当前目录不是 Git 仓库，未安装 GitHub CLI  
> 文档状态：待实施

## 0. 开发者先读

本轮目标不是把当前固定 LangGraph DAG 改名为 Agent，而是实现下面四个相互连接、边界清晰的能力：

1. **确定性 Pipeline 中的有界 N-hop ReAct 探索循环**；
2. **把每个候选 KG hop 转换为检索需求，通过真实外部 RAG 获得并验证可定位文本证据**；
3. **基于冻结多源证据与可执行 LogicalQuery 的问题生成、独立答案复算和最多一次重写**；
4. **生成后按实体—关系结构分组、组内排序、人工择优，不自动裁定语义重复**。

开始编码前，必须阅读：

- `README.md`
- `UNFINISHED_CHECKLIST.md`
- `docs/entity-question-generation-agent-roadmap.md`
- `docs/entity-question-generation-implementation-requirements.md`
- `docs/primekg-integration-design.md`
- `plans/medical-qa-react-multihop-agent-blueprint.md`
- `src/medical_qa_data_factory/generation/`
- `src/medical_qa_data_factory/knowledge_graph/`
- `src/medical_qa_data_factory/llm/`
- `src/medical_qa_data_factory/validators.py`
- `src/medical_qa_data_factory/review.py`
- `src/medical_qa_data_factory/web/`

规范优先级：本文件与 PRD v0.4 是当前真实生成 Agent 的施工依据。较早的 roadmap/implementation-requirements 中“PrimeKG-only”“不实现真实文献 RAG”等内容仅描述已经完成的历史最小闭环；发生冲突时，以 PRD v0.4 和本文件为准。

实施要求：

- 每个 Step 单独完成、单独测试、单独记录变更文件；
- 保留现有 `rule-based` 单跳路径作为回归基线；
- Agent 只能提出结构化 proposal，不能直接执行工具、SQL、文件或网络请求；
- 不保存模型隐藏思维链，只保存结构化动作、简短依据、引用和停止原因；
- PrimeKG 数据库只读；
- 外部 RAG 只作为版本化检索工具使用，本项目不重建其索引或内部推理系统；
- 不放宽现有 Schema、证据、答案、导出和人工审核门禁；
- 同组候选不等于重复，排序靠前也不等于自动通过；
- 当前默认测试基线为 316 项通过、10 项真实 PrimeKG 测试默认 deselected。

## 1. 产品决策

### 1.1 总体机制

系统采用：

> 确定性 Pipeline + 有界 KG/RAG ReAct 探索子图 + 多源证据硬门禁 + 人工分组择优

```text
输入已消歧实体
  → 确定性身份校验
  → Observe
  → Planner proposal
  → Policy Gate
  → Sealed Action
  → Tool Executor
  → 持久化 evidence/path/observation
  → 为候选 path/claim 生成逐 hop LiteratureEvidenceNeed
  → 真实外部 RAG 检索、实体链接与支持/反对判断
  → 持久化 LiteratureEvidence / EvidenceCoverageReport
  → Sufficiency Judge
      ├─ continue → 回到 Observe
      ├─ insufficient_terminal → 部分完成/证据不足
      └─ sufficient
           → Claim Builder
           → LogicalQuery / Question Blueprint
           → Question Writer
           → Answer-blind Verifier
               ├─ accepted
               ├─ needs_revision → 最多改写一次
               └─ hard_rejected
           → Machine Validation
           → 实体—关系分组
           → 组内质量排序
           → 人工保留/设为变体/两题都保留/修改/退回/拒绝
           → 导出
```

### 1.2 N-hop 决策

- 数据结构和算法原生支持 N-hop，不允许使用 `Literal[2]`、`max_length=2` 或二跳专用核心接口；
- hop 上限属于版本化 `RunPolicy`，不是契约上限；
- MVP 默认 `max_hops=3`，初始系统安全上限为 4；
- 请求只能收紧策略上限，不能扩大；
- 首轮验收必须覆盖 2-hop 和 3-hop，fixture 覆盖 2/3/4-hop；
- 后续提高安全上限时，不应重写 EvidencePath、LogicalQuery、Verifier 或分组结构。

### 1.3 相近问题处理决策

系统不自动执行“语义去重”。生成候选后：

- 按实体和规范关系结构分组；
- 按可解释质量因素排序；
- 展示文本完全相同、文本相似和可能为表达变体的提示；
- 最终由人工决定问题之间的关系。

系统不得因为相似度、embedding 或模型判断自动删除、合并、拒绝或确认问题。

### 1.4 RAG 与证据覆盖决策

- 当前代码中的 `DemoLiteratureTool` 仅用于测试，不能满足真实生成验收；
- 复用旧文献 RAG 作为外部 provider，通过版本化 adapter 接入，不在本项目重建检索索引；
- 真实 N-hop 自动生成默认 `evidence_mode=hybrid_per_hop`：每个语义 hop 至少有一条通过支持性判断的可定位文本片段；
- `kg_only`、`literature_only`、`hybrid` 继续保留用于消融、失败分析和特定题型，但必须显式标记；
- RAG 摘要、模型记忆、只有 URL 没有片段、只有片段没有稳定文档/位置标识的结果都不能成为 gold；
- 检索结果是非可信外部数据，只能作为 evidence candidate，不能携带或改变系统指令、预算、工具权限和校验策略；
- 文本证据缺失时允许在预算内改写检索、使用别名、调整允许的文献类型或换路径；最终缺失则 `insufficient_terminal`，不能用模型常识补齐。

### 1.5 本轮不做

- 不重建旧 RAG 的索引、召回模型、阅读模型或 MCP Server；
- 不使用开放 Web 搜索替代指定文献 RAG；
- 不做无预算上限的深度搜索；
- 不做自由文本工具调用；
- 不做自动语义去重；
- 不把 fake ReAct 或 rule-based 标记为真实 Agent；
- 不一次扩展到 40 个种子或 200 题。

## 2. 当前代码基线

### 2.1 已有能力

- Pydantic 领域模型和 JSON Schema；
- 不可变本地 JSON 仓库；
- LangGraph 固定 DAG 和 SQLite checkpoint；
- Episode、AgentStep、ToolObservation、RejectionRecord；
- `LanguageModelPort`、7 个 `ModelRole` 和 `FakeStructuredModel`；
- `ExplorationAction` union、`ExplorationDecision`、`EvidenceSufficiencyDecision`；
- `LiteratureTool`、`LiteratureEvidence`、`EvidenceBundle.literature_evidence` 和 `retrieve_literature` action 骨架；
- generation 层 `EvidencePath` 和 KG 层 `OrderedPath`；
- PrimeKG 类型安全实体查询、邻居查询和有界路径组装；
- 规则式 evidence curation、claim、question planning、writing、verification；
- 11 项机器校验、审核工作台和导出门禁；
- 一个 Aspirin 真实 PrimeKG smoke 产物。

### 2.2 当前实际行为

```text
verify_seed
→ collect_neighbors
→ curate_evidence
→ build_claims
→ plan_questions
→ write_questions
→ persist_draft_questions
→ verify_questions
→ machine_validate
→ finalize
```

这是一条固定 Pipeline，不是 ReAct。当前 `structured` 模式没有真实 provider adapter；`AgentStep` 主要是节点日志；`ToolObservation` 没有反馈给 Planner 决定下一动作。文献侧当前只有 `DemoLiteratureTool.collect(seed_entity)`，接口粒度不足以表达逐 hop 检索需求，也没有真实 RAG provider、索引 snapshot、chunk 级 provenance、支持/反对判断或 coverage gate。

### 2.3 已知阻断缺陷

实施新能力时必须一起解决：

1. `structured` 模式仍会因未装配真实模型而 fail fast；
2. `EntityGenerationRequest.max_hops` 仍有单跳限制；
3. 当前内存预算基于 `time.monotonic()`，resume 可能重置预算；
4. episode ID 主要由 task 派生，不同 mode/run 可能混写；
5. 当前 graph 在 revision 次数用尽后可能把仍需修改的候选加入 accepted；
6. 当前 `logical_query` 不是强类型可执行契约；
7. 当前 `duplicate_detection` 只在同一 `split_group_id` 内做文本标准化和 Jaccard 检测，并且属于阻塞型校验；这与 PRD v0.4 的人工择优决策冲突；
8. 当前 QuestionSpec 没有 `review_group_key`、排序详情和组内人工处置字段。
9. 当前正式导出只接受 `human_confirmed`，尚未实现 PRD 要求的 machine/human 两套正式导出等级。
10. 当前 ValidationCheck 缺少 question version/input digest applicability，单纯 bump validator version 不能保证旧结果失效。
11. `LiteratureTool.collect(seed_entity)` 只按种子采集，无法绑定 path hop、claim、查询过滤、分页和预算。
12. `LiteratureEvidence` 缺少 provider/index snapshot、trace ID、chunk 稳定标识、原文 hash 与对应 EvidenceNeed。
13. 当前 Sufficiency 不要求逐 hop 文本覆盖，PrimeKG 路径可能在没有原文证据时直接进入生成。

## 3. 不可破坏的不变量

1. Claim、答案、问题和路径只能引用已持久化证据。
2. 上游实体必须使用 provider/native ID/type，禁止按名称静默选第一个候选。
3. 模型只能输出 proposal；ID、turn、hash、policy、预算和状态由代码生成。
4. Executor 只能执行 Policy Gate 产生的 `SealedAction`。
5. N-hop 路径的每个相邻 step 必须连续，且来自同一 provider/snapshot。
6. Writer 不能批准自己的问题；Verifier 必须 answer-blind。
7. 模型 verdict 不能覆盖断链、缺引用、预算、Schema 或导出硬门禁。
8. 所有外部调用，包括创建正式 run 前的 provider preflight，都进入持久化预算账本和 execution journal。
9. checkpoint 不能替代不可变业务记录。
10. 排序只影响展示顺序，不改变 validation level 或 review disposition。
11. 同一审核组不等于重复；人工可以明确选择“两题都保留”。
12. 相似提示不得自动成为阻塞型失败。
13. 问题、分组、排序和人工决定的每次修改都生成新版本。
14. 分组键结构正确性可以成为阻塞门禁，但“同组/相似”这一事实本身不能成为阻塞门禁。
15. 真实 `hybrid_per_hop` 问题的每个语义 hop 都必须同时引用持久化 KG evidence 与已验证 LiteratureEvidence。
16. RAG 返回的总结不能替代原文片段；原文片段必须具有稳定文档 ID、chunk/位置、内容 hash、provider、index snapshot 和 trace。
17. 检索结果中的指令、HTML 或提示注入内容一律按数据处理，不能进入系统/开发者提示或改变工具控制流。
18. 文本证据、实体链接或支持性判断变化时，相关 coverage、claim、question、validation 和 export eligibility 必须失效并重算。

## 4. 目标模块

```text
src/medical_qa_data_factory/
├── generation/
│   ├── actions.py                 # proposal、sealed action、policy gate
│   ├── exploration.py             # observe、planner、executor
│   ├── sufficiency.py             # proposal + deterministic envelope
│   ├── logical_query.py           # 强类型查询与确定性执行
│   ├── shortcuts.py               # 更短路径与 leave-one-hop-out
│   ├── evidence_needs.py           # path/claim → 逐 hop 文献检索需求
│   ├── evidence_fusion.py          # KG/文本链接、支持/反对与 coverage
│   ├── grouping.py                # review_group_key
│   ├── ranking.py                 # 组内可解释排序
│   ├── graph.py
│   ├── service.py
│   └── ...
├── llm/
│   ├── config.py
│   ├── errors.py
│   ├── registry.py
│   ├── providers/
│   │   └── openai_compatible.py
│   └── prompts/
├── literature/
│   ├── contracts.py               # RAG request/result/snapshot 协议
│   ├── registry.py                # provider 装配与 capability
│   └── providers/
│       └── legacy_rag.py           # 旧 RAG Python/HTTP adapter
├── review.py
├── validators.py
└── web/
```

文件名可按现有项目风格调整，但职责边界必须保留。

## 5. 核心契约

### 5.1 Run 与模型调用

正式 run 创建前新增 task-scoped `GenerationPreparationRun`。它具有 `preparation_id`、task ID、请求 digest、准备阶段预算、AttemptRecord、idempotency/reconciliation 状态和 provider capability snapshots。RAG/model/KG preflight 的 401、429、timeout、usage 和 unknown outcome 全部归属该 preparation。成功后创建不可变 `GenerationRunManifest`，引用具体 preparation ID、成功 preflight attempt ID/version 与冻结 capability snapshot；只有 manifest frozen 后才能创建 episode。失败 preparation 保留审计记录，可按幂等规则恢复，但不能产生正式问题。

新增不可变 `GenerationRunManifest`：

- `run_id`、`task_id`、mode；
- graph/schema/policy version；
- request digest；
- PrimeKG provider 与 snapshot hash；
- RAG provider、capability version 与 index snapshot；
- model config ID；
- 每个 role 的 prompt hash；
- 创建时间和运行目录。

rule-based、fake、structured 必须使用不同 run、episode、thread 和产物目录。

新增服务端权威枚举 `RequiredEvidenceMode`：

```text
kg_only
literature_only
hybrid
hybrid_per_hop
```

它与消融后计算出的 `verified_route` 不是同一字段。`RunPolicy.required_evidence_mode` 在创建 run 时由受信配置确定，真实 N-hop 自动生成默认且验收要求为 `hybrid_per_hop`；模型、QuestionSpec payload 和工具返回都不能修改。该值及 policy version 必须同时绑定 RunManifest、EvidenceBundle、QuestionSpec 和 CoverageReport。run 内不能把缺证据样本降级为 `kg_only`；更换模式必须创建新 run，并保留原失败记录。Sufficiency 和 export 必须比较四处字段及 digest，任何不一致都阻塞。

模型只返回：

```text
ModelActionProposal
  action
  expected_information
  evidence_gap
  cited_observation_ids
```

服务端在校验后生成：

```text
ExplorationDecision
  decision_id
  run_id / episode_id / turn
  validated proposal
  policy_version
  canonical_action_hash
```

### 5.2 SealedAction 与执行链

Policy Gate 输出 `SealedAction`：

- 服务端 action ID；
- canonical action hash；
- exact request digest；
- 已验证实体、关系、方向、type filters、cursor 和 limit；
- 对文献动作，已验证的 EvidenceNeed、canonical entities/aliases、query、filters、page、document/chunk limits；
- provider capability version；
- budget reservation ID。

必须能从记录中验证：

```text
decision_id
→ sealed_action_id/action_hash
→ execution_id/request_digest
→ observation_id
→ evidence/path IDs
```

非法 proposal 产生 policy rejection observation，但必须产生零工具调用。

### 5.3 EvidencePath

`EvidencePath.steps` 至少 2 步，不在 Schema 中写死最大 2。运行时验证：

- `len(steps) <= RunPolicy.max_hops`；
- position 连续；
- `step[i].to_entity_id == step[i+1].from_entity_id`；
- 每一步引用已持久化的 KGEvidence；
- provider/snapshot 一致；
- source evidence 顺序与 steps 一致；
- 无自环、重复边、回到起点和断链。

持久化顺序：

1. 保存全部 KGEvidence；
2. 保存引用这些 evidence ID 的 EvidencePath；
3. 从 repository reload evidence registry 和 path；
4. 重新验证引用、版本、端点、关系、方向和 snapshot；
5. 全部成功后才发布 path observation。

### 5.4 RAG 检索与文本证据契约

现有 `LiteratureTool.collect(seed_entity)` 保留为 legacy/demo 兼容层，真实 Agent 必须使用细粒度端口：

```text
LiteratureRetrievalPort.preflight() -> LiteratureCapabilitySnapshot
LiteratureRetrievalPort.search(request: LiteratureRetrievalRequest)
  -> LiteratureRetrievalResult
LiteratureRetrievalPort.read_chunk(request: LiteratureChunkReadRequest)
  -> LiteratureChunkReadResult
```

`preflight` 必须在 task-scoped GenerationPreparationRun 中、创建不可变 RunManifest 前取得 provider capability 和可固定的 index/corpus snapshot。它与普通外部调用一样先 reserve、再 running、最后 completed/failed/unknown，并执行 reconciliation。后续 search/read request 都携带 `expected_snapshot_id`，idempotency key 包含该 snapshot。若 provider 无法提供可固定 snapshot 或执行中发生 snapshot drift，该 provider 只能产生 development/context candidate，不能产出正式 `hybrid_per_hop` coverage；当前正式 run 停止，重新 preparation/preflight 后创建新 run/version。

`LiteratureEvidenceNeed` 由代码从已持久化 path step 或 candidate claim 构造，模型只能建议检索表达，不能自报 need ID、hop、实体身份或预算：

```text
LiteratureEvidenceNeed
  need_id / run_id / episode_id
  path_id / hop_position / kg_evidence_id
  subject_entity_id / aliases
  canonical_relation / relation_phrases
  object_entity_id / aliases
  information_need
  required_support_count
  allowed_publication_types / time_range
  dependency_digest
```

Policy Gate 根据 need 生成 `LiteratureRetrievalRequest`：

```text
request_id / sealed_action_id / attempt_id
need_id
query
canonical entity IDs and aliases
filters
page/cursor
max_documents / max_chunks / token budget
expected_snapshot_id
provider capability version
```

`LiteratureRetrievalResult` 必须区分 `success`、`empty`、`timeout`、`auth_error`、`rate_limited`、`parse_error` 和 `provider_error`，并返回客观 usage、provider request/trace ID 与 `LiteratureSnapshot`。`empty` 不能转换成“关系不成立”。

扩展 `LiteratureEvidence`，至少增加：

- `need_ids`、可选的 `claim_ids`、`path_id` 和 `hop_positions`；
- provider 与 index/corpus snapshot；
- stable document ID，以及 PMID、DOI 等外部 ID；
- stable chunk/section ID、页码/段落/字符位置；
- 原始 evidence text、规范化 content hash；
- evidence version、entity-link version；
- 实际 query、rank、retrieved_at、trace ID、许可信息；
- 实体链接结果，但不把低置信度链接静默提升为规范实体。

支持性判断单独保存，禁止覆盖原始 evidence：

```text
EvidenceSupportAssessment
  assessment_id / assessment_version
  need_id / literature_evidence_id
  verdict: supports | contradicts | context_only | irrelevant | uncertain
  subject/object link result
  relation match
  cited span offsets
  deterministic checks
  evaluator config/prompt hash
  short rationale
```

每个候选 path 生成版本化 `EvidenceCoverageReport`：

```text
EvidenceCoverageReport
  path_id / path_version
  evidence_mode
  per_hop[]
    hop_position / need_id / kg_evidence_id
    supporting_assessment_refs[]
    contradicting_assessment_refs[]
    unresolved_link_ids[]
    status
  blocking_gaps[]
  provider/index snapshots[]
  coverage_policy_version
  retrieval_attempt_refs[]
  read_attempt_refs[]
  assessment_universe_digest
  dependency_digest
  answerability_probe_digest
  logical_query_digest?
  frozen_at
```

每个 assessment ref 必须精确绑定 assessment ID/version、need ID/version、path step/KG evidence version、LiteratureEvidence version/content hash、entity-link version和 cited-span digest。QuestionSpec/EvidenceRequirements 保存 required coverage report ID/version 及 probe/logical-query dependency digest。调用方不能自报 `status` 或 `blocking_gaps`；Sufficiency/export 从精确引用与当前依赖字段重新计算适用性。

Coverage builder 必须从 repository 枚举当前 need dependency digest、pinned snapshot 和 coverage policy 下全部适用的 completed retrieval/read attempts、LiteratureEvidence 与 EvidenceSupportAssessment，形成闭合 assessment universe。`supports`、`contradicts`、`context_only`、`irrelevant`、`uncertain` 都必须进入 universe，不能选择性省略。存在适用但未纳入的 assessment、未终结的 reserved/running/unknown attempt、缺少 assessment 的已回读片段，或重算 universe digest 不一致时，CoverageReport 必须阻塞 Sufficiency/export。

`hybrid_per_hop` 的 coverage 只有在每个语义 hop 至少有一条 `supports`、没有未解决的阻塞冲突、所有引用 reload 后仍有效时才通过。这里的 reload 同时包括本地不可变记录 reload 和通过 `read_chunk(expected_snapshot, document_id, chunk_id)` 对原文位置进行独立 round-trip，核对文档标识、位置与 content hash。无法 round-trip 的搜索结果只能是 `context_only` 或人工候选，不能进入 machine-validated gold coverage。证据选择必须先持久化原始 result，再保存 read result、assessment，最后生成 coverage report；不得把模型临时上下文中的片段直接写进问题。

### 5.5 LogicalQuery

禁止继续使用任意 `dict` 作为多跳语义。新增强类型 `LogicalQuery`：

- seed/start entity；
- ordered steps；
- 每一步 relation、direction、entity type constraints；
- answer variable 与 answer entity type；
- filters、negation、temporal scope；
- aggregation 和 cardinality。

确定性 evaluator 必须在完整固定 snapshot 上执行 LogicalQuery，产生权威 canonical answer set。已选 evidence/path 只用于 provenance 和证明链，不能限制答案搜索域，否则会形成“用生成输入证明生成答案”的自证循环。查询必须使用稳定排序；如果安全上限导致结果截断，则答案状态必须标记为 incomplete/ambiguous，不能声称唯一或完整。

探索阶段另设 `AnswerabilityProbe`：它是由当前 evidence/path 确定性构造的候选查询骨架，包含起点、关系/方向序列、目标类型和预期 cardinality，但不包含自然语言问题。Sufficiency 在正式 Question Planner 之前执行 probe；通过后，Question Planner 才将 probe 提升为完整 LogicalQuery 和 blueprint。这样避免“先要求 LogicalQuery 才能判充分、但判充分后才生成 LogicalQuery”的循环依赖。

Probe 与 LogicalQuery 都生成 canonical dependency digest。QuestionSpec 必须保存服务端复制的 `required_evidence_mode`、`run_policy_version`、`required_coverage_report_refs`、`answerability_probe_digest` 和 `logical_query_digest`。这些字段不能由 Writer 或审核命令更改；路径、关系、方向、过滤、答案类型或 cardinality 变化时必须生成新 probe/query 并使旧 coverage 失效。

### 5.6 分组和排序

新增字段：

```text
QuestionSpec
  review_group_key
  ranking_assessment
  review_disposition
  variant_of_question_id?
```

建议契约：

```text
RankingAssessment
  ranking_version
  total_score
  factors[]
    code
    value
    weight
    contribution
    explanation
  computed_at

ReviewDisposition
  pending
  keep_independent
  variant_of
  rejected

ReviewCommand
  keep_independent
  set_variant_of
  keep_both
  revise
  return
  reject
```

单跳分组 canonical payload：

```text
seed provider/native ID/type
+ relation
+ traversal direction
+ answer entity type
```

N-hop 分组 canonical payload：

```text
seed provider/native ID/type
+ ordered relation sequence
+ ordered traversal directions
+ answer entity type
```

中间实体类型只作为组内筛选和展示字段，不进入默认 key，以保持与 PRD v0.4 一致。多路径问题使用按 canonical path signature 排序后的路径签名列表。`review_group_key` 为 canonical JSON 的版本化 hash，不使用显示名称或自由文本。

## 6. 运行机制

### 6.1 Observe

Planner 只看到压缩、结构化的 `PlannerContext`：

- seed identity；
- 已知实体摘要；
- observation ID 与摘要；
- 已持久化 evidence/path 摘要；
- 逐 hop LiteratureEvidenceNeed、检索尝试和 coverage 摘要；
- 已执行 action hashes；
- 当前证据缺口；
- 已生成问题类型和关系组分布；
- provider capabilities；
- 剩余预算。

不得提供 SQL、数据库路径、API key、完整高扇出邻域或模型隐藏推理。

### 6.2 Decide 与 Policy Gate

Planner 每轮只输出一个 `ModelActionProposal`。Policy Gate 校验：

- observation 引用属于当前 run/episode；
- subject/target 是已知实体；
- action 在白名单内；
- capability 可用；
- relation/type/direction/cursor/limit 合法；
- 文献 query 只能服务当前 EvidenceNeed，filter、page 和文档/chunk 数量不超过 policy；
- action 未重复；
- frontier、fanout、page、hop 和总预算可用；
- 模型没有扩大任何预算。

### 6.3 Act 与持久化预算

本项目使用本地不可变文件仓库，不能假设跨文件事务。每次模型或工具 physical attempt 使用唯一 `attempt_id` 和稳定 `idempotency_key`，并以同一个版本化 `AttemptRecord` 作为 execution journal 与预算账本的事实来源：

1. 外部调用前先持久化包含预算 reservation 的 `reserved` 版本；
2. 写入 `running` 版本后才允许调用 provider；
3. 执行外部调用；
4. 写入 `completed`、`failed` 或 `unknown` 新版本，同时保存 charge/release、usage、request/response hash 和 provider request ID；
5. 聚合预算从全部 AttemptRecord 最新版本确定性派生，不依赖另一份可能漂移的计数真相。

每次 retry 和 schema repair 使用新的 attempt ID 并单独计费。启动/resume 时执行 reconciliation：过期 reserved 记录在确认未调用时 release；running 且结果未知的记录转 unknown；completed 但 usage 缺失时按预留上限保守 charge，不能按零处理。只有 provider 明确支持 idempotency key 时才可重试 unknown，否则转 `human_input_required`。reservation timeout 和 reconciliation 行为必须版本化并可测试。

### 6.4 RAG 检索、支持性判断与证据融合

首次 Sufficiency 之前，代码只从已持久化 path step 构造 pre-claim `LiteratureEvidenceNeed`；不能依赖尚未运行的正式 Claim Builder。当形成候选 path 后，代码为每个尚未覆盖的语义 hop 创建 need。Planner 可以选择 `retrieve_literature`，但不能自由检索与当前 need 无关的内容。正式 Claim Builder 若在 Sufficiency 之后发现新的 claim gap，流程必须回到 need→RAG→coverage→Sufficiency，再次通过后才能进入 Writer。执行过程是：

```text
persist need
→ planner proposes retrieve_literature
→ policy gate seals exact request and reserves budget
→ real RAG adapter executes
→ persist raw result, snapshot, trace and usage
→ normalize document/chunk identity and entity links
→ run support/contradiction assessment
→ persist assessment
→ reload all referenced records
→ recompute EvidenceCoverageReport
```

查询策略按版本固定，允许依次尝试规范名、别名、关系同义表达和受控过滤变化。每次尝试都是独立 `AttemptRecord`；同一 canonical request 在 RunManifest 固定的 index snapshot 上不得重复。分页或 query rewrite 只有在仍有阻塞 gap 且预算允许时发生；search/read 返回 snapshot 与 expected snapshot 不一致时立即标记 drift 并停止当前正式 run。

支持性判断分为两层：代码先检查稳定来源、位置、hash、实体链接和 cited span；answer-blind 的结构化模型再判断片段是否支持目标 subject–relation–object，输出 `supports/contradicts/context_only/irrelevant/uncertain`。模型不能生成或改写引用文本。存在阻塞冲突时不得用更高数量的弱支持片段投票覆盖，必须换路径、获取更强证据或进入人工/不足状态。

RAG 返回文本在进入模型上下文前必须被明确包裹为不可信 evidence data，限制长度并移除主动内容；其中出现的“忽略规则”“调用工具”等文字不得进入控制消息。日志不得记录 API key、授权头或未经许可的全文。

### 6.5 Sufficiency

模型输出 sufficiency proposal，代码生成最终 envelope：

- `continue`：存在具体证据缺口、合法动作和剩余预算；
- `sufficient`：至少存在一个 `AnswerabilityProbe`，在完整固定 KG snapshot 上执行后满足 cardinality 和完整性要求，并且当前 evidence mode 对应的 `EvidenceCoverageReport` 通过；`hybrid_per_hop` 必须逐 hop 有已验证文本支持；
- `insufficient_terminal`：证据不足且无合法动作、连续两轮无新增证据或预算耗尽。

模型不能用 sufficient 绕过断链、缺引用、文本 coverage gap、未解决冲突、歧义、shortcut 或预算硬门禁。

### 6.6 N-hop 控制

- 使用 frontier、per-relation、page、rows、path count 和 model/tool call 多维预算；
- 所有结果稳定排序后截断；
- `truncated=true` 进入 observation；
- 连续两轮没有新增有效 evidence/path 时停止；
- 对每个 hop 执行 leave-one-hop-out；
- 查找所有更短 `< N` hop 路径；存在等价捷径时拒绝或按实际最短 hop 降级；
- 3-hop 以上未知组合默认只生成中性路径连接问题，不生成因果、治疗或机制结论。

### 6.7 生成与验证

1. Claim Builder 按 hop 生成 subclaims，每个 subclaim 绑定对应 KGEvidence、LiteratureEvidence 和 EvidenceSupportAssessment；
2. RelationCompositionPolicy 对完整 relation/direction/type 序列返回 allow/neutralize/deny；
3. Question Planner 产生 LogicalQuery 和 blueprint；
4. Writer 只能读取冻结 EvidenceBundle/CoverageReport 允许的证据生成题面，默认隐藏答案和必要中间实体；
5. 确定性 evaluator 计算权威 answer set；
6. Answer-blind 模型只接收问题、允许的 KG/文本证据和无答案 verification request，判断题面蕴含、歧义和泄漏；
7. 代码比较 canonical ID、alias 和 cardinality。

结果：accepted、needs_revision、hard_rejected。revision 按 draft lineage 计数，最多一次；默认只允许修改题面，不允许修改答案、LogicalQuery、path/evidence 或 split identity。第二次仍需修改时 hard reject。

### 6.8 分组与排序

问题完成 verifier 和机器校验后计算分组与排序。排序建议采用版本化、可解释的固定因素：

| 因素 | 建议权重 | 说明 |
|---|---:|---|
| verifier accepted | 30 | 未通过者不得获得此分 |
| evidence/path 完整 | 20 | 所有必需引用存在且连续 |
| answer 可确定 | 15 | 无歧义，cardinality 一致 |
| 无更短路径 shortcut | 10 | 按实际最短路径评估 |
| 无答案/中间实体泄漏 | 10 | 机器检测 |
| 自然度 | 10 | 提示性评估 |
| 模板化程度低 | 5 | 提示性评估 |

具体权重进入 `ranking.v1` 配置，不硬编码在 UI。阻塞校验失败的候选仍可显示，但必须明确标红并可设置分数上限。`ranking_score` 不改变 validation level。

重写、证据、路径、答案类型或关系链发生变化时，重新计算 group key 和 ranking。仅修改审核备注不重算。

### 6.9 人工处置

稳定的 `ReviewDisposition` 只包含：

```text
pending
keep_independent
variant_of
rejected
```

`revise`、`return` 和“两题都保留”是命令，不是持久化 disposition。审核命令及结果：

- `keep_independent`：作为独立问题保留；
- `set_variant_of(primary)`：候选变为 `variant_of`，保存原候选审计记录，不作为独立 QuestionSpec 导出；
- `keep_both(a,b)`：原子/可恢复批命令，结果是两条记录均为 `keep_independent`；
- `revise`：产生新的 draft 版本，disposition 重置为 `pending`，重新校验、分组和排序；
- `return`：返回 draft/review queue，disposition 重置为 `pending`；
- `reject`：disposition=`rejected`，并沿用现有工作流将 `ValidationLevel` 转为 `REJECTED`。

设置 `variant_of` 是跨候选与主问题的复合操作，必须使用带唯一 operation ID 的版本化 `ReviewOperation` journal 和 optimistic expected-version 检查。禁止 self-reference、cycle、variant-of-variant 和跨组关联；主问题必须是根问题且处于 `keep_independent`。候选必须继承主问题的 `logical_question_id`、`split_group_id` 和 `dataset_split`，并绑定明确的主问题版本。主问题增加 variant、候选写入 variant_of、相关校验失效必须全部完成后 operation 才能 committed；中途崩溃由 reconciliation 继续或补偿，不能留下 dangling/半写入状态。原候选版本不得删除。

variant 操作完成后，主问题和候选都重新运行 semantic-equivalence、leakage、split integrity、group integrity 与导出 eligibility 检查。

状态组合约束：

| 命令/状态 | 前置条件 | 结果 | 可 human confirm |
|---|---|---|---:|
| keep_independent | pending + blocking checks pass | keep_independent | 是 |
| set_variant_of | pending + same group + root primary | variant_of | 作为主问题变体 |
| keep_both | 两条 pending + checks pass | 两条 keep_independent | 是 |
| revise | 非 rejected | 新 draft + pending | 否，需重验 |
| return | reviewable | draft/review queue + pending | 否 |
| reject | 非已导出冻结版本 | rejected | 否 |

禁止 `human_confirmed + pending`、`human_confirmed + rejected`，以及 human confirmed 后无新版本直接 revise/return。

machine-validated 快照允许 `pending`。进入 human-confirmed 正式导出的候选必须完成组内处置，且 variant 只能随主问题导出。

校验迁移要求：

- 将 `duplicate_detection` 从 `BLOCKING_FOR_MACHINE_VALIDATION` 移出并改为 advisory 相似候选信号；
- 新增阻塞型 `review_group_integrity`，只校验 group key 是否能由结构化实体/路径确定性复算，不判断语义重复；
- bump validator version，旧 `duplicate_detection` 记录不得被误读成新语义；
- `machine_validated` 导出允许 disposition=`pending`，manifest 必须统计未处置组；
- `human_confirmed` 导出额外要求 disposition 已解决，且 `variant_of` 指向存在、已保留、同组且可导出的主问题；
- 分组相关字段变化只使 grouping/ranking/review 相关校验失效，不应无故使证据正确性校验失效。

### 6.10 导出等级

新增显式 `ExportLevel`：

```text
development
machine_validated
human_confirmed
```

CLI/API 必须要求选择 level，例如 `export --level machine_validated|human_confirmed|development`，不能由输出目录或记录状态猜测。

- development：允许 mock、未完成校验和 pending，manifest 带水印与全部阻塞原因；
- machine_validated：仅导出非 mock、所有 machine blocking checks 对当前输入适用且通过的问题；独立 QuestionSpec 只允许 disposition=`pending` 或 `keep_independent`，`rejected` 一律排除；`variant_of` 不独立导出，只在其主问题也满足 machine eligibility 时并入主问题 variants；manifest 必须统计待人工处置组；
- human_confirmed：仅导出 human confirmed 且 disposition=`keep_independent` 的主问题；`variant_of` 不独立导出，只并入其可导出主问题的 variants；pending/rejected/dangling/cycle/跨 split 一律排除。

所有正式等级还必须满足 RunPolicy 权威 `required_evidence_mode` 的 coverage gate，而不是信任调用方声明。Gate 必须重算并比较 RunManifest、EvidenceBundle、QuestionSpec、CoverageReport 的 mode/policy/digest 和精确引用；任一不一致即阻塞。`hybrid_per_hop` 缺任一 hop 文本支持、引用无法本地 reload 或 provider round-trip、RAG snapshot/trace 缺失或存在未解决阻塞冲突时，不得进入 machine/human 正式导出；人工处置不能覆盖这一来源完整性硬门禁。`kg_only` 等消融产物必须在 QuestionSpec 和 manifest 中显式标注，不能计入本方案“真实多源 Agent”验收样本。

每个 level 有独立 eligibility predicate、excluded reason、CLI/API 测试和 manifest 字段。不得使用一套隐式 gate 同时处理两类正式导出。

### 6.11 校验结果适用性

每条 ValidationCheck 必须绑定：

- `question_id` 与 `evaluated_question_version`，用于保留真实执行 provenance；
- validator ID/version；
- 输入依赖字段的 canonical digest；
- 运行时间和结果。

Gate 不能只按 check type 读取最后一条记录，也不能机械要求 `evaluated_question_version == current_question_version`。它可以接受祖先版本真实执行的 check，但必须同时满足：validator version policy 允许，且该 check 声明的 dependency input digest 与当前问题对应依赖字段的 digest 完全一致。只有相关依赖字段变化时才失效；仅修改排序、审核备注或发生不读取证据字段的状态转换，不应让证据正确性检查失效。禁止通过改写 check 的 evaluated version 来伪造重新执行。旧 passed 记录、依赖 digest 已变化的记录或旧语义 duplicate check 都不能放行当前问题。

## 7. 分步实施计划

### Step 0：冻结基线并修复现有高风险缺陷

**上下文**：新循环不能建立在自动放行 revision 或环境不一致之上。  
**负责文件**：`generation/graph.py`、`service.py`、测试与运行说明。

任务：

1. 记录 316 项默认测试基线和真实 PrimeKG 测试命令。
2. 修复 revision 次数用尽后自动 accepted；第二次仍需修改必须 hard reject。
3. 为既有受影响产物提供重新验证入口或明确迁移说明。
4. 明确唯一受支持开发环境为 `qa_env`，增加启动前依赖检查。

验证：revision success/failure/second-failure、环境缺依赖 fail fast。  
退出标准：旧 rule-based 行为保持，错误候选不再自动通过。  
回滚：仅回退新入口，不恢复自动放行逻辑。

### Step 1：冻结共享契约、运行身份与持久化账本

**依赖**：Step 0。  
**负责文件**：generation/llm contracts、domain models、repository、schema export、测试。

任务：

1. 新增 GenerationPreparationRun、RunManifest、RequiredEvidenceMode、ModelActionProposal、SealedAction、LiteratureEvidenceNeed、LiteratureRetrievalRequest/Result、LiteratureSnapshot、LiteratureChunkReadRequest/Result、EvidenceSupportAssessment、EvidenceCoverageReport、AnswerabilityProbe、LogicalQuery、RankingAssessment、ReviewDisposition、ReviewOperation、ExportLevel。
2. 将 max_hops 改为受 RunPolicy 控制的整数；MVP 默认 3、安全上限 4。
3. 拆分模型 proposal 与服务端 envelope。
4. 实现以版本化 AttemptRecord 为事实来源的 reserve/charge/release 与 reconciliation 原语，支持 task-scoped preparation 和正式 run 两种归属。
5. 定义 mode/run/episode/thread 隔离。
6. 为 ValidationCheck 增加 question version/input digest applicability。
7. 定义旧 JSON/checkpoint/check records 的读取或拒绝策略并更新 Schema。

验证：旧记录兼容、跨 mode 混写失败、非法上调预算失败、Schema drift 检查。  
退出标准：不联网即可完整创建并 reload 新契约。  
回滚：新增字段有兼容默认；旧 rule-based 使用独立 legacy run adapter。

### Step 2：实现真实结构化模型适配器

**依赖**：Step 1。  
**负责文件**：`llm/config.py`、`errors.py`、`registry.py`、`providers/`、prompts、service/CLI、测试。

任务：

1. 实现 OpenAI-compatible structured-output adapter。
2. 配置只保存 `api_key_env`，不保存密钥。
3. 支持 timeout、429、一次 schema repair 和错误分类。
4. 每个 physical attempt 独立 reserve/charge 并记录 usage。
5. 不支持严格 JSON Schema 的 provider fail fast。
6. structured 模式从 registry 装配，不允许静默降级。

验证：本地 fake HTTP server 覆盖 success/401/429/timeout/invalid JSON/schema repair/usage missing；一次显式授权的真实模型 smoke。  
退出标准：真实 structured response 通过 Pydantic 并产生完整 provenance。  
回滚：关闭 structured mode，不影响 rule-based/fake。

### Step 3：实现 N-hop evidence/path 基础层

**依赖**：Step 1；可与 Step 2 并行。  
**负责文件**：knowledge graph path builder、generation repository/contracts、测试。

任务：

1. 将 OrderedPath/EvidencePath 泛化为 N-hop。
2. 实现 bounded frontier expansion，不把 provider `path_query=false` 谎报为 true。
3. 实现 evidence-first/path-second/reload-validation 原子流程。
4. 实现环、重复边、回到起点、跨 snapshot、断链拒绝。
5. fixture 覆盖 2/3/4-hop、正反向混合遍历和高连接度截断。

验证：每条 path 均可从持久化 evidence registry 重建；PrimeKG 保持只读。  
退出标准：至少一条真实 3-hop path 可持久化并 reload 验证。  
回滚：关闭 multi-hop capability，保留单跳。

### Step 4：接入真实文献 RAG 与逐 hop 证据融合

**依赖**：Step 1；可与 Step 2、3 并行开发 adapter，最终融合测试依赖 Step 3。  
**负责文件**：新增 `literature/`、`generation/evidence_needs.py`、`generation/evidence_fusion.py`，修改 domain/ports/repository/config/schema、测试与 RAG 探针说明。

任务：

1. 对旧 RAG 做只读 capability probe，确认 Python/HTTP 调用方式、鉴权、查询与过滤字段、分页、文档/chunk 标识、可固定索引版本、独立 read/fetch chunk、trace、usage、错误状态和许可字段；保存脱敏 fixture，不把推测写成 adapter。
2. 将粗粒度 `LiteratureTool.collect(seed_entity)` 收口为 legacy/demo adapter，新增 `LiteratureRetrievalPort.search(request)` 与 provider registry；配置只保存 secret 环境变量名。
3. 实现从 path step/claim gap 确定性生成 `LiteratureEvidenceNeed`；need 绑定 path/hop/KG evidence 和 dependency digest。
4. 实现 `preflight/search/read_chunk` legacy RAG adapter，将真实结果无损映射为 LiteratureEvidence、snapshot、trace、usage 和明确状态；缺少可固定 snapshot 或稳定 chunk/位置/round-trip 时结果只能是 development context candidate，不能成为 gold support。
5. 实现 query canonicalization、别名/关系短语受控改写、分页和相同 snapshot 下的重复请求检测。
6. 实现原始 search result 先落库、独立 read_chunk 原文核对、实体链接、EvidenceSupportAssessment、精确版本 reload validation 与 EvidenceCoverageReport；report 绑定 assessment/evidence/link/span/probe digest，并从 repository 枚举全部适用 attempt/assessment 计算 universe digest，禁止选择性漏掉反对证据。
7. 将 RAG 调用纳入 AttemptRecord reserve/charge/reconciliation；只有 provider 明确幂等时才重试 unknown。
8. 加入检索内容不可信边界、长度限制、prompt-injection 隔离、secret/全文日志脱敏和许可字段校验。

验证：本地 fake RAG server 覆盖 task-scoped preflight reserve/reconcile、search、read_chunk、success/empty/timeout/401/429/malformed/duplicate-page/usage-missing/index-change/hash mismatch；fixtures 覆盖 supports/contradicts/context-only/uncertain、选择性遗漏 assessment 被拒、未终结 attempt 阻塞、universe digest mismatch、实体链接失败、精确版本引用 reload 失败和三跳逐 hop coverage；一次显式配置的真实旧 RAG smoke 能 round-trip 同一 snapshot 的稳定文档/chunk、trace 和 hash。  
退出标准：一个真实 PrimeKG 3-hop path 能产生 3 个 EvidenceNeed，真实 RAG 至少为每一跳返回可定位候选片段，并生成可 reload 的 coverage report；若真实内容不支持某跳，正确结束为 gap 而不是伪造通过。  
回滚：关闭真实 RAG provider capability，保留 demo/fixture 测试；关闭后 `hybrid_per_hop` 必须 fail fast，不能降级为 KG-only。

### Step 5：实现 Planner、Policy Gate 和 Executor

**依赖**：Step 1、3、4；真实集成依赖 Step 2。  
**负责文件**：新增 `generation/actions.py`、`exploration.py`，修改 fake/model/repository 和测试。

任务：

1. 构造包含 KG frontier、LiteratureEvidenceNeed 和 coverage gap 的 PlannerContext。
2. Planner 只输出 ModelActionProposal。
3. Policy Gate 生成 SealedAction。
4. Executor 只消费 SealedAction。
5. 保存完整 decision→action→execution→observation 链。
6. Fake model 支持按 observation/turn 返回不同响应。

验证：KG empty、RAG empty、A→B、truncated、policy rejection 产生不同实际工具调用；非法 proposal 零调用；重复 action 被拒绝；RAG query 不能越过当前 need。  
退出标准：fake 至少两轮，第二轮工具参数依赖第一轮 observation。  
回滚：旧固定 DAG 不连接新子图。

### Step 6：接入有界 KG/RAG ReAct 和 Sufficiency

**依赖**：Step 2、5。  
**负责文件**：graph/state/routing/budgets/service/repository、测试。

任务：

1. 接入 observe→plan→gate→execute KG/RAG→persist→assess/fuse→sufficiency 循环。
2. 将持久化 budget ledger 接入 graph。
3. 实现 target、budget、duplicate、no-new-evidence、no-valid-action、provider/model error 和 unresolved-coverage 停止。
4. 实现 execution journal 与指定 thread resume。
5. completed 不重复；unknown 仅在明确幂等时重试，否则 human_input_required。
6. 全部候选拒绝但仍有预算时，将失败摘要反馈给 Planner。
7. 在本 Step 实现 path-derived AnswerabilityProbe 构造与最小确定性 evaluator，供 Sufficiency 使用；不得依赖后续正式 Claim Builder。

验证：所有 stop reason、逐 hop coverage 门禁、RAG query rewrite/换路径、重启预算不重置、调用完成但 checkpoint 前崩溃、指定 thread 恢复。  
退出标准：真实 structured run 出现两轮以上 observation-dependent 决策。  
回滚：通过 mode 关闭 agentic graph。

### Step 7：实现 N-hop 多源生成、独立验证和一次重写

**依赖**：Step 6。  
**负责文件**：claims/planning/writing/verification/logical_query/shortcuts/semantics、测试。

任务：

1. 构造 subclaims 和保守的 derived claim。
2. 实现 RelationCompositionPolicy。
3. 消费 Step 6 已通过的 AnswerabilityProbe、EvidenceCoverageReport 和冻结 EvidenceBundle，将 probe 提升为强类型 LogicalQuery，并校验 dependency digest 连续性。
4. 实现 multi_hop_relation blueprint；旧 two_hop 输入仅作兼容映射。
5. 实现全 hop leave-one-out 和更短路径 shortcut 检查。
6. 实现 AnswerBlindVerificationRequest 和确定性 answer evaluator。
7. 实现按 lineage 最多一次 revision。

验证：2/3/4-hop、断链、文本 coverage 缺口、冲突证据、错误/失效引用、shortcut、多答案、错误答案、泄漏、改写成功和再次失败。  
退出标准：至少一个真实 3-hop 问题只能依靠完整路径复算，且每一跳都有真实 RAG 文本证据。  
回滚：关闭 multi-hop generation，保留已持久化证据和路径。

### Step 8：实现来源消融与 verified route

**依赖**：Step 7。  
**负责文件**：新增/修改 ablation runner、trajectory adapter、route verification、repository、validation/export、测试。

任务：

1. 对冻结 QuestionSpec 创建三个相互隔离的消融 episode：`kg_only`、`literature_only`、`combined`；隐藏 gold answer、gold evidence、Writer rationale 和生成时的 selected evidence。
2. 每个 episode 使用独立 run/episode/thread、允许工具集合和预算，保存各自 provider snapshot、动作、trace、usage、coverage、最终 answer set 与失败原因。
3. 使用统一的确定性答案比较、cardinality 与最低证据规则计算每一路是否充分，不能由模型自报成功。
4. 从三路结果确定性计算 `verified_route`：KG、literature、hybrid、redundant 或 unanswerable/uncertain，并绑定 ablation config/version 与 QuestionSpec dependency digest。
5. 生成时 required evidence mode 与 verified route 分开保存；verified route 不得反向降低本次生成 run 的 `RequiredEvidenceMode`。
6. route label 或消融输入变化时精确失效旧结果；machine/human export 要求当前 route policy 所需的消融结果完整且适用。

验证：构造仅 KG 可答、仅文献可答、两者联合才可答、两者都可答、三路都不可答和工具失败 fixtures；验证 gold 隐藏、episode 隔离、route 计算和旧结果失效。  
退出标准：至少一个真实候选完成三路消融并产生可复算 verified route，PRD 的来源标签不再只是自报字段。  
回滚：允许关闭消融执行 UI，但正式 export gate 保持阻塞；不得用 expected route 代替 verified route。

### Step 9：实现实体—关系分组与可解释排序

**依赖**：Step 1、7、8。  
**负责文件**：新增 grouping/ranking，修改 domain/repository/review/validators、`validation.py`、`machine_validation.py`、`export.py`、schema、测试。

任务：

1. 实现 canonical review group payload 和 versioned hash。
2. 单跳和 N-hop 使用不同但兼容的 canonicalizer。
3. 实现 ranking.v1 配置和因素明细。
4. 将现有 duplicate detection 从阻塞型校验移出并 bump validator version；改为文本相同/近似表达提示。
5. 新增只校验 canonical group key 可复算性的阻塞型 `review_group_integrity`。
6. 排序不得改变 validation level、review disposition 或导出状态。
7. 实现基于 question version/input digest/validator policy 的 check applicability；旧 passed 不能放行新版本。
8. 重写或结构变化后自动重算 group/ranking，并精确失效相关校验。

验证：同实体同路径进同组；方向或关系链不同分组；同组两题可同时保留；相似提示不阻塞机器校验；伪造 group key 被 integrity 门禁阻止；旧 duplicate check 不满足新 validator version。  
退出标准：批量候选能稳定分组、稳定排序、解释每个分数。  
回滚：关闭排序展示；保留 group key 与历史审核记录。

### Step 10：实现分组审核 UI/API 与导出语义

**依赖**：Step 9。  
**负责文件**：web templates/static/app/view models、review.py、export.py、E2E 测试。

任务：

1. 新增分组审核页，默认按 ranking score 排序。
2. 并列展示问题、答案、KG 路径、逐 hop RAG 片段/来源/支持状态、coverage gap、Verifier、校验、排序因素和版本。
3. 实现 ReviewDisposition 状态约束和 keep/variant/both/revise/return/reject 命令。
4. variant_of 使用 ReviewOperation journal、optimistic version、cycle/root/same-group/split 检查和崩溃 reconciliation。
5. variant 操作后重跑相关校验，保证主问题与候选不跨 split。
6. 实现 development/machine_validated/human_confirmed 三套显式 export level 与 eligibility predicate，正式导出强制检查 evidence mode/coverage/snapshot/trace。
7. machine 快照允许 pending 并在 manifest 统计；human-confirmed 导出要求完成组内处置，variant 主问题引用必须完整。
8. 导出 manifest 记录 export level、grouping/ranking version 和处置统计。

验证：浏览器 E2E 覆盖全部操作、刷新重放、并发候选进入同组、variant operation 中途崩溃恢复、cycle/self/cross-split 拒绝、三种 export level、variant 只随主问题导出。  
退出标准：用户可以在一个页面完成组内比较与处置。  
回滚：保留旧问题编辑页；新分组页可 feature flag 关闭。

### Step 11：垂直验收与文档收口

**依赖**：Step 0—10。  
**负责文件**：固定 eval manifest、README、UNFINISHED_CHECKLIST、运行手册、验收报告。

任务：

1. 固定 3—5 个 PrimeKG 种子、snapshot hash 和 policy/model/prompt version。
2. 固定真实 RAG provider/index snapshot；无法固定或无法 read_chunk round-trip 的 provider 不进入正式验收。
3. 独立运行 rule-based、fake KG/RAG ReAct、structured KG/RAG ReAct。
4. structured 至少产生单跳、2-hop、3-hop 候选；其中真实 3-hop 候选采用 `hybrid_per_hop`。
5. 完成逐 hop 文本 coverage、三路来源消融、机器校验、分组、排序和人工处置演示。
6. 输出 KG/RAG acceptance/rejection、coverage gap、stop reason、客观 usage、PolicyCost、group size、人工保留率和改排率。
7. 更新文档，使能力声明与实际代码一致。

验证命令：

```powershell
& 'D:\Anaconda\envs\qa_env\python.exe' -m pytest -q -m "not primekg_integration"
& 'D:\Anaconda\envs\qa_env\python.exe' -m pytest -q -m primekg_integration
```

退出标准：满足第 10 节 Definition of Done。  
回滚：真实模型和 agentic mode 可关闭，rule-based 持续可运行。

## 8. 依赖图与并行性

```text
Step 0 baseline
   ↓
Step 1 contracts/run/ledger
   ├───────────────┬────────────────────┐
   ↓               ↓                    ↓
Step 2 model     Step 3 N-hop path     Step 4 real RAG/fusion
   └───────────────┴──────────┬─────────┘
                              ↓
                    Step 5 planner/gate/executor
           ↓
        Step 6 KG/RAG ReAct/sufficiency
           ↓
        Step 7 generation/verifier
           ↓
        Step 8 source ablation/verified route
           ↓
        Step 9 grouping/ranking
           ↓
        Step 10 review UI/export
           ↓
        Step 11 acceptance
```

- Step 2、Step 3 与 Step 4 的 adapter 部分可并行，Step 1 完成后不得各自修改共享契约；
- Step 4 的逐 hop 融合测试依赖 Step 3 提供稳定 path/hop identity；
- Step 8 的 runner 框架可提前准备，但正式消融输入必须等待 Step 7 冻结 QuestionSpec；
- Step 9 的纯 ranking 配置可在 Step 8 后期准备，但 group key 必须等待 LogicalQuery/path 结构稳定；
- Step 10 的静态页面可先准备，真实 API 接线依赖 Step 9；
- 当前无 Git，多开发者并行时必须按负责文件边界工作并保存变更清单。

## 9. 测试矩阵

| 层级 | 必测内容 |
|---|---|
| Contract | 新旧 JSON、PreparationRun/RunManifest、N-hop、LiteratureEvidenceNeed/Result/Snapshot、SupportAssessment、CoverageReport、AnswerabilityProbe、LogicalQuery、group/ranking/review/export |
| Run identity | mode/run/episode/thread 隔离、request digest、版本不匹配 |
| Model adapter | success、401、429、timeout、schema repair、usage missing |
| RAG adapter | preflight/search/read_chunk、success/empty/401/429/timeout/parse error、分页、pinned snapshot/drift、round-trip hash、trace、usage、unknown outcome |
| Budget | preparation/run AttemptRecord 状态、reserve/charge、retry/repair、orphan reconciliation、unknown outcome |
| Policy | KG/RAG 白名单、未知实体、EvidenceNeed 绑定、capability、预算、重复、高扇出 |
| Planner | KG/RAG observation 反事实、下一次实际工具调用改变、非法 proposal 零调用 |
| Path | 2/3/4-hop、方向、断链、环、跨 snapshot、持久化 reload |
| Literature evidence | 稳定 document/chunk/位置/hash、实体链接、supports/contradicts/uncertain、引用 reload |
| Fusion | 逐 hop need、coverage 重算、冲突与缺口、evidence 变更精确失效 |
| Coverage closure | 全部适用 attempt/assessment universe、遗漏/未终结阻塞、universe digest 重算 |
| Sufficiency | probe + evidence-mode coverage → continue/sufficient/insufficient_terminal 与硬门禁 |
| Generation | 冻结 EvidenceBundle、组合语义、LogicalQuery、cardinality、shortcut、leakage |
| Verifier | answer-blind、错误答案、歧义、一次重写 |
| Source ablation | gold 隐藏、KG-only/Literature-only/Combined 隔离执行、verified route 确定性计算与失效 |
| Grouping | 单跳/N-hop key、方向差异、多路径 canonicalization |
| Ranking | 稳定排序、因素解释、版本变化、失败样本仍可见 |
| Human review | 状态组合、keep/variant/both/revise/return/reject、复合写崩溃恢复、不可变历史 |
| Validation applicability | evaluated version provenance、ancestor check digest 复用、相关字段变化失效、旧 passed policy |
| Export | 三种显式 level、evidence-mode/coverage gate、pending machine、rejected 排除、variant 仅随主问题归并、resolved human、跨 split 拒绝 |
| Security | RAG prompt injection 隔离、secret/header 脱敏、未许可全文不落日志 |
| E2E | 真实 PrimeKG + 真实 RAG、fake KG/RAG ReAct、structured smoke、UI 全流程 |

## 10. Definition of Done

只有全部满足，才能声明本轮完成：

- [ ] structured 模式调用真实配置模型，缺配置时 fail fast；
- [ ] provider preflight 归属可审计、可计费、可恢复的 GenerationPreparationRun，frozen manifest 引用成功 attempt；
- [ ] rule-based、fake、structured 的 run/episode/thread/产物隔离；
- [ ] RequiredEvidenceMode 由 RunPolicy 权威指定并绑定 run/bundle/question/coverage；run 内无法降级绕过逐 hop RAG；
- [ ] 至少一个真实 run 有两轮以上 observation-dependent 决策；
- [ ] Executor 只执行 SealedAction，非法 proposal 零工具调用；
- [ ] 所有 model/tool attempts 进入持久化预算和 execution journal；
- [ ] orphan reservation/running attempt 可在 resume 时确定性 reconciliation；
- [ ] resume 不重置预算，并恢复指定 thread；
- [ ] 至少一条真实 3-hop 路径逐步验证并 reload；
- [ ] 真实 RAG provider adapter 可用，缺配置时 `hybrid_per_hop` fail fast，不静默降级；
- [ ] 每个 path hop 都能生成稳定 LiteratureEvidenceNeed，并记录真实 query、trace、usage 与 index snapshot；
- [ ] 原始 LiteratureEvidence 具有稳定 document/chunk/位置/hash，并与支持性判断分开持久化；
- [ ] 每条 gold 文本证据都能在固定 snapshot 通过 read_chunk 独立 round-trip 并核对位置/hash；
- [ ] CoverageReport 精确绑定 assessment/evidence/entity-link/cited-span/probe/query 版本与 digest；
- [ ] CoverageReport 对 pinned snapshot 下全部适用 attempts/assessments 集合闭合，不能选择性遗漏 contradicts/uncertain；
- [ ] 至少一个真实 3-hop 问题通过完整路径复算、无更短等价路径，且三个 hop 均有真实 RAG `supports` 文本证据；
- [ ] 任一 hop 缺文本支持、引用失效或存在阻塞冲突时 Sufficiency 不能通过；
- [ ] Verifier answer-blind，第二次仍需修改时 hard reject；
- [ ] KG-only、Literature-only、Combined 三路消融隐藏 gold、相互隔离并产生可复算 verified route；
- [ ] 候选按实体与完整有序关系结构稳定分组；
- [ ] ranking score 可解释且只改变展示顺序；
- [ ] 文本相似和同组提示不自动裁定重复；
- [ ] 人工可以保留、设为变体、两题都保留、修改、退回和拒绝；
- [ ] variant 复合操作不会产生 self/cycle/dangling/跨 split 或半写入状态；
- [ ] 校验保留真实 evaluated version；只有 validator policy 合法且 dependency digest 未变的祖先检查可复用；
- [ ] development、machine_validated、human_confirmed 三种导出等级有独立 gate 和 manifest；
- [ ] machine export 排除 rejected，variant 不作为独立 QuestionSpec 导出；
- [ ] human-confirmed 导出只包含已完成组内处置的问题；
- [ ] 正式导出检查 evidence mode、CoverageReport、RAG snapshot/trace，消融样本不冒充 `hybrid_per_hop`；
- [ ] PrimeKG 保持只读；
- [ ] 默认、真实 PrimeKG、真实 RAG、Agent、group/ranking 和 E2E 测试通过；
- [ ] README、PRD、未完成清单和 UI 能力标签一致。

## 11. 反模式

出现任一项均视为未完成：

- 将固定 DAG 重新命名为 ReAct；
- Planner 永远执行硬编码 neighbor→expand→finish；
- `retrieve_literature` 只保留枚举和 demo，真实运行仍不调用外部 RAG；
- 先让模型写 claim/问题，再搜索能支持它的片段；
- 只给整条路径做一次宽泛检索，而不保存逐 hop EvidenceNeed 与 coverage；
- 把 RAG 摘要、模型生成引用、只有 URL 的记录或无法定位的片段当作 gold；
- 用支持片段数量覆盖明确反对证据；
- RAG 不可用时静默切换 KG-only 并仍标记 hybrid；
- 让调用方修改 QuestionSpec.evidence_mode 或 CoverageReport.status 来放行导出；
- 只把搜索响应落盘再 reload，不从固定 snapshot 独立回读原文核对 hash；
- coverage 只引用 literature ID，不绑定 assessment/evidence/link/span/probe/query 的精确版本；
- CoverageReport 只挑 supports，漏掉同一 need/snapshot 下的 contradicts、uncertain 或未终结 attempt；
- preflight 不进入预算/执行日志，或在 manifest 未冻结时启动 episode；
- 让检索文本中的指令进入控制提示或改变工具权限；
- 模型自报 decision ID、episode、turn、action hash 或预算；
- Executor 直接消费模型输出；
- 使用无序 edge list 作为 N-hop 路径；
- 只验证路径首尾，不验证每个相邻 step；
- Writer 自己决定答案正确；
- Verifier 能看到 candidate answer 或 Writer rationale；
- revision 次数用尽后自动 accepted；
- 模型不可用时静默回退并仍标记 structured；
- 用相似度或 ranking score 自动删除、合并或拒绝问题；
- 把“同审核组”展示成“系统判定重复”；
- variant 合并后删除原始候选历史；
- variant 关联到自己、variant-of-variant、形成环或跨 split；
- 复合 variant 写入失败后留下 dangling/半写入状态；
- 排序直接修改 validation level；
- 用当前精选 evidence 限制答案搜索域，形成自证式 verifier；
- Gate 只按 check type 读取旧 passed 记录而不校验输入适用性；
- 为新能力放宽机器校验或导出门禁；
- 保存 API key 或模型隐藏思维链；
- 修改 PrimeKG 原始数据库。

## 12. 风险与缓解

| 风险 | 缓解 |
|---|---|
| N-hop 路径爆炸 | frontier/relation/page/path/tool/token/time/cost 多维预算 |
| 每 hop RAG 调用导致成本和延迟上升 | need 去重、受控 query rewrite、分页上限、跨题只复用相同 snapshot 与 dependency digest 的原始结果 |
| RAG 没有稳定 chunk 或索引版本 | capability probe fail；降为 context candidate，不允许进入 gold coverage |
| run 中途索引漂移 | request 固定 expected snapshot；检测漂移后停止并新建 run/version，禁止混合正式 coverage |
| 检索到的文本只相关但不支持关系 | 独立 EvidenceSupportAssessment 与 cited-span 检查 |
| 文献证据与 KG 冲突 | 保存双方证据并阻塞自动 sufficient；换路径或转人工，不做多数投票 |
| Coverage 选择性遗漏反对结果 | repository 枚举完整 attempt/assessment universe，universe digest 与未终结 attempt 硬门禁 |
| preflight 无运行身份 | task-scoped PreparationRun 统一预算、attempt journal、幂等与恢复；成功后才冻结 manifest |
| 文献内容 prompt injection | 内容数据隔离、长度限制、主动内容清理、tool gate 不读取文献指令 |
| 文献许可或全文泄漏 | 保存许可字段，日志/导出只保留允许片段和 hash，发布前许可审查 |
| evidence mode 被降级绕过 | RunPolicy 权威字段跨 manifest/bundle/question/report 一致性校验；换模式必须新 run |
| 来源标签由生成过程自报 | 独立三路消融隐藏 gold，由确定性规则计算 verified route |
| 长链语义夸大 | RelationCompositionPolicy 默认 neutralize/deny |
| 模型结构化输出不稳定 | 严格 Schema、一次 repair、持续失败即停止 |
| 恢复后重复付费 | ledger reservation、execution journal、idempotency key |
| unknown 外部结果 | 仅幂等 provider 可重试，否则转人工 |
| 同组候选过多 | 可解释排序、批量处置、统计高频低质量模式 |
| 自动去重误删有效问题 | 不自动裁定；人工可以两题都保留 |
| 排序误导人工 | 展示因素与扣分；明确排序不是质量真值 |
| 当前无 Git 难回滚 | 每步独立、feature flag、旧模式保留、记录变更清单 |

## 13. 计划变更协议

若实现证据表明本方案假设不成立，不得静默改变目标。必须在本文末尾追加：

```text
### Plan Mutation YYYY-MM-DD / <id>
- Evidence:
- Affected steps:
- Proposed change:
- Preserved invariants:
- New risks:
- User approval required: yes/no
```

不得通过计划变更取消：模型与工具边界、N-hop 逐步验证、真实 RAG 逐 hop 文本 coverage、独立 Verifier、持久化预算、人工重复判断权、不可变审核记录和正式导出门禁。

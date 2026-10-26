# 面向实体的自动探索与问题生成：缺口分析与 Agent 技术路线

> **历史路线，已停止作为需求依据。** 当前产品需求以
> `docs/medical-qa-data-factory/entity-question-generation-implementation-requirements.md` requirements-v2 为准。

> 状态：核心能力设计草案  
> 优先级：P0  
> 更新日期：2026-10-17  
> 适用项目：Medical QA Data Factory

> 历史范围说明（2026-10-20）：本文中的 PrimeKG-only 阶段描述用于记录早期最小闭环，不再定义当前真实生成 Agent 的范围。当前实施以 `plans/real-generation-agent-implementation-plan.md` 与 PRD v0.4 为准；真实外部 RAG 和逐 hop 文本证据 coverage 已进入本轮主链路。

## 1. 核心目标

系统的核心产品能力不是“能够查询 PrimeKG”，而是：

```text
输入一个医学实体
→ 自动解析并消歧
→ 在一个或多个知识源中有预算地探索
→ 选择可靠且有出题价值的证据
→ 构造可追溯 claim
→ 生成多类型问题、标准答案和证据要求
→ 独立验证、自动校验、持久化并进入人工审核
```

目标命令形态：

```bat
python -m medical_qa_data_factory generate entity ^
  --entity "Aspirin" ^
  --entity-id D001241 ^
  --entity-type exposure ^
  --kg-provider primekg ^
  --question-count 10 ^
  --max-hops 2 ^
  --workspace data/runs/aspirin-v1
```

最终输出至少包括：

- `GenerationTask`：任务状态、配置、预算、调用记录和错误；
- `EvidenceBundle`：实体、图谱边、文献证据、claim、快照和溯源；
- `QuestionSpec`：问题、变体、答案、taxonomy、证据要求和路线；
- `GenerationEpisode`：Agent 的计划、工具调用、观察、停止原因和成本；
- 校验结果：机器校验等级、失败原因和人工审核入口。

## 2. 当前系统实际能力

### 2.1 已具备

- `GenerationTask`、`EvidenceBundle`、`QuestionSpec` 数据契约；
- 不可变版本化本地仓库；
- 任务状态、自动校验、审核工作台和导出门禁；
- `KGTool`、`LiteratureTool`、`DraftAgent` 高层端口；
- PrimeKG provider-neutral 接口、实体解析、邻居查询和诊断 CLI；
- 成本模型、用量记录和工具快照的基础结构；
- Mock 模式下从种子到问题草稿的端到端演示。

### 2.2 当前真实行为

`demo run` 不查询 PrimeKG。它为任何输入实体伪造固定关系：

```text
输入实体 --targets--> PTGS1
```

之后用固定模板生成一条问题。因此 Mock 流程只能验证存储、状态和审核，不能验证真实自动探索与问题生成能力。

PrimeKG CLI 当前只执行：

```text
实体名称 → 实体解析 → 邻居/路径 JSON
```

查询结果尚未进入 claim 构造、问题生成和正式流水线。

## 3. 核心缺口

| 能力 | 当前状态 | 缺失内容 | 优先级 |
|---|---|---|---:|
| 实体身份校验 | 部分完成 | 类型安全 ID、上游身份契约和 fail-fast 校验 | P0 |
| 图谱探索 | 只有查询接口 | 探索策略、循环、预算、停止条件 | P0 |
| 多跳探索 | 当前实现不可靠 | 有序路径、关系过滤、分支控制 | P0 |
| 证据筛选 | 未实现 | 可靠性、信息量、可出题性和去重评分 | P0 |
| Claim 构造 | 仅 Mock 模板 | 基于证据的结构化 claim 生成与校验 | P0 |
| 问题规划 | 未实现 | 问题类型、难度、路线和数量分配 | P0 |
| 问题生成 | 仅固定模板 | 基于 claim/evidence 的受约束生成 | P0 |
| 独立验证 | 未实现 | 答案复算、证据充分性和反泄漏验证 | P0 |
| Agent 编排 | 未实现 | 状态机、工具观察、恢复、回放 | P0 |
| LLM 端口 | 未实现 | provider-neutral structured generation | P0 |
| 真实生成 CLI | 未实现 | `generate entity` 与任务配置 | P0 |
| Episode 轨迹 | 未实现 | step、tool call、observation、stop reason | P1 |
| 文献探索 | 仍是 Demo | 真实 RAG provider 和混合探索 | P1 |
| 来源消融 | 未实现 | KG-only、Literature-only、Combined | P1 |
| 质量评测 | 校验器已有 | 核心能力 eval 集和基线指标 | P0 |

## 4. 设计原则

1. **确定性编排，Agent 做语义决策**：状态转换、预算、持久化、重试和门禁由代码控制；Agent 不直接控制文件和数据库。
2. **结构化输出**：Agent 必须返回 Pydantic 可校验对象，不接受自由文本作为流水线控制信号。
3. **证据先于问题**：任何 claim、答案和问题都必须引用已经持久化的 evidence ID。
4. **规划与执行分离**：Agent 提出探索动作，工具执行器校验并执行；Agent 不能直接拼 SQL。
5. **生成与验证分离**：生成问题的 Agent 不能直接决定其最终通过；独立 verifier 和确定性校验器负责门禁。
6. **有预算的循环**：每次探索都有 hop、边数、工具调用、token、金额和时间上限。
7. **空结果是观察，不是异常**：合法无证据进入下一决策；连接和数据错误进入恢复流程。
8. **全程可回放**：相同快照、配置、模型版本和随机种子应能够重放每一步。
9. **provider-neutral**：编排器面向 `KnowledgeGraphProvider`、`LiteratureTool` 和 `LanguageModelPort`，不依赖 PrimeKG 或某家模型。
10. **eval-first**：先定义实体到问题的能力评测，再实现 Agent，防止“能运行但不会正确出题”。

## 5. 推荐架构

```text
CLI / API
   │
   ▼
EntityQuestionGenerationService
   │
   ▼
GenerationOrchestrator ─────────────────────────────┐
   │                                                │
   ├─ Entity Resolver（确定性 + 人工门禁）          │
   ├─ Exploration Planner Agent                     │
   ├─ KG/Literature Tool Executor（确定性）         │
   ├─ Evidence Curator Agent                        │
   ├─ Claim Builder Agent                           │
   ├─ Question Planner Agent                        │
   ├─ Question Writer Agent                         │
   ├─ Answer Verifier Agent                         │
   └─ Machine Validators（确定性）                  │
                                                    │
   ◄──── Episode Store / Budget Ledger / Trace ─────┘
```

不建议使用 CrewAI 等“角色聊天”框架。第一版直接采用 LangGraph `StateGraph`
作为编排运行时，并把业务状态定义为项目自己的 Pydantic contracts。LangGraph 负责节点、
条件边、checkpoint、interrupt 和恢复；项目 contracts 负责可发布的数据语义，避免业务模型
与某个框架的内部对象绑定。

选择 LangGraph 的原因：

- 状态和停止条件可测试；
- 工具调用可严格校验；
- 错误恢复可预测；
- 方便持久化和回放；
- 支持确定性节点和 Agent 节点混合；
- 支持 checkpoint、人工中断和恢复；
- 它是低层编排运行时，不要求同时采用 LangChain 的高层 Agent 抽象。

编排模式采用混合模式：

- ReAct 用于探索规划和证据充分性判断；
- function calling/structured output 用于所有动作执行；
- LangGraph `StateGraph` 控制任务生命周期，节点内部仍使用普通 Python 和项目端口。

## 6. Agent 与确定性组件分工

### 6.1 Entity Resolver

类型：确定性上游契约校验服务。

职责：

- 接收上游提供的 `provider_id`、`native_id`、`entity_type`、`canonical_name`；
- 在目标 provider 中验证该实体是否存在且类型一致；
- 上游身份无效或缺失时 fail fast，不在本项目内做模糊消歧；
- 输出稳定 `EntityRef` 和校验轨迹。

第一版假设实体消歧由数据上游完成。LLM 不参与医学实体身份选择，本项目也不静默回退到
“按名称取第一个候选”。未来若上游不能保证唯一身份，再单独增加消歧阶段。

### 6.2 Exploration Planner Agent

类型：语义规划 Agent。

输入：

- 已解析实体；
- provider 能力；
- 已观察证据摘要；
- 可用关系和实体类型；
- 当前预算；
- 已生成问题的类型分布；
- 上一步失败或空结果。

输出：

```python
class ExplorationDecision(ContractModel):
    action: Literal["neighbors", "paths", "literature_search", "stop"]
    rationale: str
    relation_filters: list[str]
    target_types: list[str]
    direction: Literal["outgoing", "incoming", "both"]
    start_entity_id: str
    end_entity_id: str | None
    max_hops: int
    limit: int
    expected_information: str
    stop_reason: str | None
```

Agent 只能从白名单动作和已知实体中选择，不能输出 SQL、路径或任意工具名。

### 6.3 Tool Executor

类型：确定性组件。

职责：

- 校验动作是否在 provider capability 范围内；
- 应用预算和安全上限；
- 执行 KG 或文献工具；
- 转换成统一 observation；
- 保存原始结果、trace、snapshot 和 usage；
- 分类错误并提供安全的 retry 建议。

统一工具观察：

```python
class ToolObservation(ContractModel):
    status: Literal["success", "warning", "error"]
    summary: str
    next_actions: list[str]
    artifact_ids: list[str]
    evidence_ids: list[str]
    error_code: str | None
    retryable: bool
    truncated: bool
    trace_id: str
```

### 6.4 Evidence Curator Agent

类型：语义评估 Agent + 确定性预过滤。

确定性预过滤：

- 去重；
- 删除跨类型 ID 污染；
- 检查来源和快照；
- 限制高连接度节点；
- 过滤不允许的关系或来源；
- 检查 evidence schema。

Agent 评估：

- 与种子实体的相关性；
- 医学信息量；
- 是否适合形成可回答问题；
- 是否需要第二来源支持；
- 是否存在冲突或不确定性。

输出 `EvidenceSelection`，只能引用输入中的 evidence ID。

### 6.5 Claim Builder Agent

类型：结构化生成 Agent。

输出：

```python
class ClaimProposal(ContractModel):
    claim_text: str
    claim_type: ClaimType
    subject_entity_id: str
    predicate: str
    object_entity_id: str | None
    object_value: str | None
    supporting_evidence_ids: list[str]
    contradicting_evidence_ids: list[str]
    confidence: float
    rationale: str
```

约束：

- 不允许引用不存在的 evidence ID；
- 不允许把图谱中没有的方向翻转为因果关系；
- association 不能自动升级为 causal；
- claim 必须能由引用证据逐项复核；
- 低置信度或冲突 claim 不直接进入 gold 问题。

### 6.6 Question Planner Agent

类型：组合规划 Agent。

职责：根据 claim、证据结构和目标分布生成问题蓝图，而不是直接写最终问题。

```python
class QuestionBlueprint(ContractModel):
    blueprint_id: str
    claim_ids: list[str]
    question_type: Literal[
        "single_fact",
        "constrained_list",
        "multi_hop",
        "mechanism",
        "evidence_verification",
        "unanswerable",
    ]
    answer_form: AnswerForm
    reasoning_pattern: list[str]
    expected_route: Route
    difficulty: Literal["easy", "medium", "hard"]
    required_evidence_ids: list[str]
    leakage_constraints: list[str]
```

规划器负责数量和分布，例如 10 道题不应全部来自第一条邻接边。

### 6.7 Question Writer Agent

类型：受约束生成 Agent。

输入只包含：

- 一个 `QuestionBlueprint`；
- 对应 claim；
- 必需证据；
- 风格、语言和禁止泄漏规则。

输出 `QuestionDraft`：

- canonical question；
- 问题变体；
- gold answer；
- acceptable variants；
- taxonomy；
- evidence requirements；
- 生成说明。

Writer 不得自行增加证据或发明答案。

### 6.8 Answer Verifier Agent

类型：独立验证 Agent。

Verifier 不读取 Writer 的自由文本理由，只接收问题、允许使用的证据和结构化答案候选。它需要：

- 独立回答问题；
- 比较答案语义；
- 检查证据是否充分；
- 检查问题是否泄漏答案；
- 标记歧义、多个合理答案和错误前提；
- 输出通过、退回或拒绝。

MVP 中 Writer 和 Verifier 使用同一个模型端口和同一个模型配置，但必须使用不同 prompt、
独立上下文，并禁止把 Writer 的隐藏推理传给 Verifier。模型 provider 暂不选型，只保留
`LanguageModelPort` 和 deterministic fake；接入真实模型不阻塞编排和契约开发。

## 7. 编排状态机

建议新增显式状态：

```text
pending
  ↓
resolving_entity
  ├─ invalid upstream identity → failed / needs_input
  └─ identity verified
       ↓
planning_exploration
       ↓
executing_tool
       ↓
observing
  ├─ evidence insufficient + budget remains → planning_exploration
  ├─ no progress → curating_evidence
  ├─ retryable error → executing_tool
  └─ fatal error → failed / partial
       ↓
curating_evidence
       ↓
building_claims
       ↓
planning_questions
       ↓
writing_questions
       ↓
verifying_answers
  ├─ revision allowed → writing_questions
  ├─ rejected → record rejection
  └─ accepted
       ↓
machine_validation
       ↓
persisted
       ↓
completed / partial / needs_review
```

状态转换必须由 orchestrator 校验，Agent 只能提出 decision，不能直接设置任务状态。

## 8. 探索循环与停止条件

### 8.1 探索预算

每个实体必须配置：

```python
class ExplorationBudget(ContractModel):
    max_hops: int = 2
    max_tool_calls: int = 20
    max_edges_observed: int = 500
    max_entities_expanded: int = 50
    max_agent_steps: int = 30
    max_wall_time_seconds: int = 300
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_cost: float | None = None  # 第一版不启用金额门禁
```

任何 Agent 输出都不能扩大预算。

### 8.2 强制停止原因

```text
target_question_count_reached
evidence_sufficient
no_new_evidence
no_valid_actions
invalid_upstream_identity
tool_budget_exhausted
token_budget_exhausted
cost_budget_exhausted
time_budget_exhausted
provider_unavailable
human_input_required
```

停止原因必须写入 episode，而不是只记录自然语言总结。

### 8.3 防止探索爆炸

- 默认 1-hop，只有 planner 明确说明信息缺口时才进入 2-hop；
- 对高连接度节点先按关系和目标类型过滤；
- 每次最多返回有限边数；
- 不重复扩展相同 canonical entity；
- 对相同动作计算 action hash，禁止无变化重试；
- 连续两次没有新增有效 evidence 时停止；
- paths 未满足正确性验收前不向 Agent 暴露。

## 9. Tool Action Space

第一版只暴露小而稳定的工具集：

| 工具 | 类型 | 用途 |
|---|---|---|
| `kg.resolve_entity` | 中等 | 解析和消歧实体 |
| `kg.get_neighbors` | 中等 | 有界邻居查询 |
| `kg.get_paths` | 中等、高风险 | 正确实现后才启用 |
| `literature.search` | 中等 | 后续真实文献检索 |
| `evidence.get` | 微工具 | 按 ID 读取已保存证据 |
| `episode.observe_budget` | 微工具 | 获取剩余预算 |
| `orchestrator.stop` | 微工具 | 请求以枚举原因停止 |

不要提供：

- `run_sql(sql)`；
- 无限制 `graph_search(query)`；
- 允许 Agent 写文件或修改 artifact 的工具；
- 一个同时完成搜索、生成和保存的 catch-all 工具。

所有工具响应统一包含 `status`、`summary`、`next_actions`、`artifacts` 和结构化 data。

## 10. 新增数据契约

### 10.1 什么是 Agent Episode

一个 Agent episode 就是“一次面向某个实体的完整生成运行”。例如执行一次：

```text
generate entity --entity Aspirin --question-count 5
```

会创建一个 episode。它类似任务执行日志和可恢复事务，记录：

- 初始实体和生成配置；
- LangGraph 走过哪些节点；
- Agent 每一步提出什么动作；
- 调用了哪些工具、得到哪些 observation；
- 收集了哪些 evidence、claim 和问题；
- 为什么继续、重试或停止；
- 使用的 prompt/policy/model 版本；
- token、工具调用、耗时和错误。

Episode 不是“Agent 的人格”，也不是长期记忆。它解决的是审计、调试、checkpoint、恢复和
复现实验。LangGraph 中建议令 `thread_id = episode_id`：LangGraph checkpoint 保存运行时
状态，项目自己的 `GenerationEpisode` 保存稳定的业务审计记录。

正式数据集只需要引用 `episode_id` 和必要 manifest；完整 episode 默认保存在内部审计目录，
避免把 prompt、模型原始响应和大量中间状态全部发布出去。

### 10.2 契约列表

建议新增：

```text
EntityGenerationRequest
ExplorationPolicy
ExplorationDecision
ToolObservation
EvidenceSelection
ClaimProposal
QuestionBlueprint
QuestionDraft
VerificationResult
GenerationEpisode
AgentStep
ToolCallRecord
StopReason
```

`GenerationEpisode` 建议包含：

```python
class GenerationEpisode(ContractModel):
    episode_id: str
    task_id: str
    seed_entity: EntityRef
    state: str
    policy_version: str
    prompt_versions: dict[str, str]
    model_snapshots: list[ToolSnapshot]
    steps: list[AgentStep]
    evidence_ids: list[str]
    claim_ids: list[str]
    question_ids: list[str]
    usage_ids: list[str]
    stop_reason: StopReason | None
    started_at: UtcDateTime
    completed_at: UtcDateTime | None
```

Episode 应独立于 `QuestionSpec`，避免把大量运行轨迹塞进最终数据集记录。

## 11. LLM 端口

新增 provider-neutral 模型接口：

```python
class StructuredGenerationRequest(ContractModel):
    role: str
    system_prompt_version: str
    input_payload: dict[str, Any]
    output_schema_name: str
    temperature: float
    max_output_tokens: int
    idempotency_key: str


class StructuredGenerationResult(ContractModel):
    parsed: dict[str, Any]
    invocation: InvocationConfig
    usage: UsageRecord
    raw_response_id: str | None


class LanguageModelPort(Protocol):
    def generate_structured(
        self,
        request: StructuredGenerationRequest,
        response_model: type[ContractModel],
    ) -> StructuredGenerationResult: ...
```

要求：

- 所有 prompt 有版本号；
- 输出经过 Pydantic 校验；
- schema 失败最多自动修复一次；
- 第二次失败进入明确错误状态；
- 保存模型 ID、revision、temperature、token、调用 ID 和成本；
- 测试使用 deterministic fake model，不调用真实 API。

## 12. Orchestrator 接口

```python
class EntityQuestionGenerationService:
    def generate(
        self,
        request: EntityGenerationRequest,
    ) -> EntityGenerationResult:
        ...


class GenerationOrchestrator:
    def run(self, episode: GenerationEpisode) -> GenerationEpisode:
        ...

    def resume(self, episode_id: str) -> GenerationEpisode:
        ...
```

Orchestrator 每一步遵循：

```text
读取状态
→ 检查预算
→ 枚举合法动作
→ 调用 Agent 决策或确定性处理器
→ 校验结构化输出
→ 执行工具
→ 持久化 observation 和 usage
→ 更新状态
→ 判断停止或继续
```

每一步先持久化输入摘要和 idempotency key，再执行外部调用，确保进程中断后可以安全恢复。

## 13. 问题生成策略

### 13.1 MVP 问题类型

第一版支持三类即可：

1. `single_fact`：一个明确边即可回答；
2. `constrained_list`：同一关系和约束下的多个实体；
3. `multi_hop`：两条可验证边组成的路径。

第二阶段再增加：

- 机制解释；
- 文献证据验证；
- 冲突证据题；
- unanswerable；
- KG 与文献混合问题。

### 13.2 问题生成约束

- 问题不能直接包含答案字符串；
- 问题不能依赖未保存证据；
- 答案必须能从 required evidence 复算；
- 多跳题必须保存有序路径；
- 列表题必须定义集合边界和完整性规则；
- 所有通过结构校验的关系都可以作为候选出题来源；
- 问题措辞必须服从关系语义：association 只能生成关联表述，只有明确的因果或机制关系
  才能生成因果、作用机制类表述；
- 同一事实的同义表达属于同一 `logical_question_id`；
- 同一实体不能只生成单一模板。

单实体题数由配置控制，第一版建议：

```text
min_question_count = 1
default_question_count = 5
max_question_count = 20
```

CLI 输入超出范围时直接拒绝，而不是由 Agent 自行扩大数量。

## 14. Eval-first 评测设计

实现 Agent 前建立最小 eval 集：5 个实体，每个实体人工定义可接受事实、禁止关系和至少 2 个参考问题。

建议覆盖：

- 药物/暴露；
- 疾病；
- 基因/蛋白；
- 解剖结构；
- 一个跨类型 ID 冲突实体，用于验证 provider 查询隔离；实体身份仍由 fixture 上游明确给出。

### 14.1 能力指标与低门槛人工审核策略

第一版生成数量有限，所有候选问题都进入人工审核。因此不设置很高的“自动通过率”作为
发布前置条件；核心目标是机制能够稳定产生至少一个可信问题，同时不让明确错误进入审核队列。

机器硬门禁只拦截：

- Schema 或跨对象引用错误；
- evidence 来源不可追溯；
- answer 与 required evidence 明确不一致；
- 跨类型 ID 污染；
- 问题直接泄漏答案；
- 重复问题；
- 不存在或类型不一致的上游实体。

自然度、难度、模板化程度、问题多样性和边界案例先作为 warning，由人工逐题决定。下面的
指标用于观察改进趋势，不全部作为阻塞阈值。

| 指标 | MVP 目标 |
|---|---:|
| 实体解析准确率 | 100%（5 个种子） |
| evidence 来源可追溯率 | 100% |
| 每个实体至少一个可信问题 | 100%（MVP 种子） |
| claim-evidence 明确错误 | 0 条进入人工审核 |
| 问题不可回答的明确错误 | 0 条进入人工审核 |
| gold answer 明确错误 | 0 条进入人工审核 |
| 答案泄漏率 | 0% |
| 跨类型 ID 污染率 | 0% |
| 完全重复率 | 0% |
| 人工自然度通过率 | 记录基线，暂不设硬阈值 |
| 任务可回放率 | 100% |

### 14.2 运行指标

- 每题工具调用次数；
- 每题观察边数；
- Agent step 数；
- token、耗时、金额；
- retry 次数；
- stop reason 分布；
- pass@1 与修订后 pass@2；
- 每个实体成功生成的问题数。

### 14.3 回归评测

每次改动至少验证：

- 现有 117 个默认测试；
- PrimeKG 真实库集成测试；
- 5 个实体的固定 replay；
- ID 冲突、空结果、高连接度和无效上游实体；
- 模型超时、无效 JSON、预算耗尽和断点恢复。

## 15. 技术路线

### Phase 0：修复 PrimeKG 正确性

目标：提供可信的探索工具。

- [ ] native ID 查询绑定实体类型，消除跨类型 ID 污染；
- [ ] 修复 `target_types` 的方向相关过滤；
- [ ] 修复实体解析的类型相关条件；
- [ ] paths 正确实现前关闭 capability 和 CLI；
- [ ] 修复 nullable cursor；
- [ ] SHA256 改为冻结 manifest 预计算；
- [ ] 增加真实回归：`disease:8315` 不得返回 `BRAP`；
- [ ] 增加错误过滤回归：目标类型不匹配必须为空。

完成标准：图谱工具不会产生已知错误证据。

### Phase 1：定义核心 contracts 和 eval 基线

- [ ] 新增 generation request、episode、step、decision、observation；
- [ ] 新增 `LanguageModelPort` 与 deterministic fake；
- [ ] 使用 LangGraph `StateGraph` 定义节点和条件边；
- [ ] 开发环境配置 SQLite checkpointer，并令 `thread_id = episode_id`；
- [ ] 建立 5 实体 eval fixture；
- [ ] 定义能力指标和基线报告；
- [ ] 定义 prompt 和 policy 版本规范；
- [ ] 定义 exploration budget 与 stop reason。

完成标准：尚未接真实模型也能用 fixture 回放完整状态机。

### Phase 2：PrimeKG-only 最小垂直闭环

第一版不做开放式 ReAct，使用规则探索 + Agent 生成，先证明端到端正确性：

```text
实体精确解析
→ 1-hop 有界邻居
→ 确定性过滤和去重
→ Claim Builder
→ Question Planner
→ Question Writer
→ Verifier
→ 现有校验器
→ 不可变存储
```

- [ ] 实现 `EntityQuestionGenerationService`；
- [ ] 实现 LangGraph orchestrator，确定性节点控制状态和门禁；
- [ ] 接入 `KnowledgeGraphEvidenceCollector`；
- [ ] 实现 claim、blueprint、writer、verifier；
- [ ] 新增 `generate entity` CLI；
- [ ] 修复 pipeline 多 seed 仅处理最后 seed 的问题；
- [ ] 正确传播异常 `code/retryable`；
- [ ] 支持配置 1～20 题，默认 5 题；
- [ ] 跑通单实体至少 1 道可信问题。

完成标准：Aspirin 使用真实 PrimeKG 生成至少 1 道有证据、可复核并经人工接受的问题；
其他候选允许被人工退回或拒绝。

### Phase 3：Agentic 探索循环

- [ ] 实现 Exploration Planner Agent；
- [ ] 暴露受限 action space；
- [ ] 实现观察、新证据检测和 action hash；
- [ ] 支持按信息缺口选择第二个关系或实体；
- [ ] 实现预算和所有 stop reason；
- [ ] 保存 episode 和 step；
- [ ] 支持失败恢复和 resume；
- [ ] 加入 Evidence Curator Agent。

完成标准：Agent 能在不超过预算的情况下自主选择多次查询，并说明停止原因。

### Phase 4：可信 2-hop 与多类型问题

- [ ] 新增 `GraphPath` 有序路径契约；
- [ ] 修复并启用路径查询；
- [ ] 生成可复算的两跳问题；
- [ ] 支持 constrained-list 完整性规则；
- [ ] 增加问题类型和难度分配；
- [ ] 处理高连接度实体和路径爆炸。

完成标准：固定 2-hop eval 中路径、问题和答案全部可回放。

### Phase 5：文献与多源编排

- [ ] 接入真实文献 RAG；
- [ ] planner 能选择 KG 或 Literature action；
- [ ] 合并但不混淆两个来源的证据；
- [ ] 实现 KG-only、Literature-only、Combined 消融；
- [ ] 根据消融确定 `verified_route`；
- [ ] 支持冲突和证据不足问题。

完成标准：每条正式问题有可复算的来源路线和消融记录。

### Phase 6：真实 MVP

- [ ] 5 个种子、约 20 道问题；
- [ ] 全部机器校验；
- [ ] 同模型、独立 prompt 和上下文的 verifier；
- [ ] 人工审核与修订；
- [ ] 质量和失败报告；
- [ ] 带 manifest、hash、episode、snapshot 的导出。

## 16. 建议代码结构

```text
src/medical_qa_data_factory/
├── generation/
│   ├── contracts.py
│   ├── orchestrator.py
│   ├── service.py
│   ├── state_machine.py
│   ├── policies.py
│   ├── budgets.py
│   ├── prompts/
│   │   ├── exploration_planner.v1.md
│   │   ├── evidence_curator.v1.md
│   │   ├── claim_builder.v1.md
│   │   ├── question_planner.v1.md
│   │   ├── question_writer.v1.md
│   │   └── answer_verifier.v1.md
│   └── agents/
│       ├── exploration_planner.py
│       ├── evidence_curator.py
│       ├── claim_builder.py
│       ├── question_planner.py
│       ├── question_writer.py
│       └── answer_verifier.py
├── llm/
│   ├── contracts.py
│   ├── ports.py
│   ├── fake.py
│   └── providers/
├── knowledge_graph/
├── domain/
└── cli.py

tests/
├── evals/
│   ├── entity_generation_cases.json
│   └── expected_facts.json
├── test_generation_state_machine.py
├── test_generation_orchestrator.py
├── test_generation_replay.py
├── test_agent_contracts.py
├── test_generation_cli.py
└── test_entity_generation_eval.py
```

## 17. 开发任务拆分

每个任务应能够独立验证，避免一次让 Agent 修改整个系统：

1. 修复 PrimeKG ID 类型隔离，并增加失败回归；
2. 修复 target type 方向过滤，并增加失败回归；
3. 暂时关闭不正确的 paths capability；
4. 定义 `GenerationEpisode` 和 `AgentStep`；
5. 定义 exploration budget 和 stop reason；
6. 实现 deterministic state machine 空壳；
7. 实现 `LanguageModelPort` 和 FakeModel；
8. 实现规则版 1-hop evidence curator；
9. 实现结构化 Claim Builder；
10. 实现 Question Blueprint；
11. 实现 Writer；
12. 实现 Verifier；
13. 串接现有校验器和仓库；
14. 增加 `generate entity` CLI；
15. 完成单实体端到端 eval；
16. 再引入 Exploration Planner Agent；
17. 实现 episode replay/resume；
18. 正确实现并启用 2-hop。

## 18. 核心验收标准

只有以下条件同时满足，才认为核心需求完成：

- [ ] 输入一个实体即可启动真实生成任务；
- [ ] 上游必须提供 provider、native ID 和 entity type，系统验证失败时不得继续；
- [ ] Agent 可以在受限动作空间内选择探索动作；
- [ ] 每次探索受预算和停止条件控制；
- [ ] 所有 evidence 来自固定 provider snapshot；
- [ ] 所有 claim 引用具体 evidence ID；
- [ ] 所有问题引用具体 claim 和 evidence；
- [ ] gold answer 可以从 required evidence 复算；
- [ ] Writer 和 Verifier 使用同一模型但保持 prompt 与上下文隔离；
- [ ] 失败、空结果、重试和停止原因可以回放；
- [ ] 单实体可以生成多种问题而非固定模板；
- [ ] 生成结果通过现有机器校验并进入审核工作台；
- [ ] 真实 PrimeKG 查询不存在跨类型 ID 污染；
- [ ] 5 个固定实体 eval 达到预定指标；
- [ ] 每题 token、工具调用、时间和成本均可统计。

## 19. 当前最优先的下一步

不要立即实现完整多 Agent 系统。先完成以下最小路径：

```text
修复 PrimeKG 正确性
→ 建立 GenerationEpisode 与 eval
→ 规则版 1-hop 探索
→ Claim Builder Agent
→ Question Planner/Writer Agent
→ 独立 Verifier
→ generate entity CLI
```

这条路径先交付真正可用的核心功能，再把规则探索替换或增强为 Agentic 探索循环。否则同时开发图查询、多跳、多个 Agent、文献 RAG 和 UI，会难以判断错误来自哪一层。

## 20. 已确认决策

1. **模型 provider 暂不选型**：只实现 `LanguageModelPort`、structured output 契约和 deterministic fake。
2. **Writer 与 Verifier 暂用同一模型**：使用不同 prompt 和独立上下文。
3. **问题数量可配置**：范围 1～20，默认 5；后续根据人工审核结果调整。
4. **金额预算后续确定**：第一版仍保留工具调用、边数、step、hop 和时间硬上限，`max_cost` 可为空。
5. **第一版 PrimeKG-only**：暂不接入文献 RAG 和来源消融。
6. **实体消歧由上游负责**：上游必须给出 provider、native ID、entity type 和名称，本项目只验证。
7. **所有合法关系均可成为出题候选**：但问题措辞必须符合关系语义，association 不得写成 causal。
8. **Episode 内部保存，数据集引用**：完整运行轨迹默认不进入公开数据，只在 manifest 中引用 `episode_id`。
9. **LangGraph 作为第一版默认编排运行时**：使用 `StateGraph`、checkpoint 和 interrupt；业务 contracts 保持框架无关。
10. **低机器门槛 + 人工全量审核**：机器只阻塞明确错误；自然度、多样性和难度先 warning。MVP 的最低能力目标是每个种子至少产生 1 道人工接受的可信问题。

## 21. 仍待后续决定

- 真实模型 provider 和具体模型；
- 默认工具调用、时间和金额预算；
- 真实文献 RAG 接入时机；
- episode 的保留期限和公开字段；
- 通过首批人工审核后是否提高机器阈值；
- Phase 4 之后 Writer 与 Verifier 是否改用不同模型。

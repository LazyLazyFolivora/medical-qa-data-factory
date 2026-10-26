# Medical QA ReAct 多跳生成 Agent 改造蓝图

> **历史设计，已停止作为需求依据。** 当前产品需求以
> `docs/entity-question-generation-implementation-requirements.md` requirements-v2 为准。
> 本文件描述的复杂多跳 Agent 只可作为实现参考，不是默认产品目标。

> 用途：交给新的编码模型冷启动执行。  
> 项目目录：`E:\Cursor\assumption\projects\medical-qa-data-factory`  
> 执行模式：direct mode；当前目录不是 Git 仓库，不得假设存在分支、PR 或可回滚提交。  
> 生成日期：2026-10-17  
> 状态：待实施

## 0. 给编码模型的首要指令

本任务不是在现有固定流水线旁边再包装一层“Agent”命名，而是要把核心探索决策恢复为此前设计的受控 ReAct 循环，并加入可信的多跳证据链和多跳问题生成。

开始改代码前，必须完整阅读：

1. `README.md`
2. `UNFINISHED_CHECKLIST.md`
3. `docs/entity-question-generation-agent-roadmap.md`
4. `docs/entity-question-generation-implementation-requirements.md`
5. `docs/primekg-integration-design.md`
6. `src/medical_qa_data_factory/generation/`
7. `src/medical_qa_data_factory/knowledge_graph/`
8. `src/medical_qa_data_factory/llm/`
9. `src/medical_qa_data_factory/machine_validation.py`
10. 与上述模块对应的全部测试

实施时必须遵守：

- 先修复基线，再增加能力；不能通过跳过测试、放宽校验或吞掉异常来制造“通过”。
- 每一步都必须能被单独验证；不要一次性重写整个项目。
- 保留不可变产物、Episode/AgentStep 审计、SQLite checkpoint、预算门禁、机器校验和人工审核。
- Agent 只能输出结构化决策，不能直接执行 SQL、拼接文件路径或指定任意工具名。
- 不保存或展示模型的隐藏思维链。只保存可审计的结构化决策字段、简短依据、预期信息和停止原因。
- 任何多跳答案必须由有序证据链支持；不允许把互不相连的边拼成“多跳”。
- 不允许把 association 自动升级为 causal、therapeutic 或 mechanistic claim。
- 真实模型、真实文献工具不可用时必须 fail fast 或明确降级，不能静默退回规则模式后仍声称运行了 ReAct。
- 不修改 `datasets/PrimeKG/primekg.sqlite`；数据库保持只读。

## 1. 目标与非目标

### 1.1 总目标

从一个上游已消歧的医学实体出发，系统应在预算内自主执行以下循环：

```text
Observe current evidence and gaps
        ↓
Exploration Planner produces a structured decision
        ↓
Policy/Tool Executor validates and executes a whitelisted action
        ↓
Persist observation, evidence, usage and snapshot
        ↓
Curate evidence and judge sufficiency
        ├─ insufficient → next ReAct turn
        └─ sufficient   → claims → question blueprints → drafts
                                      ↓
                              independent verification
                                      ↓
                           machine validation → human review
```

目标能力：

- 一跳与可信二跳 KG 探索；
- Agent 根据证据缺口选择下一动作，而不是固定查询全部 incoming/outgoing 后立即停止；
- 有序、可追溯、可验证的多跳证据路径；
- 单事实、受约束列表、二跳关系、机制线索和证据验证等问题蓝图；
- Writer 与 Verifier 解耦；Verifier 独立复算答案；
- 达到题数、证据充分、无新证据、预算耗尽或工具失败时可解释停止；
- 全过程可恢复、可回放、可审计。

### 1.2 本蓝图不要求

- 不要求自由聊天式多 Agent；使用一个确定性 orchestrator 管理多个职责明确的 Agent/组件。
- 不要求 Agent 自己写 SQL、执行 shell 或访问任意网络地址。
- 不要求一次扩展到 40 个种子或 200 题。
- 不要求立即公开数据集。
- 不要求支持超过 2-hop；先把 2-hop 做正确，再考虑更深路径。
- 不要求以自然语言保存 ReAct 的隐藏推理过程。

## 2. 当前真实基线

### 2.1 已具备

- Pydantic 领域契约：`GenerationTask`、`EvidenceBundle`、`QuestionSpec` 等；
- 本地不可变 JSON 版本仓库；
- LangGraph `StateGraph`；
- SQLite checkpoint、episode、step、tool observation 和 rejection record；
- 预算计数、停止原因、部分完成和失败状态；
- PrimeKG 只读实体解析及 incoming/outgoing 邻居查询；
- 确定性 evidence curation、claim、question blueprint、writer、verifier；
- 11 项机器校验、人工审核工作台和正式导出门禁；
- 一个真实 Aspirin PrimeKG smoke 产物。

### 2.2 当前并不具备

- 真实 ReAct Observe → Decide → Act 循环；
- Exploration Planner 的动态动作选择；
- Evidence Sufficiency Agent；
- 动态扩展中间实体；
- 可靠的有序 2-hop 路径；
- 多跳 claim 与多跳 blueprint；
- 真实结构化模型调用；
- 文献 RAG 和 KG/Literature 混合动作；
- 独立答案复算；
- 基于 verifier 反馈的真实重写。

### 2.3 当前固定流水线

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

这是一条可审计 DAG，但不是 ReAct。`AgentStep` 目前只是节点日志，`ToolObservation` 也没有反馈给 Planner 决定下一动作。

### 2.4 已确认基线缺陷

实施新能力前先修复：

1. `RuleBasedAnswerVerifier` 把“候选答案位于 edge.head”错误等同于 `answer_is_seed`。它必须比较真实 `focus_entity_id/seed_canonical_id`，不能根据 head/tail 位置猜测种子。
2. `qa_env` 当前缺少 `requests` 和 `tenacity`，导致 LangGraph/`langchain-core` 导入失败；`requirements.txt` 与环境不可复现。
3. `rule-based` 与 `fake` 当前装配相同组件，模式名称具有误导性。
4. `README.md` 与 `UNFINISHED_CHECKLIST.md` 对 PrimeKG、测试数量和未完成项的描述不一致。
5. PrimeKG `paths()` 不能提供满足生成要求的完整有序路径，当前 capability 正确地声明为 `path_query=false`；在修复前不得向 Planner 暴露。

## 3. 不可破坏的系统不变量

1. **证据先于结论**：claim、答案和问题只能引用已持久化 evidence/path ID。
2. **身份严格**：上游提供 provider/native ID/type；系统不能按名称静默选择第一个候选。
3. **工具白名单**：Planner 只选择枚举动作，Tool Executor 再校验 capability、参数和预算。
4. **状态由代码控制**：Agent 可以提出 `continue/finish` 建议，最终路由由 orchestrator 决定。
5. **业务记录与 checkpoint 分离**：checkpoint 不能替代不可变 AgentStep/Observation/Artifact。
6. **生成与验证分离**：Writer 不能批准自己的问题。
7. **候选答案不是 gold**：人工确认前保持 candidate 语义；底层兼容字段不得影响 UI 表述。
8. **无证据不是工具错误**：合法空结果是 observation；连接、超时、配置和解析错误结构化分类。
9. **预算不可扩大**：Agent 输出不能提高 hop、边数、调用次数、token、时间或金额上限。
10. **关系语义保守**：未知关系使用中性表述。
11. **重放幂等**：相同 episode/action/idempotency key 不得生成重复产物。
12. **多跳必须有序**：路径必须满足 `edge[i].tail == edge[i+1].head`，或显式记录反向遍历后的等价连续性。

## 4. 目标架构

```text
EntityGenerationService
  └─ GenerationOrchestrator (LangGraph)
      ├─ VerifySeed                 deterministic
      ├─ ObserveState              deterministic
      ├─ ExplorationPlanner        model/structured agent
      ├─ ActionPolicyGate          deterministic
      ├─ ToolExecutor              deterministic
      │   ├─ KG neighbors
      │   ├─ KG bounded expansion
      │   ├─ KG ordered paths (only after capability passes)
      │   └─ Literature retrieval (later step)
      ├─ EvidenceCurator           deterministic prefilter + semantic agent
      ├─ SufficiencyJudge          structured agent + deterministic guardrails
      ├─ ClaimBuilder              structured agent or deterministic fallback
      ├─ QuestionPlanner           structured agent
      ├─ QuestionWriter            structured agent
      ├─ AnswerVerifier            independent structured agent
      ├─ MachineValidators         deterministic
      └─ Artifact/Episode stores
```

必须保持“Agent 提议、代码执行和裁决”的边界。

## 5. ReAct 的工程定义

本项目中的 ReAct 不等于自由文本 scratchpad。每一轮只允许产生一个结构化 decision，且动作必须使用 Pydantic discriminated union；不能用一个充满可选字段的通用模型冒充动作类型：

```python
class ExplorationActionType(str, Enum):
    QUERY_NEIGHBORS = "query_neighbors"
    EXPAND_ENTITY = "expand_entity"
    QUERY_PATHS = "query_paths"
    RETRIEVE_LITERATURE = "retrieve_literature"
    FINISH = "finish"


class NeighborAction(ContractModel):
    action_type: Literal["query_neighbors"]
    subject_entity_id: str
    direction: Literal["incoming", "outgoing", "both"]
    relations: list[str] = Field(default_factory=list)
    target_types: list[str] = Field(default_factory=list)
    max_results: int = Field(ge=1, le=100)
    cursor: str | None = None


class ExpandAction(ContractModel):
    action_type: Literal["expand_entity"]
    subject_entity_id: str
    direction: Literal["incoming", "outgoing", "both"]
    relations: list[str] = Field(default_factory=list)
    target_types: list[str] = Field(default_factory=list)
    max_results: int = Field(ge=1, le=100)


class PathAction(ContractModel):
    action_type: Literal["query_paths"]
    start_entity_id: str
    target_entity_id: str | None = None
    max_hops: Literal[2] = 2
    relations: list[str] = Field(default_factory=list)
    target_types: list[str] = Field(default_factory=list)
    max_paths: int = Field(ge=1, le=50)


class LiteratureAction(ContractModel):
    action_type: Literal["retrieve_literature"]
    query: str
    entity_ids: list[str] = Field(min_length=1)
    max_results: int = Field(ge=1, le=50)


class FinishAction(ContractModel):
    action_type: Literal["finish"]
    stop_reason: StopReason


ExplorationAction = Annotated[
    NeighborAction | ExpandAction | PathAction | LiteratureAction | FinishAction,
    Field(discriminator="action_type"),
]


class ExplorationDecision(ContractModel):
    decision_id: str
    episode_id: str
    turn: int
    action: ExplorationAction
    expected_information: str
    evidence_gap: str
    cited_observation_ids: list[str] = []
```

约束：

- action-specific Schema 必须强制必填字段并禁止无关字段；
- `subject_entity_id` 必须来自种子或已观察实体集合；
- `target_entity_id` 必须来自已知实体，除非动作定义允许为空；
- relations/target types 必须经过 provider capability 和本地策略校验；
- `QUERY_PATHS` 只有 capability 真实可用时才能选择；
- `FINISH` 必须给出结构化 `stop_reason`；
- `expected_information` 与 `evidence_gap` 是简短审计说明，不得要求或保存隐藏思维链；
- 每轮最多一个动作；本轮不定义 composite action，多个查询必须分别计 turn、调用和预算。
- action hash 使用 canonical JSON，并包含 policy version、provider snapshot、cursor 和全部过滤条件。
- `GraphCapabilities` 必须新增明确的 `bounded_expansion`；它与尚不可用的 `path_query` 分开。

每次工具执行返回扩展后的 observation：

```python
class ToolObservation(ContractModel):
    observation_id: str
    episode_id: str
    decision_id: str
    tool_name: str
    action_type: ExplorationActionType
    status: Literal["success", "empty", "warning", "error"]
    result_artifact_ids: list[str]
    discovered_entity_ids: list[str]
    new_evidence_ids: list[str]
    rows_examined: int
    truncated: bool
    error_code: str | None
    retryable: bool
    elapsed_ms: int
```

## 6. 多跳证据契约

不能继续用“一组无序 KGEvidence”冒充路径。新增公开、版本化的路径契约：

```python
class EvidencePathStep(ContractModel):
    position: int = Field(ge=0)
    evidence_id: str
    traversal_direction: Literal["forward", "reverse"]
    from_entity_id: str
    relation: str
    to_entity_id: str
    provider_id: str
    snapshot_id: str
    query_trace_id: str


class EvidencePath(ContractModel):
    schema_version: str
    path_id: str
    path_version: str
    episode_id: str
    task_id: str
    bundle_id: str
    provider_id: str
    snapshot_id: str
    start_entity_id: str
    end_entity_id: str
    steps: list[EvidencePathStep] = Field(min_length=2, max_length=2)
    intermediate_entity_ids: list[str] = Field(min_length=1)
    source_evidence_ids: list[str] = Field(min_length=2)
    path_hash: str
    query_trace_ids: list[str] = Field(min_length=2)
```

必须验证：

- step position 从 0 连续递增；
- 每一步引用存在的 KGEvidence；
- 遍历后的 `to_entity_id` 与下一步 `from_entity_id` 完全一致；
- start/end 与首尾 step 一致；
- path hash 由 snapshot、方向化 step 和 evidence ID 稳定派生；
- 同一路径反向表示必须按统一 canonicalization 策略处理；
- 不能跨 provider/snapshot 拼接；
- 路径中的每条边均通过关系语义策略。
- `source_evidence_ids` 必须严格等于按 position 排序后的 `steps.evidence_id`；
- `intermediate_entity_ids` 必须从 steps 连续连接处派生，不能形成第二份真相；
- validator 必须从 evidence registry 重新验证真实 head/tail/relation，不能只相信 path 自报字段；
- `EvidenceBundle` 增加 versioned `paths` 集合；`EvidenceRequirements` 增加 `required_path_ids`；repository、Schema、export 和 UI 都必须保留路径 provenance。

实现上优先采用“有界逐层 neighbors 扩展 + 项目侧组装有序路径”。只有 PrimeKG `paths()` 能通过同等正确性测试后，才可直接使用 provider path capability。

## 7. 多跳 claim 与问题蓝图

扩展 `QuestionBlueprint`，同时保持旧单跳 JSON 可读取：

```python
class QuestionBlueprint(ContractModel):
    # existing fields remain backward compatible
    question_type: Literal[
        "single_fact",
        "constrained_list",
        "two_hop_relation",
        "mechanism_clue",
        "evidence_verification",
    ] = "single_fact"
    hop_complexity: int = Field(default=1, ge=1, le=2)
    required_path_ids: list[str] = Field(default_factory=list)
    intermediate_entity_ids: list[str] = Field(default_factory=list)
    leakage_constraints: list[str] = Field(default_factory=list)
```

二跳问题最低要求：

- `hop_complexity == 2`；
- 至少一个 `required_path_id`；
- 至少两条不同 evidence；
- 答案是路径终点或由路径支持的受约束集合；
- 问题文本不能直接泄露答案；
- 默认不得泄露中间实体，否则问题退化为一跳拼接；
- 必须执行 shortcut 检测：若种子与答案之间存在可直接回答同一谓词的一跳证据，标记 `multi_hop_shortcut`；
- claim 必须逐 hop 引用证据，不能只引用最终边。

此外新增 `RelationCompositionPolicy`。它按下面的组合签名显式返回 `allow/neutralize/deny`：

```text
(relation_1, direction_1, middle_entity_type,
 relation_2, direction_2, answer_entity_type)
```

- 路径连续不代表医学推理可组合；未知组合只能生成中性的“图谱中通过某类中间实体连接”问题。
- 未经显式 allow 的组合禁止生成 mechanism、causal、therapeutic 表述。
- 两条 association 边不能推出机制、治疗或因果结论。
- 每个组合 claim 使用独立 `DerivedClaim`（或等价 derivation 结构），保存 `subclaim_ids`、`required_path_ids`、`composition_policy_version` 和结论语义；不能硬塞进普通单三元组 Claim。

Blueprint 还必须保存可执行的 `logical_query`：起点、按顺序的 relation/direction/type constraints、答案 cardinality 和 aggregation。多跳必需性按该语义签名判断，而不是按文本关键词判断：

- 执行 leave-one-hop-out ablation；移除任一 hop 后，答案集合不得仍唯一包含候选答案；
- 在固定 snapshot 与允许证据空间内重新求解答案集合；
- 多个合法终点时必须生成 list/ambiguous 问题或拒绝，不能任取一条路径当唯一答案；
- shortcut 检测比较等价 logical query 的一跳可回答性。

## 8. 探索与停止策略

### 8.1 Planner 可见状态

只提供必要的、经过压缩的结构化状态：

- seed identity；
- 已知实体摘要；
- 已执行 action hashes；
- observation 摘要；
- 已选 evidence/path；
- 当前证据缺口；
- 已生成问题类型与分布；
- 剩余预算；
- provider capabilities。

不得把数据库连接、文件路径、原始 SQL 或整个高连接度邻域塞给模型。

### 8.2 强制停止条件

扩展 `StopReason`：

- `target_reached`
- `evidence_sufficient`
- `evidence_exhausted`
- `no_new_evidence`
- `no_valid_action`
- `budget_exhausted`
- `duplicate_action`
- `all_candidates_hard_rejected`
- `provider_error`
- `model_error`
- `human_input_required`
- `fatal_error`

确定性门禁优先于 Agent 建议：

- 达到目标题数立即停止；
- budget 任一维度耗尽立即停止；
- 同一 action hash 不得重复执行；
- 连续两轮没有新增有效 evidence/path 时停止；
- 发现高连接度扩张且无过滤条件时拒绝动作；
- 模型输出不合法最多修复一次，仍不合法则 `model_error`；
- Agent 请求未声明 capability 时拒绝，并把结构化错误作为下一轮 observation；
- 不允许无限 revision 或 retry。

停止原因必须定义优先级和 EpisodeState 映射。`duplicate_action` 首先是可恢复 policy observation，只有达到重复阈值才转成 terminal `no_valid_action`。建议优先级：安全/身份致命错误 > 预算耗尽 > target reached > evidence sufficient > no new evidence > provider/model failure > partial exhaustion。`target_reached` 的计数口径固定为 **accepted 且 machine-validated candidate**；人工确认不阻塞 generation episode。

模型错误处理必须有矩阵：auth/config 直接 fail fast；timeout/rate limit 有界重试；Schema 只修复一次；context overflow 先执行确定性压缩一次；refusal 记录并停止或转人工；重复非法动作达到阈值后 `no_valid_action`。所有尝试都计入 token、time、cost 和 model-call ledger。

所有图谱名称、relation 和文献片段都按不可信数据处理：限制长度与字符、使用明确 data delimiter、忽略其中指令文本；工具参数由 canonical registry 重建，禁止把模型回显的外部文本直接作为 SQL、路径或未校验查询参数。

## 9. 模型端口与装配

现有 `LanguageModelPort` 可以保留，但必须扩展角色：

```python
ModelRole = Literal[
    "exploration_planner",
    "evidence_curator",
    "sufficiency_judge",
    "claim_builder",
    "question_planner",
    "question_writer",
    "answer_verifier",
]
```

装配要求：

- `rule-based`：仅用于离线回归与单跳兼容；CLI 必须明确标注“不具备 ReAct”。
- `fake`：测试专用，按预设响应驱动多轮 action/observation；不能与 rule-based 共用同一行为。
- `structured`：真实模型模式，使用配置注入的 provider adapter；缺少模型配置时 fail fast。
- Planner、Writer、Verifier 使用独立 role、prompt version 和上下文。
- 保存 invocation ID、model config ID、prompt version、token usage、elapsed time 和解析错误。
- 密钥只从环境或外部 secret provider 获取，不写入产物、日志或仓库。
- 真实模型 adapter 位于 `src/medical_qa_data_factory/llm/providers/`；core 不 import 具体厂商 SDK。

## 10. LangGraph 目标状态机

```text
verify_seed
  → observe
  → plan_action
  → validate_action
      ├─ invalid but recoverable → record_observation → observe
      ├─ fatal                  → fail
      └─ valid                  → execute_action
  → persist_observation
  → curate_evidence
  → judge_sufficiency
      ├─ continue → observe
      ├─ finish_without_candidate → finalize_partial
      └─ generate → build_claims
  → plan_questions
  → write_questions
  → persist_drafts
  → verify_answers
      ├─ one bounded rewrite              → write_questions
      ├─ hard reject/ambiguous/shortcut   → record_generation_observation
      └─ accepted                         → machine_validate
  → assess_generation_progress
      ├─ accepted machine-validated count reached → finalize_artifact_links
      ├─ budget/evidence exhausted               → finalize_partial
      └─ question deficit with actionable gap    → observe
  → finalize_artifact_links → finalize
```

每次循环必须递增 `turn` 和 step sequence。State 至少新增：

- `turn`
- `known_entities`
- `decision_ids`
- `executed_action_hashes`
- `observation_ids`
- `frontier_entity_ids`
- `evidence_path_ids`
- `no_new_evidence_turns`
- `sufficiency_decisions`
- `remaining_budget_snapshot`
- `generation_feedback_observation_ids`
- `accepted_machine_validated_count`

Graph state 仍只保存 JSON/Pydantic 可序列化数据。

## 11. 分步施工计划

### Step 0：恢复可复现基线并修复已知错误

**上下文**：当前环境甚至不能完整导入 LangGraph；incoming-edge 问题会被错误拒绝。任何 Agent 改造都必须建立在可信基线上。

**负责文件**：

- `requirements.txt`
- `pyproject.toml`
- `src/medical_qa_data_factory/generation/verification.py`
- `src/medical_qa_data_factory/generation/graph.py`
- 对应测试

**任务**：

1. 补齐并锁定运行所需直接依赖；确保新建 `qa_env` 后 `pip check` 通过。
2. 修复 `answer_is_seed`：Verifier 显式接收 focus/seed ID。
3. 增加 incoming、outgoing 两种方向的正反测试。
4. 让 `fake` 与 `rule-based` 装配行为明确分离，或暂时从用户 CLI 移除误导模式。
5. 更新测试基线数量，不把 `__pycache__` 当项目产物。
6. `pyproject.toml` 声明直接依赖，并生成项目选择的 constraints/lock artifact；不能只修补当前机器的 `qa_env`。

**验证**：

```powershell
$env:PYTHONNOUSERSITE='1'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
python -m pip check
python -m pytest tests -q
```

还必须在干净环境执行 editable install 后重复 `pip check` 与测试。

**退出标准**：默认非真实库测试全绿；Aspirin incoming/outgoing 候选不因端点位置被误拒绝。

**回滚**：仅回滚本步涉及的依赖与 verifier 修改；不得删除既有校验。

### Step 1：定义 ReAct、路径和充分性契约

**上下文**：先稳定数据模型，再改图。旧 JSON 必须可读取。

**负责文件**：

- `generation/contracts.py`
- `knowledge_graph/contracts.py`
- `llm/contracts.py`
- `generation/state.py`
- `domain/evidence.py`
- `domain/questions.py`
- `repository.py`
- `generation/repository.py`
- `export.py`
- `schema_export.py`
- contract/schema 测试

**任务**：

1. 增加 `ExplorationDecision`、action enum、扩展 observation。
2. 增加 `EvidencePath`/`EvidencePathStep`。
3. 增加 `EvidenceSufficiencyDecision`，至少包含 verdict、缺口、引用 evidence/path、建议动作和 stop reason。
4. 扩展 Blueprint，但保持旧字段默认值和读取兼容。
5. 扩展模型 role。
6. 导出新 JSON Schema，并增加兼容性 fixture。
7. 冻结 record placement：decision、observation、path、execution journal、model invocation、usage 各自的 collection、ID/version 规则与 repository API。
8. 建立“schema → collection → writer → reader → validator → export → UI consumer”迁移矩阵；后续 Step 只能按矩阵实现，不能各自另造存储位置。
9. 兼容默认值必须显式：`question_type=single_fact`、`hop_complexity=1`、list 使用 `default_factory`；旧真实 fixture 必须可读取，而不只是新模型 round-trip。
10. `expected_route` 为后续 `kg/literature/hybrid` 留出兼容枚举，不能继续只允许 `kg`。

**验证**：contract round-trip、非法 action、路径断裂、跨 snapshot、预算越权、旧 QuestionSpec 兼容测试。

**退出标准**：所有控制信号均可结构化校验；core contract 不 import LangGraph 或具体 provider。

**回滚**：新字段均保持可选/有默认值；必要时可停用新 graph 而不破坏旧数据。

### Step 2：实现可信的有序二跳路径层

**上下文**：当前 `paths()` 不满足生成正确性。先在项目侧以有界邻居扩展组装路径。

**负责文件**：

- `knowledge_graph/collector.py`
- `knowledge_graph/contracts.py`
- `knowledge_graph/providers/primekg.py`
- 新增 `knowledge_graph/path_builder.py`
- 对应单元和真实库集成测试

**任务**：

1. 从已验证种子执行受限第一跳。
2. 本步只实现纯函数路径组装和有界 executor primitive；使用测试策略提供中间实体，Planner 选择在 Step 4 集成。
3. 第二跳查询必须带关系/目标类型/数量限制。
4. 生成有序方向化路径并稳定去重。
5. 保留每条底层 KGEvidence、query trace 和 snapshot。
6. 做高连接度保护和 action hash 去重。
7. 在实现通过完整测试前保持 provider `path_query=false`；项目侧 bounded expansion 使用独立 capability。

**验证**：

- 人工构造图 fixture 覆盖 forward-forward、forward-reverse、重复边、环、自环、断链；
- 路径顺序和 canonical hash 稳定；
- PrimeKG 真实只读 smoke；
- 查询前后数据库 size/mtime/hash 不变。

**退出标准**：任意输出路径都能逐 step 重放并验证连续性。

### Step 3：实现 Planner、Policy Gate 与 Tool Executor

**上下文**：这是第一个真正的 Agent 决策步骤；模型只提议动作，不能直接调用工具。

**负责文件**：

- 新增 `generation/exploration.py`
- 新增 `generation/actions.py`
- `llm/ports.py`
- `llm/fake.py`
- 新增真实 provider adapter 与配置加载
- `generation/repository.py`
- 对应测试

**任务**：

1. Planner 输入压缩后的 observation/state，输出 `ExplorationDecision`。
2. Fake model 支持预设多轮响应，测试可精确控制动作序列。
3. Policy Gate 校验已知实体、工具白名单、capability、关系、类型和预算。
4. Executor 成为唯一工具调用入口。
5. 保存 decision、invocation、observation、usage 和 action hash。
6. 非法动作作为结构化 observation 返回 Planner；安全违规直接失败。
7. 增加真实 `structured` 模式并 fail fast 检查配置。

**验证**：Planner 不能注入 SQL/路径/任意工具；重复动作被阻止；预算无法由模型扩大；fake 两轮探索可重放。增加反事实测试：对相同 turn-1 state 分别注入 empty、A→B、A→D、truncated、policy rejection，Planner 必须产生不同且有效的下一动作或停止；`cited_observation_ids` 必须真实存在、属于当前 episode。

**退出标准**：测试中可观察到至少两轮不同 action，第二轮明确依赖第一轮 observation；fake 只能证明编排，声称真实 Agent 还必须有一次 configured structured-model smoke。

### Step 4：把固定 DAG 改造成有界 ReAct 循环

**上下文**：保留旧单跳兼容入口，但新的 `agentic`/`structured` 模式必须进入循环。

**负责文件**：

- `generation/graph.py`
- `generation/routing.py`
- `generation/service.py`
- `generation/budgets.py`
- `generation/state.py`
- `generation/contracts.py`
- `cli.py`
- replay/graph/CLI 测试

**任务**：

1. 加入 observe、plan、validate、execute、persist、sufficiency 节点。
2. 将 tool call、edge、path、model token、step、wall time统一纳入预算。
3. 实现 `no_new_evidence`、`duplicate_action`、`no_valid_action`、`model_error`。
4. 连续两轮无新证据停止。
5. checkpoint 后恢复必须从最后安全边界继续；已持久化 completed action 不重复调用。
6. artifact 仍先于 terminal AgentStep/checkpoint 持久化。
7. 被拒绝、歧义或存在 shortcut 的生成结果必须成为新的 observation；题数不足且仍有预算时回到探索，而不是直接 partial。
8. CLI 输出每轮摘要和最终 stop reason，但不输出隐藏思维链。

**恢复语义**：外部调用采用 at-least-once，不得笼统宣称 exactly-once。新增 action execution journal：`planned → reserved → running → completed | failed | unknown`，使用稳定 idempotency key。只有 provider 明确支持幂等时才可宣称 exactly-once；调用已发生但结果未持久化便崩溃时标记 `unknown`，根据工具性质安全重试或转 `human_input_required`。预算使用持久化累计 ledger；wall time 保存绝对 started_at；金额使用 amount + currency，不能只依赖进程内 `time.monotonic()`。

**验证**：目标达成、证据不足继续、预算耗尽、模型错误、provider 暂态错误、崩溃恢复、重复 action 全部有测试。

**退出标准**：运行轨迹中存在真正的 decision → action → observation → next decision 因果链。

### Step 5：实现多跳 claim、规划、写作与 shortcut 检测

**上下文**：探索得到路径后，不能继续用单边模板冒充多跳题。

**负责文件**：

- `generation/claims.py`
- `generation/planning.py`
- `generation/writing.py`
- `generation/semantics.py`
- 新增 `generation/shortcuts.py`
- domain/question contracts 和测试

**任务**：

1. Claim Builder 逐 hop 构造结构化子 claim，并可构造受约束组合 claim。
2. Planner 根据路径生成 `two_hop_relation` blueprint。
3. Writer 隐藏答案，默认隐藏中间实体。
4. 检测一跳捷径、答案泄漏、路径拼接错误和语义升级。
5. 生成多个自然表达时保持同一 `logical_question_id/split_group_id`。
6. 多跳题的 `required_evidence_ids` 与 `required_path_ids` 完整。

**验证**：至少覆盖 drug→protein→disease、disease→protein→pathway 等 fixture；断裂路径不能生成问题；存在一跳捷径时拒绝或降级成单跳。

**退出标准**：系统能从两条连续 PrimeKG 边生成一题可由完整路径回答、不能仅靠单条必需证据回答的问题。

### Step 6：实现独立 Answer Verifier 与真实 revision

**上下文**：当前 verifier 主要做引用和端点检查，revision 路由也没有消费反馈。

**负责文件**：

- `generation/verification.py`
- `generation/writing.py`
- `generation/graph.py`
- `llm/` roles/prompts/providers
- machine validation 扩展及测试

**任务**：

1. Verifier 第一阶段只接收问题和允许证据/路径，不接收 Writer 自由理由，也不接收候选答案。
2. Verifier answer-blind 地计算答案集合和推导引用；第二阶段由确定性代码比较 canonical IDs、aliases 和 cardinality。
3. 输出结构化 entailment、路径完整性、shortcut、leakage 和 ambiguity 结果。
4. `needs_revision` 必须携带结构化修改约束；Writer 消费约束后最多改写一次。
5. 改写后重新执行 verifier，不得自动放行。
6. 规则 verifier 继续作为硬门禁；模型 verdict 不能覆盖引用不存在等确定性错误。

**验证**：错误答案、部分路径答案、泄漏答案、歧义答案、改写成功和改写失败。

**退出标准**：Verifier 能在不知道 Writer 生成理由的情况下复算二跳答案并拒绝不一致候选。

### Step 7：接入文献动作与多源充分性判断

**依赖**：Step 3、4、6 完成后实施；不阻塞 PrimeKG-only ReAct 的首次验收。

**负责文件**：

- `ports.py`/literature contracts
- 新增 literature provider adapter
- `generation/actions.py`
- `generation/exploration.py`
- evidence bundle、ablation 和测试

**任务**：

1. 实现真实 Literature provider，保存 PMID/DOI/title/snippet/location/license/index version。
2. 增加 `RETRIEVE_LITERATURE` 白名单动作。
3. Planner 只有在 KG 证据不足或需要文献验证时选择文献动作。
4. 区分 empty、timeout、auth、parse、provider unavailable。
5. 记录 KG-only、Literature-only、Combined 的充分性结果。

**退出标准**：至少一个问题具有可复算的 hybrid route，且工具失败不会被解释为无证据。

### Step 8：UI、报告、数据划分与文档收口

**负责文件**：

- `web/`
- `export.py`
- `machine_validation.py`
- `README.md`
- `UNFINISHED_CHECKLIST.md`
- 新增运行手册和 E2E 测试

**任务**：

1. UI 展示每轮 decision/action/observation、剩余预算和停止原因。
2. 可视化有序路径，标出中间实体、方向和来源。
3. 明确标识运行模式：rule-based、fake ReAct、structured ReAct。
4. 增加路径完整性、multi-hop shortcut、planner action validity 校验。
5. split 以 logical question/path family 为原子，防止变体和同路径跨集合泄漏。
6. 更新 README 与 checklist，删除“已有接口即已有 Agent 能力”的误导表述。
7. 跑 5 个种子、约 20 题 MVP 并输出质量/失败报告。

**退出标准**：用户能从 UI 看懂“Agent 为什么执行该动作、发现了什么、为何停止”，且正式导出包含路径、模型、工具、校验和版本溯源。

## 12. 依赖图与并行性

```text
Step 0 baseline
   ↓
Step 1 contracts
   ├───────────────┐
   ↓               ↓
Step 2 paths     Step 3 planner/executor
   └───────┬───────┘
           ↓
        Step 4 ReAct graph
           ↓
        Step 5 multi-hop generation
           ↓
        Step 6 independent verification
           ├──────────────┐
           ↓              ↓
        Step 7 literature Step 8 UI/docs (部分可先行)
           └───────┬──────┘
                   ↓
                MVP audit
```

可并行：

- Step 2 与 Step 3 在 Step 1 后可并行，但不得同时修改同一 contract 文件；先冻结 Step 1 Schema。
- Step 8 的静态 UI 设计可与 Step 5/6 并行，真实数据接线必须等 contracts 稳定。
- Literature provider 可与多跳 writer 开发并行，但混合路由集成依赖 Step 4/6。

## 13. 测试与验收矩阵

### 13.1 固定预算与查询上限

实现前必须把默认值写进 versioned policy/config，并允许 request 只向下收紧：

- per-action fanout；
- frontier entity count；
- rows examined；
- paths emitted；
- per-relation cap；
- cursor/page cap；
- total edge/path/tool/model-call/token/time/cost budget。

所有查询先稳定排序再截断。`truncated=true` 必须进入 observation，并能影响下一轮 decision。逐中间实体二跳查询必须受 frontier 和 N+1 调用预算共同约束。

### 13.2 可重复 MVP manifest

5-seed MVP 不能临时挑选。新增固定 manifest，至少记录：

- provider/native ID/entity type/canonical name；
- PrimeKG snapshot hash；
- policy/prompt/model config version；
- temperature/random seed；
- 每种目标问题类型和期望 route。

质量阈值至少包括：accepted count、2-hop count、broken path=0、shortcut=0、unsupported semantic escalation=0、ambiguous single-answer=0、resume duplicate=0，并报告 rejection rate、工具空结果率和成本分布。

最低测试层次：

1. **Contracts**：Schema、兼容性、非法结构。
2. **Policy**：动作白名单、预算、capability、重复动作。
3. **Graph fixtures**：有序路径、环、反向边、断链、shortcut。
4. **Planner fake**：预设两到三轮 ReAct 行为。
5. **Graph orchestration**：所有路由和停止原因。
6. **Replay**：action 完成后 checkpoint 前崩溃；恢复无重复调用与产物。
7. **PrimeKG integration**：只读实体、边、二跳 smoke。
8. **Model integration**：结构化输出成功、Schema 修复一次、持续失败。
9. **Generation semantics**：不把 association 写成 causal/therapeutic。
10. **Verifier**：独立答案复算、路径完整性、泄漏、歧义。
11. **Web/E2E**：轨迹、路径、审核和导出。
12. **MVP audit**：5 seeds / ~20 questions / quality report。

ReAct 核心验收场景：

```text
Given: seed A and no evidence
Turn 1: planner selects bounded neighbor query
Observe: A → B evidence
Turn 2: planner identifies missing endpoint and expands B
Observe: B → C evidence
Sufficiency: ordered A → B → C path is sufficient
Generate: a two-hop blueprint whose answer is C
Verify: independent verifier recomputes C from both steps
Stop: target_reached or evidence_sufficient
```

还必须执行 Planner 反事实组：保持 seed、预算和 turn-1 state 相同，只替换 observation 为 empty、A→B、A→D、truncated、policy rejection；下一动作或停止决策必须随有效信息变化，且每个 decision 引用真实 `cited_observation_ids`。这用于防止“固定 query→expand 脚本”冒充 ReAct。

失败场景必须覆盖：

- Planner 重复相同动作；
- Planner 请求未知实体；
- Planner 请求未开放工具；
- 高连接度节点无过滤扩张；
- 第一跳成功、第二跳为空；
- 两条边不连续；
- 路径跨 snapshot；
- 一跳捷径存在；
- 模型输出非法 JSON/Schema；
- provider timeout；
- checkpoint 恢复；
- token/tool/edge/time 任一预算耗尽。

## 14. 反模式清单

出现以下任一项，视为没有完成本蓝图：

- 把固定 LangGraph DAG 重新命名为 ReAct；
- 用 `AgentStep` 日志数量证明存在 Agent 决策；
- Planner 永远按硬编码顺序查询 outgoing、incoming、2-hop；
- 将模型自由文本解析成工具名或 SQL；
- 保存模型隐藏思维链；
- 使用无序 edge list 作为多跳路径；
- 仅检查“有两条边”而不检查连续性；
- Writer 自己决定答案正确；
- verifier 使用 Writer 的自由理由作为事实来源；
- 在模型不可用时静默使用 rule-based，却仍标记 `structured/react`；
- 通过放宽机器校验或导出门禁让新问题通过；
- 多跳题可以只靠一条 required evidence 回答；
- 修改原始 PrimeKG 数据库；
- 将 API key 写入配置、日志或产物；
- 忽略当前 incoming-edge verifier bug；
- 为了测试通过跳过真实 PrimeKG 身份类型隔离。

## 15. 运行、发布与回滚策略

当前项目无 Git 仓库，实施前强烈建议先初始化受控版本管理或创建完整目录快照。若仍使用 direct mode：

- 每个 Step 完成后保存测试输出和变更文件清单；
- 不跨 Step 混合无关重构；
- 新能力使用显式 mode/feature flag；
- 保留旧 `rule-based` 单跳路径作为回归基线，直到 structured ReAct 验收通过；
- Schema 只做向后兼容扩展；
- provider capability 默认关闭，只有测试通过后启用；
- 真实模型和文献 provider 可单独关闭；
- 任何 Step 失败时，回退该 Step 的装配入口，而不是删除已持久化数据。

## 16. 计划变更协议

新的编码模型如发现本蓝图假设不成立，不得静默改变目标。必须：

1. 在本文件末尾追加 `Plan Mutation`；
2. 写明发现的证据、受影响 Step、替代方案和风险；
3. 保持“不变量”和 Definition of Done 不变，除非用户明确批准；
4. 若拆分 Step，保留原 Step ID，例如 `Step 3A/3B`；
5. 若跳过 Step，说明其验收标准由哪个后续 Step 覆盖；
6. 不得以“框架复杂”为由把 ReAct 重新降级成固定 DAG。

模板：

```text
### Plan Mutation YYYY-MM-DD / <id>
- Evidence:
- Affected steps:
- Proposed change:
- Preserved invariants:
- New risks:
- User approval required: yes/no
```

## 17. Definition of Done

只有同时满足以下条件，才可以声称“Agent 核心能力已实现”：

- [ ] 受控 fake episode 执行两轮以上 decision/action/observation 循环，且 Planner 反事实组通过；
- [ ] 至少一次 configured structured-model smoke 成功；只有 fake 通过时不得宣称真实 Agent 已实现；
- [ ] 第二轮动作依赖第一轮 observation，而不是预设固定顺序；
- [ ] Planner 只能选择白名单动作，Policy Gate 会拒绝越权动作；
- [ ] 生成并持久化至少一条可逐 hop 验证的有序二跳路径；
- [ ] 至少一题真实使用两跳证据，且不存在可回答同一问题的一跳捷径；
- [ ] Writer 与 Verifier 分离，Verifier 独立复算答案；
- [ ] 生成被拒绝、歧义或 shortcut 导致题数不足时，反馈进入 observation 并在预算内重新探索；
- [ ] 模型不可用时不会静默伪装成 ReAct；
- [ ] 每轮 decision、action、observation、usage、budget 和 stop reason 可审计；
- [ ] checkpoint 恢复不会重复工具调用或产物；
- [ ] 恢复语义按 execution journal 验证：completed 不重复，unknown outcome 不伪装成 exactly-once；
- [ ] PrimeKG 保持只读，所有真实集成测试通过；
- [ ] 默认测试、Schema 兼容测试、ReAct 测试和 E2E 测试通过；
- [ ] 5 个种子、约 20 题 MVP 中包含单跳和可信多跳问题；
- [ ] README、未完成清单与实际能力一致。

## 18. 给执行模型的建议起始顺序

第一次会话只做 Step 0，不要同时重写 graph。完成并报告：

1. 基线测试命令与结果；
2. 环境缺失依赖；
3. `answer_is_seed` 修复及回归测试；
4. 受影响文件清单；
5. 下一步 Step 1 的 contract 设计提案。

之后每次会话只执行一个 Step 或一个明确的子 Step。任何时候都优先保证证据正确性、路径连续性和可审计性，而不是追求生成题数。

---

### Plan Mutation 2026-10-18 / step-0-and-1-executed
- Evidence:
  - 蓝图声称「当前环境甚至不能完整导入 LangGraph」，实测属实但根因不是 `requests`/`tenacity` 缺失：
    现有可用的 Conda 环境 `qa_env`（Python 3.12.15）已装齐 `langgraph 1.2.16`、
    `langgraph-checkpoint-sqlite 3.1.2`、`requests 2.31.0`、`tenacity 8.5.0`；
    失败的是 `C:\Python312`（无 langgraph）。断言的 `pip check` 问题来自环境内其他无关包
    （akshare、onnxruntime 等），本项目直接依赖链无冲突。
  - 测试基线不是 83 项：`tests` 默认 256 项通过、真实 PrimeKG 只读 8 项通过。
- Affected steps: Step 0（各项）、Step 1（契约部分）
- Proposed change:
  - Step 0 已全部完成并报告；Step 1 的契约部分一并完成（动作 union、`EvidencePath`/`EvidencePathStep`、
    `EvidenceSufficiencyDecision`、多跳 `QuestionBlueprint`、7 个 `ModelRole`、`bounded_expansion` capability、
    `SchemaVersion` 兼容默认值、JSON Schema 重新导出且无 drift）。
  - 额外修复一个蓝图未提及的既有缺陷：`LocalArtifactRepository.load_latest` 仅按 `st_mtime_ns` 取最新，
    同一文件系统 tick 内写入两个版本时会随机返回旧版本（`test_episode_versions_accumulate` 约 1/6 概率失败）。
    已改为 `(st_mtime_ns, stem)` 确定性 tiebreak。此修复属于「先修复基线」范围，未放宽任何校验。
  - 未实施项：Step 1 记录的「migration matrix / record placement 冻结文档」尚未落成独立文档，
    已由 `generation/repository.py` 现有 collection 常量与新增测试的行为约束表达；Step 2 起需补正式矩阵文档。
- Preserved invariants: 未重写 LangGraph DAG；未放宽机器校验；未把 association 升级为 causal/therapeutic；
  未保存隐藏思维链；PrimeKG 只读（size/mtime/SHA256 均未变）。
- New risks: 当前 `qa_env` 是唯一可用环境，`constraints.txt` 由该环境冻结；若改用其他环境需重新生成。
  Editable install 因用户 site-packages 权限失败（预存权限问题，非本改动引入），已用 `PYTHONPATH` 验证导入。
- User approval required: no

### Plan Mutation 2026-10-18 / step-2-executed
- Evidence:
  - 蓝图假设 `drug_protein` 是药物侧出边，实测 PrimeKG 快照中 `drug_protein` 与
    `disease_protein` 都指向蛋白侧（`primekg:gene/protein:*`），药物/疾病节点本身不持有
    指向蛋白的出边。因此真实二跳方向是 `disease --disease_protein--> protein
    <--drug_protein-- drug`，第二跳为 reverse 遍历。
  - `exposure:D001241`（Aspirin）在该快照中仅 2 条边，无法构造二跳；集成测试改用
    `disease:8315`（prostate cancer）。
  - 蛋白实体类型字符串是 `gene/protein`，不是 `protein`；用 `protein` 过滤会得到 0 条边。
- Affected steps: Step 2（集成测试的种子与过滤参数）
- Proposed change:
  - `OrderedPath`/`OrderedPathStep` 放在 KG 层（`knowledge_graph/contracts.py`），
    以 edge id 表达；Step 1 的 `EvidencePath` 留在 generation 层并负责绑定
    `KGEvidence` 与 episode。两层分开是因为 `knowledge_graph` 不能反向 import
    `generation`（会成环）。
  - `bounded_intermediates` 同时受 `max_intermediates` 与 `per_relation_cap` 约束；
    两者都会先生效，已在测试中分别覆盖（前者测总量，后者测单关系扇出）。
- Preserved invariants: provider `path_query` 仍为 `false`，未启用；PrimeKG 只读
  （size/mtime/SHA256 均未变）；路径连续性由真实 edge 端点校验，不相信调用方断言；
  断链、自环、重复边、回到起点一律拒绝。
- New risks: `OrderedPath` 目前只被测试消费，尚未接入 generation 流程；
  Step 4 需要把它转成 `EvidencePath` 并绑定 evidence registry。
- User approval required: no

# 生物医药问题生成与 Query Planner 训练数据架构及开发标准

> 状态：目标架构与重构依据  
> 受众：`medical-qa-data-factory` 与 `query-planner` 的开发、测试和数据工程人员  
> 更新日期：2026-10-25  
> 适用范围：问题生成、任务验证、Planner rollout、训练样本导出

本文是上述适用范围内的目标架构依据。与 `medical-qa-data-factory/entity-question-generation-implementation-requirements.md` 中“出题探索轨迹可直接作为 Query Planner 训练轨迹”的描述冲突时，以本文为准；原文中的产品范围、运行预算和数据治理要求在未冲突处继续有效。

`medical-qa-data-factory` 的项目内实施文档见 [项目文档索引](medical-qa-data-factory/README.md)。

## 1. 结论

系统应拆成两个相互连接但职责独立的闭环：

1. **任务生成闭环**负责发现、定义和验证一道值得训练的问题；
2. **Planner 求解闭环**负责让 `query-planner` 在不知道答案和 gold evidence 的前提下求解该问题，并记录真实状态、动作、观察和停止标签。

两个闭环之间通过版本化的 `ValidatedTask` 和 `QuestionSpec` 交接。问题的规范语义由强类型 `LogicalQuery` 表示；自然语言题面只是该任务的一种表达，不是 gold 语义的唯一来源。

本架构的首要开发原则是：

> **有限证据负责发现候选，完整冻结世界负责定义问题是否成立。**

候选生成器可以只读取一次探索得到的局部 evidence slice，但答案集合、基数、歧义、shortcut 和 insufficient 必须在 Planner 可访问的完整 `ValidationWorldSnapshot` 上重新计算。局部 slice 中只有一个答案，不得作为单答案题成立的依据。

目标数据流如下：

```text
Validation World Snapshot
    -> Evidence Discovery and Sampling
    -> Generation Evidence Slice
    -> Candidate Sources
    -> Query Compiler
    -> Task Validator
    -> ValidatedTask
    -> Question Realizer
    -> Text Semantic Verifier
    -> QuestionSpec
    -> Planner Rollout Runner
    -> PlannerRollout
    -> Training Dataset Assembler
```

这项重构的主要目的不是增加 Agent 数量，而是保证以下标签能够客观成立：

- 当前状态应当 `continue`、`complete` 还是 `insufficient`；
- 继续检索时，哪个动作具有更高的证据收益和更合理的成本；
- 一道题的答案、必要证据、逻辑路径和不可回答性可以在固定数据快照上复算。

## 2. 当前实现与目标之间的关键差距

### 2.1 当前主链路

当前 LangGraph 主链路大致为：

```text
verify_seed
    -> collect_neighbors | agent_explore
    -> curate_evidence
    -> build_claims
    -> plan_questions
    -> write_questions
    -> persist_draft_questions
    -> verify_questions
    -> machine_validate
    -> finalize
```

这条链路已经具备以下可复用能力：

- provider-neutral 的 KG、文献和结构化模型端口；
- 预算控制、checkpoint、resume 和不可变 artifact 存储；
- evidence、claim、question、verification 和训练导出契约；
- ReAct evidence exploration；
- 强类型 `LogicalQuery`、snapshot evaluator、shortcut check 和 blind verification 等基础模块。

### 2.2 当前问题规划仍以单条边为中心

`QuestionPlanner` 直接遍历 curated edge，每条边生成一个 blueprint。`EnrichedEntityQuestionPlanner` 对文献增强 claim 分组，但生产的仍然是 `single_fact`，并主要引用一条 KG evidence。

因此当前所谓 Agent 模式的实际分工是：

```text
Agent 寻找证据
    -> 确定性 Planner 从单条边选择题目
    -> LLM Writer 改写题面
```

它不是“Agent 提出任务语义”。`QuestionBlueprint` 虽然已经包含 `hop_complexity`、`required_path_ids`、`intermediate_entity_ids` 和 `logical_query`，但主生产路径没有稳定地产生和验证真正的组合查询。

### 2.3 主链路没有把逻辑验证作为强制门禁

项目已有 `LogicalQuery`、`SnapshotQueryEvaluator`、shortcut check、answerability probe 和 blind verification，但主图中的 `plan_questions -> write_questions -> machine_validate` 没有把它们组成统一的任务验收阶段。

当前 `question_spec_from_draft` 还通过 `len(draft.evidence_ids)` 推断 hop count。多个不连续 evidence 可能被误判为多跳题；有序路径、方向、过滤条件和基数约束也可能在持久化时丢失。

### 2.4 当前 PlannerTrajectory 记录的是出题探索，不是下游求解

`medical-qa-data-factory` 当前的 `PlannerTrajectory` 记录从种子实体出发、为了获取出题材料而执行的探索动作。

`query-planner` 的运行时则从问题文本和空 EvidenceStore 开始，循环执行：

```text
parse question
    -> build observable state
    -> decide stop state
    -> enumerate legal actions
    -> select action
    -> execute
    -> charge cost
    -> update evidence
```

两种轨迹的初始信息、目标和动作含义不同，不能作为同一种训练数据。后续契约必须明确区分：

- `GenerationTrace`：系统如何找到材料并构造题目；
- `PlannerRollout`：下游 Planner 如何在题目给定后求解。

## 3. 架构边界

### 3.1 任务生成闭环

任务生成闭环负责回答：

- 什么问题值得生成；
- 问题的精确定义是什么；
- 固定快照上的答案是什么；
- 是否存在歧义、捷径或不可执行条件；
- 怎样把同一任务表达成自然语言。

它不负责为下游模型伪造最优求解轨迹。

### 3.2 Planner 求解闭环

Planner 求解闭环负责回答：

- 面对该问题时，当前可观察状态是什么；
- 下一步有哪些合法动作；
- 应该继续、完成还是判定证据不足；
- 每个动作产生了哪些新证据并消耗了多少资源。

它不能访问：

- gold answer；
- required evidence ID；
- 生成器的隐藏上下文；
- 候选任务生成时使用的目标路径。

### 3.3 交接对象

两个闭环之间只通过已发布 artifact 交接：

```text
ValidatedTask -> QuestionSpec -> PlannerRollout
```

禁止通过共享内存、prompt 或临时文件向 rollout 泄漏 gold 信息。

## 4. 任务生成闭环设计

### 4.1 Validation World Snapshot 与 Generation Evidence Slice

Evidence Snapshot 是任务正确性的环境边界。它至少包括：

- KG provider、schema version 和 snapshot ID；
- 文献 provider、索引版本和读取时间；
- 规范化实体、关系、方向和 provenance；
- 可用工具及其能力版本；
- 截断、分页和缺失数据说明。

所有答案复算、shortcut 检查和 insufficient 判定必须针对同一快照。数据更新后应生成新的任务版本，不能静默改写旧任务。

实现中必须区分两个对象：

```python
class GenerationEvidenceSlice(ContractModel):
    slice_id: str
    world_snapshot_id: str
    evidence_ids: list[str]
    sampling_policy_version: str
    episode_id: str


class ValidationWorldSnapshot(ContractModel):
    snapshot_id: str
    provider_manifests: list[str]
    tool_environment_id: str
    entity_schema_version: str
    relation_schema_version: str
    completeness_scope: dict[str, object]
```

`GenerationEvidenceSlice` 是候选发现材料，允许不完整；`ValidationWorldSnapshot` 是验收边界，必须覆盖该任务声明可查询的完整 KG、文献索引和工具环境。这里的“完整”是相对于发布 manifest 定义的 operational world，不是声称覆盖整个人类生物医药知识。

### 4.2 Candidate Sources

候选生成采用可插拔 source，而不是把所有逻辑写进一个 Question Planner。

```python
class QueryCandidateSource(Protocol):
    def propose(
        self,
        evidence_slice: GenerationEvidenceSlice,
        seed: EntityRef,
        spec: GenerationSpec,
    ) -> list[QueryCandidate]: ...
```

第一阶段实现三类 source：

| Source | 作用 | 允许的自由度 |
|---|---|---|
| `PathQuerySource` | 从已验证路径产生稳定基线题 | 低 |
| `AgentQuerySource` | 发现有生物医药意义的条件组合和问题意图 | 高 |
| `AdversarialQuerySource` | 产生容易暴露 stopping 或 action selection 缺陷的题 | 中 |

Source 只产生候选，不得直接写入 gold 数据集。无论来源如何，都必须输出同一个 `QueryCandidate` 契约并进入相同验证链路。

`QueryCandidate` 必须记录 `generation_slice_id` 和 `world_snapshot_id`。Candidate Source 无权声明最终答案唯一，也无权把 slice 内的端点直接写成 gold answer。

### 4.3 Query Compiler

Query Compiler 把候选编译成规范 `LogicalQuery`。编译结果必须包含：

- 起始实体；
- 有序 relation steps；
- 每一步方向和目标实体类型；
- answer variable 和 answer type；
- expected cardinality；
- filters、aggregation、negation 和 temporal scope；
- snapshot ID；
- dependency digest 和来源 candidate ID。

编译失败属于候选拒绝，不应由 Writer 尝试补救。

建议新增：

```text
src/medical_qa_data_factory/generation/candidates/
    base.py
    path_source.py
    agent_source.py
    adversarial_source.py

src/medical_qa_data_factory/generation/query_compiler.py
```

### 4.4 Task Validator

Task Validator 是候选进入题面生成前的强制门禁。它由确定性检查和独立模型检查组成，但最终接受条件由代码控制。

确定性检查至少包括：

1. `LogicalQuery` schema 和 digest 有效；
2. query 可在完整 `ValidationWorldSnapshot` 上执行，且 evaluator 输入不是 `GenerationEvidenceSlice`；
3. answer set 未被截断；
4. answer cardinality 与声明一致；
5. required path 连续且方向正确；
6. 不存在违反题目难度定义的更短路径或直接关系；
7. 所需动作在下游 Planner 的动作空间内可达；
8. evidence provenance 完整；
9. gold answer 由 evaluator 计算，不从 Agent 文本复制；
10. 数据集 split fingerprint 可计算；
11. 完整世界答案集合与候选提出时的局部答案集合分别保存，不能混用；
12. 单答案题只有在完整世界答案集合基数为 1 时才能通过。

模型检查用于难以完全规则化的项目，例如问题意图是否有生物医药意义、证据是否支持特定语义强度。模型检查只能产生结构化审查结果，不能跳过确定性失败。

建议输出：

```python
class TaskValidationReport(ContractModel):
    candidate_id: str
    query_id: str
    accepted: bool
    answer: QueryAnswer
    required_evidence_ids: list[str]
    valid_route_ids: list[str]
    shortest_route_cost: float | None
    shortcut_findings: list[ShortcutFinding]
    reason_codes: list[str]
    validator_versions: dict[str, str]
    generation_slice_id: str
    validation_world_snapshot_id: str
    slice_answer_ids: list[str]
    world_answer_ids: list[str]


class ValidatedTask(ContractModel):
    task_id: str
    source_candidate_id: str
    logical_query: LogicalQuery
    answer: QueryAnswer
    required_evidence_ids: list[str]
    valid_route_ids: list[str]
    snapshot_manifest_id: str
    generation_slice_id: str
    validation_world_snapshot_id: str
    difficulty_features: DifficultyFeatures
    validation_report_id: str
```

建议新增：

```text
src/medical_qa_data_factory/generation/task_validation.py
src/medical_qa_data_factory/generation/task_contracts.py
```

### 4.5 Question Realizer

Question Realizer 只接收 `ValidatedTask` 和语言、风格配置，输出一个或多个 `QuestionVariant`。

Writer 可以改变措辞，但不能改变：

- `LogicalQuery`；
- answer set；
- cardinality；
- filters 和 temporal scope；
- required evidence；
- 可回答或不可回答状态。

```python
class QuestionVariant(ContractModel):
    variant_id: str
    task_id: str
    question_text: str
    language: str
    style_profile: str | None
    writer_model_id: str
    prompt_version: str
```

现有 `QuestionWriter` 可以改造成该接口；`QuestionBlueprint` 应逐步缩减为 UI 或迁移兼容对象，不再承担 gold 任务语义。

### 4.6 Text Semantic Verifier

题面生成后执行两类验证：

1. **round-trip 验证**：独立解析题面并重建 query intent，与原 `LogicalQuery` 比较；
2. **blind 验证**：验证器只看到题面和允许使用的工具，从快照重新作答。

以下情况必须拒绝或改写：

- 题面扩大或缩小答案集合；
- 丢失关键限制条件；
- 暴露答案字符串或中间实体；
- 同一题面存在多个合理 query；
- blind answer 与 evaluator answer 不一致。

通过后发布 `QuestionSpec`。`QuestionSpec` 必须显式引用 `task_id`、`query_id` 和 `variant_id`，hop count 必须来自 `len(logical_query.steps)`，不得从 evidence 数量推断。

## 5. Planner 求解闭环设计

### 5.1 Rollout 输入

Rollout Runner 只接收下列公开输入：

```python
class PlannerRolloutRequest(ContractModel):
    rollout_id: str
    question_id: str
    question_text: str
    planner_config_id: str
    tool_environment_id: str
    budget: PlannerBudget
    random_seed: int
```

`ValidatedTask` 中的 gold answer、required evidence 和 valid route 不能进入 Planner state。它们只允许在 rollout 结束后的 labeler 和 evaluator 中使用。

### 5.2 Rollout 执行

Rollout 应直接调用 `query-planner` 的正式 pipeline，而不是在 data factory 中模拟另一套 planner：

```text
QuestionSpec
    -> query-planner parser
    -> state construction
    -> stop policy
    -> legal action enumeration
    -> action selector
    -> executor
    -> evidence update
    -> terminal decision
```

每一步记录 action 执行前的状态和执行后的 observation：

```python
class PlannerTransition(ContractModel):
    rollout_id: str
    step: int
    observable_state: dict
    encoded_state: list[float]
    legal_actions: list[PlannerAction]
    decision: Literal["continue", "complete", "insufficient"]
    selected_action_id: str | None
    selected_action_features: list[float]
    estimated_cost: dict
    actual_cost: dict
    new_evidence_ids: list[str]
    observation_status: str | None


class PlannerRollout(ContractModel):
    rollout_id: str
    question_id: str
    planner_config_id: str
    environment_snapshot_id: str
    transitions: list[PlannerTransition]
    final_decision: str
    final_answer: dict | None
    total_cost: dict
    created_at: UtcDateTime
```

### 5.3 标签生成

训练标签不能仅由被测 policy 自己的决定产生。标签生成器需要对照 `ValidatedTask` 和执行环境，区分三种来源：

- **行为标签**：实际 policy 做了什么；
- **结果标签**：动作后获得了什么证据、是否提高了答案充分性；
- **监督标签**：在该状态下根据 gold task 和候选动作评估出的推荐 decision 或 action ranking。

停止标签的最低要求：

| 标签 | 成立条件 |
|---|---|
| `complete` | 当前可观察证据足以推出 gold answer，且满足任务证据约束 |
| `continue` | 当前证据不足，但至少存在一个预算内合法动作可增加必要信息 |
| `insufficient` | 当前证据不足，且在给定快照、工具和预算约束下不存在可行完成路线 |

动作训练不应长期使用 `len(new_evidence_ids)` 作为唯一 utility。建议至少拆为：

```text
evidence_novelty
constraint_progress
answer_space_reduction
sufficiency_gain
duplicate_penalty
cost
```

最终 utility 公式必须有独立版本号，并保留组成项，便于重新标注。

建议新增：

```text
src/medical_qa_data_factory/rollout/
    runner.py
    contracts.py
    labeler.py
    evaluator.py
    export.py
```

如果决定由 `query-planner` 项目持有 rollout runner，则 data factory 只保留客户端和导入器，但契约语义不变。

## 6. Insufficient 任务

`insufficient` 不能等同于随机检索失败、provider 故障或预算耗尽。必须显式构造并验证。

推荐从一个可回答的 `ValidatedTask` 派生：

- 移除一条不可替代的关键边；
- 限制某个必要工具或数据源；
- 使用证据尚未出现的历史快照；
- 保留相似但不能完成推理的干扰证据。

派生任务需要新的 task ID、snapshot/environment manifest 和验证报告。验证器必须证明在规定动作空间和预算下不存在可行完成路线。

以下终止不能标注为语义上的 `insufficient`：

- HTTP 或数据库异常；
- checkpoint 恢复失败；
- 达到安全 guard；
- 模型输出 schema 无效；
- provider 返回无法解释的截断结果。

这些情况应使用运行错误或 `unknown` 状态，避免污染 stopping head。

## 7. 目标 LangGraph

任务生成主图建议调整为：

```text
verify_seed
    -> build_snapshot_manifest
    -> discover_evidence
    -> propose_query_candidates
    -> compile_queries
    -> validate_tasks
    -> realize_questions
    -> verify_question_semantics
    -> persist_question_specs
    -> finalize_generation
```

其中：

- `discover_evidence` 可以继续使用当前 ReAct 子图；
- `propose_query_candidates` 聚合多个 Candidate Source；
- `compile_queries` 不调用 Writer；
- `validate_tasks` 是发布前门禁；
- `realize_questions` 只处理题面；
- rollout 不应默认放在同一个 LangGraph transaction 中，避免外部 planner 失败回滚已验证题目。

Rollout 作为第二个可独立重试的作业：

```text
load_published_question
    -> create_isolated_environment
    -> run_query_planner
    -> evaluate_rollout
    -> build_labels
    -> export_training_samples
```

## 8. Artifact 与存储关系

推荐关系如下：

```text
GenerationEpisode
    1 -> N QueryCandidate
QueryCandidate
    0 -> 1 ValidatedTask
ValidatedTask
    1 -> N QuestionVariant
QuestionVariant
    0 -> 1 published QuestionSpec
QuestionSpec
    1 -> N PlannerRollout
PlannerRollout
    1 -> N PlannerTrainingSample
```

所有 artifact 使用不可变版本。重新生成题面只新增 variant；修改 query 语义必须生成新的 task；使用新 planner 重跑只新增 rollout。

发布数据集建议保留：

- `QuestionSpec`；
- 最小必要的 task/query manifest；
- `PlannerRollout` 或去敏后的 transition；
- label policy 和 utility policy 版本；
- split group 和 leakage fingerprint。

内部审计数据可以额外保留 Agent prompt、原始响应、工具 trace 和 rejection report，但不得默认进入公开训练集。

## 9. 必须保持的系统不变量

1. 每个已发布问题都引用一个通过验证的 `LogicalQuery`。
2. gold answer 由完整 `ValidationWorldSnapshot` evaluator 计算，而不是由 Writer、Candidate Source 或 generation slice 决定。
3. Candidate Source 不能直接发布 gold task。
4. Writer 不能修改任务语义。
5. 验证器与候选生成器使用独立上下文。
6. hop count 由有序 query step 数量决定。
7. generation trace 与 planner rollout 使用不同 schema 和存储集合。
8. rollout 初始状态不包含 gold answer、required evidence 或目标路径。
9. `insufficient` 必须是可证明的任务属性，不是运行失败的兜底标签。
10. 所有 snapshot、validator、prompt、model、label 和 utility policy 都有版本号。
11. resume 不得重复物理工具调用、transition 或训练样本。
12. 训练、验证和测试划分不得共享等价 query 结构或高风险实体组合。
13. `GenerationEvidenceSlice` 与 `ValidationWorldSnapshot` 必须使用不同类型和接口，validator 不接受 slice 代替 world。
14. slice 中答案数量为 1 不能推出世界答案数量为 1；cardinality 只读取 world evaluation。
15. 题面不得通过“在当前证据中”“根据这组资料”等措辞把局部采样范围伪装成问题语义，除非 source-bounded QA 本身就是显式产品目标。

## 10. 迁移计划

### 阶段一 纠正语义和契约

目标是不改变现有生产入口，先阻止继续产生语义混淆的数据。

- 将当前 `PlannerTrajectory` 重命名或版本化为 `GenerationExplorationTrace`；
- 新增 `QueryCandidate`、`ValidatedTask`、`QuestionVariant` 和 `PlannerRollout`；
- 新增 `GenerationEvidenceSlice` 和 `ValidationWorldSnapshot`，并禁止二者共用 evaluator 接口；
- `QuestionSpec` 增加 `task_id`、`query_id`、`variant_id`；
- 停止通过 evidence 数量推断 hop count；
- 为旧 artifact 提供显式 migration adapter，不静默修改。

验收条件：新旧数据可以同时读取，任何消费者都能区分出题轨迹和求解轨迹。

### 阶段二 接通正式任务验证

- 将现有 probe、`LogicalQuery` evaluator 和 shortcut checker 接入主图；
- 先让 `PathQuerySource` 产生 1-hop 和 2-hop 基线候选；
- 只有 `ValidatedTask` 可以进入 Writer；
- 增加 round-trip 和 blind verification；
- 为每个 rejection reason 建立计数和样例。

验收条件：每个发布问题都能在完整验证世界上复算答案；局部唯一但全集多答案的候选全部被拒绝或改成集合题，并能解释拒绝原因。

### 阶段三 接入 AgentQuerySource

- Agent 输出结构化 candidate 或 query intent；
- 编译器拒绝不可表达或不可执行的候选；
- Agent 与 Path source 共用同一验证器；
- 比较不同 source 的通过率、任务多样性和 downstream rollout 价值。

验收条件：Agent 提升有效任务覆盖率，而不是只提高候选数量。

### 阶段四 接通真实 Query Planner rollout

- 定义跨项目版本化协议；
- 对发布问题创建隔离 rollout；
- 保存完整 legal action set 和 transition；
- 在 rollout 后使用 gold task 生成监督标签；
- 替换以新增 evidence 数量为唯一 utility 的标签规则。

验收条件：停止数据和动作数据可以直接被 `query-planner` 的训练脚本读取，并可追溯到问题、task、环境和 label policy。

### 阶段五 数据治理与规模化

- 增加结构去重、实体隔离和组合路径隔离；
- 建立 source、题型、难度、终止类型和成本分布；
- 对高风险题型进行人工抽样；
- 建立新 snapshot 和新 planner 版本的回归基准。

## 11. 开发标准

### 11.1 类型与接口

- `GenerationEvidenceSlice` 和 `ValidationWorldSnapshot` 必须是两个不同的强类型，禁止使用同一个 `EvidenceBundle` 参数兼容两种语义；
- Candidate Source 的公开接口只能接收 slice 和 snapshot reference，不能获得发布权限；
- Task Validator 和 Query Evaluator 必须显式接收完整 world，调用方缺少 world 时直接失败；
- `QueryAnswer` 必须携带 `validation_world_snapshot_id`、是否截断、扫描范围和 evaluator version；
- `ValidatedTask` 必须同时引用 candidate、slice、world、query 和 validation report；
- 所有跨项目对象先定义版本化 contract，再编写 adapter，不直接共享内部 Python 对象。

### 11.2 查询和验证实现

- world evaluator 必须查询完整 snapshot/index，禁止通过 `required_evidence_ids`、candidate evidence IDs 或 episode observation IDs 限制搜索域；
- slice evaluation 只允许用于诊断和对比，结果字段命名为 `slice_answer_ids`，不得赋给 gold answer；
- cardinality、ambiguity、shortcut 和 insufficient 只读取 world evaluation；
- snapshot 不可用、不完整、截断或 provider 失败时返回 `not_evaluated` 或明确错误，禁止回退到 slice；
- 任何为了让候选通过而新增的 filter 都必须来自任务证据和业务语义，并进入 `LogicalQuery` digest；
- Writer 和 Style Skill 不能增加“当前证据范围”“本次查询结果”等局部范围限定来改变 query 语义。

### 11.3 持久化和命名

- 字段名必须明确使用 `generation_slice_*` 或 `validation_world_*`，禁止含义不清的 `evidence_scope`、`context` 或 `snapshot`；
- validation report 同时保存 slice answer 和 world answer，便于审计局部到全集的差异；
- world snapshot 或 completeness scope 变化时必须生成新的 task version；
- 不覆盖失败候选和拒绝报告；修订产生新版本并保留 lineage；
- export 只读取 `ValidatedTask.world_answer_ids`，不得从 draft、blueprint 或 slice 重建 gold。

### 11.4 失败语义

- `world_answer_count > 1` 且题目声明单答案：拒绝为 `world_cardinality_mismatch`；
- `slice_answer_count = 1` 且 `world_answer_count > 1`：额外记录 `local_uniqueness_only`；
- world evaluation 截断：拒绝为 `world_answer_truncated`；
- world snapshot 不可访问：状态为 `not_evaluated`，不得标记 accepted；
- source-bounded QA 只有在产品配置显式开启并把来源边界写入 `LogicalQuery` 时才允许，不能由 Writer 临时添加。

### 11.5 Pull Request 最低要求

任何修改 Candidate Source、Query Compiler、evaluator、validator、Writer 或 export 的 PR 都必须包含：

- 一个局部与全集答案相同的正例；
- 一个局部单答案、全集多答案的拒绝例；
- 一个 world unavailable 或 truncated 的 fail-closed 例；
- schema 和 migration 兼容测试；
- 对新增 reason code 的文档和报告断言。

## 12. 测试要求

### 12.1 单元测试

- Candidate Source 只能输出合法候选引用；
- Query Compiler 保持路径顺序和方向；
- LogicalQuery digest 对语义变化敏感；
- evaluator 对 cardinality、截断和 snapshot mismatch 正确失败；
- evaluator 拒绝以 `GenerationEvidenceSlice` 代替 `ValidationWorldSnapshot`；
- slice 中一个答案、world 中多个答案时，单答案候选必须失败；
- 两跳 evaluator 必须扩展完整世界中的所有合法 bridge 并合并终点，不能只验证 candidate path；
- 多个 bridge 指向不同终点时单答案失败，多个 bridge 收敛到同一终点时按去重后的 world answer set 判断；
- Writer 输出不能更改 task ID 和 query ID；
- hop count 不依赖 evidence ID 数量；
- generation trace 不能被 rollout loader 接受。

### 12.2 集成测试

- `PathQuerySource -> ValidatedTask -> QuestionSpec` 完整闭环；
- 候选生成只读取 slice，Task Validator 独立读取完整 world；
- Agent 候选和规则候选共用同一门禁；
- blind verifier 不接收 gold answer；
- rollout 从空 EvidenceStore 开始；
- 同一 QuestionSpec 可以由不同 planner config 重放；
- resume 不产生重复 artifact。

### 12.3 数据质量测试

- answer recomputation pass rate；
- ambiguous、shortcut、leakage 和 truncation rejection rate；
- round-trip query equivalence rate；
- blind answer agreement rate；
- stopping label class balance；
- action utility 与最终任务成功率的相关性；
- train/test query fingerprint collision rate。

## 13. 第一版不做的内容

- 不要求使用 CrewAI 或多个角色互相对话；
- 不要求 Agent 直接生成可发布题目；
- 不要求一开始支持任意自然语言逻辑；
- 不要求把生成和 rollout 放进同一个事务；
- 不保存模型隐藏思维链；
- 不在没有环境证明的情况下声称某条路线是唯一最优路线；
- 不把 provider 故障、超时或预算耗尽当成 `insufficient` gold label。

## 14. 尚需产品和研究侧确认的参数

下列项目影响数据分布，但不阻塞架构实现：

- 第一批任务支持哪些生物医药实体类型和 relation composition；
- `complete` 所需的最低证据充分性规则；
- 动作成本中数据库访问、工具调用、token 和时延的权重；
- 是否训练单一最优动作，还是训练 action ranking 或 preference pair；
- insufficient 是否考虑预算约束，还是只考虑工具和快照可达性；
- 人工审核集中在哪些高风险题型；
- dataset split 以实体、query fingerprint、关系组合还是它们的并集为隔离单位。

这些决策必须进入版本化配置，不应硬编码在 Candidate Source、Writer 或训练脚本中。


# 客观医学题目生成系统开发计划

> 状态：待实施  
> 制定日期：2026-10-28  
> 架构修订：2026-10-29  
> 运行时与评测补充：2026-10-29  
> 基线参考：`main@7b5d81c`；实施前必须记录实际 working tree checkpoint  
> 产品说明：[客观医学题目生成系统整体方案阅读版](evidence-first-question-derivation-reader-version-2026-10-29.md)  
> 验证 Skill：[`objective-question-validator`](../skills/objective-question-validator/SKILL.md)

## 1. 改造目标

本计划把现有“从一条图谱边构造题目”的链路改造成一条可审计的客观题目生产线：

```text
目标实体
  -> 多来源材料收集
  -> LLM 语义理解与候选事实提取
  -> 候选问题和候选答案探索
  -> 独立答案验证与问题语义冻结
  -> 最终题面生成
  -> 客观题独立复核
  -> 确定性发布门禁
  -> public package + audit package
```

最终发布题必须同时满足：

1. 题面自包含，不依赖隐藏的图谱、数据库、检索结果或候选集合；
2. 答案是现实世界中的事实，不是某个数据源的收录结果；
3. 答案有明确判断标准，并能通过适合该事实类型的独立材料复核；
4. 单答案、多答案、集合完整性和必要限定均已显式处理；
5. 题面没有将相关性升级为因果、治疗、诊断或预后结论；
6. 最终验证结果为 `PASS`；`REJECT` 和 `INDETERMINATE` 均阻塞发布；
7. 没有合格事实时允许稳定产出 0 题，不能为了满足数量降低门槛。

## 2. 本次架构修订解决的歧义

旧计划有三个容易误导实施的地方，本版明确修正。

### 2.1 数据源只能提供材料

PrimeKG、CTD、PubMed、RAG 和其他数据库返回的是 `EvidenceMaterial`。图谱边、数据库记录和文献片段可以提出候选事实，但不能直接充当客观真值。

内部 snapshot、provider filter、PMID 集合和检索时间只用于复现与审计，不能成为用户不可见的答案边界。

### 2.2 LLM 可以理解材料并探索题目

LLM 不只负责润色题面。它可以：

- 理解非结构化文献；
- 提取候选事实和限定条件；
- 区分否定、相关、风险、因果和治疗语义；
- 识别可提问的信息位；
- 同时提出候选问题和候选答案。

但这些输出只能处于 `PROPOSED` 状态。LLM 不能使用自己的第一次输出作为答案证明，也不能自行把候选升级为可发布题目。

### 2.3 独立确定答案不等于不用 LLM

“独立”指职责、上下文和证据路径独立：

- Answer Verification Subagent 不直接相信问题设计子 Agent 的结论；
- 验证不能只重读产生候选的同一条图谱边或模型摘要；
- 验证必须检查竞争答案、反例、冲突和关系强度；
- 验证可以使用 LLM、检索和确定性工具；
- 最终发布由确定性门禁决定，不由 Agent 自报。

## 3. 非目标

本阶段不建设：

- 关于数据库内容本身的问答集；
- “现有研究是否支持”“文献是否提到”等元证据题；
- 依赖开放世界穷尽证明的“所有相关疾病”题；
- 自动发布存在显著科学争议的关联、风险或因果题；
- 模型自由文本形式的私有思维过程存储；
- 为了达到目标题数而放宽验证规则；
- 前端从 Vue 迁移到 React。

## 4. 必须长期保持的系统不变量

| ID | 不变量 |
|---|---|
| INV-01 | discovery source 不等于 truth source |
| INV-02 | 隐藏 metadata 不得修补不完整的公开题面 |
| INV-03 | source absence 不得推导现实世界中的否定结论 |
| INV-04 | 一个 supporting record 不得证明答案集合完整 |
| INV-05 | LLM 输出必须经过 schema 校验和独立复核 |
| INV-06 | 题面生成不得改变冻结的问题语义和答案契约 |
| INV-07 | 只有 ObjectiveValidationReport.PASS 可以发布 |
| INV-08 | public package 不包含内部 source filter、ID 或隐藏答案信息 |
| INV-09 | audit metadata 从第一步持续写入，不在最后补写 provenance |
| INV-10 | provider failure、未评估和证据不足必须保持不确定状态 |
| INV-11 | 结构化推导链只记录可复现映射，不存储自由形式 chain-of-thought |
| INV-12 | 0 题是合法完成状态，不是自动重试或降级规则的触发条件 |
| INV-13 | 子 Agent 不直接写共享可变状态；正式状态只能由协调器基于版本和前置条件推进 |
| INV-14 | 一次运行固定模型、prompt、skill、schema、policy、tool 和 provider adapter 版本，运行中不得静默漂移 |
| INV-15 | 重试、恢复和重复投递不得生成语义重复的正式产物或重复扣减预算 |
| INV-16 | 多 Agent 架构必须通过单 Agent、共享上下文 Workflow 和隔离式多 Agent 对照评测证明收益 |

## 5. 当前技术栈和复用边界

### 5.1 保留的技术形态

- 前端：Vue 3 + Vite；
- Web/API：Python + Starlette；
- 生成编排：现有 generation graph、service、repository 和 state 模块；
- LLM 接入：现有 `llm/contracts.py`、provider adapter 和 structured output；
- 数据源：PrimeKG、CTD、PubMed 及后续 RAG adapter；
- 审核和导出：现有 review、validation、export 和 release 模块。

React 不解决客观性问题，本计划不引入前端框架迁移。

### 5.2 可以复用但需要改造的模块

| 现有模块 | 复用内容 | 需要改变 |
|---|---|---|
| `generation/react_graph.py` | Agent 循环和动作审计 | 增加客观题工作流状态与阶段 |
| `generation/skills/registry.py` | 检索技能和工具注册 | 明确材料发现 Skill 不拥有真值权限 |
| `evidence_enrichment/*` | 文献抓取、span、claim 和 hash | 将输出定位为候选材料和候选 claim |
| `generation/writing.py` | 风格化题面生成 | 只接受冻结后的 QuestionContract |
| `generation/verification.py` | 答案检查基础能力 | 增加竞争答案和独立来源要求 |
| `generation/blind_verification.py` | 隔离上下文验证 | 接入客观题公开题面审查 |
| `generation/repository.py` | 运行状态和 artifact 持久化 | 保存材料、候选、冻结契约和验证报告 |
| `generation/task_release.py` | 发布隔离 | 只接受 ObjectiveValidationReport.PASS |
| Vue 工作台 | 实体选择、进度和结果展示 | 增加阶段、拒绝原因和审计视图 |

### 5.3 旧链路兼容策略

旧 `TaskCandidate`、CTD task 和 v2 release 不改写原有语义。新链路使用独立 schema version 和 feature flag。旧 artifact 可读，但不能在无显式迁移的情况下升级为新 `PASS`。

## 6. 目标架构

```text
Vue Generation Workspace
          |
          v
Starlette API + Deterministic Task Orchestrator
          |
          v
Question Production Main Agent
  |
  +-> Material Retrieval Subagent instances
  |     -> PrimeKG / CTD / PubMed / RAG adapters
  |     -> EvidenceMaterial[] + EvidenceAnchor[]
  |
  +-> Fact Understanding Subagent
  |     -> CandidateClaim[]
  |
  +-> Question Design Subagent
  |     -> QuestionHypothesis[]
  |     -> after verification: QuestionDraft + StructuredDerivationTrace
  |
  +-> Answer Verification Subagent
  |     -> BlindAnswerQuery
  |     -> VerificationPlan + VerificationAttempt[]
  |     -> VerificationCoverageReport
  |     -> AnswerResolution + FrozenQuestionContract
  |
  +-> Release Review Subagent
        -> objective-question-validator
        -> ObjectiveValidationReport
          |
          v
Deterministic Release Gate
  -> PublicQuestionPackage
  -> InternalAuditPackage
```

确定性 Task Orchestrator 是主 Agent 的宿主和权限边界。它执行状态转换、预算扣减、artifact 持久化和 release gate；主 Agent 负责需要语义判断的规划与调度。

## 7. 主 Agent 与子 Agent 设计

### 7.1 Question Production Main Agent

**任务级目标：** 在给定目标实体、题目数量上限、难度、风格和预算下，尽可能产生通过客观性门禁的题目；若没有合格事实，返回可解释的 0 题结果。

**输入：**

```text
GenerationGoal
RunBudget
current run state
artifact summaries
subagent capability registry
```

**职责：**

- 制定和更新检索计划；
- 创建 `SubagentTask` 并选择对应子 Agent；
- 汇总子 Agent 的结构化输出；
- 维护候选事实和候选题优先级；
- 根据 `EvidenceNeed` 决定是否继续检索；
- 控制轮次、预算、超时和停止条件；
- 将候选依次送入验证、题面生成和发布复核；
- 输出 `MainAgentDecision` 和面向页面的阶段说明。

**工具权限：** 读取 run state、artifact summary、预算和 capability；创建子任务；请求合法状态转换。主 Agent 不直接调用 provider，不直接写正式 evidence、claim、answer 或 validation artifact。

**禁止事项：**

- 不得自行宣布答案正确；
- 不得修改子 Agent 已固化产物；
- 不得把 `REJECT` 或 `INDETERMINATE` 改成 `PASS`；
- 不得把 proposed answer 传入盲态验证请求；
- 不得绕过状态机、预算和发布门禁；
- 不得为达到目标题数降低质量策略。

**输出：** `MainAgentDecision`，包含 action、target subagent、input artifact IDs、预算申请、预期信息增益和 stop reason。所有决定必须可审计。

### 7.2 Material Retrieval Subagent

**输入：** 主 Agent 下发的 `RetrievalSubtask`，包含目标实体、evidence need、允许的 provider、查询预算和停止条件。

**职责：** 调用 PrimeKG、CTD、PubMed、RAG 等工具，保存原始材料、anchor、query manifest、attempt 和错误。

**实例模型：** 同一种子 Agent 可以同时创建多个实例，每个实例只负责一个 provider 或一个明确检索目标。并行实例不能共享未落盘的临时结论。

**允许输出：** `EvidenceMaterial`、`EvidenceAnchor`、`RetrievalAttempt` 和新的 `EvidenceNeed` 建议。

**禁止事项：** 不解释最终事实，不提出 gold，不做发布判断，不把 empty 或 failure 解释为否定事实。

**完成条件：** 计划 attempt 到达终态，或预算/超时结束并明确返回不完整状态。

### 7.3 Fact Understanding Subagent

**输入：** 已持久化的材料、anchor 和实体消歧结果；不读取候选题和 proposed answer。

**职责：** 理解结构化与非结构化材料，处理同义词、否定、指代、人群、剂量、方向和研究设计，输出候选事实、限定条件和关系强度。

**允许输出：** `CandidateClaim`、`EntityResolution` 补充需求和 evidence gap。

**禁止事项：** 不生成正式题目，不为了某种题型选择性解释原文，不修改原始材料，不把候选 claim 标记为客观真值。

**完成条件：** 每个 claim 绑定可回读 anchor，未关闭的实体歧义和证据缺口被明确标记。

### 7.4 Question Design Subagent

**候选设计模式输入：** 已固化的 `CandidateClaim` 和公开产品约束，不读取事实理解阶段的自由解释。

**候选设计职责：** 识别可提问信息位，提出 `QuestionHypothesis`、proposed answer、公开条件、答案类型、基数和风险标记。

**题面实现模式输入：** `FrozenQuestionContract`。该模式与候选设计使用独立调用，只负责生成 `QuestionDraft`、标准答案展示和简洁解释。

**禁止事项：** 不回写 claim，不把 proposed answer 当 gold；在题面实现模式中不得增加事实、删除必要条件、改变 cardinality 或升级关系强度。

**完成条件：** 候选模式输出状态为 `PROPOSED`；实现模式通过确定性 contract round-trip 检查。

### 7.5 Answer Verification Subagent

**输入分层：**

1. 盲态阶段只接收 `BlindAnswerQuery`；
2. 比较阶段才接收 blind result 和 proposed answer；
3. 反例阶段接收候选、盲态结果和验证计划。

**职责：** 独立求解、比较答案、搜索竞争答案和反例、检查 completeness basis，输出 `VerificationPlan`、attempt、coverage 和 `AnswerResolution`。

**工具权限：** 可以调用验证政策允许的 provider；不能读取探索 Agent 的自由解释，也不能使用 discovery summary 代替原文证据。

**禁止事项：** 不接受主 Agent 预设 gold，不在 coverage 不完整时返回 VERIFIED，不把 source absence 当作 negative。

**完成条件：** 所有 expected verification attempts 有终态；coverage 完整；输出 VERIFIED、REJECTED 或 INDETERMINATE，并在 VERIFIED 时生成 `FrozenQuestionContract`。

### 7.6 Release Review Subagent

发布复核必须由两个隔离的 `SubagentTask` 完成，不能在同一上下文中用提示词模拟隔离：

1. `PublicQuestionAuditTask` 只包含最终公开题面，字段策略禁止答案、FrozenQuestionContract、evidence 和内部 metadata；
2. 第一阶段结果固化后，`AnswerEvidenceAuditTask` 才包含题面、正式答案、FrozenQuestionContract 和允许使用的独立 evidence audit package。

**职责：** 两次调用分别输出 `PublicQuestionAuditResult` 和 `AnswerEvidenceAuditResult`。确定性模块校验两个 input digest 和终态，再合并为 `ObjectiveValidationReport`。

**禁止事项：** 不改写题目后再判 PASS，不修补隐藏前提，不发布题目，不覆盖答案验证结果。

**完成条件：** 两阶段均返回严格 JSON 且通过；任一阶段失败、超时、解析失败或 INDETERMINATE，合并结果都不能 PASS。第二阶段不得回写第一阶段结果。

### 7.7 主子 Agent 交接规则

- 所有调用通过版本化 `SubagentTask` 和结构化 artifact 交接；
- 子 Agent 不直接调用其他子 Agent，只能向主 Agent 返回结果或 `EvidenceNeed`；
- 子 Agent 不得修改其他 Agent 的产物，只能产生新版本；
- 主 Agent 只能看到完成当前调度所需的最小字段；
- 盲态验证投影由确定性模块生成，主 Agent 无权添加 proposed answer；
- 同一个底层模型可以承担不同角色，但 system prompt、上下文、工具权限、缓存和 trace 必须隔离；
- 任何 Agent 的自然语言说明都不是门禁依据，门禁只读取结构化 artifact。

### 7.8 各角色失败语义和恢复责任

子 Agent 不能只返回笼统的 `FAILED`。每个 `SubagentResult` 必须使用统一失败分类，协调器再根据角色和阶段决定恢复动作。

| 角色 | 可重试失败 | 需要重新规划 | 不可继续或直接终止 | 恢复后的约束 |
|---|---|---|---|---|
| Material Retrieval | 网络抖动、临时限流、provider 短暂不可用 | 查询过宽、实体别名未解析、材料不足 | 权限错误、契约错误、预算耗尽 | 使用同一 idempotency key；保留旧 attempt；empty 与 failure 分开 |
| Fact Understanding | 临时模型错误、结构化输出解析失败 | 实体歧义、anchor 不足、材料相互矛盾 | 输入 hash 不匹配、必需字段缺失 | 不覆盖旧 claim；补充材料后产生新版本 |
| Question Design | 临时模型错误、格式错误 | 候选不唯一、题型与事实不匹配 | 输入 claim 已失效、公开条件无法自包含 | 不通过追加隐藏条件修补旧题；产生新 hypothesis |
| Answer Verification | 临时 provider/模型失败 | coverage 不足、需要竞争答案或反例材料 | 发现确定冲突、答案不唯一、预算耗尽 | 未完成覆盖时保持 `INDETERMINATE`；不得重试到偶然 PASS |
| Release Review | 临时模型错误、严格 JSON 解析失败 | 无；上游必须重新生成新版本 | 任一审查明确 REJECT、输入 digest 失效 | 复核不能修改题目；新题面必须创建新的 review task |
| Main Agent | 决策调用临时失败 | 信息增益不足、子任务不可用、预算需要重分配 | 重复决策、越权 action、总预算耗尽 | 只能从最后一个已确认 checkpoint 继续，不重放已提交副作用 |

子任务结果使用三个正交维度，不能把执行错误和事实判断混成一个状态：

```text
execution_status = SUCCEEDED | FAILED | UNKNOWN
domain_outcome = SUPPORTED | CONFLICT | INSUFFICIENT | NOT_APPLICABLE
verdict = VERIFIED | REJECTED | INDETERMINATE | NOT_EMITTED
```

只有 `execution_status=FAILED/UNKNOWN` 时才填写执行错误类别：

```text
TRANSIENT_TOOL_FAILURE
RATE_LIMITED
TIMEOUT_UNKNOWN_OUTCOME
INVALID_TASK_CONTRACT
PERMISSION_DENIED
MODEL_OUTPUT_INVALID
BUDGET_EXHAUSTED
STALE_INPUT
DUPLICATE_OPERATION
VERSION_INCOMPATIBLE
CHECKPOINT_INTEGRITY_ERROR
INTERNAL_ERROR
```

证据不足和证据冲突写入 `domain_outcome`，再由事实类型政策映射为业务 verdict。只有被 policy 明确标记为可重试的 `error_category` 才能自动重试。

## 8. 确定性模块

以下能力不得交给 Agent 自由裁量：

- contract/schema 校验；
- 实体 ID、别名和关系方向规范化；
- artifact ID、digest 和版本生成；
- 单答案与多答案字段一致性；
- public/hidden 字段隔离；
- stale validation 失效；
- `PASS` 发布门禁；
- snapshot、attempt、provider error 和成本记录；
- 状态机合法转换；
- 0 题完成状态；
- release package 和 audit package 投影。

### 8.1 外部材料安全边界

PubMed、RAG、网页和数据库文本全部按不可信数据处理：

- 原文只能作为引用数据，不能作为 Agent 指令执行；
- 系统 prompt 必须用明确边界包裹外部内容；
- 工具调用只能来自系统允许的 action 和 provider policy；
- 文本中的“忽略前文”“调用工具”等内容按普通引文保存；
- 所有模型输出必须通过严格 schema 校验；
- 非 JSON、字段越权、解析失败和超时均记录 attempt，并按阶段进入可恢复失败或 `INDETERMINATE`。

### 8.2 状态所有权 并发和幂等

- 子 Agent 只提交候选 artifact，不直接更新 run 或 hypothesis 的正式状态；
- Task Orchestrator 是正式状态、预算账本和发布状态的唯一写入者；
- `runtime_bundle_id` 标识本次运行固定的软件和政策版本；`state_revision` 是单调递增的并发修订号，两者不得复用；
- 每次状态提交必须携带 `expected_state_revision`、`expected_state` 和 `input_digest`；协调器以 compare-and-set 校验修订号，避免状态离开后又回到同名状态造成 ABA 问题；
- `operation_key` 标识一次逻辑操作且在重试间保持稳定；`attempt_id` 标识一次实际调用且每次唯一；只有已 `COMMITTED` 的 operation 可以直接复用结果；
- `RETRYABLE_FAILED` 可以由 policy 创建新的 attempt，`UNKNOWN` 必须先解析状态；重复 attempt/result 不重复写 artifact、不重复扣减预算；
- 每个并行 subtask 有独立生命周期、`subtask_revision` 和预登记的 child result slot；child 先幂等提交到自己的 slot，再由协调器根据已登记 child 集合执行聚合状态转换；
- run 级 revision 变化不能直接淘汰输入仍有效的 append-only child result；聚合提交必须检查 child slot、输入 lineage 和聚合前置条件；
- 不以分布式锁替代版本校验。即使未来使用锁，也必须保留 digest、前置状态和幂等约束。

### 8.3 运行版本固定和跨版本恢复

创建 `GenerationRun` 时生成不可变 `RunManifest`，固定：

```text
orchestrator_version
main_agent_version
subagent_role_versions
model_ids + model_parameters
prompt_digests
skill_digests
schema_versions
validation_policy_version
tool_contract_versions
provider_adapter_versions
data_snapshot_or_retrieval_time_policy
runtime_bundle_id + runtime_bundle_digest
```

运行中部署新代码不得改变已创建任务的 manifest。checkpoint 恢复时：

1. 校验 manifest 和已保存 artifact digest；
2. 完全兼容时按原版本继续；
3. 需要迁移时创建 successor manifest 和 successor run，并保存 lineage；不得修改原 manifest；
4. 无法证明兼容时停止为 `VERSION_INCOMPATIBLE`，不得混用新旧 prompt、skill、schema 或验证政策；
5. 已发布题目的旧验证报告在其输入、政策或 skill digest 变化后标记 stale，但保留历史记录。

prompt、skill、schema、policy 和 provider adapter 必须保存到按 digest 寻址的不可变 registry；Python 运行时代码以不可变 runtime bundle 或 container image 保存。旧 bundle 至少保留到所有引用它的 run 到达终态并超过审计保留期。只有版本号、但无法加载对应 bundle 时，运行进入 `VERSION_INCOMPATIBLE`，不能声称可恢复。

## 9. 核心数据契约

建议使用 `objective-question-v1` 作为新链路 schema family。

### 9.1 EvidenceMaterial

保存数据源返回的原始材料，而不是已经确认的事实。

关键字段：

```text
material_id
provider_id + provider_version
source_record_id
retrieved_at
content_hash
raw_content_ref
material_type
retrieval_request_id
terminal_status
```

### 9.2 EvidenceAnchor

定位支持某个候选解释的原始位置：

```text
material_id
document_id
section_or_field
start_offset / end_offset
quoted_text
quote_hash
```

没有可回读 anchor 的生成摘要不能作为正式验证材料。

原始内容必须写入本地不可变 blob 或版本化对象；`raw_content_ref` 不能只指向可能变化的远程 URL。

### 9.3 EntityResolution

所有正式实体在进入 claim 前必须完成消歧：

```text
mention
namespace
canonical_id
canonical_name
species
aliases
authority
resolution_status = RESOLVED | AMBIGUOUS | UNRESOLVED
candidate_entities
```

基因/蛋白混淆、物种缺失或多候选未关闭时，不得形成正式问题契约。

### 9.4 CandidateClaim

由 LLM 或规则从材料中提出：

```text
claim_id
subject
predicate
object_or_value
qualifiers
claim_strength
polarity
supporting_anchor_ids
contradicting_anchor_ids
extraction_model_version
status=PROPOSED
```

`CandidateClaim` 不代表事实已经客观成立。

### 9.5 QuestionHypothesis

允许探索阶段同时提出问题和答案：

```text
hypothesis_id
candidate_claim_ids
question_intent
proposed_public_scope
proposed_answer_type
proposed_answer_values
proposed_cardinality
discovery_anchor_ids
risk_flags
status=PROPOSED
```

该契约明确区分 `proposed_answer_values` 和正式 gold。任何发布代码不得读取前者作为 gold。

### 9.6 BlindAnswerQuery

由 `QuestionHypothesis` 投影产生，只包含：

```text
question_intent
public_scope
answer_type
answer_cardinality
required_relation_strength
```

严禁包含 `proposed_answer_values`、discovery anchor、候选答案别名和探索 Agent 的解释。BlindAnswerQuery 的 digest 必须进入最终验证报告。

### 9.7 VerificationPlan VerificationAttempt 和 VerificationCoverageReport

`VerificationPlan` 保存需要验证的事实、竞争答案策略、计划调用的 provider、预算和停止条件。

`VerificationAttempt` 保存每次查询的 query、provider、开始和结束时间、终态、错误、返回材料和 anchor。

`VerificationCoverageReport` 至少保存：

```text
expected_attempt_ids
terminal_attempt_ids
successful_attempt_ids
failed_attempt_ids
unknown_attempt_ids
budget_exhausted
competing_answer_queries
contradiction_queries
coverage_status = COMPLETE | INCOMPLETE
```

provider failure、预算耗尽、未知终态或 expected/terminal 不一致时必须 `INDETERMINATE`。

### 9.8 AnswerResolution

记录独立验证结果：

```text
resolution_id
hypothesis_id
status = VERIFIED | REJECTED | INDETERMINATE
verified_answer_values
answer_cardinality
public_scope
supporting_anchor_ids
contradicting_anchor_ids
competing_answers_checked
completeness_basis
verification_sources
verification_policy_version
blind_answer_query_digest
verification_plan_id
verification_coverage_report_id
```

`completeness_basis` 首期允许：

```text
CANONICAL_IDENTITY
AUTHORITATIVE_DEFINITION
AUTHORITATIVE_ENUMERATION
EXPLICIT_PUBLIC_OPTIONS
DETERMINISTIC_CALCULATION
NOT_ESTABLISHED
```

`NOT_ESTABLISHED` 不能生成正式单答案或 exact-set 题。`AUTHORITATIVE_DEFINITION` 和 `AUTHORITATIVE_ENUMERATION` 必须额外保存 authority、版本、适用范围和该来源具有定义权的依据。

### 9.9 FrozenQuestionContract

只有 `AnswerResolution.status=VERIFIED` 才能生成：

```text
contract_id
question_intent
public_scope
answer_type
answer_values
answer_cardinality
claim_strength
required_public_conditions
allowed_wording_constraints
forbidden_semantic_upgrades
resolution_id
```

题面生成只能改变表达，不能改变此契约。

### 9.10 StructuredDerivationTrace

保存可复现映射，不保存自由形式 chain-of-thought：

```text
material -> anchor -> candidate claim
candidate claim -> question hypothesis
hypothesis -> answer resolution
resolution -> frozen contract
frozen contract -> question draft
```

每条边记录 rule/prompt/model/tool version 和输入输出 digest。

### 9.11 ObjectiveValidationReport

发布复核先产生两个不可变阶段结果：

```text
PublicQuestionAuditResult
  task_id
  public_question_digest
  self_contained
  source_independent
  asks_underlying_fact
  entity_unambiguous
  cardinality_clear
  verdict

AnswerEvidenceAuditResult
  task_id
  question_answer_contract_digest
  independently_supported
  unique_or_closed
  contradiction_checked
  relation_strength_preserved
  verdict
```

确定性 merger 只能在两个结果均为 PASS、digest 匹配且无 stale input 时生成最终 `ObjectiveValidationReport`。最终输出与 `objective-question-validator` 保持一致：

```text
verdict = PASS | REJECT | INDETERMINATE
release_allowed
primary_reason
secondary_reasons
public_question_audit
normalized_claim
answer_audit
blocking_findings
evidence_used
validator_version
input_digest
```

只有 `PASS + release_allowed=true` 可以进入 release。

### 9.12 EvidenceNeed 和 RetrievalAttempt

受控补检索使用独立契约：

```text
evidence_need_id
origin_stage
target_claim_or_hypothesis_id
information_required
allowed_providers
budget
max_rounds
stop_conditions
```

每个 `RetrievalAttempt` 引用 need、provider request、返回材料、终态和错误。Agent 不得通过生成新的 need 绕过 run 级总预算。

### 9.13 主子 Agent 调度契约

`GenerationGoal` 固定用户目标和产品约束：

```text
run_id
target_entity
question_count_upper_bound
difficulty_targets
style_targets
allowed_fact_types
run_budget
run_manifest_id
runtime_bundle_id
```

`SubagentTask` 是唯一合法的子 Agent 调用入口：

```text
subtask_id
subtask_revision
child_slot_id
parent_decision_id
subagent_role
role_version
goal
input_artifact_ids
input_digest
runtime_bundle_id
expected_state
expected_state_revision
operation_key
initial_attempt_id
visible_field_policy
allowed_tools
budget
stop_conditions
expected_output_schema
```

`SubagentResult` 保存 `execution_status`、`domain_outcome`、`verdict`、输出 artifact IDs、EvidenceNeed、usage、error 和 stop reason。禁止只返回不可验证的自然语言结论。

`MainAgentDecision` 保存主 Agent 每次调度选择、依据的 state digest、预期信息增益、目标子 Agent、预算申请和停止决定。协调器必须校验该 action 在当前状态是否合法。

### 9.14 RunManifest Checkpoint 和 OperationRecord

`RunManifest` 固定第 8.3 节所列版本。`Checkpoint` 只保存已确认状态，不把执行中的未知结果假设为失败：

```text
checkpoint_id
run_id + runtime_bundle_id
confirmed_state
state_revision
last_committed_sequence
open_operation_ids
created_at
```

每次模型或工具调用写入 `OperationRecord`：

```text
operation_key
attempt_id + retry_ordinal
parent_subtask_id
operation_type
input_digest
started_at + finished_at
deduplication_status = IN_FLIGHT | COMMITTED | RETRYABLE_FAILED | UNKNOWN | TERMINAL_FAILED
execution_status + error_category
domain_outcome + verdict
output_artifact_ids
usage + cost + latency
```

一次协调器 commit 必须原子保存 state transition、artifact references、budget ledger update、operation terminal update 和单调递增的 commit sequence。Checkpoint 引用 `last_committed_sequence`，不分别读取可能来自不同提交时刻的 state、artifact 和 budget 快照。

如果单个数据库事务不能覆盖消息队列或任务分发，使用 transactional outbox/inbox 或等价协议：先原子提交业务状态和待发送事件，再异步投递并去重消费。恢复时先对 `open_operation_ids` 做状态核对，再决定复用、补记终态或重试；不得因为进程中断就默认外部调用没有发生。

## 10. 状态机

状态分为 run 级和 hypothesis 级，不能用一条线表示一个任务中的全部候选。

### 10.1 QuestionHypothesis 生命周期

```text
PROPOSED
  -> VERIFICATION_PLANNED
  -> VERIFYING
  -> ANSWER_VERIFIED
  -> QUESTION_REALIZED
  -> RELEASE_REVIEWED
  -> PUBLISHED
```

候选终态：

```text
REJECTED
INDETERMINATE
```

### 10.2 GenerationRun 聚合状态

```text
CREATED
  -> MATERIALS_COLLECTED
  -> CLAIMS_PROPOSED
  -> HYPOTHESES_PROPOSED
  -> CANDIDATES_PROCESSING
  -> COMPLETED
```

run 级旁路状态：

```text
COMPLETED_WITH_ZERO_QUESTIONS
PROVIDER_FAILED
RECOVERY_REQUIRED
VERSION_INCOMPATIBLE
CANCELLED
```

run 保存每个 hypothesis 状态计数；`COMPLETED_WITH_ZERO_QUESTIONS` 只能由所有候选均到达 REJECTED/INDETERMINATE，或没有产生 hypothesis 时聚合得到。

`RECOVERY_REQUIRED` 和 `VERSION_INCOMPATIBLE` 是运维阻塞态，不得聚合成 0 题完成。`CANCELLED` 只表示用户或系统明确取消，不能伪装成失败或无合格事实。hypothesis 级只使用业务终态 `REJECTED/INDETERMINATE`；运行时完整性问题挂在 run/subtask/operation 上，避免污染事实判断。

### 10.3 受控补检索循环

事实理解或答案验证发现缺口时，可以产生：

```text
EvidenceNeed
  -> RetrievalAttempt
  -> EvidenceMaterial
  -> resume exploration or verification
```

每个循环必须声明预算、最大轮次、允许的 provider、stop reason 和 failure terminal。达到预算、超时或存在未知终态时不得继续尝试到“碰巧得到 PASS”，而应返回 `INDETERMINATE`。

规则：

- 任一后续阶段不得改写已保存的上游 artifact；
- 修订必须生成新 version 和 digest；
- `INDETERMINATE` 不得自动转成 negative；
- provider 重试只更新 attempt，不覆盖旧 attempt；
- `COMPLETED_WITH_ZERO_QUESTIONS` 是成功完成，不触发降级造题。

### 10.4 自动重试 重新规划和人工介入

恢复策略由版本化 policy 决定，不由 Agent 在自然语言中自行选择：

| 情况 | 默认动作 |
|---|---|
| 临时网络错误、明确未执行的限流 | 指数退避并在上限内重试 |
| 超时且外部结果未知 | 按 operation key 和 provider capability 解析状态，再决定是否创建新 attempt |
| 参数、权限、schema 或字段越权 | 不重试，记录工程错误 |
| 证据不足或检索方向不合适 | 返回主 Agent 重新规划，受总预算限制 |
| 明确事实冲突、答案不唯一、题面不自包含 | 终止当前 hypothesis 为 REJECTED |
| provider failure 导致验证覆盖不完整 | 当前 hypothesis 为 INDETERMINATE |
| 版本不兼容或 artifact digest 不一致 | 停止自动恢复，进入人工运维检查 |

人工可以修复 provider 配置、批准版本迁移或标注案例，但不能手工把 `REJECTED/INDETERMINATE` 改成 `PASS`。任何人工动作必须产生审计记录；需要改变题目或答案时创建新 hypothesis/version 并重新经过完整门禁。

每个 tool/provider capability 必须声明：

```text
side_effect_class = READ_ONLY | IDEMPOTENT_WRITE | NON_IDEMPOTENT_WRITE
supports_idempotency
supports_status_lookup
safe_to_repeat
```

`TIMEOUT_UNKNOWN_OUTCOME` 的处理依赖该 capability：支持 status lookup 时先查询；只读且 `safe_to_repeat` 时允许创建新 attempt 并记录额外成本；有副作用且无法查状态、也无外部幂等保证时进入 `RECOVERY_REQUIRED`，禁止自动重放。

### 10.5 循环检测和终止条件

除最大轮次、时间、Token 和成本外，协调器还要检测：

- 相同 `subagent_role + goal + input_digest + allowed_tools` 的重复子任务；
- 连续决策的 state digest 未变化；
- 连续 evidence need 语义重复；
- 预期信息增益低于 policy 阈值；
- 同一错误类别连续达到上限；
- 所有候选都已进入终态。

命中循环条件时保存明确 stop reason。不得只依赖模型输出“完成”来结束，也不得在未达最大步数时允许相同行为无限重复。

## 11. 客观性和答案门禁

### 11.1 按事实类型选择验证政策

“数据源无关”不表示所有事实都不需要权威标准。必须区分数据源覆盖范围和规范定义权：

| 事实类型 | 可接受的验证依据 | 额外要求 |
|---|---|---|
| 实体身份和基因定位 | 规范权威来源，必要时独立交叉确认 | 实体、物种和版本明确 |
| 分类和命名 | 具有定义权的标准或本体 | 标准名称及会改变答案的版本必须进入公开题意 |
| 封闭选项 | 题面公开的完整候选集合 | 验证每个选项，不依赖隐藏候选 |
| 确定性计算 | 公开输入、单位和版本化公式 | 计算可重放 |
| 权威枚举 | 具有正式定义权的枚举 | 保存 authority、版本、适用范围和定义权依据 |
| 观察性关联 | 多来源直接证据和冲突审查 | 首期默认不自动发布 |

PrimeKG、CTD、普通 RAG 索引和一次检索结果的收录范围永远不能作为 `completeness_basis`。规范标准型题目只有在标准本身构成事实定义、且必要的标准名称和版本公开时才能通过。

### 11.2 公开题面门禁

- `self_contained`：题面无需隐藏上下文即可解释；
- `source_independent`：题面不询问数据库或检索结果；
- `asks_underlying_fact`：不是“研究是否支持”或“文献是否提到”；
- `entity_unambiguous`：公开实体可唯一识别；
- `scope_complete`：会改变答案的条件全部公开；
- `cardinality_clear`：单答案、多答案或显式选项清楚；
- `strength_accurate`：题面没有语义升级。

### 11.3 答案门禁

- `independently_supported`；
- `competing_answers_checked`；
- `unique_or_closed`；
- `conflict_checked`；
- `direction_consistent`；
- `completeness_basis != NOT_ESTABLISHED`；
- `verification_source_not_only_discovery_artifact`。
- `verification_coverage_report.status == COMPLETE`；
- blind answer 与最终 resolution 的比较结果已保存。

### 11.4 必须拒绝的题目

- 依赖 PrimeKG、CTD、RAG 或某次检索范围才成立；
- 使用“现有研究是否支持”“根据当前证据”等元证据谓词；
- 从 source absence 推导现实否定；
- 开放式单答案但存在竞争答案；
- exact-set 无法证明完整；
- 关联被写成因果、治疗、预防、诊断或预后；
- 事实存在重大争议或随时间变化但缺少必要时间边界；
- 只有生成摘要，没有可回读原文；
- 验证依赖与发现阶段相同的单一非权威记录。

### 11.5 shortcut 分级

`shortcut_available` 不等于事实错误。若事实经独立验证，可以进入一般事实问答集；若不需要目标推理过程，则不得进入 evidence-reasoning 或 query-planner 训练集。

每题分别记录：

```text
objective_fact_valid
evidence_grounded
reasoning_required
training_eligible
```

## 12. 数据保存和输出

### 12.1 持续保存

每个阶段立即保存输入、输出、版本、错误和 digest。不能在最后根据成功结果反推或补造 provenance。

### 12.2 PublicQuestionPackage

只包含：

- question；
- answer；
- concise explanation；
- 允许公开的 citation；
- public schema/version。

### 12.3 InternalAuditPackage

包含：

- target entity；
- retrieval requests 和 attempts；
- EvidenceMaterial 和 EvidenceAnchor；
- CandidateClaim；
- QuestionHypothesis；
- AnswerResolution；
- VerificationPlan、VerificationAttempt 和 VerificationCoverageReport；
- FrozenQuestionContract；
- StructuredDerivationTrace；
- ObjectiveValidationReport；
- rejected candidates；
- model、prompt、skill、tool、data 和 policy version；
- RunManifest、Checkpoint 和 OperationRecord；
- usage、cost 和 provider failure。

### 12.4 可观测性边界

每个决策点记录输入 state digest、选择的 action、候选 action 类型、选择依据代码、预算变化和终态；每个调用记录工具、延迟、Token、成本、错误类别和重试次数。记录结构化决策事实，不保存模型自由形式 chain-of-thought。

Trace 必须能够区分问题来源：模型输出错误、检索质量不足、provider/tool 故障、契约校验失败、协调器拒绝、预算终止和发布门禁拒绝。这样才能定位退化来自模型、prompt、数据、工具还是运行时。

## 13. Web 和 API

### 13.1 Vue 页面

保留现有 `frontend/src/App.vue` 和组件体系，新增或扩展：

- 生成工作台：显示各阶段计数和 0 题结果；
- 任务详情：显示材料、候选事实、候选问题和拒绝原因；
- 审核视图：公开题目与内部证据定位并排展示；
- 质量统计：通过率、拒绝原因、人工推翻率和平均成本。
- 运行状态：当前固定版本、checkpoint、失败类别、重试次数和恢复状态；
- 架构评测：单 Agent、共享上下文 Workflow 与隔离式多 Agent 的质量和成本对照。

### 13.2 Starlette API

现有接口保持兼容，新增或扩展以下资源：

```text
POST /api/generate
GET  /api/generation/{task_id}
GET  /api/tasks/{task_id}/materials
GET  /api/tasks/{task_id}/claims
GET  /api/tasks/{task_id}/hypotheses
GET  /api/questions/{question_id}/audit
```

API 不返回模型私有推理文本，只返回结构化推导链和业务可读原因。

### 13.3 首期运行预算

以下参数进入版本化 policy，不硬编码在 Agent prompt：

```text
max_agent_rounds_per_run
max_llm_calls_per_hypothesis
max_provider_attempts
provider_timeout_seconds
max_materials_per_entity
max_total_tokens_per_run
max_cost_per_run
max_wall_time_per_run
max_parallel_provider_calls
max_retries_by_error_category
max_repeated_decisions
min_expected_information_gain
```

达到任一预算时保存 stop reason。若预算导致验证覆盖不完整，对应 hypothesis 必须为 `INDETERMINATE`；不得通过截断材料换取 PASS。

## 14. 实施依赖图

```text
Step 1 Contracts and acceptance fixtures
  -> Step 2 Material and anchor persistence
       -> Step 3 Subagent contracts fact understanding and question design
            -> Step 4 Answer verification
                 -> Step 5 Frozen contract and realization
                      -> Step 6 Objective review and release gate
                           -> Step 7 Main Agent orchestration and repository integration
                                -> Step 8 Provider migration
                                     -> Step 9 Web and API
                                     -> Step 10 Scale, evaluation and rollout
```

Step 5 的前端无关部分与 Step 2 的 UI 只读投影可以并行开发，但不得绕过依赖提前定义新语义。Step 9 和 Step 10 在 Step 8 接口稳定后可以并行。

执行模式采用 direct mode。每步开始前记录 working tree 状态，保留用户已有修改，禁止 reset 或破坏性清理。

Python 测试统一使用：

```powershell
$env:PYTHONPATH='src'; & 'D:\Anaconda\envs\qa_env\python.exe' -m pytest <tests> -q
```

## 15. 分步实施计划

### Step 1 冻结契约和验收样例

**依赖：** 无  
**执行档位：** 最强推理  
**主要文件：**

- `docs/medical-qa-data-factory/objective-question-methodology.md`
- `docs/medical-qa-data-factory/objective-question-acceptance.md`
- `src/medical_qa_data_factory/generation/evidence_provider.py` 新增接口
- `tests/fixtures/objective_questions/`
- `schemas/v1/`

**上下文：** 开发前必须先消除“source-bounded 也算客观事实”和“LLM 不得提出答案”的旧歧义。

**任务：** 固定本计划第 4、8、9、10、11 节契约和事实类型验证政策；定义统一 `EvidenceProviderPort` 的 request、attempt、material、anchor 和 terminal status；冻结 `operation_key/attempt_id/retry_ordinal`、`runtime_bundle_id/state_revision/subtask_revision`、三维结果模型、provider capability、原子 commit sequence、版本兼容规则和 metric spec schema；建立正例、反例和边界 fixture；明确旧 task type 不自动迁移。

**验证：** schema 导出测试；人工根据文档可判断 source-relative、meta-evidence、竞争答案和 causal overclaim。

**退出标准：** fixture 至少覆盖第 16 节全部案例，且每个案例有唯一预期 verdict/reason。

**回滚：** 只回退文档、schema draft 和 fixture。

### Step 2 实现材料和原文定位持久化

**依赖：** Step 1  
**执行档位：** 默认  
**主要文件：**

- `src/medical_qa_data_factory/evidence_enrichment/contracts.py`
- `src/medical_qa_data_factory/generation/repository.py`
- `src/medical_qa_data_factory/generation/path_persistence.py`
- `tests/test_generation_path_persistence.py`
- `tests/test_generation_repository.py`

**上下文：** 后续事实和答案必须能够回读到原始材料，不能只保存模型摘要。

**任务：** 实现 `EvidenceMaterial`、`EvidenceAnchor`、attempt 和 content hash；保存 provider terminal status；禁止覆盖旧 attempt。

原始内容必须保存为不可变 blob 或版本化对象；同时实现 `EntityResolution`，未关闭的实体歧义不得进入 claim。

**验证：**

```powershell
$env:PYTHONPATH='src'; & 'D:\Anaconda\envs\qa_env\python.exe' -m pytest tests/test_generation_path_persistence.py tests/test_generation_repository.py tests/test_evidence_contracts.py -q
```

**退出标准：** 任一正式 anchor 可回读原文并校验 hash；provider failure 与 empty 明确区分。

**回滚：** 新 artifact type 独立注册，旧 repository reader 保持不变。

### Step 3 实现主子调度契约 事实理解和问题设计子 Agent

**依赖：** Step 2  
**执行档位：** 最强推理  
**主要文件：**

- `src/medical_qa_data_factory/generation/fact_exploration.py` 新增
- `src/medical_qa_data_factory/generation/question_design.py` 新增
- `src/medical_qa_data_factory/generation/subagent_contracts.py` 新增
- `src/medical_qa_data_factory/generation/exploration.py`
- `src/medical_qa_data_factory/generation/react_graph.py`
- `src/medical_qa_data_factory/llm/contracts.py`
- `tests/test_objective_fact_exploration.py` 新增
- `tests/test_question_design_subagent.py` 新增
- `tests/test_subagent_contracts.py` 新增

**上下文：** 事实理解和问题设计属于两个子 Agent。前者不能看到题目目标，后者只能读取已固化 claim。主 Agent 只能通过 `SubagentTask` 调用它们。

**任务：** 实现 `SubagentTask`、`SubagentResult` 和 role capability 校验；事实理解子 Agent 只生成并固化 `CandidateClaim`；问题设计子 Agent 读取固化 claim 后生成 `QuestionHypothesis`；要求每个 claim 绑定 anchor；限制 claim strength；按语义身份去重；两个子 Agent 使用隔离 prompt、context、tool policy 和 trace；对外部文本应用第 8.1 节的数据边界和 prompt injection 防护。

**验证：**

```powershell
$env:PYTHONPATH='src'; & 'D:\Anaconda\envs\qa_env\python.exe' -m pytest tests/test_objective_fact_exploration.py tests/test_question_design_subagent.py tests/test_subagent_contracts.py tests/test_generation_exploration.py tests/test_generation_react_graph.py -q
```

**退出标准：** 非结构化 fixture 能提取限定、否定和关系强度；事实理解子 Agent 看不到题目候选；问题设计子 Agent 不能回写 claim；无 anchor 的 claim 不能进入 hypothesis；proposed answer 不会被读取为 gold；非法跨角色字段和工具调用被拒绝。

**回滚：** 通过 feature flag 停用新 exploration node。

### Step 4 实现独立答案验证 Agent

**依赖：** Step 3  
**执行档位：** 最强推理  
**主要文件：**

- `src/medical_qa_data_factory/generation/objective_answer_verification.py` 新增
- `src/medical_qa_data_factory/generation/answer_verification_subagent.py` 新增
- `src/medical_qa_data_factory/generation/verification.py`
- `src/medical_qa_data_factory/generation/blind_verification.py`
- `tests/test_objective_answer_verification.py` 新增
- `tests/test_answer_verification_subagent.py` 新增

**上下文：** 候选答案可以由 LLM 提出，但正式答案必须通过隔离上下文和独立材料确认。

**任务：** 实现 role-typed `AnswerVerificationSubagent` adapter，只接受对应 `SubagentTask` schema 和验证工具白名单；实现 `BlindAnswerQuery`；完成盲态求解、结果比较和反例搜索三遍流程；实现 `VerificationPlan`、`VerificationAttempt`、`VerificationCoverageReport`、竞争答案搜索、冲突检查、事实类型 completeness policy 和 VERIFIED/REJECTED/INDETERMINATE 状态；保存独立 subagent trace 和 `SubagentResult`。

**验证：**

```powershell
$env:PYTHONPATH='src'; & 'D:\Anaconda\envs\qa_env\python.exe' -m pytest tests/test_objective_answer_verification.py tests/test_answer_verification_subagent.py tests/test_generation_world_evaluation.py tests/test_generation_task_validation.py -q
```

**退出标准：** 所有调用都有 role-typed task/result/trace；非法字段和非白名单工具被拒绝；盲态调用看不到 proposed answer 和 discovery anchor；多答案不被压成单答案；source absence 不形成 negative；只有 discovery artifact 时不能 VERIFIED；显式选项可形成闭合范围；coverage 不完整、provider failure 或预算耗尽时为 INDETERMINATE。

**回滚：** verification 保持 analysis-only，不向 task compiler 输出。

### Step 5 冻结问题契约并生成题面

**依赖：** Step 4  
**执行档位：** 默认  
**主要文件：**

- `src/medical_qa_data_factory/generation/objective_question_contracts.py` 新增
- `src/medical_qa_data_factory/generation/writing.py`
- `src/medical_qa_data_factory/generation/prompt_writing.py`
- `src/medical_qa_data_factory/generation/task_realization.py`
- `tests/test_objective_question_realization.py` 新增

**上下文：** 最终题面只能表达验证后的问题语义，不能让 Writer 再次决定答案。

**任务：** 实现 `FrozenQuestionContract`、`StructuredDerivationTrace` 和 Question Design Subagent 的题面实现模式；该模式只接收冻结契约，不读取候选设计自由文本；禁止 source-relative/meta-evidence wording；检查 round trip。

**验证：**

```powershell
$env:PYTHONPATH='src'; & 'D:\Anaconda\envs\qa_env\python.exe' -m pytest tests/test_objective_question_realization.py tests/test_prompt_question_writing.py tests/test_generation_task_realization.py -q
```

**退出标准：** Writer 改写不改变 scope、cardinality 和 claim strength；推导链可由 artifact digest 重放；不保存自由 CoT。

**回滚：** 新 Writer 只处理 objective-question-v1。

### Step 6 接入客观题 Skill 和发布门禁

**依赖：** Step 5  
**执行档位：** 最强推理  
**主要文件：**

- `skills/objective-question-validator/SKILL.md`
- `src/medical_qa_data_factory/generation/objective_validation.py` 新增
- `src/medical_qa_data_factory/generation/release_review_subagent.py` 新增
- `src/medical_qa_data_factory/generation/task_release.py`
- `src/medical_qa_data_factory/validation_applicability.py`
- `tests/test_objective_question_validation.py` 新增
- `tests/test_release_review_subagent.py` 新增
- `tests/test_generation_task_release.py`

**上下文：** Skill 已存在，但尚未接入运行时、输入 digest、版本失效和发布门禁。

**任务：** 实现 role-typed `ReleaseReviewSubagent` adapter；从项目 skill 路径打包和加载 skill；将 skill content hash 纳入 validator version；分别实现 `PublicQuestionAuditTask` 和 `AnswerEvidenceAuditTask` 的字段可见性策略、独立 context 和 tool policy；第一阶段结果固化后才能创建第二阶段任务；实现严格 JSON parser 和确定性 report merger；只有两阶段均 PASS 才允许最终 PASS；skill、输入、证据或验证政策变化时旧报告失效；非 JSON、超时、解析失败和模型拒绝统一落为带具体原因的 INDETERMINATE。

**验证：**

```powershell
$env:PYTHONPATH='src'; & 'D:\Anaconda\envs\qa_env\python.exe' -m pytest tests/test_objective_question_validation.py tests/test_release_review_subagent.py tests/test_generation_task_release.py tests/test_validation_applicability.py -q
```

**退出标准：** public-only task 无法访问答案、contract、evidence 和内部 metadata；两阶段都有 role-typed task/result/trace；任一阶段非 PASS 时最终不能 PASS；REJECT/INDETERMINATE 无法导出；伪造 PASS、旧 digest 或缺 evidence audit 均被门禁拒绝。

**回滚：** objective-question-v1 release flag 默认关闭，旧 release 不受影响。

### Step 7 接入题目生产主 Agent 协调器 状态和仓库

**依赖：** Step 6  
**执行档位：** 默认  
**主要文件：**

- `src/medical_qa_data_factory/generation/state.py`
- `src/medical_qa_data_factory/generation/routing.py`
- `src/medical_qa_data_factory/generation/graph.py`
- `src/medical_qa_data_factory/generation/main_agent.py` 新增
- `src/medical_qa_data_factory/generation/subagent_dispatch.py` 新增
- `src/medical_qa_data_factory/generation/runtime_contracts.py` 新增
- `src/medical_qa_data_factory/generation/operation_repository.py` 新增
- `src/medical_qa_data_factory/generation/service.py`
- `src/medical_qa_data_factory/generation/repository.py`
- `tests/test_objective_question_workflow.py` 新增
- `tests/test_question_production_main_agent.py` 新增
- `tests/test_generation_runtime_safety.py` 新增
- `tests/test_generation_version_recovery.py` 新增

**上下文：** 各子 Agent 和 artifact 已定义，需要由一个受确定性协调器托管的主 Agent 进行规划和调度。主 Agent 负责语义决策，协调器负责权限、状态、预算和门禁。

**任务：** 实现 `GenerationGoal`、`RunManifest` 和 `MainAgentDecision`；建立 capability registry 和 `SubagentTask` dispatcher；增加 run、subtask 和 hypothesis 级状态；实现协调器单写、单调 state revision、child result slot、operation/attempt 分离、input digest 和去重提交；将 state transition、artifact references、budget ledger、operation terminal 和 commit sequence 原子保存，跨队列使用 outbox/inbox；校验每次主 Agent action 的合法性；保存 `OperationRecord`、阶段计数和拒绝原因；实现 `EvidenceNeed -> RetrievalAttempt` 受控循环、按错误类别重试、循环检测、预算和 stop reason；确保主 Agent 无法读取或注入盲态禁止字段；实现 checkpoint reload、未知外部调用状态核对、同 bundle 恢复和 successor run 迁移。

**验证：**

```powershell
$env:PYTHONPATH='src'; & 'D:\Anaconda\envs\qa_env\python.exe' -m pytest tests/test_objective_question_workflow.py tests/test_question_production_main_agent.py tests/test_generation_runtime_safety.py tests/test_generation_version_recovery.py tests/test_generation_graph.py tests/test_generation_repository.py tests/test_generation_replay.py -q
```

**退出标准：** 主 Agent 能按 evidence need 调度正确子 Agent；非法越权、修改固化 artifact、向盲态验证注入 proposed answer 和绕过门禁均被协调器拒绝；并发 child result 均可进入预登记 slot，互斥聚合转换只有一个成功；重复提交不会覆盖状态、重复写 artifact 或重复扣预算；checkpoint 对应单一 commit sequence，无撕裂状态；0 题任务完成状态正确；同一 runtime bundle 重放得到相同 artifact IDs；兼容版本可以恢复，不兼容版本被阻塞；provider failure 可恢复且不被当作阴性事实。

**回滚：** feature flag 切回 legacy graph。

### Step 8 迁移 PrimeKG CTD PubMed 和 RAG 适配层

**依赖：** Step 7  
**执行档位：** 默认  
**主要文件：**

- `src/medical_qa_data_factory/generation/primekg_ctd_pubmed.py`
- `src/medical_qa_data_factory/generation/ctd_direct_evidence.py`
- `src/medical_qa_data_factory/generation/literature_execution.py`
- `src/medical_qa_data_factory/generation/rag_evidence.py` 新增
- `src/medical_qa_data_factory/generation/material_retrieval_subagent.py` 新增
- `src/medical_qa_data_factory/evidence_enrichment/service.py`
- `tests/test_rag_evidence.py` 新增
- `tests/test_material_retrieval_subagent.py` 新增

**上下文：** 数据源适配层只产生材料和 anchor，不再直接产生 disease task 或 gold answer。

**任务：** 实现 role-typed `MaterialRetrievalSubagent` adapter，可按 provider 创建隔离实例；每个实例只接受对应 `RetrievalSubtask`、provider 工具白名单和预算；按 Step 1 的 `EvidenceProviderPort` 实现各 adapter，并声明 `side_effect_class`、`supports_idempotency`、`supports_status_lookup` 和 `safe_to_repeat`；拆除“边到题目”的直连；统一 terminal status；保留 query、snapshot、span 和 hash；RAG 必须保存原始文档 anchor，检索片段摘要不能单独验证答案；保存每个实例的 `SubagentResult` 和独立 trace；创建脱敏离线 fixture。

**验证：**

```powershell
$env:PYTHONPATH='src'; & 'D:\Anaconda\envs\qa_env\python.exe' -m pytest tests/test_primekg_ctd_pubmed_pipeline.py tests/test_ctd_direct_evidence.py tests/test_evidence_enrichment.py tests/test_generation_primekg_integration.py tests/test_rag_evidence.py tests/test_material_retrieval_subagent.py -q
```

**退出标准：** 四类来源都通过 MaterialRetrievalSubagent 调用并产生 role-typed task/result/trace；provider 实例之间上下文隔离；非法 provider 工具调用被拒绝；统一材料契约可持久化；任何来源都不能直接写 gold；离线测试不依赖外网。

**回滚：** provider adapter version 分开注册，legacy service 保留一个版本周期。

### Step 9 更新 Vue 页面和 Starlette API

**依赖：** Step 8  
**执行档位：** 默认  
**主要文件：**

- `frontend/src/App.vue`
- `frontend/src/components/TaskRun.vue`
- `frontend/src/components/ExecutionTrace.vue`
- 新增 audit/quality 组件
- `src/medical_qa_data_factory/web/app.py`
- `src/medical_qa_data_factory/web/view_models.py`
- `tests/test_web.py`

**上下文：** 页面需要解释系统正在做什么和为什么没有题，而不是只显示底层错误码。

**任务：** 展示阶段进度、候选数量、业务拒绝原因、0 题结果和 audit view；public 页面不泄漏内部字段；内部 audit API 和页面要求 reviewer 权限，并记录访问审计；保持现有 Vue 技术栈。

**验证：**

```powershell
$env:PYTHONPATH='src'; & 'D:\Anaconda\envs\qa_env\python.exe' -m pytest tests/test_web.py tests/test_web_groups.py -q
Push-Location frontend
npm test -- --run
npm run build
Pop-Location
```

**退出标准：** 用户只选实体即可运行；任务详情能解释每个阶段；审核页可查看 anchor 和结构化推导链；public/hidden 无泄漏。

**回滚：** capability flag 切回旧 workspace projection。

### Step 10 规模化评估和切换默认入口

**依赖：** Step 8；可与 Step 9 并行  
**执行档位：** 最强推理负责评估设计，默认档负责运行  
**主要文件：**

- `eval/objective_questions/`
- `src/medical_qa_data_factory/generation/ablation_runner.py`
- `src/medical_qa_data_factory/generation/ranking.py`
- `src/medical_qa_data_factory/generation/task_release.py`
- 相关评估测试和 manifest

**上下文：** 不能以“能生成题”作为上线标准，需要测误放行、误拒绝、成本和可复现性。

**任务：** 建立不少于 100 条的人工金标统计集，并把第 16 节 zero-tolerance 反例作为独立的发布阻塞回归集；建立三种架构基线：单 Agent 完整生成、共享上下文固定 Workflow、当前隔离式多 Agent；测试多来源和单来源 ablation，以及去掉盲态验证、去掉发布复核、共享上下文等组件消融；执行 provider timeout、unknown-outcome、部分 RAG 返回、LLM 非法 JSON、stale skill report、并发重复提交、旧版本恢复、checkpoint 中断恢复和重复材料等失败注入；灰度切换默认入口。

对照实验分为两类，不能混用结论：

1. **架构隔离实验：** 三种方案使用相同的材料快照、模型族、工具、事实政策、发布门禁、最大候选数和题目类型；只改变角色划分、上下文可见性和调度组织。使用配对样本，固定温度/随机种子策略并重复运行，报告均值、方差和置信区间。
2. **生产方案实验：** 每种方案使用其合理运行预算，比较最终质量、成本和 P50/P95 延迟，用于部署决策，但不把差异全部归因于多 Agent 架构。

评测前在版本化 `evaluation_policy` 中预注册：样本、运行次数、随机性策略、主要指标、成本上限和架构选择规则，防止看到结果后修改成功标准。隔离式多 Agent 只有在 zero-tolerance 不退化、统计性 false acceptance 不劣于基线，并且预注册的答案污染/题面泄漏/人工推翻指标至少一项改善且成本增量不超过上限时，才能作为默认架构；否则选择满足质量要求的更简单方案。

分层评测指标：

| 层级 | 核心指标 |
|---|---|
| Material Retrieval | 有效材料率、anchor 完整率、重复材料率、provider failure/timeout 率 |
| Fact Understanding | claim 精确率、限定条件丢失率、关系强度误判率、实体歧义关闭率 |
| Question Design | 客观候选率、隐藏前提率、meta-evidence/source-relative 比例、重复题率 |
| Answer Verification | 错误答案误通过率、竞争答案发现率、coverage 完整率、冲突发现率 |
| Release Review | 不合格题误放行率、合格题误拒绝率、public/hidden 泄漏率 |
| End-to-end | false acceptance、false rejection、每道合格题成本、P50/P95 延迟、0 题率、恢复成功率、人工推翻率 |
| Architecture | 相对单 Agent/共享上下文基线的质量增益、成本增量和延迟增量 |

每个指标必须有机器可读 `MetricSpec`：

```text
metric_id + version
population
numerator
denominator
exclusions
label_source + adjudication_protocol
aggregation_method
confidence_interval_method
minimum_sample_size
```

“有效材料率”“客观候选率”“答案污染”“恢复成功率”等名称不能直接进入报告，必须先按上述字段定义分母、排除规则和标注来源。统计集用于估计真实风险并报告置信区间；zero-tolerance 回归集只用于阻塞已知严重错误，不能代替风险估计。

`LLM-as-a-Judge` 只能作为辅助信号。发布门禁和 zero-tolerance 指标必须以确定性规则、人工金标和可复核证据为准。

**验证：** 全量 Python 测试、前端测试、schema export、离线 golden run 和 replay diff。

**退出标准：** zero-tolerance 回归集误放行为 0；统计性人工金标集报告 false acceptance/false rejection 的点估计和置信区间，观察到 0 次误放行也不得表述为风险为 0；false rejection 目标不高于 10%，样本不足时不得宣称达标；隔离式多 Agent 相比基线的质量收益和额外成本有配对实验报告并满足预注册选择规则，若无可证明收益则回退到更简单架构；人工抽查可解释；0 题率、拒绝原因、失败类别和恢复结果可观测；新入口可通过 flag 回滚。

**回滚：** 默认入口恢复 legacy；保留新 artifact 供审计，不删除运行记录。

## 16. 必备验收案例

| ID | 案例 | 预期 |
|---|---|---|
| OBJ-01 | “在 PrimeKG 中 X 与哪种疾病有关” | `SOURCE_RELATIVE` |
| OBJ-02 | “现有研究是否支持 X 与 Y 有关” | `META_EVIDENCE_QUESTION` |
| OBJ-03 | “X 与哪种疾病有关”且存在多个答案 | `ANSWER_NOT_UNIQUE` |
| OBJ-04 | 只因隐藏 CTD filter 才唯一 | `MISSING_PUBLIC_CONTEXT` + `SOURCE_RELATIVE` |
| OBJ-05 | 关联证据被写成“导致” | `CAUSAL_OVERCLAIM` |
| OBJ-06 | PubMed 未评估或 provider 失败 | `INDETERMINATE` |
| OBJ-07 | 从数据库未收录推导“不存在关系” | `UNSUPPORTED_ANSWER` |
| OBJ-08 | BRCA1 人类染色体位置且独立权威来源一致 | `PASS` |
| OBJ-09 | 胰岛素分泌细胞类型且实体明确 | `PASS` |
| OBJ-10 | 题面公开四个候选且答案唯一 | `PASS` |
| OBJ-11 | exact-set 只有采样材料、无完整性依据 | `INCOMPLETE_CLOSED_SET` |
| OBJ-12 | 事实正确但 KG 可直接回答 | fact dataset 可用；reasoning dataset 不可用 |
| OBJ-13 | 题面遗漏会改变答案的人群或物种 | `MISSING_PUBLIC_CONTEXT` |
| OBJ-14 | 验证只复读 discovery agent 摘要 | `INSUFFICIENT_VALIDATION_EVIDENCE` |
| OBJ-15 | 同语义不同措辞 | 同一 semantic identity，不重复发布 |
| OPS-01 | 同一 committed operation 因消息重复被提交两次 | 复用原结果，不重复 artifact 和预算 |
| OPS-02 | 子 Agent 基于旧 expected state revision 提交互斥转换 | `STALE_INPUT`，不得覆盖当前状态 |
| OPS-03 | provider 超时但外部结果未知 | 按 capability 查询状态；不可查询的副作用操作进入 `RECOVERY_REQUIRED` |
| OPS-04 | 进程在验证完成后、原子 commit 前后分别崩溃 | 按 `last_committed_sequence` 恢复，不产生撕裂状态或重复预算 |
| OPS-05 | 运行中部署了新 prompt、skill 或代码 | 当前 run 使用可加载的原 runtime bundle；迁移创建 successor run |
| OPS-06 | checkpoint 与 artifact digest 不一致 | 阻塞自动恢复并进入人工运维检查 |
| OPS-07 | 主 Agent 连续生成相同 evidence need | 循环检测停止并记录 stop reason |
| OPS-08 | 两个并发检索子任务返回合法材料 | 均进入各自 child slot；聚合状态只提交一次 |
| OPS-09 | 可重试 attempt 失败后再次执行 | operation key 不变、attempt id 递增，不复用失败结果 |
| OPS-10 | 旧 runtime bundle 已无法加载 | `VERSION_INCOMPATIBLE`，不得静默改用新版本 |
| EVAL-01 | 同一金标集比较三种架构 | 固定模型、数据和预算，输出质量/成本/延迟报告 |
| EVAL-02 | 去掉盲态验证或共享全部上下文 | 能量化答案污染和误放行变化 |

## 17. 完成定义

本改造完成需要同时满足：

- 一个题目生产主 Agent 和五类子 Agent 的职责在运行 trace 中可区分；
- 主 Agent 的每次调度都有 `MainAgentDecision`，五类子 Agent 的每次调用都有 role-typed `SubagentTask`、`SubagentResult` 和独立 trace；
- 发布复核由真正隔离的 public-only 和 answer-evidence 两个任务组成，第一阶段不可见答案和内部证据；
- LLM 可以从非结构化材料提出事实和问题，但不能自证答案；
- 所有正式题都有原文 anchor、独立答案验证和冻结问题契约；
- 题面和答案通过 `objective-question-validator`；
- `REJECT`、`INDETERMINATE` 和 stale PASS 无法发布；
- public 和 audit package 严格隔离；
- 结构化推导链可重放，不包含自由 chain-of-thought；
- PrimeKG、CTD、PubMed 和 RAG 只作为材料源；
- 0 题任务能够正常完成并解释原因；
- Vue 页面能够展示阶段、结果和业务拒绝原因；
- 旧 artifact 语义保持不变；
- 每次运行具有不可变 RunManifest，跨版本恢复有显式兼容或迁移结论；
- 协调器是正式状态唯一写入者，并发 child slot、单调 revision、原子 commit sequence 和重复提交通过一致性测试；
- 引用中的旧 runtime bundle、prompt、skill、schema 和 policy 可以按 digest 加载，或明确进入版本不兼容状态；
- 各角色失败类别、重试上限、重新规划和终止规则可观测且可测试；
- 单 Agent、共享上下文 Workflow 和隔离式多 Agent 已完成预注册的配对对照评测，并能说明复杂度是否值得；
- 全量测试、离线 golden fixture 和 replay 验证通过。

## 18. 反模式目录

- 先从图谱边选定答案，再为该答案寻找文献；
- 把 EvidenceMaterial 命名或处理成已确认事实；
- 用“现有研究是否支持”包装不稳定关系；
- 让 Writer 根据 discovery evidence 自行选择 gold；
- 让 validator 只查看生成 Agent 的摘要；
- 用内部 corpus closure 冒充现实世界 truth closure；
- 用 source absence 推导 negative answer；
- 用增加 qualifier 的方式事后制造唯一答案；
- 把模型自由解释保存为可审计推理；
- 只记录成功题，丢弃失败候选和 provider attempt；
- 将 INDETERMINATE 自动重试到得到 PASS；
- 让多个 Agent 共同修改一个无版本的全局 state；
- 仅靠分布式锁解决旧结果覆盖和重复提交；
- 把 runtime bundle 版本、状态并发修订号和 subtask 修订号混成一个 `run_version`；
- 把同一个幂等键的失败 attempt 永久当成可复用结果；
- 让 run 级 revision 变化丢弃仍然合法的并行 child result；
- 分开保存状态、artifact、预算和 operation 终态，再把它们拼成伪 checkpoint；
- 用 Agent 投票代替事实类型验证政策和证据仲裁；
- 在运行中静默切换模型、prompt、skill、schema 或 provider adapter；
- 把进程超时直接当成外部调用未执行并原样重试；
- 只用 LLM-as-a-Judge 或最终任务成功率证明多 Agent 有效；
- 未与单 Agent/共享上下文 Workflow 对照就宣称多 Agent 更优；
- 首期把每个子 Agent 拆成独立网络服务而没有并发、扩缩容或故障隔离依据；
- 为完成目标题数降低门禁；
- 为此次改造迁移到 React；
- 直接改变旧 v2 schema 或 release 的解释。

## 19. 计划变更协议

任何语义、契约或步骤变化必须记录：

```text
change_id
requested_change
reason
affected_invariants
affected_steps
schema_or_version_change
new_dependencies
regression_risk
verification_update
rollback_point
```

允许：拆分过大步骤、增加 provider、增加稳定事实类型、将不成熟题型降级为研究候选、根据真实材料调整 fixture。

禁止：删除客观性门禁换取题量、把未评估视为否定、让 Agent 绕过发布门禁、覆盖旧 snapshot、在无迁移记录时改变公开题目语义。

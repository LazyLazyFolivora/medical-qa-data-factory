# medical-qa-data-factory 开发架构

> 状态：当前实施依据  
> 项目：`medical-qa-data-factory`  
> 更新日期：2026-10-26

## 1. 产品定义

`medical-qa-data-factory` 是一个生物医药任务数据工厂。

它接收一个种子实体，在可追溯的知识图谱、文献和其他生物医药数据源中收集证据，围绕该实体构造多种可验证问题，并输出：

1. 给 Query Planner 求解的自然语言问题；
2. 不向 Planner 暴露的标准答案和完成条件；
3. 用于事后计算“某次动作带来了多少信息增益”的评估元数据。

本项目不负责预先规定 Planner 应执行什么动作，也不负责实现文献搜索、PrimeKG 查询或 CTD 查询引擎本身。它通过 provider adapter 调用这些外部能力，统一证据表示，并据此构造和验证任务。

一句话概括：

```text
从实体出发，先构造可验证的事实任务，再把任务表达成自然问题。
```

## 2. 最终交付边界

### 2.1 本项目负责

- 解析并规范化种子实体；
- 从已配置 provider 收集候选证据；
- 将图边、数据库记录和文献结论规范化为统一 claim；
- 从证据组合中提出多个候选任务；
- 计算并验证答案、信息需求和完成条件；
- 生成自然语言问题并验证题面没有改变任务语义；
- 发布 Planner 输入、隐藏评估目标、证据目录和版本 manifest。

### 2.2 本项目不负责

- 实现 PrimeKG、CTD、PubMed 或 RAG 的底层检索服务；
- 运行 Query Planner 的交互式求解循环；
- 枚举某个状态下所有实际可用动作；
- 给出 `correct_action`、`best_action` 或固定解题路线；
- 伪造动作成本、动作收益或最优轨迹；
- 训练动作价值模型或停止策略。

后五项属于 `query-planner` 及其运行环境。本项目提供的是计算收益所需的任务真值，而不是收益标签本身。

### 2.3 实施层级

本文描述完整目标架构，但不是所有模块都属于第一版发布门槛：

- **MVP**：证据搜索、候选生成、针对性验证、最终统一验证、问题与隐藏评估目标发布；
- **P1**：结构化 EvidenceGap、搜索优先级、多源 claim 等价和搜索饱和判断；
- **P2**：完整 SearchFrontier、逐轮 checkpoint、断点恢复、增量发布和依赖失效追踪。

开发和验收必须按这一层级执行。不得因为 P1/P2 尚未完成而阻塞满足 MVP 标准的数据集试运行。

## 3. 核心设计结论

### 3.1 主流程是证据驱动的造题循环

系统不得先让模型自由写一道题，再事后猜答案和补元数据。它也不应只搜索一次、生成一次就结束。正式主流程是：

```text
SeedEntity
  -> 初始化 GenerationEpisode
  -> 搜索一批证据
  -> EvidenceNormalization
  -> 更新 EvidenceBundle 和 SearchFrontier
  -> 判断当前证据能否构造并验证新任务
       ├─ 能：生成 ValidatedTask，去重后发布题目
       └─ 不能：记录具体 EvidenceGap
  -> 判断下一步
       ├─ 有可解决的 gap 且搜索未充分：继续搜索
       ├─ 目标已完成：停止
       ├─ 定义范围内搜索已穷尽：停止
       ├─ 搜索已饱和：停止
       ├─ 预算耗尽：停止
       └─ provider 不可用：停止或等待
```

循环内部的一轮仍遵循严格顺序：

```text
EvidenceCollection
  -> EvidenceNormalization
  -> TaskCandidate
  -> TaskCompiler
  -> TaskValidator
  -> ValidatedTask
  -> QuestionRealizer
  -> TextSemanticVerifier
```

每轮可以积累候选题，但 MVP 在 episode 结束时统一使用最终 EvidenceBundle 验证并发布。这样不要求第一版实现实时撤回和完整依赖追踪。增量发布与自动失效处理属于后续能力。

自由出题 Agent 可以参与 `TaskCandidate` 生成，用于发现更有研究意义的问题；但 Agent 只能提案，不能自行认证答案、唯一性、证据完整性、搜索充分性或任务可完成性。

### 3.2 “证据不足”和“还没搜全”必须分开

系统不能证明现实世界中绝对“没有证据”，只能对本次 `GenerationSpec` 规定的来源、查询步骤和预算作出操作性判断：

- `search_incomplete`：仍有与 EvidenceGap 匹配的合法搜索方向尚未执行；
- `required_searches_completed`：本次规格要求的搜索步骤已完成，仍无法满足需求；
- `search_saturated`：连续若干轮没有新增有效 claim 或新任务；该能力在 P1 实现；
- `budget_exhausted`：可能仍有搜索方向，但本次预算不足；
- `provider_unavailable`：外部来源失败，不能解释为“没有证据”；
- `question_targets_met`：已经达到配置的题目数量、类型和多样性目标。

因此，MVP 不要求证明所有可能搜索方向都已穷尽，只要求如实说明“规定搜索已经完成”还是“因为预算或故障没有完成”。预算耗尽、工具失败和搜索未完成不能写成“没有证据”。

### 3.3 有限证据生成，定义世界验收

“有限证据生成”是指：候选任务可以由一条边、一段文献、一个局部子图或 Agent 的有限探索启发产生。

“定义世界验收”是指：候选不能仅凭这块局部材料发布，必须在任务声明的 `ValidationEnvironment` 中重新检查。这个环境可能包括：

- 冻结的 PrimeKG 快照；
- 固定版本的 CTD 或其他结构化数据库；
- 带检索日期、查询式和文献标识的文献集合；
- 上述来源的组合。

这里的“世界”是一个可重放、边界明确的操作性验证环境，不等于人类全部生物医药知识。系统必须分别声明两种有效性：

- **执行有效性**：在指定数据快照和查询语义下，答案可重复计算；
- **生物医药有效性**：题面所表达的关联、方向、极性、因果强度、适用人群和时间范围得到证据支持。

PrimeKG 中只有一个匹配结果，只能证明“在该快照和查询定义下唯一”；如果题面声称现实世界中绝对唯一，则还需要适合该声明的外部证据，否则必须收窄措辞或拒绝。

### 3.4 任务描述信息需求，不描述固定动作

同一个事实可以通过图谱查询、文献检索、缓存读取或未来新增工具获得。因此，任务真值应描述“需要知道什么”，而不是“必须调用哪个工具”。

正确建模：

```text
requirement: 证明 Drug A 与 Protein X 存在 targets 关系
acceptable evidence: 任意满足该 claim 语义和证据质量门槛的证据
```

错误建模：

```text
correct_action: relation_expand(Drug A, targets)
expected_route: KG -> literature -> finish
```

只有当题目本身明确要求某种来源，例如“根据随机对照试验”，来源类型才是任务语义的一部分；否则不能把生成题目时使用的来源变成 Planner 的强制路线。

### 3.5 动作增益由状态变化计算，不由出题器臆造

对于隐藏信息需求集合 `R` 和 Planner 当前证据状态 `S`，评估器计算：

```text
covered(S) = 在状态 S 中已由有效证据满足的 requirements
gain(a, S) = covered(S_after) - covered(S_before)
cost(a, S) = 执行器记录的真实成本向量
```

因此，下游训练样本应来自真实或受控 rollout：

```text
(state_before, candidate_action_metadata)
    -> (observed_gain, observed_cost, state_after)
```

本项目只提供 `R`、匹配规则和任务完成规则。`state_before`、实际动作、observation、延迟、token、调用费用和失败信息必须由 Query Planner 运行时记录。

### 3.6 一个最小完整示例

假设验证 fixture 中存在两个已核验 claim：

```text
Drug A --targets--> Protein X
Protein X --associated_with--> Disease D
```

且在声明的验证范围内，`Protein X` 是同时满足两个条件的唯一蛋白。平台可以构造：

```yaml
question: Drug A 所作用且与 Disease D 相关的蛋白是哪一种？
gold_answer: Protein X
requirements:
  - req-1: Drug A targets Protein X
  - req-2: Protein X associated_with Disease D
completion_rule:
  all_of: [req-1, req-2]
```

这道题不规定先查图谱还是先搜文献。一次 KG 动作若新增 `req-1`，它获得对应 coverage gain；一次文献动作若新增 `req-2`，它获得另一部分 gain；无关或重复动作的 gain 为零。动作的延迟、费用和失败状态由真实执行器记录。

如果验证范围中还有 `Protein Y` 同时满足两个条件，这个单答案候选必须拒绝或改成边界明确的集合题，不能因为生成器最先发现 `Protein X` 就把它设为唯一答案。

## 4. 总体架构

```text
Seed Entity -> Entity Resolver -> Generation Orchestrator
                                      |
                                      v
                           Search Planner / Frontier
                                      |
                                      v
                         Evidence Provider Adapters
                         KG / literature / CTD / ...
                                      |
                                      v
                 Evidence Normalizer -> EvidenceBundle
                                      |
                                      v
                Candidate Sources -> Compiler -> Validator
                                      |
                    ┌─────────────────┴─────────────────┐
                    v                                   v
             ValidatedTask                         EvidenceGap
                    |                                   |
                    v                                   v
          Writer + Text Verifier          Search Sufficiency Judge
                    |                                   |
                    v                                   |
          去重并加入本轮输出                 继续搜索 / 明确停止
                    |                                   |
                    └──────────> 更新 EpisodeState <───┘
                                      |
                         未停止时进入下一轮搜索
                                      |
                                      v
                    ┌─────────────────┼──────────────────┐
                    v                 v                  v
      planner_questions.jsonl  evaluation_targets.jsonl  evidence_catalog.jsonl
```

`GenerationOrchestrator` 只管理造题过程。它既可以执行 `GenerationSpec` 允许的探索性搜索，也可以为了具体 EvidenceGap 执行验证性搜索。该决策是生产日志，不是 Query Planner 训练数据中的正确动作。

MVP 至少保存 episode 的输入、最终 EvidenceBundle、候选、验证结果和停止原因。逐轮 checkpoint 与崩溃恢复属于 P2。

## 5. 核心数据契约

以下为语义契约。字段名称可以随代码风格调整，但边界不能被合并或省略。

### 5.1 `EntityRef`

```yaml
canonical_id: "..."
name: "..."
entity_type: drug | gene | protein | disease | pathway | ...
namespace: "..."
aliases: []
resolution_status: resolved | ambiguous | unresolved
```

同名异实体不得静默合并。用于题面的名称必须能在不泄漏答案的前提下唯一指向目标实体。

### 5.2 `ValidationEnvironment`

描述任务在哪些来源和版本上被验收：

```yaml
environment_id: "..."
created_at: "..."
providers:
  - provider_id: primekg
    snapshot_id: "..."
    schema_version: "..."
    manifest_hash: "..."
  - provider_id: literature
    corpus_or_index_version: "..."
    retrieval_date: "..."
    query_manifest_hash: "..."
completeness_scope: "..."
```

任何无法固定版本、无法保存来源或无法说明截断状态的数据，均不能用于产生已验证任务。

### 5.3 `EvidenceRecord`

保存 provider 原始 observation 的可审计引用：

```yaml
evidence_id: "..."
provider_id: "..."
source_type: kg_edge | database_record | literature_passage
source_locator: {}
retrieved_at: "..."
content_hash: "..."
raw_ref: "..."
```

文献证据至少要能定位到文献标识和原文 span；结构化证据至少要能定位到快照中的记录或边。

### 5.4 `NormalizedClaim`

异质来源统一为可匹配的语义 claim：

```yaml
claim_id: "..."
subject: EntityRef
predicate: "..."
object: EntityRef | ScalarValue
qualifiers:
  polarity: positive | negative | mixed
  epistemic_status: asserted | associated | causal | uncertain
  population: null
  temporal_scope: null
  dose_or_exposure: null
supporting_evidence_ids: []
contradicting_evidence_ids: []
normalizer_version: "..."
```

图谱三元组、数据库记录和文献句子可以支持同一个 claim，但它们的证据强度和限定条件不能被抹平。“相关”不能归一化为“导致”，“可能降低风险”不能归一化为“预防”。

### 5.5 `EvidenceBundle`

围绕一个种子实体保存本次任务构造所见到的实体、claim 和证据。它用于候选发现，不自动等于任务的完整答案。

### 5.6 `GenerationEpisodeState` 与 `EvidenceGap`

`GenerationEpisodeState` 是造题循环的状态。MVP 必需字段为实体、累计证据、累计 claim、候选任务、最终验证结果、预算和停止原因；下面的逐轮 frontier 字段属于 P1/P2 扩展：

```yaml
episode_id: "..."
seed_entity: {}
round: 3
evidence_ids: []
normalized_claim_ids: []
published_task_ids: []
published_task_signatures: []
rejected_candidate_ids: []
open_evidence_gaps: []
search_frontier: []
exhausted_frontier: []
recent_round_yield:
  new_claims: 0
  new_validated_tasks: 0
budget_state: {}
stop_reason: null
```

`EvidenceGap` 不能只是“需要更多证据”，至少要说明：

```yaml
gap_id: "..."
gap_type: answer_completeness | claim_support | contradiction | qualifier | entity_resolution
description: "尚未确认 Drug A 是否还有其他 targets"
target_claim_pattern: {}
search_scope: {}
compatible_provider_capabilities: []
resolution_status: open | resolved | exhausted | blocked
```

`EvidenceGap` 在 MVP 中用于指导针对性补搜，但不是所有探索性查询都必须绑定 gap。P1 再引入完整 `SearchFrontier` 管理：记录尚未执行的合法查询方向和已经完成的规定搜索。系统只需要判断 `GenerationSpec` 要求的步骤是否完成，不需要证明现实世界搜索空间已经穷尽。

### 5.7 `TaskCandidate`

候选只表达一个待验证的任务意图：

- 种子实体；
- 目标问题类型；
- 候选答案或答案变量；
- 候选 claim 组合；
- 候选限定条件；
- 候选发现来源；
- 生成器版本和 prompt 版本。

Candidate Source 可以是确定性路径规则、模板、检索增强 Agent 或自由出题 Agent。候选中出现的答案只是待核验假设，不能直接写入 gold。

### 5.8 `InformationRequirement`

每个 requirement 表示完成题目需要确认的一项信息：

```yaml
requirement_id: req-1
description: "确认 Drug A 的目标蛋白是 Protein X"
claim_pattern:
  subject_id: "Drug A canonical ID"
  predicate_family: targets
  object_id: "Protein X canonical ID"
qualifier_constraints: {}
evidence_quality_rule: "..."
required: true
weight: 1.0
```

`claim_pattern` 用于自动判断一个新 observation 是否满足需求。它不能引用动作名，也不能要求特定 provider，除非来源本身属于问题语义。

### 5.9 `AcceptableEvidenceSet`

有些题只需一条 claim；有些题需要若干 claim 的组合；同一 requirement 也可能有多个等价证据来源。使用布尔表达式描述完成条件：

```yaml
acceptable_evidence_sets:
  - all_of: [req-1, req-2]
  - all_of: [req-3]
completion_rule:
  any_set_satisfied: true
```

这表示可接受的证据组合，不表示执行顺序。不要把实际生成时用过的 evidence ID 全部设为必需项。

### 5.10 `AnswerContract`

```yaml
answer_type: entity | entity_set | scalar | categorical
canonical_answers: []
accepted_aliases: []
cardinality: exactly_one | one_or_more | exact_set
normalization_rule: "..."
scope_statement: "..."
```

单答案问题必须证明唯一性成立于声明范围。集合题必须能说明集合的封闭范围，否则不得声称列举完整。

### 5.11 `TaskSpec` 与 `ValidatedTask`

`TaskSpec` 汇总：

- `task_id` 和 `seed_entity`；
- 规范化任务语义；
- `AnswerContract`；
- `InformationRequirement[]`；
- `AcceptableEvidenceSet[]` 和完成规则；
- 验证环境；
- gold claim 与验证证据引用；
- 题型、难度因素和数据治理字段。

只有 `TaskValidator` 可以把 `TaskSpec` 提升为 `ValidatedTask`。Writer 只接收 `ValidatedTask`。

### 5.12 发布视图必须分离

同一任务至少导出两个访问权限不同的视图。

`PlannerQuestionInput`：

```yaml
qid: "..."
question: "..."
qtype: "..."
budget: {}
max_steps: 8
sufficiency_threshold: 1.0
note: "仅允许放置不泄漏真值的运行说明"
environment_ref: "..."
```

`EvaluationTarget`：

```yaml
qid: "..."
answer_contract: {}
information_requirements: []
acceptable_evidence_sets: []
completion_rule: {}
gold_claim_ids: []
validation_report_ref: "..."
```

Planner 输入中不得出现 gold answer、required evidence、完成 rubric 或生成路径。若现有 Query Planner schema 仍包含 `requirement_groups`，兼容适配器只能把它放入评估侧配置；不得把隐藏真值暴露给策略输入。

与当前 Query Planner `Question` 契约的映射如下：

| 当前字段 | 发布规则 |
|---|---|
| `qid`、`question`、`qtype` | 直接由 `PlannerQuestionInput` 提供 |
| `budget`、`max_steps`、`sufficiency_threshold` | 作为运行约束提供，不代表正确路线 |
| `note` | 只能包含公开运行说明，必须经过泄漏扫描 |
| `requirement_groups` | 从 Planner 策略输入移出，只在 evaluator/labeler 侧由 `EvaluationTarget` 读取 |

如果现有运行时暂时无法移除 `requirement_groups`，可以保留字段以兼容反序列化，但传给策略模型的 view 必须剔除其内容。该兼容状态应被记录为待迁移项，不能视为长期数据契约。

## 6. 候选任务如何生成

每个种子实体应并行使用多种 Candidate Source，避免数据集只学到单一模板：

1. **结构化关系源**：从 KG 或结构化数据库中的关系、路径和约束组合产生候选；
2. **claim 组合源**：将语义兼容的多源 claim 组合成单跳、约束或多证据任务；
3. **自由出题 Agent**：读取有限 EvidenceBundle，提出有研究价值的任务意图；
4. **反事实筛选源**：寻找看似可问、但因多解、冲突或证据不足应被拒绝的候选，用于提高过滤器能力。

首批可发布题型继续以 [生成问题的题型与语言验收规范](generated-question-types-and-language-criteria.md) 为准。架构允许未来扩展题型，但任何新题型都必须先定义：答案契约、需求匹配规则、完整性边界和可执行验收用例。

生成多个问题时，“不同”必须指任务语义不同，不能只是同一 claim 的同义改写。可接受的差异包括：

- 查询目标不同；
- 关系语义不同；
- 约束条件不同；
- 所需 claim 组合不同；
- 证据范围或回答粒度不同。

## 7. 造题循环与停止语义

### 7.1 MVP 循环

第一版只要求以下闭环：

1. 按 `GenerationSpec` 对实体执行探索性证据搜索；
2. 保存证据并归一化 claim；
3. 根据当前 EvidenceBundle 生成候选问题；
4. 对每个候选执行针对性验证；
5. 证据不足但可以补充时，生成 EvidenceGap 并定向补搜；
6. 达到题目目标、完成规定搜索、预算耗尽或 provider 失败时停止；
7. 使用 episode 最终 EvidenceBundle 对拟发布任务统一验证；
8. 保存通过项、拒绝项和真实停止原因。

探索性搜索用于发现“这个实体周围有什么值得问”；验证性搜索用于确认某道候选题的答案、基数、语义或冲突。只有验证性搜索必须绑定具体 EvidenceGap。

### 7.2 判断当前证据“够出题”

当前证据足够，不是指“模型能写出一句问题”，而是至少存在一个候选同时满足：

- 答案有可定位的证据；
- 题面没有超出证据语义；
- 在本次规定验证范围内没有已知的额外答案或未解决冲突；
- 能定义回答该题需要的 information requirements；
- 不向下游规定固定动作路线；
- 与当前 episode 的其他任务不是同一语义的表面改写。

不满足时可以定向补搜，也可以直接放弃候选。第一版不要求为了挽救每个候选而持续搜索。

### 7.3 判断“没搜全”

MVP 不判断开放世界是否已经“搜全”，只判断 `GenerationSpec` 中要求的搜索是否完成：

- 规定查询尚未执行或分页未完成：`search_incomplete`；
- 规定查询已正常完成但仍不足：`required_searches_completed`，候选可以拒绝；
- 还有查询但预算不足：`budget_exhausted`；
- 查询因服务故障未完成：`provider_unavailable`。

这保证停止结论诚实，同时避免为了证明“所有可能证据都不存在”而构建不可完成的系统。

### 7.4 停止条件

每个 episode 必须且只能记录一个主停止原因：

| stop reason | 含义 | 是否等价于证据不足 |
|---|---|---|
| `question_targets_met` | 数量和基本类型目标已达到，拟发布任务已完成最终验证 | 否 |
| `required_searches_completed` | `GenerationSpec` 规定的搜索已完成，没有更多合格题 | 可以，只表示规定范围内不足 |
| `budget_exhausted` | 时间、调用、token 或费用达到上限 | 否 |
| `provider_unavailable` | 必需外部来源不可访问或持续失败 | 否 |
| `policy_stopped` | 生成策略主动停止 | 否，需保存简短原因 |

达到题目数量目标只能停止继续发现新题，不能跳过已选题目的答案完整性和语义验证。只要某个 provisional 任务仍存在 `answer_completeness`、`contradiction` 或其他验证关键 gap，就不能以 `question_targets_met` 正式结束 episode。

### 7.5 后续增强，不作为 MVP 门槛

- P1：EvidenceGap 驱动的搜索优先级、同一 claim 的多源等价匹配、搜索饱和判断；
- P2：完整 SearchFrontier、逐轮 checkpoint、自动恢复、增量发布和依赖失效追踪。

MVP 采用“episode 结束统一验证”，因此不需要在搜索过程中实时撤回题目。

## 8. TaskValidator

Validator 至少包含六类检查。

### 8.1 答案正确性

- gold answer 能从已保存 claim 和证据重新推导；
- KG 查询展开所有合法分支，不把候选路径端点直接当答案；
- 文献 claim 与引用 span 一致；
- 多源结果冲突时不得静默选择有利来源。

### 8.2 基数和范围

- 单数问法对应 `exactly_one`；
- 集合题在声明范围内可以枚举完整集合；
- 局部唯一不能冒充验证环境唯一；
- 数据快照唯一不能被写成现实世界绝对唯一。

### 8.3 信息需求可判定

- 每个 requirement 都有机器可执行的 claim matching 规则；
- 至少存在一个可满足的 `AcceptableEvidenceSet`；
- completion rule 能从 EvidenceStore 状态确定性计算；
- requirement 不绑定无必要的动作或 provider。

### 8.4 生物医药语义

- predicate、方向和实体类型正确；
- association、causality、treatment、prevention、risk 等强度不被夸大；
- polarity、population、dose、时间和研究类型等关键限定没有丢失；
- 文献支持和反对证据均被处理。

### 8.5 题面等价

Writer 输出必须回译为结构化语义，并和 `ValidatedTask` 比较：实体、关系、限定、答案类型、基数和范围均一致。Writer 不能修改 gold 任务来适配自己生成的句子。

### 8.6 数据治理

- provenance、版本和 hash 完整；
- 近重复、模板泄漏和跨 split 实体泄漏受控；
- 题面不包含答案、内部 evidence ID、动作名或生成路径；
- 相同任务的语言改写不能跨 split。

推荐的拒绝码至少包括：

```text
entity_resolution_ambiguous
provider_snapshot_mismatch
evidence_truncated
missing_provenance
claim_not_supported
claim_semantics_mismatch
conflicting_evidence_unresolved
empty_answer
cardinality_mismatch
scope_not_closed
requirement_not_machine_checkable
no_satisfiable_evidence_set
route_binding_detected
question_semantics_mismatch
answer_leakage
near_duplicate
```

## 9. Provider 边界

Provider adapter 负责调用外部系统并返回标准 observation。每个 adapter 必须暴露 capability manifest：

- 支持的实体和关系类型；
- 是否支持分页和完整枚举；
- 版本或检索时间；
- 成本信息是否可用；
- 截断、超时和失败语义；
- provenance 定位能力。

本项目可以在“造题阶段”调用 provider 来发现和验证任务；这不代表下游 Planner 必须调用相同 provider。生成侧 observation 也不能冒充下游动作成本。

## 10. 与 Query Planner 的接口

数据流分三层：

| 层 | 产生方 | 核心内容 |
|---|---|---|
| 任务数据 | medical-qa-data-factory | question、gold answer、requirements、completion rule、validation environment |
| 交互数据 | query-planner rollout | state before、candidate actions、chosen action、observation、real cost、state after |
| 派生标签 | evaluation/labeler | newly satisfied requirements、coverage gain、answer support gain、task completion |

下游动作可以是异质的，例如图关系扩展、实体查询、路径搜索、文献检索或未来工具。统一之处不在动作实现，而在每个动作执行后都把 observation 规范化为 `EvidenceRecord + NormalizedClaim`，再使用同一 requirement matcher 计算状态变化。

禁止在任务数据中发布以下字段：

```text
correct_action
best_action
expected_route
optimal_trajectory
must_use_literature
```

其中最后一项只有在问题明确限定“文献证据”时，才能作为任务条件出现；通常情况下不应存在。

## 11. 建议代码结构

```text
src/medical_qa_data_factory/
  contracts/
    entities.py
    evidence.py
    episodes.py
    tasks.py
    publication.py
  orchestration/
    generation_orchestrator.py
    search_frontier.py        # P2
    search_sufficiency.py     # P1
    stop_policy.py
  providers/
    base.py
    registry.py
    primekg_adapter.py
    literature_adapter.py
    ctd_adapter.py
  normalization/
    entity_resolver.py
    claim_normalizer.py
  generation/
    candidate_source.py
    path_candidate_source.py
    claim_candidate_source.py
    agent_candidate_source.py
    task_compiler.py
  validation/
    answer_validator.py
    requirement_matcher.py
    biomedical_semantics.py
    task_validator.py
    text_semantic_verifier.py
  realization/
    question_writer.py
  publication/
    exporter.py
    manifest.py
```

具体目录可按现有代码迁移，但核心端口应保持独立：`EvidenceProvider`、`CandidateSource`、`TaskCompiler`、`TaskValidator`、`QuestionRealizer` 和 `DatasetExporter`。

## 12. 发布产物

一个数据集版本至少包含：

```text
dataset-release/
  planner_questions.jsonl
  evaluation_targets.jsonl
  evidence_catalog.jsonl
  generation_episodes.jsonl
  validation_reports.jsonl
  rejected_candidates.jsonl
  dataset_manifest.json
  quality_report.json
```

- `planner_questions.jsonl` 可以提供给 Planner；
- `evaluation_targets.jsonl` 只提供给 evaluator/labeler；
- `evidence_catalog.jsonl` 用于证据匹配、审计和重放；
- `generation_episodes.jsonl` 在 MVP 保存 episode 摘要、规定搜索完成情况、预算和停止原因；P2 再保存完整逐轮 frontier 和 checkpoint；
- `rejected_candidates.jsonl` 用于分析生成器缺陷，不能混入训练集；
- manifest 必须固定数据、代码、模型、prompt、provider 和 schema 版本。

## 13. 开发顺序

### MVP-1：证据、任务和发布契约

- 实现 `EntityRef`、`EvidenceRecord`、`NormalizedClaim` 和 `ValidationEnvironment`；
- 定义 provider adapter 与 capability manifest；
- 至少为一个 KG provider 和一个文献 provider 建立最小 fixture；
- 实现 Planner 可见问题与隐藏 EvaluationTarget 的隔离。

退出条件：给定 observation 可以稳定生成带 provenance 的规范 claim。

### MVP-2：候选生成与最终验证

- 实现 `InformationRequirement`、`AcceptableEvidenceSet`、`AnswerContract` 和 `TaskSpec`；
- 实现最小 `GenerationEpisodeState` 和 `EvidenceGap`；
- 实现 requirement matcher、completion evaluator 和 Validator；
- 加入多解、冲突、截断、范围不封闭和语义夸大测试。

退出条件：不依赖自然语言题面即可判断一个任务是否有效、何时完成，以及是否值得进行一次定向补搜。

### MVP-3：简单造题循环

- 先实现结构化路径源和 claim 组合源；
- 接入自由出题 Agent 作为候选源；
- 所有来源统一经过 Compiler 和 Validator；
- 同一实体生成语义不同的多个任务。
- 支持探索搜索、一次或多次定向补搜、最终统一验证；
- 保存 `question_targets_met`、`required_searches_completed`、`budget_exhausted` 或 `provider_unavailable`。

退出条件：Agent 幻觉、局部多解和不可验证问题不能进入 `ValidatedTask`。

### MVP-4：题面生成与数据集发布

- Writer 只接收 `ValidatedTask`；
- 实现题面回译和语义等价检查；
- 分别导出 Planner 可见数据和隐藏评估目标；
- 加入答案与 rubric 泄漏扫描。

退出条件：发布数据可供 Query Planner 使用，但策略无法读取 gold 和固定路线。

### MVP-5：下游 rollout 对接

- Query Planner 接收 `PlannerQuestionInput`；
- evaluator 私下加载 `EvaluationTarget`；
- observation 使用相同 claim normalizer；
- 由前后状态计算 gain，由执行器记录 cost；
- 验证不需要 `correct_action` 也能生成训练标签。

退出条件：至少一个 KG 动作和一个文献动作可以在同一任务上产生可比较的真实增益/成本记录。

### P1：自动化搜索增强

- EvidenceGap 驱动的搜索优先级；
- 多源 claim 等价匹配；
- 连续无产出的搜索饱和判断；
- 更完整的题型覆盖和质量报告。

### P2：生产可靠性

- 完整 SearchFrontier；
- 逐轮 checkpoint 和断点恢复；
- 增量任务发布、依赖追踪和自动失效；
- 大规模 split、泄漏控制和长期数据版本治理。

## 14. 架构不变量

| ID | 不变量 |
|---|---|
| ARCH-01 | 没有可追溯证据和验证环境，不得生成 gold task |
| ARCH-02 | Candidate Source 和 Writer 均无权创建 `ValidatedTask` |
| ARCH-03 | gold answer 必须重新验证，不能直接复制候选答案 |
| ARCH-04 | requirement 描述信息，不描述固定动作或路线 |
| ARCH-05 | Planner 可见数据与隐藏评估目标物理分离 |
| ARCH-06 | 动作 gain 来自状态前后 requirement 覆盖变化 |
| ARCH-07 | 动作 cost 来自真实执行记录，不由任务工厂估造 |
| ARCH-08 | 异质 observation 必须先规范化为 claim 再参与统一评估 |
| ARCH-09 | 自由出题 Agent 可以提案，但不能绕过确定性门禁 |
| ARCH-10 | 数据快照内成立与现实世界成立必须在措辞和验证上区分 |
| ARCH-11 | 验证性补搜必须绑定具体 EvidenceGap；探索性搜索可以由 GenerationSpec 驱动 |
| ARCH-12 | 预算耗尽、provider 失败和搜索未完成不得标记为证据不存在 |
| ARCH-13 | 每个 episode 必须保存主停止原因和足以审计的查询结果 |
| ARCH-14 | 正式发布前必须使用 episode 的最终 EvidenceBundle 统一验证拟发布任务 |

可执行通过条件见 [项目验收标准](acceptance-criteria.md)。题型和语言细则见 [生成问题的题型与语言验收规范](generated-question-types-and-language-criteria.md)。

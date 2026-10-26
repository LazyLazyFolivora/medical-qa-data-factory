# 医学 QA 与 Query Planner 训练数据生成系统：产品需求文档

> 文档状态：当前唯一产品需求真源  
> 版本：requirements-v2  
> 更新日期：2026-10-21  
> 适用项目：`medical-qa-data-factory`

## 0. 文档定位

本文件定义系统现在要解决的问题、必须产生的数据、核心流程和验收标准。

发生冲突时，产品需求以本文件为准。以下文件仅作为历史设计、实现细节或现状记录：

- `docs/medical-qa-data-factory/entity-question-generation-agent-roadmap.md`：历史技术路线；
- `plans/medical-qa-react-multihop-agent-blueprint.md`：历史复杂 Agent 蓝图；
- `plans/real-generation-agent-implementation-plan.md`：历史施工计划；
- `docs/medical-qa-data-factory/current-implementation-gaps.md`：当前代码与本需求之间的差距。

本文件不要求为了“像 Agent”而增加复杂度。所有设计必须直接服务于两个数据产物：

1. 基于知识图谱和知识库生成的医学 QA 数据；
2. 用于训练 Query Planner 动作选择器的探索轨迹数据。

---

## 1. 产品定义

### 1.1 一句话定义

给定一个已消歧的医学实体，使用 LLM 选择知识图谱或知识库查询动作，发现可用于出题的实体、关系和证据，生成难度适中的问题与答案，同时保存 Query Planner 的状态—动作—观察轨迹。

### 1.2 核心流程

```text
输入种子实体与题目要求
  → LLM 查看当前已知实体、证据和缺口
  → LLM 选择下一步 KG/RAG 动作
  → 系统校验并执行动作
  → 新实体、关系和证据返回为 observation
  → LLM 决定继续探索或开始出题
  → 基于本次证据生成问题和候选答案
  → 独立校验答案、证据和难度
  → 输出 QAItem 与 PlannerTrajectory
```

### 1.3 LLM 的角色

LLM 是探索和出题的决策者，负责：

- 根据当前状态选择下一步查询动作；
- 判断应继续扩展哪个实体；
- 判断某项事实是否需要知识库文本证据；
- 在满足题目要求后选择停止探索；
- 基于已冻结证据生成问题和候选答案。

LLM 不直接执行 SQL、HTTP、文件读取或任意工具调用。所有动作都必须经过服务端校验并由受控 Executor 执行。

### 1.4 系统的角色

系统负责：

- 精确验证种子实体身份；
- 向 LLM 暴露有限、结构化的动作空间；
- 执行 KG/RAG 查询并返回结构化 observation；
- 保存实体、关系、文献、查询轨迹和快照信息；
- 判断证据引用是否合法；
- 校验题目难度、答案唯一性和证据充分性；
- 生成稳定、版本化、可导出的训练数据。

---

## 2. 产品目标

### 2.1 必须实现

1. 从一个已消歧医学实体开始探索。
2. LLM 能根据上一轮 observation 选择下一步动作。
3. 探索过程可以在知识图谱中发现新的实体和关系。
4. LLM 可以在需要时调用外部 RAG 获取文本证据。
5. 基于本次实际取得的证据生成问题和候选答案。
6. 问题默认以简单、适中为主，而不是追求最长路径或最高推理难度。
7. 每道题都能追溯到生成它的实体、KG 证据、文献证据和探索 episode。
8. 每次探索都保存可用于 Query Planner 训练的动作选择轨迹。
9. 成功、低收益、被拒绝和失败动作均可用于训练或分析。
10. 数据契约必须版本化，旧数据不能被静默改写。

### 2.2 明确不追求

- 不以构造复杂自主 Agent 为目标；
- 不要求固定探索轮数；
- 不要求所有题目固定跳数；
- 不要求每一条 KG 边都调用 RAG；
- 不要求默认生成三跳以上难题；
- 不要求 LLM 自由编造答案或证据；
- 不保存模型隐藏思维链；
- 不让 Query Planner 直接控制底层数据库、网络地址或文件路径；
- 不为了凑题而持续扩展低价值实体；
- 不把运行预算字段当成题目难度定义。

---

## 3. 任务输入

一次生成任务至少包含以下输入。

### 3.1 SeedEntity

种子实体由上游完成消歧，本系统只做精确验证。

```yaml
provider_id: primekg
native_id: D001241
entity_type: exposure
canonical_name: Aspirin
```

系统不得遇到歧义时静默选择第一个候选。

### 3.2 GenerationSpec

`GenerationSpec` 描述想生成什么样的题，而不是规定 Planner 必须走多少轮。

建议字段：

```yaml
spec_version: generation-spec-v1
question_count: 3
difficulty_targets:
  - easy
  - medium
allowed_answer_types:
  - entity
allowed_sources:
  - knowledge_graph
  - literature
reasoning_constraints:
  min_hops: 1
  max_hops: 2
evidence_constraints:
  min_total_evidence: 1
  min_kg_evidence: 1
  literature_policy: when_needed
language: zh-CN
```

这些字段可以按数据生产批次调整并版本化。它们不是写死在系统中的全局常量。

### 3.3 SafetyBudget

运行保护与 `GenerationSpec` 分离。它只用于防止死循环、无限调用和失控成本，例如：

```yaml
max_tool_calls: 20
max_wall_time_seconds: 180
max_edges_observed: 500
max_model_attempts: 30
```

Planner 可以在预算内自由决定探索轮数。到达安全上限时，本次 episode 明确结束为 budget exhausted，不得伪装成正常完成。

---

## 4. 题目难度

难度由最终问题实际依赖的推理结构和证据决定，不由 LLM 自报，也不由探索轮数决定。

### 4.1 Easy

- 通常依赖一条明确 KG 关系；
- 答案为一个明确实体或短事实；
- 至少一条可追溯证据；
- 不需要复杂关系组合。

### 4.2 Medium

满足以下一种即可：

- 依赖两条连续 KG 关系；
- 依赖一条 KG 关系和一条必要的文献证据；
- 需要根据明确限定条件从多个邻居中确定唯一答案。

### 4.3 Hard

Hard 不是默认生产目标。只有具体数据批次明确要求时才启用，例如三跳关系、多来源组合或冲突证据判断。

### 4.4 难度判定

系统根据冻结证据计算并保存：

- `hop_count`；
- `required_kg_evidence_count`；
- `required_literature_evidence_count`；
- `constraint_count`；
- `answer_candidate_count`；
- `difficulty_label`；
- `difficulty_policy_version`。

探索动作很多不代表题目很难。无效查询和绕路不得增加难度等级。

---

## 5. Query Planner 动作空间

Planner 每轮只能从服务端公布的动作中选择一个。

### 5.1 `query_neighbors`

查询某个已知实体的邻居。

```yaml
action_type: query_neighbors
subject_entity_id: primekg:exposure:D001241
direction: outgoing
relations: []
target_types: []
max_results: 20
```

### 5.2 `expand_entity`

继续探索上一轮发现的实体。该实体必须已经出现在当前 episode 的 observation 中。

### 5.3 `retrieve_literature`

当某个候选事实需要文本支持时，查询外部知识库。

```yaml
action_type: retrieve_literature
query: Aspirin PTGS1 mechanism
entity_ids:
  - primekg:exposure:D001241
  - primekg:gene:5742
evidence_need_id: need-...
max_results: 10
```

RAG 是按需工具。是否需要文本证据由题目要求、关系语义和当前证据缺口共同决定。

### 5.4 `finish`

Planner 认为已有材料足以生成目标难度的问题时提出停止。系统仍需检查最低证据要求；证据不足的 finish 必须被拒绝并记录。

### 5.5 动作约束

- 动作只能引用当前 episode 已知实体、observation 和 evidence need；
- 模型不能指定工具实现、数据库路径、SQL、URL 或凭据；
- 重复动作、未知实体、超限参数和不支持能力必须被 Policy Gate 拒绝；
- 被拒动作不执行工具，但保留为 Planner 负样本候选。

---

## 6. 探索与停止语义

### 6.1 不固定探索轮数

系统不规定每次必须探索 N 轮。Planner 根据以下信息决定下一步：

- 当前已知实体；
- 最近 observation；
- 已收集证据；
- 尚未满足的题目约束；
- 证据缺口；
- 可用动作；
- 剩余安全预算。

### 6.2 正常停止

满足以下条件时可以进入出题：

- 已发现至少一个有出题价值的事实或实体链；
- 实际 hop 和证据数量满足 `GenerationSpec`；
- 答案候选能够绑定到稳定实体 ID 或可验证短事实；
- 必需的文本证据已经取得；
- 没有阻塞型冲突或未解决歧义。

### 6.3 非正常停止

以下情况不生成正式 QA：

- 没有发现可用证据；
- 连续动作没有信息增益；
- 模型持续提出非法或重复动作；
- KG/RAG provider 不可用；
- 达到安全预算；
- 答案候选不唯一且无法通过约束消除；
- 证据不足以支撑目标难度。

episode 可以保留为 Planner 训练或失败分析数据，但不能伪装为成功 QA。

---

## 7. 知识图谱和知识库证据

### 7.1 KG 证据

每条用于出题的 KG 边必须保存：

- head、relation、tail；
- canonical entity IDs；
- 查询方向；
- provider 与 snapshot；
- source record ID；
- query trace；
- retrieval action。

多跳题必须由连续的有序边组成。边的集合不能自动视为路径。

### 7.2 文献证据

RAG 搜索结果只有在独立回读后才能成为正式文献证据。至少保存：

- 文档和 chunk ID；
- 原始文本；
- 文本 hash；
- 字符或段落位置；
- provider snapshot；
- 检索 query；
- 与目标事实的支持关系。

### 7.3 RAG 调用原则

以下情况通常需要 RAG：

- `GenerationSpec` 明确要求 KG + literature；
- KG 关系不足以直接支撑问题措辞；
- 问题涉及机制、因果、治疗或其他容易被关系名过度解释的语义；
- Planner 发现明确证据缺口。

简单、语义明确的一跳事实不必为了形式完整强制调用 RAG。

---

## 8. 问题与答案生成

### 8.1 Writer 输入

Writer 只能看到本次 episode 冻结后的：

- 种子实体；
- 允许使用的已发现实体；
- 必要 KG 证据；
- 必要文献证据；
- 目标难度和题型；
- 候选答案身份；
- 明确的禁止泄漏约束。

### 8.2 Writer 输出

Writer 输出结构化候选：

- `question_text`；
- `candidate_answer`；
- `answer_entity_id` 或事实标识；
- 使用的 evidence IDs；
- 使用的关系链；
- 题型和语言。

### 8.3 答案约束

- 答案必须来自本次探索发现的实体或证据；
- 实体答案必须绑定 canonical ID；
- Writer 不得引入未检索到的新事实；
- 问题文本不能直接泄漏答案；
- 关系措辞不得把 association 擅自改写为因果、治疗或机制结论。

### 8.4 独立验证

Verifier 不信任 Writer 的解释，只根据问题和冻结证据检查：

- 答案是否可以从证据重新得到；
- 引用是否存在且属于本次 episode；
- 问题是否存在多个合理答案；
- 问题难度是否符合 `GenerationSpec`；
- 文献是否真正支持相关措辞；
- 是否存在答案泄漏或错误方向。

复杂的 shortcut 和 leave-one-hop-out 检查只在多跳题需要时启用，不作为所有简单题的强制流程。

---

## 9. 数据产物与契约

系统输出两个相互关联、用途不同的数据集。

### 9.1 QAItem

`QAItem` 面向 QA 训练、评测和人工审核。

它可以实现为现有 `QuestionSpec + EvidenceBundle` 的版本化导出投影，不要求再复制一套
相互独立的业务存储模型；但导出的字段和 Schema 必须稳定。

```yaml
schema_version: qa-item-v1
question_id: question-...
question_version: question-....v1
logical_question_id: logical-...
language: zh-CN
question_text: "..."
question_type: single_fact | constrained_entity | relation_chain
difficulty: easy | medium | hard

answer:
  text: "PTGS1"
  canonical_entity_id: primekg:gene:5742
  aliases: []
  answer_type: entity

source:
  seed_entity_id: primekg:exposure:D001241
  discovered_entity_ids: []
  generation_episode_id: episode-...
  planner_trajectory_id: trajectory-...

reasoning:
  hop_count: 1
  ordered_relation_chain: []
  intermediate_entity_ids: []
  evidence_path_id: null

evidence:
  required_kg_evidence_ids: []
  required_literature_evidence_ids: []
  provider_snapshot_ids: []

validation:
  answer_recomputed: true
  evidence_satisfied: true
  ambiguity_check: passed
  leakage_check: passed
  validation_level: machine_validated
```

### 9.2 PlannerTrajectory

`PlannerTrajectory` 面向 Query Planner 动作选择训练。

```yaml
schema_version: planner-trajectory-v1
trajectory_id: trajectory-...
episode_id: episode-...
seed_entity_id: primekg:exposure:D001241
generation_spec_id: spec-...
model_config_id: model-...
steps: []
terminal_reason: question_ready
generated_question_ids: []
trajectory_quality: accepted
```

### 9.3 PlannerStep

每个 step 表示一个可以转换成训练样本的状态—动作—结果单元。

```yaml
step_id: planner-step-...
turn: 2

state:
  known_entities: []
  observation_summaries: []
  evidence_summary: {}
  evidence_gaps: []
  target_difficulty: medium
  unsatisfied_constraints: []
  available_actions: []

selected_action:
  action_type: retrieve_literature
  parameters: {}
  expected_information: "..."
  evidence_gap: "..."

execution:
  policy_result: accepted
  sealed_action_id: sealed-...
  observation_id: observation-...
  status: success

outcome:
  discovered_entity_ids: []
  new_evidence_ids: []
  evidence_gain: 1
  satisfied_constraints: []
  useful: true
```

不保存模型隐藏思维链。`expected_information` 和 `evidence_gap` 只是简短、可审计的动作标签。

### 9.4 训练样本派生

从 `PlannerStep` 至少可以派生：

```text
输入：planner state + available actions
标签：selected action type + bounded parameters
反馈：policy result + observation + evidence gain + terminal QA quality
```

可用正样本包括：

- 最终产生合格 QA 的有效动作；
- 带来新实体或必要证据的中间动作；
- 在证据充分时正确选择 finish。

可用负样本包括：

- 重复动作；
- 未知实体动作；
- 没有信息增益的查询；
- 证据不足时 finish；
- 导致错误、歧义或不可验证问题的轨迹。

训练导出必须保留标签生成规则和版本，不能简单把所有 LLM 选择都当成 gold action。

---

## 10. 数据集质量要求

### 10.1 QA 质量

正式 QA 至少满足：

- 问题自然、明确、无答案泄漏；
- 答案与 canonical identity 一致；
- 必要证据可以重新加载；
- 证据方向和关系语义正确；
- 实际难度满足任务要求；
- 不存在未处理的答案歧义；
- 自动生成结果在人工确认前只称候选答案。

### 10.2 Planner 轨迹质量

正式 Planner 训练轨迹至少满足：

- 每一步 state 只包含动作发生前可见的信息；
- selected action 能被服务端重新校验；
- observation 与实际工具执行一致；
- evidence gain 可从前后状态计算；
- QA 成败能够回链到整条 trajectory；
- 同一 episode 不混入其他任务的实体、证据或 observation；
- resume 后不会重复记录同一物理动作。

### 10.3 数据划分

训练、验证和测试划分必须防止以下泄漏：

- 同一 logical question 的不同措辞跨 split；
- 同一证据路径的近重复问题跨 split；
- 高度相似的种子—答案实体组合跨 split；
- 同一原始问题的人工修订版本跨 split。

---

## 11. 可观测性和运行保护

这些能力用于数据可信和故障恢复，不应主导产品设计。

必须记录：

- episode、trajectory 和 step identity；
- model/provider 配置版本；
- tool/model attempt 状态；
- request digest 与 observation ID；
- token、耗时、调用次数和错误码；
- KG/RAG snapshot；
- terminal reason。

安全预算可以按运行环境配置。预算变化不得改变既有数据的语义，也不得被写成题目难度。

---

## 12. 对外入口

正式入口保持：

```text
medical-qa-data-factory generate entity
```

核心参数应覆盖：

- workspace；
- task ID；
- seed entity identity；
- model mode/config；
- generation spec；
- KG provider；
- literature provider；
- safety budget。

运行结果至少返回：

- task ID；
- episode ID；
- trajectory ID；
- bundle IDs；
- question IDs；
- terminal reason；
- 是否满足目标题数和难度要求。

---

## 13. 验收标准

### 13.1 最小功能验收

使用一个测试种子和 deterministic fake model，证明：

1. 第一轮从种子实体查询 KG；
2. 第二轮能看到第一轮 observation，并选择一个新发现实体继续查询；
3. Planner 能根据 evidence gap 选择 `retrieve_literature`；
4. RAG 结果经过独立回读后成为正式 LiteratureEvidence；
5. Planner 在证据充分后选择 finish；
6. Writer 只使用本次冻结证据生成问题和候选答案；
7. Verifier 能从证据重新得到答案；
8. 系统同时保存一个 QAItem 和一条完整 PlannerTrajectory；
9. 每个 PlannerStep 都能导出为动作选择训练样本。

### 13.2 真实工具验收

- PrimeKG 使用只读真实 snapshot；
- 至少一次真实 WeKnora search/read-back 成功；
- 至少生成一道 easy 和一道 medium 候选题；
- medium 题实际依赖两跳 KG 或 KG + literature；
- 所有引用均可按 ID 和 snapshot 重新加载；
- 不修改 PrimeKG 原始数据库或 WeKnora 索引。

### 13.3 回归验收

- `rule-based` 基线仍可运行；
- structured 模式缺配置时 fail fast；
- 模型或工具失败时不静默降级；
- checkpoint/resume 不重复物理调用和训练 step；
- 默认测试和真实 PrimeKG 测试全部通过。

---

## 14. 实施优先级

### P0：先形成训练数据闭环

1. 定义并实现 `GenerationSpec`、`PlannerTrajectory`、`PlannerStep`、`QAItem`。
2. 将现有 ReAct proposal、sealed action、observation 写入 PlannerStep。
3. 计算 `evidence_gain`、constraint satisfaction 和 trajectory terminal quality。
4. 将正式 KG 探索结果连接到 QAItem。

### P0：接入按需 RAG

1. 将 `retrieve_literature` 接入正式 Executor。
2. 将 evidence need 绑定到候选事实，而不是强制每 hop 检索。
3. 完成 search → read-back → hash/location/snapshot 校验。
4. 将文献 observation 和 evidence gain 写入 PlannerStep。

### P0：接入 LLM 出题与独立校验

1. Writer 使用冻结证据生成候选问题和答案。
2. Verifier 独立复算，不读取 Writer 的隐藏理由。
3. 将 QA 校验结果回传为 trajectory 的终局质量信号。

### P1：生产与训练导出

1. 导出 Planner 的正负动作样本。
2. 版本化标签生成和难度判定策略。
3. 完成 split 与泄漏检测。
4. 生成人工审核和质量报告。

---

## 15. 当前实现状态

当前代码状态不在本需求文档中滚动维护，统一记录在：

- `docs/medical-qa-data-factory/current-implementation-gaps.md`

需求是否完成只能由验收标准和实际测试证明，不能因为某个类、接口或模块已经存在就标记完成。

---

## 16. 待确认的生产参数

以下参数不阻塞架构实现，但在正式批量生产前必须按数据用途确定并版本化：

- easy/medium 的目标比例；
- 每个种子的目标题数；
- 哪些关系语义必须有 literature 支持；
- QA 与 Planner 训练集的 split 粒度；
- trajectory 正负样本的采样和平衡策略；
- 人工审核抽样比例；
- 使用真实 LLM 生成训练标签时的模型和 prompt 版本。

这些参数应属于数据生产配置，不应散落在 Planner、Executor 或 Writer 的代码中。

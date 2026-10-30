# Agent 生成循环、预算与约束说明

## 1. 两层循环

生成流程不是单个大循环，而是“外层固定流水线 + 内层 ReAct 循环”。

### 外层固定 LangGraph

外层节点按产品阶段执行：

```text
verify_seed
  → agent_explore
  → curate_evidence
  → realize_validated_tasks
  → finalize_artifact_links / finalize_partial
```

入口在 `graph.py`：

- `verify_seed`：确认种子实体的 native ID 和类型；
- `agent_explore`：运行内层 ReAct 循环；
- `curate_evidence`：去重并筛选证据；
- `realize_validated_tasks`：把已通过完整世界验证的候选转成最终题目；
- `finalize_*`：保存结果和停止原因。

### 内层 ReAct LangGraph

内层按轮次运行：

```text
observe
  → decide
  → gate
  → act
  → persist
  → assess
  → observe / done
```

各节点职责：

| 节点 | 职责 |
| --- | --- |
| `observe` | 汇总证据、候选、缺口和剩余预算 |
| `decide` | 调用结构化模型选择下一动作或提出候选 |
| `gate` | 校验动作是否在白名单、预算、实体和参数范围内 |
| `act` | 执行通过 gate 的动作；被拒绝的动作不会执行 |
| `persist` | 写入 PlannerStep、工具调用和 Skill 记录 |
| `assess` | 编译并验证新提出的候选 |

### 局部拒绝

一次动作被拒绝不会自动终止整个内层循环。

例如本次 RGS20 运行：

- 第一轮查询 RGS20 邻域，消耗 50 条边；
- 第二轮想一次查询 50 条边，但只剩 30 条，因此该动作被 gate 拒绝；
- Agent 仍可继续选择候选、停止或不消耗边预算的动作；
- 候选被拒绝后，内层循环仍可继续寻找其他候选，直到达到轮次、预算或终止条件。

因此，“某个动作被拒绝”与“整个 agent_explore 失败”不是一回事。

对相似动作的处理：

- 单次结果为空或执行报错的动作，不会进入“已执行动作”去重集合；
- 同一动作只有成功后才阻止完全重复；
- 一次宽查询已经保存的边，再次被窄查询命中时复用已有证据，不会覆盖
  历史证据，也不会把同一条边再次算成新证据。

## 2. 预算

Web 生成当前使用以下上限：

| 预算 | 当前值 | 含义 |
| --- | ---: | --- |
| `max_model_attempts` | 20 | 内层 Agent 最大决策轮次 |
| `max_tool_calls` | 20 | KG/CTD/PubMed 工具调用次数上限 |
| `max_edges_observed` | 500 | 所有图查询读取的原始边数上限 |
| `max_wall_time_seconds` | 300 | 总墙钟时间上限 |
| 单次查询 | 50 | 由 `min(50, max_edges_observed)` 限制 |

边预算不是“可返回证据条数”，而是工具返回并检查过的原始行数。两个查询都请求
`max_results=50` 时，会占用最多 100 条边预算。

当前值定义在 `web/app.py` 的 `SafetyBudget` 构造处。CLI 可以按用例提供不同的
`SafetyBudget`。

## 3. 候选答案

`TaskCandidate` 是假设，不是已验证题目。

当前生产编译器只接受单答案实体题：

```python
len(candidate.proposed_answer_ids) == 1
```

模型一次返回多个可能答案，不等于可以生成一道多答案题。它必须从候选集合中选定
一个答案实体，再由完整世界验证器确认唯一性。

本次 RGS20 候选提供了 10 个 anatomy 实体，所以编译器直接拒绝：

```text
single-answer candidate must propose exactly one entity
```

这个限制在 `TaskCompiler` 中执行。历史 `AgentDecisionRecord` 可能是在旧规则下写入
的，因此不能把新规则直接加在反序列化阶段，否则旧任务会无法打开。新候选进入编译器
时会得到具体的 `TaskCompilationError`，该正文会写入 `ValidationFeedback.message`
并显示在诊断界面，而不是只留下模糊的 `candidate_not_compilable`。

## 4. 约束从哪里来

`constraints` 不是数据库自动生成的，也不是固定的第二个查询。它由模型在
`propose_candidate` 决策中构造。

一个 `constrained_entity` 任务包含：

1. 一个从 seed 到答案实体的主关系；
2. 至少一个从答案实体到固定已知实体的约束路径。

约束的目的：

- 排除其他也满足主关系的实体；
- 让答案不仅“相关”，而且在完整图中唯一；
- 给最终问题增加可独立核验的限定条件。

本次候选的错误设计：

```text
主关系：RGS20 --anatomy_protein_present--> anatomy
约束：  anatomy --anatomy_protein_present--> RGS20
```

约束只是把主关系反向走了一遍，没有增加信息。当前编译器现已明确拒绝这种重复约束。
有效约束应使用不同关系或不同固定终点，例如：

```text
anatomy --expressed_in--> 某个固定组织
anatomy --part_of--> 某个固定解剖区域
```

具体可接受文案和语义还要经过题目验收，而不是只看结构能否编译。

## 5. PubMed 证据需求

`retrieve_literature` 不能自行编造 `evidence_need_id`。它必须从当前
`planner_context.open_needs` 中复制一个确切的 `need_id`。如果没有开放的
evidence need，literature 动作不会进入可选动作集合。

本次 NRF1 轨迹中：

- 模型提交的 need ID 不在当前开放集合；
- 或没有提交 need ID；
- 因此三次 literature 动作都在 gate 被拒绝，没有调用 PubMed。

## 6. 本次 RGS20 轨迹如何解读

1. RGS20 邻域查询成功，获得 50 条证据；
2. `expand_entity` 请求 50 条，但只剩余 30 条边，发生局部预算拒绝；
3. Agent 请求结束探索并将候选交给验证器；
4. 候选包含 10 个 answers，编译失败；
5. 内层循环继续，使用剩余 30 条边查询 `brain`；
6. 没有形成第二个合格候选，最终为 0/3 题，停止原因是
   `all_candidates_hard_rejected`。

另外，NRF1 轨迹中出现过两种执行器问题：

- `expand_entity` 已经读取的边，在更窄的 `query_neighbors` 中再次出现；
- 执行失败的 query 被错误地计入已执行动作，导致重试被 `duplicate_action` 拦截。

当前修复后，重复边会复用首次证据，失败动作允许重试。

## 7. 推荐阅读顺序

项目总览：

1. `docs/medical-qa-data-factory/README.md`
2. `docs/medical-qa-data-factory/development-architecture.md`
3. `docs/medical-qa-data-factory/entity-question-generation-implementation-requirements.md`

运行链路：

1. `src/medical_qa_data_factory/generation/graph.py`
2. `src/medical_qa_data_factory/generation/react_graph.py`
3. `src/medical_qa_data_factory/generation/actions.py`
4. `src/medical_qa_data_factory/generation/entity_planner.py`

候选与验证：

1. `src/medical_qa_data_factory/generation/task_contracts.py`
2. `src/medical_qa_data_factory/generation/task_compiler.py`
3. `src/medical_qa_data_factory/generation/candidate_assessment.py`
4. `src/medical_qa_data_factory/generation/task_validation.py`

前端与可观测性：

1. `docs/medical-qa-data-factory/generation-frontend-vue.md`
2. `frontend/src/App.vue`
3. `frontend/src/components/TaskRun.vue`
4. `frontend/src/components/ExecutionTrace.vue`

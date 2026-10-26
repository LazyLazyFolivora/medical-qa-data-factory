# medical-qa-data-factory 原架构重建实施计划

> 状态：实施中；阶段 0-2 与 constrained_entity 第一条纵切已落地，其余阶段未完成  
> 项目：`medical-qa-data-factory`  
> 更新日期：2026-10-26  
> 适用范围：删除独立 `mvp` 实现，基于原有 `generation / knowledge_graph / literature / domain` 骨架重新实现数据工厂

## 1. 背景与结论

当前 `src/medical_qa_data_factory/mvp/` 是一套与原项目并行的新实现。它能够在手写 fixture 上导出结构化文件，但生成的题目本质上是把预设图边翻译成自然语言，并使用同一局部 fixture 验证唯一性。

例如：

```text
Drug A --targets--> Protein X
Protein X --associated_with--> Disease D
```

由此生成：

```text
Drug A 靶向哪种蛋白？
Drug A 所靶向的蛋白，进一步与哪种疾病相关？
```

这些题在真实生物医药世界中通常具有多个答案。它们只在人工构造的极小 fixture 中唯一，因此不符合以下核心原则：

```text
有限证据用于发现候选问题；
完整、冻结、可重放的定义世界用于验收候选问题。
```

本次重建不继续修补独立 MVP，而是删除该实现，回到项目原始骨架，在真实 PrimeKG、文献 provider 和原有生成链路上实现正确的数据工厂。

## 2. 重建目标

最终系统应当完成：

1. 接收一个已经消歧的真实生物医药实体；
2. 使用真实知识图谱、文献和其他 provider 发现候选事实；
3. 从候选事实提出结构化任务意图，而不是直接自由生成题面；
4. 将任务意图编译为可执行的 `LogicalQuery`；
5. 在冻结的 `ValidationWorldSnapshot` 中重新计算完整答案集合；
6. 只有通过答案、基数、语义、证据和任务价值验证的候选才能成为 `ValidatedTask`；
7. Writer 只能把 `ValidatedTask` 表达为自然语言，不能决定答案和唯一性；
8. 发布 Planner 可见问题和 evaluator 可见隐藏目标；
9. Query Planner 在真实运行时中自行产生状态、合法动作、实际收益和成本数据；
10. 生成过程中的动作不得冒充 Query Planner 的正确动作标签。

一句话目标：

```text
从真实证据空间反向构造客观成立、答案边界明确、对 Planner 有训练价值的问题。
```

## 3. 实施原则

### 3.1 只保留一套主架构

重建后，以下目录是唯一业务主干：

- `src/medical_qa_data_factory/generation/`
- `src/medical_qa_data_factory/knowledge_graph/`
- `src/medical_qa_data_factory/literature/`
- `src/medical_qa_data_factory/domain/`
- 原有 repository、export、CLI 和 Web

不得继续维护 `mvp` 与原始 pipeline 两套职责重叠的实现。

### 3.2 候选发现与任务认证分离

候选发现可以使用：

- 局部图边；
- 图路径；
- 文献片段；
- 结构化数据库记录；
- 规则；
- 自由出题 Agent。

但候选发现只能生成 `TaskCandidate`。它不能决定：

- gold answer；
- 答案是否唯一；
- 搜索是否完整；
- 文献是否充分；
- 题目是否可以发布。

### 3.3 生成轨迹与 Planner rollout 分离

数据工厂的搜索行为用于造题和调试，应记录为：

- `GenerationTrace`
- `GenerationStep`
- `generation_yield`

Query Planner 的训练数据必须在题目发布后，由 Query Planner 正式运行时产生：

- `state_before`
- `legal_actions`
- `selected_action`
- `observation`
- `actual_cost`
- `coverage_before`
- `coverage_after`
- `observed_gain`
- `state_after`

两种轨迹不得共用 schema、loader 或导出目录。

### 3.4 不为题量牺牲质量

对一个真实实体生成零道合格题是合法结果。系统不得为了达到题数目标而：

- 从多答案关系中随意选择一个答案；
- 使用局部证据冒充完整世界；
- 增加证据不支持的限定；
- 把 association 改写为因果、治疗或预防；
- 用同义改写制造虚假多样性。

## 4. 目标主流程

```text
SeedEntity
  -> Entity Resolution
  -> Evidence Exploration
  -> Evidence Normalization
  -> Candidate Discovery
  -> TaskCandidate
  -> LogicalQuery Compiler
  -> ValidationWorld Evaluator
  -> Task Validator
       ├─ rejected -> RejectionRecord / EvidenceGap
       └─ accepted -> ValidatedTask
  -> Question Writer
  -> Round-trip Semantic Verification
  -> Blind Verification
  -> Deduplication and Value Gate
  -> PlannerQuestionInput + EvaluationTarget
```

造题循环：

```text
从实体出发
  -> 搜索一批真实证据
  -> 判断当前证据能否形成候选任务
  -> 对候选执行完整世界验证
  -> 若无法发布，判断原因
       ├─ 存在可解决的 EvidenceGap：继续针对性搜索
       ├─ GenerationSpec 规定搜索已完成：停止
       ├─ provider 失败：以失败停止或等待
       ├─ 预算耗尽：以预算耗尽停止
       └─ 已达到合格题目标：停止
```

## 5. 删除范围

### 5.1 删除源码与测试

- `src/medical_qa_data_factory/mvp/`
- `tests/mvp/`

### 5.2 删除入口与演示产物

- `cli.py` 中的 `mvp run` 命令及 handler；
- `runs/mvp-demo-20261026/`；
- 其他只服务于独立 MVP fixture 的运行产物。

### 5.3 删除或改写错误文档

- 删除只描述独立 MVP 实现的文档；
- 把错误宣称“已完成”的验收报告标记为失效或删除；
- 更新 `current-implementation-gaps.md`；
- 保留并以以下三份文档为本项目当前规范依据：
  - `development-architecture.md`
  - `acceptance-criteria.md`
  - `generated-question-types-and-language-criteria.md`

### 5.4 删除安全

当前项目目录不是 Git 仓库。正式删除前必须先创建可恢复快照，记录：

- 删除文件清单；
- 文件内容 hash；
- 当前测试结果；
- 当前依赖环境。

快照仅用于恢复，不继续参与项目运行。

## 6. 阶段 0：恢复原始基线

### 6.1 工作内容

1. 建立删除前快照；
2. 删除第 5 节列出的 MVP 内容；
3. 使用 `rg` 清除所有 `medical_qa_data_factory.mvp` 引用；
4. 修复测试环境中缺失的 `langgraph` 依赖；
5. 运行原始 pipeline、CLI 和 Web 测试；
6. 运行真实 PrimeKG 只读集成测试；
7. 输出 baseline report。

### 6.2 完成门槛

- `medical-qa-data-factory mvp` 命令不存在；
- 代码中不存在 `medical_qa_data_factory.mvp` 引用；
- 原始 pipeline 可以启动；
- 默认测试可以完整收集；
- 非外部服务测试全部通过；
- PrimeKG 集成测试能够单独运行；
- 所有跳过项均记录为 `not_evaluated`，不得写成 passed。

## 7. 阶段 1：统一核心数据契约

### 7.1 需要建立或补齐的契约

- `ValidationWorldSnapshot`
- `TaskCandidate`
- `TaskSpec`
- `ValidatedTask`
- `InformationRequirement`
- `AcceptableEvidenceSet`
- `AnswerContract`
- `ValidationReport`
- `PlannerQuestionInput`
- `EvaluationTarget`

### 7.2 主要修改位置

- `generation/contracts.py`
- `domain/questions.py`
- `domain/evidence.py`
- `generation/logical_query.py`

### 7.3 强制不变量

```text
TaskCandidate != TaskSpec != ValidatedTask != QuestionSpec
```

- Candidate 不能直接交给 Writer；
- Candidate 不能直接导出为正式数据；
- 只有 `ValidationReport.passed == true` 才能构造 `ValidatedTask`；
- ValidationReport 必须绑定 candidate、query、snapshot、validator version 和输入 digest；
- Writer 不能修改答案、query、filters、qualifiers 或 cardinality；
- Planner 公开视图不能包含答案、requirements 和验证路径；
- 所有正式 artifact 必须引用不可变版本。

### 7.4 完成门槛

- 通过 schema round-trip 测试；
- Candidate 被 exporter 和 Writer 拒绝；
- 失败 ValidationReport 无法提升任务；
- 任一语义字段变化都会改变 query/task digest；
- 公开视图泄漏扫描通过。

## 8. 阶段 2：建立真实 Validation World

这是重建工作的核心阶段。

### 8.1 PrimeKG ValidationWorldSnapshot

每次正式验收必须固定：

- provider ID；
- snapshot ID；
- schema version；
- 数据 manifest 或内容 hash；
- relation semantics policy version；
- 查询能力；
- 分页和最大返回限制；
- 创建时间和数据路径标识。

### 8.2 PrimeKGWorldEvaluator

新增生产级 evaluator，直接针对完整 PrimeKG 快照执行 `LogicalQuery`，不得使用候选发现阶段的局部边集合代替完整世界。

必须支持：

- forward/reverse direction；
- entity type filter；
- relation filter；
- 多条件交集；
- 两跳完整分支；
- 去重；
- cardinality；
- pagination；
- truncation detection；
- snapshot mismatch rejection。

两跳查询必须执行：

```text
bridges = ALL(seed --r1--> bridge)
answers = UNION(ALL(bridge --r2--> answer) for bridge in bridges)
```

不能只执行候选生成器选中的一条路径。

### 8.3 文献世界边界

文献 top-k 检索不能证明开放世界唯一性。第一版约束：

- 文献可以支持关系措辞、限定条件、极性和 claim；
- 文献不得单独证明“现实世界只有一个答案”；
- 文献单答案题必须有封闭候选集，或明确属于固定文献集合的 source-bounded task；
- 文献检索必须保存 query、检索日期、snapshot/index version、文档/chunk ID、原文 span 和 hash；
- provider failure、timeout 和 incomplete pagination 不得解释为“无证据”。

### 8.4 完成门槛

必须通过以下对抗用例：

1. 局部 slice 只有 `Protein X`，完整世界还有 `Protein Y`：单答案拒绝；
2. 候选只发现一个 bridge，其他 bridge 能到达其他答案：单答案拒绝；
3. query 结果截断：拒绝；
4. snapshot 不一致：拒绝；
5. filter 漏失后答案增加：拒绝；
6. provider timeout：记录失败，不记录 no evidence；
7. 完整世界答案严格为一个实体：允许进入后续验证。

## 9. 阶段 3：重写候选任务发现

候选发现只输出结构化任务意图，不直接生成自然语言问题。

### 9.1 第一版支持题型

1. 单关系实体定位；
2. 带约束的唯一实体选择；
3. 隐藏中间实体的两跳问题；
4. 显式中间线索的两跳问题。

开放列表题、数量题、是非题、开放机制解释、比较推荐和临床决策题暂缓。

### 9.2 单关系实体定位

单关系查询只有在完整世界答案集合严格为 1 时才能作为单答案候选。

例如：

```text
阿司匹林作用于哪种蛋白？
```

如果完整 PrimeKG 查询返回多个蛋白，则必须拒绝，不得选取其中一个作为 gold。

### 9.3 带约束的唯一实体选择

优先生产这类题：

```text
C1 = 满足条件 A 的完整实体集合
C2 = 满足条件 B 的完整实体集合
Answer = C1 ∩ C2
```

只有 `|Answer| == 1`，且所有产生唯一性的约束都真实出现在题面中，才允许发布单答案题。

这类题通常比普通一跳题更有 Planner 价值，因为不同约束可以对应不同证据需求。

### 9.4 隐藏中间实体的两跳问题

必须合并所有第一跳 bridge 的第二跳答案。只有完整答案并集严格为 1 时，才能使用“哪种”“哪个”等单答案措辞。

### 9.5 显式中间线索的两跳问题

题面可以公开中间实体，但仍需完整执行最终 query 并验证答案基数。公开 bridge 只改变问题难度，不降低正确性要求。

### 9.6 自由出题 Agent

允许 Agent：

- 提出值得研究的任务意图；
- 推荐可能具有判别力的真实约束；
- 发现纯路径规则难以提出的问题；
- 为候选标注预期信息需求。

禁止 Agent：

- 直接确定 gold；
- 自行声明唯一性；
- 自行声明搜索充分；
- 自行把文献关联提升为因果；
- 绕过 Compiler 和 Validator 发布题目。

### 9.7 QuestionValueGate

在昂贵验证前增加任务价值门禁，拒绝：

- 纯 relation 中文翻译；
- 使用“某药物”“某蛋白”等未绑定实体的问题；
- 无限定的广泛“相关”问题；
- 答案空间天然开放的单答案问法；
- 只在生成 slice 中唯一的问题；
- 没有真实信息需求差异的问题；
- 同一任务的表面同义改写；
- 没有生物医药意义的图结构偶然路径。

## 10. 阶段 4：Task Compiler 与 Validator

### 10.1 Task Compiler

Compiler 将 TaskCandidate 编译为：

- 强类型 `LogicalQuery`；
- start entity；
- answer variable；
- ordered steps；
- direction；
- filters；
- qualifiers；
- temporal scope；
- expected cardinality；
- `InformationRequirement[]`；
- `AcceptableEvidenceSet[]`；
- validation world reference。

Compiler 不写题面，也不把 candidate answer 直接写入 gold。

### 10.2 Task Validator

Validator 必须：

1. 在完整 ValidationWorldSnapshot 中执行 query；
2. 得到 authoritative world answer set；
3. 对比 slice answer set 和 world answer set；
4. 检查 cardinality；
5. 检查 query 路径连续性；
6. 检查 snapshot 和版本；
7. 检查分页和截断；
8. 检查 shortcut；
9. 检查 evidence provenance；
10. 检查关系方向、极性、因果强度和 qualifier；
11. 检查题型是否在支持范围内；
12. 生成不可变 ValidationReport；
13. 通过后才产生 ValidatedTask。

### 10.3 稳定拒绝码

至少包括：

- `world_cardinality_mismatch`
- `answer_set_truncated`
- `snapshot_mismatch`
- `unsupported_claim`
- `qualifier_not_supported`
- `relation_semantics_violation`
- `shortcut_detected`
- `ambiguous_seed`
- `broken_query_path`
- `provider_failure`
- `incomplete_search`
- `low_question_value`
- `unsupported_question_type`

### 10.4 完成门槛

- Golden Suite 中所有 hard gate 100% 通过；
- 任一拒绝都有枚举 reason code；
- world answer 只能来自 evaluator；
- Writer 无法接收未验证任务；
- slice 唯一、world 多解用例不会误通过。

## 11. 阶段 5：Writer 与题面语义回环

### 11.1 Writer 输入

Writer 只能看到：

- ValidatedTask；
- 允许公开的实体名称；
- LogicalQuery 的公开语义；
- 必须保留的真实限定；
- 禁止泄漏的答案和中间实体；
- 语言与风格配置。

### 11.2 Writer 权限

Writer 只能产生 `QuestionVariant`，不能修改：

- gold answer；
- answer cardinality；
- query steps；
- filters；
- qualifiers；
- temporal scope；
- validation world；
- information requirements。

### 11.3 题面验证链

每个 QuestionVariant 必须经过：

1. 答案和别名泄漏扫描；
2. 中间实体泄漏扫描；
3. 关系语义强度检查；
4. qualifier 保留检查；
5. round-trip query reconstruction；
6. blind answer verification；
7. 自然度和领域语言检查；
8. 近重复检查。

核心门禁：

```text
normalize(parse(question_text)) == normalize(original LogicalQuery)
```

不能只通过关键词黑名单判断题面语义是否一致。

### 11.4 完成门槛

- 漏掉 population、study、time、dose 或 exposure qualifier 的题面被拒绝；
- association 被写成导致、治疗或预防时被拒绝；
- 隐藏答案或中间实体泄漏时被拒绝；
- 题面可重建为与原 query 等价的结构；
- blind verifier 得到的答案集合与 world evaluator 一致。

## 12. 阶段 6：在原始 Generation Service 中实现循环

### 12.1 主要修改位置

- `generation/service.py`
- `generation/react_graph.py`
- `generation/exploration.py`
- `generation/evidence_needs.py`
- `generation/sufficiency.py`
- `generation/repository.py`

### 12.2 循环职责

Generation Service 负责：

- 执行 GenerationSpec 规定的探索性搜索；
- 保存 evidence 和 normalized claims；
- 调用多种 Candidate Source；
- 对候选执行针对性补搜；
- 在 episode 结束时使用最终证据重新验证所有候选；
- 保存拒绝原因和 EvidenceGap；
- 如实记录停止原因。

### 12.3 停止原因

- `question_targets_met`
- `required_searches_completed`
- `search_saturated`
- `budget_exhausted`
- `provider_unavailable`
- `no_valid_candidates`
- `cancelled`

不得把 budget exhausted、provider failure 或 incomplete search 写成 no evidence。

### 12.4 生成轨迹重命名

凡是记录造题 Agent 行为的数据，应使用 generation 命名。原有名称若暗示它是 Query Planner gold，应完成 schema migration：

```text
PlannerTrajectory -> GenerationTrace
PlannerStep       -> GenerationStep
evidence_gain     -> generation_yield / new_claim_count / task_progress
```

历史数据 loader 必须显式识别旧版本，不能静默当作新 Planner rollout。

## 13. 阶段 7：对接 Query Planner 数据契约

### 13.1 当前兼容问题

Query Planner 当前 `Question` 把公开问题和 `requirement_groups` 放在同一个对象中。虽然 evaluator 才应读取 requirements，但物理数据边界仍存在泄漏风险。

### 13.2 数据工厂发布视图

Planner 可见文件：

```json
{
  "qid": "...",
  "question": "...",
  "qtype": "...",
  "budget": {},
  "max_steps": 8,
  "sufficiency_threshold": 1.0,
  "environment_ref": "..."
}
```

Evaluator 可见文件：

```json
{
  "qid": "...",
  "answer_contract": {},
  "information_requirements": [],
  "acceptable_evidence_sets": [],
  "completion_rule": {},
  "gold_claim_ids": [],
  "validation_world_ref": "...",
  "validation_report_ref": "..."
}
```

### 13.3 Query Planner 侧改造

1. policy loader 只加载公开问题；
2. evaluator 按 qid 单独加载 EvaluationTarget；
3. KG claim pattern 可兼容映射为 Triple alternatives；
4. 文献和异质证据不得强行压成 Triple；
5. 增加 source-neutral requirement matcher；
6. rollout 从空 EvidenceStore 开始；
7. 每一步保存完整 legal action set；
8. selected action 必须来自 legal action set；
9. gain 由 requirement coverage 前后变化计算；
10. cost 使用执行器实际记录，不由数据工厂估计。

### 13.4 输出边界

medical-qa-data-factory 只输出任务真值：

- 自然语言问题；
- answer contract；
- information requirements；
- acceptable evidence sets；
- completion rule；
- validation world；
- evidence provenance。

它不输出：

- correct action；
- best action；
- expected route；
- 伪造的 action gain；
- 伪造的 action cost；
- 最优停止步骤。

## 14. 阶段 8：CLI、Web 与审核界面

### 14.1 正式入口

继续使用：

```text
medical-qa-data-factory generate entity
```

不得重新增加独立 `mvp run`。

### 14.2 CLI 输出

至少返回：

- task ID；
- episode ID；
- seed identity；
- snapshot ID；
- candidate count；
- validated task count；
- published question count；
- rejection distribution；
- stop reason；
- artifact paths。

### 14.3 Web 审核页面

审核者需要看到：

- 种子实体；
- generation slice；
- LogicalQuery；
- slice answer set；
- world answer set；
- 所有 bridge；
- filters 和 qualifiers；
- evidence provenance；
- rejection reason；
- Writer 题面；
- round-trip 结构；
- blind verification；
- Planner 公开输入；
- evaluator 隐藏目标。

审核页面必须能回答：

```text
为什么这道题在声明的完整世界中只有这个答案？
```

## 15. 阶段 9：测试与正式验收

### 15.1 Golden Suite

为四种第一版题型分别准备：

- 至少 20 个经过人工确认的正例；
- 至少 20 个 hard negative。

必须覆盖：

- 局部唯一、全集多解；
- 多 bridge；
- 错误方向；
- 关系被增强为因果；
- qualifier 丢失；
- 查询截断；
- provider failure；
- snapshot mismatch；
- 答案泄漏；
- 中间实体泄漏；
- shortcut；
- 同义改写重复；
- generation trace 与 Planner rollout 混淆。

### 15.2 真实 PrimeKG Pilot

选择至少 20 个真实实体，覆盖：

- 药物或 exposure；
- gene/protein；
- disease；
- pathway；
- phenotype。

每个实体不强制产生问题。Pilot 记录：

- 每实体候选数；
- 完整世界验证通过率；
- 各拒绝码数量；
- 人工接受率；
- 各题型分布；
- 每道题的 world answer cardinality；
- provider failure 和 incomplete query 数量。

### 15.3 人工审核

每道发布题至少检查：

- 生物医药语义是否成立；
- 是否自然、明确、独立可读；
- 是否完整表达所有真实约束；
- 是否还存在其他合理答案；
- 关系方向和强度是否正确；
- 证据是否支持 population、time、dose 等限定；
- 是否有明显 shortcut；
- 是否适合训练目标 Query Planner；
- 是否产生真实的异质信息需求。

### 15.4 发布门禁

第一版只有在以下条件全部满足时才能称为完成：

- 所有 gold answer 都由真实 ValidationWorldSnapshot 复算；
- 单答案题的 world answer set 严格为 1；
- 不存在未处理的 truncation 或 incomplete pagination；
- 所有发布题面通过 round-trip 和 blind verification；
- Planner 输入不存在 gold 泄漏；
- 至少完成一次真实 PrimeKG Pilot；
- 至少完成人工抽检并达到验收阈值；
- Query Planner 可以读取发布数据并完成真实 rollout；
- generation trace 没有被当作 Planner supervision；
- 所有 P0/P1 缺陷为零。

## 16. 推荐实施顺序

开发必须按以下顺序推进：

1. 删除错误 MVP，恢复原始测试基线；
2. 建立 ValidationWorldSnapshot；
3. 实现 PrimeKGWorldEvaluator；
4. 补全 LogicalQuery 对 filters 和 qualifiers 的表达；
5. 只实现“带约束的唯一实体选择”一种高价值题型；
6. 用一个真实实体完成端到端生成和人工检查；
7. 增加隐藏两跳、显式两跳和少量真正唯一的一跳题；
8. 完成 TaskValidator 和稳定拒绝码；
9. 完成 Writer round-trip；
10. 完成 PlannerQuestionInput 与 EvaluationTarget 分离导出；
11. 对接 Query Planner 正式 rollout；
12. 完成 20 个真实实体 Pilot；
13. 通过人工审核后再考虑批量生产和更多题型。

关键中间门禁：

```text
如果系统不能从一个真实实体生成至少一道经完整世界验证、
且人工认为有研究和 Planner 训练价值的问题，
则停止扩展框架，先修正出题与验证方法。
```

## 17. 预期代码改动地图

| 区域 | 处理方式 | 主要职责 |
|---|---|---|
| `mvp/` | 删除 | 不再保留第二套实现 |
| `knowledge_graph/providers/primekg.py` | 扩展 | 完整世界查询与 snapshot 能力 |
| `knowledge_graph/contracts.py` | 扩展 | 查询完整性、分页、截断、manifest |
| `generation/logical_query.py` | 扩展 | 强类型任务语义与完整执行 |
| `generation/probe.py` | 改造 | 候选可回答性探测，不认证 gold |
| `generation/composition.py` | 改造 | 约束交集和合法关系组合 |
| `generation/shortcuts.py` | 强化 | 真实 shortcut 验收 |
| `generation/contracts.py` | 整合 | Candidate、Task、ValidationWorld 等契约 |
| `generation/writing.py` | 收窄权限 | 只做 ValidatedTask 到题面表达 |
| `generation/blind_verification.py` | 强化 | round-trip、qualifier、语义等价 |
| `generation/service.py` | 重构编排 | 真实造题循环 |
| `generation/react_graph.py` | 限定用途 | 候选探索和 generation trace |
| `generation/training.py` | 拆分/迁移 | 不再把生成动作当 Planner 标签 |
| `generation/training_export.py` | 改造 | 公开问题与隐藏目标分离 |
| `repository.py` | 扩展 | 保存不可变 task/query/report artifact |
| `export.py` | 扩展 | 引用完整、无泄漏的正式发布 |
| `web/` | 改造 | 展示 slice/world、query 和验证证据 |
| `cli.py` | 清理与改造 | 删除 mvp，保留正式 generate 入口 |

## 18. 最终交付物

正式交付至少包括：

1. 删除独立 MVP 后的单一代码主干；
2. 版本化 ValidationWorldSnapshot；
3. PrimeKGWorldEvaluator；
4. 四类支持题型的结构化 Candidate Source；
5. Task Compiler；
6. Task Validator；
7. Writer 和 round-trip verifier；
8. PlannerQuestionInput；
9. EvaluationTarget；
10. evidence catalog；
11. generation trace；
12. validation report 与 rejection records；
13. dataset manifest 和 quality report；
14. Golden Suite；
15. 真实 PrimeKG Pilot；
16. 人工审核报告；
17. Query Planner 集成样例和 rollout 验收报告。

## 19. 不属于本次重建的内容

本次不负责：

- 预先枚举所有 Planner 动作；
- 为 Planner 指定固定路线；
- 伪造 action gain 或 cost；
- 训练最终模型；
- 开放式临床建议；
- 自动生成不可验证的机制解释；
- 为达到题量目标降低唯一性和证据标准。

本次重建首先解决一个问题：

```text
能否从真实生物医药证据中，稳定地产生少量真正正确、
答案边界明确、对 Query Planner 有训练价值的问题？
```

只有这个问题得到肯定答案后，才进入规模化生产阶段。

# medical-qa-data-factory 验收标准

> 状态：当前实施依据  
> 项目：`medical-qa-data-factory`  
> 更新日期：2026-10-26

## 1. 验收目标

第一阶段不是证明平台已经覆盖全部生物医药知识，而是证明它能够：

1. 从一个实体出发搜索证据；
2. 生成若干语义不同的问题；
3. 为每个发布问题提供有来源、不过度表达的答案；
4. 在证据不足时选择补搜或放弃候选；
5. 如实记录为什么停止；
6. 向 Query Planner 隐藏答案和固定路线，同时提供评估所需的隐藏元数据。

验收按三个层级执行：

- **MVP P0**：发布数据必须满足；任何一项失败都不能发布；
- **P1**：提高自动化、搜索效率和多源一致性，不阻塞首个 MVP；
- **P2**：大规模生产可靠性，不阻塞功能验证。

## 2. MVP P0：单题发布门槛

### 2.1 答案有证据

| ID | 通过条件 |
|---|---|
| MVP-E01 | gold answer 至少由一条可定位的 EvidenceRecord 支持 |
| MVP-E02 | 结构化证据能定位到 provider、版本和记录；文献证据能定位到文献标识和支持结论的文本 span |
| MVP-E03 | provider 超时、分页失败或返回被截断时，该结果不能作为完整答案证明 |
| MVP-E04 | Candidate 或 Agent 提出的答案必须经过独立 Validator，不能直接复制为 gold |

### 2.2 问题没有超过证据

| ID | 通过条件 |
|---|---|
| MVP-S01 | 关系方向、实体类型和极性正确 |
| MVP-S02 | association 不写成 causality，风险相关不写成预防、治疗或疗效 |
| MVP-S03 | 会改变结论的人群、疾病亚型、剂量或时间范围没有被静默删除 |
| MVP-S04 | 题面与结构化 TaskSpec 的实体、关系、限定和答案类型一致 |

第一版可以使用模板、规则、LLM 审核和人工抽检共同完成语义验证，不要求实现形式化自然语言回译器。

### 2.3 不制造已知的唯一性错误

| ID | 通过条件 |
|---|---|
| MVP-A01 | 单答案题在 `GenerationSpec` 声明的验证来源和查询范围内没有已知第二答案 |
| MVP-A02 | 已知存在多个合法答案时，不能只保留生成器最先看到的一个 |
| MVP-A03 | 无法合理确认唯一性时，增加真实限定、改为边界明确的集合题，或者拒绝候选 |
| MVP-A04 | 两个别名指向同一 canonical entity 时只算一个答案；同名异实体不能静默合并 |

这里要求的是一次有记录的针对性额外答案检查，不要求穷尽现实世界所有数据库和文献。

### 2.4 任务元数据足够用于评估

每个发布问题至少具有：

- `qid`；
- 自然语言问题；
- `AnswerContract`；
- 一个或多个 `InformationRequirement`；
- 简单、确定性的 `completion_rule`；
- 支持 gold answer 的 claim 和 evidence 引用；
- 验证来源及版本。

通过条件：给定同一 EvidenceStore，requirement matcher 总能得到相同的已满足需求集合和完成状态。

MVP 允许 requirement 先使用较小的 predicate schema，不要求第一版完成所有 KG 与文献 claim 的跨来源等价映射。

### 2.5 Planner 数据隔离

| ID | 通过条件 |
|---|---|
| MVP-P01 | Planner 可见文件不包含 gold answer、gold evidence、requirements 或 completion rule |
| MVP-P02 | 隐藏 EvaluationTarget 与 Planner 输入分文件，只通过 `qid` 连接 |
| MVP-P03 | 数据中不存在作为监督真值的 `correct_action`、`best_action`、`expected_route` 或 `optimal_trajectory` |
| MVP-P04 | 动作 gain 和 cost 不由出题器伪造；它们由后续 rollout 的状态变化和真实执行记录产生 |

### 2.6 最小造题循环

MVP 循环为：

```text
探索性搜索
  -> 生成候选题
  -> 针对性验证
       ├─ 通过：保留候选
       ├─ 有明确缺口且值得继续：定向补搜
       └─ 不可靠或成本过高：放弃候选
  -> 达到目标或触发停止条件
  -> 用最终 EvidenceBundle 统一验证并发布
```

MVP 只要求以下停止状态：

| stop reason | 含义 |
|---|---|
| `question_targets_met` | 题目目标达到，拟发布题已完成最终验证 |
| `required_searches_completed` | GenerationSpec 规定的搜索完成，没有更多合格题 |
| `budget_exhausted` | 仍可能继续，但预算已达到上限 |
| `provider_unavailable` | 必需外部来源失败，搜索没有正常完成 |
| `policy_stopped` | 生成策略主动停止，并保存简短原因 |

必要门禁：

- 探索性搜索不要求绑定 EvidenceGap；
- 针对某个候选题进行验证性补搜时，必须说明缺少什么；
- `budget_exhausted` 和 `provider_unavailable` 不能写成“没有证据”；
- 达到题目数量不能跳过拟发布题目的最终验证；
- episode 中途生成的题只算 candidate，正式发布以最终 EvidenceBundle 的验证结果为准。

### 2.7 题目多样性与去重

给定证据足够的实体，系统至少能够生成两个规范任务语义不同的问题。以下情况不算不同问题：

- 只替换“哪个”“哪一种”“请问”等表面措辞；
- 只改变语序；
- 同一个 TaskSpec 的多种语言改写。

任务目标、关系、约束、所需 claim 组合或答案粒度发生变化，才可以计为不同任务。

### 2.8 题目验收样例

下面的样例用于说明“什么题可以通过”，不是把示例中的事实直接当作生产 gold。正例必须先在实际 `ValidationEnvironment` 中得到同样的答案、基数和语义验证结果。

#### 2.8.1 正例使用的验证 fixture

假设测试环境已经保存并验证以下 claim：

```text
HER2 receptor --encoded_by--> ERBB2 gene
ERBB2 gene --located_on--> human chromosome 17

Drug A --targets--> Protein X
Protein X --associated_with--> Disease D
Protein Y --associated_with--> Disease D

Study S reports:
在 Population P 中，Exposure E 与 Disease Q 风险降低相关。
```

并且针对每道题执行了必要的额外答案检查：

- 在声明的结构化数据范围内，只有 `ERBB2` 编码 HER2；
- 在声明范围内，只有 `Protein X` 同时满足“被 Drug A 靶向”和“与 Disease D 相关”；
- Study S 在 Population P 中报告的目标结果只有 `Disease Q`；
- 所有关系方向、实体类型和文献限定均已核验。

#### 2.8.2 可以通过的题目

| 题目 | Gold answer | 为什么可以通过 |
|---|---|---|
| `HER2 受体由哪个基因编码？` | `ERBB2` | 关系明确；规定范围内答案唯一；答案可由结构化证据复算 |
| `哪个基因既编码 HER2 受体，又位于人类 17 号染色体？` | `ERBB2` | 两个真实约束的交集为单元素，不依赖模糊的“相关” |
| `编码 HER2 受体的基因位于哪条人类染色体？` | `17 号染色体` | 两跳关系方向明确；所有合法中间节点展开后终点仍唯一 |
| `Drug A 所靶向且与 Disease D 相关的蛋白是哪一种？` | `Protein X` | `Protein Y` 只满足疾病关联，不满足药物靶向；交集唯一 |
| `Study S 报告，在 Population P 中，Exposure E 与哪种疾病的风险降低相关？` | `Disease Q` | 问题明确限定到具体研究和人群；使用“相关”而非“预防” |

上述题目仍然不是仅凭句子就自动通过。例如，如果实际验证发现 HER2 对应多个编码记录，第一题必须拒绝；如果 Study S 同时报告多个疾病结局，最后一题必须改为集合问法或拒绝。

#### 2.8.3 不能通过的题目

| 题目 | 不通过原因 | 处理方式 |
|---|---|---|
| `阿司匹林已知作用于哪种蛋白？` | 通常可能存在多个靶点；没有约束时不能假定单答案 | 增加由真实证据支持的判别约束，或改为有封闭范围的集合题 |
| `与 BRCA1 相关的蛋白参与哪种生物过程？` | 起始蛋白、关系和过程均可能多解；“相关”含义过宽 | 固定具体蛋白和关系，增加能够被执行的约束 |
| `某药物靶点所参与的通路与哪种疾病相关？` | 药物、靶点、通路和疾病均未确定，存在多层开放变量 | 给出锚点实体并把每一步关系写清楚 |
| `在当前证据范围内，阿司匹林使用与哪种癌症的风险降低相关？` | 用不可复现的“当前证据范围”掩盖答案完整性问题，而且可能存在多个合法癌症结局 | 使用真实研究、人群、时间或数据版本限定 |
| `Exposure E 可以预防哪种疾病？` | fixture 只支持风险降低相关，题面升级成因果和预防 | 改为“与哪种疾病风险降低相关” |
| `PrimeKG 中 HER2 的 tail 节点是什么？` | 暴露数据库和图结构实现语言，不是自然生物医药问题 | 改为“HER2 受体由哪个基因编码？” |
| `Drug A 与什么有关？` | 答案类型和关系语义都不明确 | 明确询问靶点、疾病、通路或其他具体意图 |
| `列出 Drug A 的所有相关疾病。` | “相关”过宽且开放世界中难以证明“所有” | 指定关系类型和封闭的数据或研究范围，否则拒绝 |

#### 2.8.4 可以修复，但必须重新验证

下面的修改只是形成更好的候选，修改后仍要重新计算答案，不能直接通过：

| 原问题 | 候选修改 | 还需要验证什么 |
|---|---|---|
| `Drug A 作用于哪种蛋白？` | `Drug A 所靶向且与 Disease D 相关的蛋白是哪一种？` | 两个条件的完整交集是否严格只有一个蛋白 |
| `Exposure E 可以预防哪种疾病？` | `Study S 报告，在 Population P 中，Exposure E 与哪种疾病风险降低相关？` | 文献 span 是否支持关系、极性、人群和唯一结局 |
| `与 BRCA1 相关的蛋白参与哪种过程？` | `Protein X 参与哪条 DNA 修复通路？` | `Protein X` 是否为正确锚点，以及规定范围内是否只有一条合法通路 |

#### 2.8.5 一个完整通过样例

```yaml
planner_question:
  qid: task.fixture.drug-a.001
  question: Drug A 所靶向且与 Disease D 相关的蛋白是哪一种？
  qtype: constrained_entity

evaluation_target:
  qid: task.fixture.drug-a.001
  answer_contract:
    answer_type: entity
    canonical_answers: [Protein X]
    cardinality: exactly_one
    scope_statement: fixture-v1
  information_requirements:
    - id: req-1
      claim: Drug A targets Protein X
    - id: req-2
      claim: Protein X associated_with Disease D
  completion_rule:
    all_of: [req-1, req-2]
  gold_evidence:
    - evidence-for-drug-a-targets-protein-x
    - evidence-for-protein-x-associated-with-disease-d
```

Planner 只能收到 `planner_question` 部分。`evaluation_target` 由 evaluator 私下持有，用来判断证据覆盖和最终答案；它不表示 Planner 必须先执行靶点查询或必须使用某个 provider。

## 3. MVP P0 自动化回归用例

### 3.1 Agent 幻觉

Agent 提出证据中不存在的答案或关系。候选必须以 `claim_not_supported` 拒绝，Writer 不能通过常识补齐。

### 3.2 局部单解但规定范围多解

候选只看到 `Drug A -> Protein X`，规定验证查询同时返回 `Protein Y`。单答案问法必须拒绝或改写，不能丢弃 `Protein Y`。

### 3.3 文献相关被写成因果

证据只支持“X 与 Y 相关”，Writer 输出“X 导致、预防或治疗 Y”。题面必须拒绝。

### 3.4 文献命中但不支持 claim

标题或关键词包含目标实体，但保存的 span 没有表达目标关系。该结果不能成为 gold evidence。

### 3.5 查询失败与无证据分离

分别模拟超时、分页失败、预算耗尽和 provider 不可用。系统必须记录真实状态，不能生成 `required_searches_completed` 或“没有证据”的结论。

### 3.6 规定搜索正常完成

GenerationSpec 规定的查询均正常完成，但没有形成更多合格任务。允许以 `required_searches_completed` 停止，不要求继续尝试无限关键词或所有外部来源。

### 3.7 达到题量但候选未验证

题目数量已经达标，但其中一题仍缺少唯一性检查或存在未解决冲突。不得以 `question_targets_met` 发布该题。

### 3.8 最终 EvidenceBundle 推翻早期候选

第一轮候选认为答案只有 `Protein X`，后续证据出现 `Protein Y`。episode 结束时统一验证必须拒绝原单答案题。MVP 不要求中途实时撤回。

### 3.9 重复改写

同一 TaskSpec 生成三个语言版本，最终只能计为一个规范任务。

### 3.10 Planner 泄漏扫描

扫描 Planner 发布文件及嵌套字段，确认不包含答案、requirements、gold evidence、supporting path 或正确动作字段。

### 3.11 Gain 可以由状态差计算

给定：

```text
S0: 无 requirement 满足
S1: req-1 满足
S2: req-1 和 req-2 满足
```

labeler 能确定性得到新增 requirement；重复证据不产生新增 coverage。测试不需要预设哪个动作正确。

## 4. MVP 数据集验收

首个试运行不强制 1,000 条。建议先选择 20 至 50 个证据较丰富的实体，验证闭环是否成立。

发布版本必须报告：

- 输入实体数；
- EvidenceRecord 和 NormalizedClaim 数；
- 候选题数、通过题数和主要拒绝原因；
- 每个实体的合格题数；
- question type、predicate、answer type 和来源组合分布；
- exact duplicate 和规范任务重复数量；
- 各 stop reason 数量；
- provider 失败、预算耗尽和不完整查询数量。

MVP 发布硬指标：

- 发布样本 P0 检查通过率：100%；
- gold evidence provenance 完整率：100%；
- Planner gold 泄漏数：0；
- 人工抽检发现的明确事实错误：0；
- 同一规范 TaskSpec 跨 split：0。

不要求第一版达到：

- 所有实体都能生成固定数量的问题；
- 各题型均匀分布；
- 所有 claim 都有 KG 和文献双重支持；
- 对开放世界证明答案绝对完整。

## 5. P1：自动化和搜索质量

以下项目有价值，但不阻塞 MVP：

1. 更结构化的 EvidenceGap 和搜索优先级；
2. KG 与文献 observation 对同一 requirement 的等价匹配；
3. `all_of`、`any_of` 和 alternative evidence set 的复杂组合；
4. 基于连续无产出的 `search_saturated` 判断；
5. 自动题面回译和更严格的语义等价检查；
6. 更完整的题型覆盖、难度控制和数据分层；
7. 基于实体家族、claim 和任务簇的防泄漏 split；
8. 自动化质量漏斗和拒绝原因分析。

P1 对应回归用例包括：

- 同一个 source-neutral requirement 可由合格 KG 或文献 claim 满足；
- hybrid task 需要多个 claim 时，缺少任一必需项都不能完成；
- 连续无产出达到配置阈值后才允许 `search_saturated`；
- 自动语义检查能够发现方向、基数、限定和因果强度变化。

## 6. P2：生产可靠性

以下项目只在需要大规模、长期运行时实施：

1. 完整 SearchFrontier 与查询方向状态管理；
2. 每轮 checkpoint、崩溃恢复和幂等重放；
3. provisional task 增量发布；
4. 新证据触发依赖任务自动失效和版本化；
5. 多 provider 的完整性与冲突治理；
6. 大规模数据集版本、血缘和长期审计；
7. 更严格的覆盖率、吞吐量和成本 SLO。

P2 可以要求逐轮 episode 日志和恢复后一致性，但这些不是首个可用版本的通过条件。

## 7. 人工抽检

每个发布版本按题型和来源组合抽样，检查：

1. 问题是否自然、明确且独立可读；
2. 答案是否被引用证据支持；
3. 文献语义是否被夸大；
4. 是否存在当前已知但被遗漏的答案；
5. requirements 是否足以支持答案；
6. 是否错误规定 Planner 的工具路线；
7. Planner 视图是否泄漏隐藏真值。

发现系统性错误时，应增加相应回归测试，而不是只删除被抽中的坏样本。

## 8. MVP 验收输出物

MVP 必须提供：

1. `planner_questions.jsonl`；
2. `evaluation_targets.jsonl`；
3. `evidence_catalog.jsonl`；
4. `validation_reports.jsonl`；
5. `rejected_candidates.jsonl`；
6. `dataset_manifest.json`；
7. `quality_report.json`；
8. 自动化测试结果；
9. 人工抽检记录；
10. 已知限制和未支持题型清单。

`generation_episodes.jsonl` 在 MVP 中可以只保存 episode 级摘要；P2 才要求完整逐轮状态和恢复信息。

仅展示若干“看起来合理”的题目不能通过验收。MVP 的最低证明是：答案有来源、表达不过度、没有已知唯一性错误、停止状态诚实、Planner 看不到 gold 或固定动作路线。

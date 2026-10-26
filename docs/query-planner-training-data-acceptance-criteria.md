# 生物医药问题生成与 Query Planner 训练数据验收标准

> 状态：目标架构验收规范  
> 受众：开发、测试、数据工程、算法和验收负责人  
> 更新日期：2026-10-25  
> 关联架构：[生物医药问题生成与 Query Planner 训练数据架构](query-planner-training-data-architecture.md)

## 1. 文档目的

本文定义目标架构完成到什么程度才可以合并、试运行和发布训练数据。验收对象包括：

- 候选任务生成；
- `LogicalQuery` 编译与执行；
- 任务验证；
- 自然语言题面生成与语义验证；
- Query Planner 隔离 rollout；
- stopping 和 action selection 标签；
- 数据集导出、切分、复现和运行可靠性。

本文不使用“流水线成功运行”或“生成了一定数量的问题”作为质量验收结论。系统验收必须证明：发布问题的任务语义正确、下游 rollout 无 gold 泄漏、训练标签有可复核依据。

现有 `docs/medical-qa-data-factory/acceptance-report-v1.md` 只证明旧单跳基线和图探索可达性，不覆盖本文定义的新任务验证、题面语义等价、Planner rollout 和标签正确性，不能作为目标架构的最终验收报告。

## 2. 验收原则

### 2.1 两级验收

验收分为两个层次：

1. **Artifact 级硬门禁**：判断单个 `ValidatedTask`、`QuestionSpec`、`PlannerRollout` 或训练样本是否允许发布；
2. **系统级发布门禁**：判断当前代码、配置、快照和模型组合是否具备批量生产资格。

Artifact 级门禁不允许用平均值抵消失败。例如 99% 的题答案正确，不代表剩余 1% 可以进入正式数据集。

### 2.2 指标分类

| 类型 | 含义 | 处理方式 |
|---|---|---|
| Hard gate | 正确性、安全性或数据完整性要求 | 任一失败即阻止该 artifact 或发布批次 |
| Quality target | 衡量生成质量、覆盖率或效率 | 未达标时阻止正式放量，可继续开发试运行 |
| Monitoring metric | 用于发现漂移和退化 | 不单独阻止发布，但必须记录并设置告警 |

### 2.3 证据优先级

验收结论的证据优先级从高到低为：

1. 固定输入上的确定性自动测试；
2. 独立 evaluator 在冻结快照上的执行结果；
3. 隔离环境中的端到端回放；
4. 双人独立人工审核；
5. 单一 LLM judge 的评分。

LLM judge 不能覆盖 schema、digest、snapshot、引用完整性、数据泄漏或路径连续性等确定性失败。

### 2.4 有限生成与完整世界验收

正式验收采用以下不变量：

```text
GenerationEvidenceSlice S is a subset of ValidationWorldSnapshot W
Candidate q = Generate(S)
GoldAnswerSet = Evaluate(q, W)
```

局部 slice 只用于产生候选。答案、基数、歧义、shortcut 和 insufficient 只能由完整验证世界 `W` 决定。

“完整世界”必须在 manifest 中操作化定义为 Planner 可访问的固定 KG、文献索引、工具集合、schema 和时间边界。它不等于全部现实医学知识，但不能缩小为生成器本轮碰巧收集到的 evidence。

## 3. 验收环境与固定材料

每次正式验收必须冻结以下材料并写入 manifest：

- Git commit 或源码归档 hash；
- Python 版本和依赖 lock/hash；
- KG provider、schema version、snapshot ID 和文件 hash；
- 文献 provider、语料版本、索引版本和 snapshot ID；
- `ValidationWorldSnapshot` manifest、完整性范围和 tool environment ID；
- 每个候选的 `GenerationEvidenceSlice` ID 和 sampling policy version；
- Candidate Source 版本；
- Query Compiler 和 validator 版本；
- Writer、semantic verifier 和 blind verifier 的模型及 prompt 版本；
- Query Planner config、encoder、stop policy、selector 和 executor 版本；
- label policy、utility policy 和 cost policy 版本；
- 随机种子；
- 安全预算和 Planner 预算；
- 测试集 manifest 及其 hash。

缺少任一适用字段时，运行可以作为开发诊断，但不能形成正式验收结论。

## 4. 验收数据集

### 4.1 确定性 Golden Suite

维护一个小型、可人工完全检查的固定图和文献快照，至少覆盖：

- 正常 1-hop 单答案；
- 正常 2-hop 单答案；
- 多答案且声明 `many`；
- 声明单答案但实际多答案；
- 无答案；
- 存在一跳 shortcut 的伪多跳题；
- 路径方向错误；
- 不连续路径；
- snapshot mismatch；
- evaluator 截断；
- filter 改变答案集合；
- aggregation 为 `count` 和 `exists`；
- 可证明的 `insufficient`；
- provider error、timeout 和 budget exhausted；
- 答案泄漏和中间实体泄漏；
- 题面遗漏限定条件；
- 同一题面对应多个合理 query。
- generation slice 中只有一个答案，但 validation world 中存在多个答案；
- generation slice 和 validation world 被错误地当作同一个输入对象；
- 题面使用“当前证据范围内”等措辞掩盖世界基数冲突。
- 两跳查询存在多个 bridge，且不同 bridge 产生不同终点，单答案题必须拒绝；
- 两跳查询存在多个 bridge，但所有合法路径收敛到同一终点，允许在无截断时判定为单答案。

Golden Suite 中每个 case 必须给出人工确认的预期状态、答案集合、合法路径、非法原因和预期 reason code。

### 4.2 真实快照 Pilot Set

正式上线前至少准备一个真实生物医药快照 Pilot Set：

- 至少 200 个通过 Task Validator 的可回答任务；
- 至少 50 个可证明的 insufficient 任务；
- 1-hop 和 2-hop 均有覆盖；
- 至少覆盖药物、疾病、基因或蛋白中的三类实体；
- 至少覆盖 10 种已批准的 relation composition；
- 单一 seed entity 不得占任务数的 5% 以上；
- 单一 answer entity 不得占任务数的 3% 以上。

如果产品首批支持范围小于上述类型或关系数量，必须在 manifest 中明确缩小范围，并由产品负责人批准；不得把未覆盖能力写成已验收。

### 4.3 对抗与负例集

至少包含以下负例：

- 100 个 schema、路径、snapshot、cardinality 或 provenance 错误的任务候选；
- 50 个存在直接答案泄漏或同义词泄漏的题面；
- 50 个遗漏 filter、temporal scope、negation 或 cardinality 的题面；
- 50 个把运行失败伪装成 `insufficient` 的轨迹；
- 50 个包含重复证据、无收益动作或非法动作的 transition。

负例可以程序化构造，但每种 mutation 的预期 reason code 必须明确。

## 5. Artifact 级发布门禁

### 5.1 QueryCandidate

候选只允许进入编译阶段，不允许直接成为 gold 数据。

| ID | 验收项 | 类型 | 通过标准 | 验收证据 |
|---|---|---|---|---|
| CAND-001 | 契约有效 | Hard gate | schema 校验通过；ID、source、seed、snapshot 引用完整 | schema validation result |
| CAND-002 | 引用闭合 | Hard gate | 所有 evidence、entity 和 observation ID 均存在于当前 episode | reference check report |
| CAND-003 | Source 可追溯 | Hard gate | source 类型、版本、配置和运行 ID 完整 | candidate metadata |
| CAND-004 | 不直接发布 | Hard gate | repository/exporter 拒绝把 candidate 当作 `QuestionSpec` 或训练样本导出 | negative integration test |

Candidate acceptance rate 是生成效率指标，不是质量门禁。Agent 产生大量被拒候选不会直接污染数据集，但会影响成本和后续放量决策。

### 5.2 LogicalQuery 与 Query Compiler

| ID | 验收项 | 类型 | 通过标准 | 验收证据 |
|---|---|---|---|---|
| QUERY-001 | 强类型结构 | Hard gate | steps 连续编号，relation、direction、start entity 和 answer variable 完整 | contract test |
| QUERY-002 | 语义 digest | Hard gate | 任一语义字段变化都会改变 digest；字段顺序变化不会改变 digest | property test |
| QUERY-003 | 路径连续性 | Hard gate | 每一步的输出实体集合是下一步输入集合；不连续路径被拒绝 | compiler/evaluator test |
| QUERY-004 | 快照绑定 | Hard gate | query snapshot 与 evaluator snapshot 不一致时不得返回有效答案 | Golden Suite |
| QUERY-005 | 答案复算 | Hard gate | accepted query 的 gold answer 只能来自 evaluator 输出 | provenance test |
| QUERY-006 | Cardinality | Hard gate | `one`、`many`、`any` 与实际答案集合一致；不一致时拒绝 | Golden Suite |
| QUERY-007 | 截断处理 | Hard gate | answer truncated 时不得发布为 gold | truncation test |
| QUERY-008 | Hop 定义 | Hard gate | hop count 等于 `len(logical_query.steps)`，不读取 evidence 数量 | unit and migration test |
| QUERY-009 | 编译确定性 | Hard gate | 同一 candidate、配置和 snapshot 生成相同 query ID、payload 和 digest | replay test |
| QUERY-010 | 世界级执行 | Hard gate | evaluator 只接受 `ValidationWorldSnapshot`；传入 generation slice 必须类型失败或显式拒绝 | interface and negative test |

Golden Suite 上 QUERY-001 至 QUERY-010 必须 100% 通过。

### 5.3 Task Validator 与 ValidatedTask

| ID | 验收项 | 类型 | 通过标准 | 验收证据 |
|---|---|---|---|---|
| TASK-001 | 验证前不可写题 | Hard gate | 没有 accepted validation report 的 candidate 不能进入 Realizer | graph routing test |
| TASK-002 | Answer set 正确 | Hard gate | evaluator answer 与人工 golden answer 完全一致 | Golden Suite |
| TASK-003 | Shortcut 检查 | Hard gate | 有更短禁用路线的任务被拒绝或按实际难度重标 | shortcut report |
| TASK-004 | 路线可达 | Hard gate | 至少一条 valid route 能由下游正式动作空间执行 | action-space simulation |
| TASK-005 | Provenance 完整 | Hard gate | required evidence 都有 provider、snapshot、location 和内容 hash | provenance audit |
| TASK-006 | Reason code 稳定 | Hard gate | 每个拒绝都至少有一个枚举 reason code，不只保存自由文本 | rejection artifact |
| TASK-007 | 版本闭合 | Hard gate | task 引用 query、snapshot manifest 和 validation report 的不可变版本 | repository check |
| TASK-008 | 对抗负例识别 | Quality target | 对抗任务 false acceptance rate 不超过 1% | adversarial report |
| TASK-009 | 合法任务保留 | Quality target | 人工确认合法任务的 false rejection rate 不超过 10% | labeled validation set |
| TASK-010 | 局部全集分离 | Hard gate | validation report 同时记录 slice ID、world snapshot ID、slice answers 和 world answers | artifact audit |
| TASK-011 | 全集基数 | Hard gate | 单答案任务只有在 world answer set 基数为 1 时通过；slice 基数不得参与最终判断 | subset-vs-world Golden Suite |
| TASK-012 | 两跳全分支 | Hard gate | 两跳答案必须合并完整世界中所有第一跳 bridge 的第二跳终点；不得只执行候选路径 | branching-path Golden Suite |

TASK-008 的任何 false acceptance 都必须人工分析。如果错误涉及答案、snapshot、泄漏或 gold provenance，即使总体比例未超过 1%，也要阻止正式发布，直至增加对应 hard gate 或完成风险签字。

### 5.4 QuestionVariant 与 QuestionSpec

| ID | 验收项 | 类型 | 通过标准 | 验收证据 |
|---|---|---|---|---|
| TEXT-001 | ID 绑定 | Hard gate | variant 和 published question 显式引用 task ID、query ID 和 variant ID | contract test |
| TEXT-002 | Gold 不可变 | Hard gate | Writer 无法修改 answer、query、cardinality、filter 和 snapshot | interface test |
| TEXT-003 | Round-trip 等价 | Hard gate | 题面重建的 query 与原 query 在规范化语义字段上等价 | semantic verification report |
| TEXT-004 | Blind answer 一致 | Hard gate | blind verifier 得到的答案集合与 evaluator answer 一致 | blind verification report |
| TEXT-005 | 答案泄漏 | Hard gate | 题面不包含答案实体、唯一别名或被禁止的中间实体 | leakage scan |
| TEXT-006 | 语言完整性 | Hard gate | 题面非空、语言匹配、无截断、无未解析占位符 | deterministic validators |
| TEXT-007 | 歧义 | Hard gate | 无两个不等价 query 可合理解释同一题面 | semantic verifier or human adjudication |
| TEXT-008 | 题面自然度 | Quality target | Pilot Set 双人审核中至少 90% 判定为无需改写即可使用 | human review report |
| TEXT-009 | 审核一致性 | Quality target | 双人对“语义正确/不正确”的 Cohen's kappa 不低于 0.80 | review statistics |
| TEXT-010 | 不洗白局部范围 | Hard gate | 不使用“当前证据范围内”“根据这组记录”等措辞把局部采样变成题目限定，除非任务显式声明为 source-bounded QA | deterministic and semantic check |

TEXT-003 和 TEXT-004 是单题门禁，不是批次平均指标。验证失败的 variant 可以重写，但每次重写必须产生新 variant version；不得覆盖原失败记录。

### 5.5 PlannerRollout

| ID | 验收项 | 类型 | 通过标准 | 验收证据 |
|---|---|---|---|---|
| ROLL-001 | 空证据起步 | Hard gate | step 0 的 EvidenceStore 为空，除公开问题输入外没有预装 evidence | integration test |
| ROLL-002 | Gold 隔离 | Hard gate | planner process 无法访问 answer、required evidence、valid route 和 generation trace | dependency and payload audit |
| ROLL-003 | 正式运行时 | Hard gate | rollout 调用 `query-planner` 正式 parser/state/stop/selector/executor，不复制实现 | integration trace |
| ROLL-004 | 合法动作全集 | Hard gate | 每个 transition 保存执行前的 legal action set | rollout schema check |
| ROLL-005 | 动作合法性 | Hard gate | selected action 必须属于该步 legal action set | transition validator |
| ROLL-006 | 前后状态一致 | Hard gate | new evidence 和 cost 能从 executor observation 与 ledger 对账 | replay and ledger test |
| ROLL-007 | Terminal 完整 | Hard gate | rollout 只有一个 terminal decision，且 terminal 后无动作 | state-machine test |
| ROLL-008 | 错误不伪装 | Hard gate | provider error、timeout、schema error 和 guard exhaustion 不标为语义 `insufficient` | fault-injection test |
| ROLL-009 | 可重放 | Hard gate | 固定 planner、环境和随机种子时，确定性运行的 transition 内容与 digest 一致 | replay test |
| ROLL-010 | 多策略隔离 | Hard gate | 同一 question 可保存多个 planner config 的 rollout，互不覆盖 | repository test |

### 5.6 Planner 标签和训练样本

| ID | 验收项 | 类型 | 通过标准 | 验收证据 |
|---|---|---|---|---|
| LABEL-001 | 行为与监督分离 | Hard gate | schema 明确区分 behavior decision、outcome 和 supervised label | contract test |
| LABEL-002 | Complete | Hard gate | 只有当前 evidence 足以推出 gold answer 且满足证据约束时才能标注 | oracle-state test |
| LABEL-003 | Continue | Hard gate | 当前不足且存在预算内合法增益动作时才能标注 | oracle-state test |
| LABEL-004 | Insufficient | Hard gate | 当前不足且给定环境内无可行完成路线时才能标注 | exhaustive small-graph test |
| LABEL-005 | Unknown 保护 | Hard gate | 无法证明三类标签时进入 unknown/review，不强制归类 | ambiguous-state test |
| LABEL-006 | Utility 可解释 | Hard gate | utility 保存 novelty、constraint progress、answer-space reduction、sufficiency gain、penalty 和 cost 组成项 | sample schema audit |
| LABEL-007 | Policy 版本 | Hard gate | label、utility 和 cost policy 均有版本号 | export manifest |
| LABEL-008 | 非法动作负例 | Hard gate | 非法动作不得作为正标签，可按明确策略生成负例 | negative sampling test |
| LABEL-009 | Oracle 正确性 | Hard gate | Golden Suite 构造状态上的 stop label 和 action ranking 100% 符合预期 | Golden Suite |
| LABEL-010 | 人工一致性 | Quality target | Pilot 抽样中监督 stopping label 与专家裁决一致率不低于 95%，各类 precision 不低于 90% | adjudication report |

不能继续使用 `len(new_evidence_ids)` 作为唯一 utility 通过正式验收。该值可以保留为 `evidence_count_gain` 组成项，但必须与任务约束进展和成本分开保存。

### 5.7 Dataset Export

| ID | 验收项 | 类型 | 通过标准 | 验收证据 |
|---|---|---|---|---|
| DATA-001 | 引用完整 | Hard gate | task、question、rollout、transition 和 sample 不存在 dangling ID | referential-integrity report |
| DATA-002 | Schema 有效 | Hard gate | 正式导出的每一行都通过对应版本 schema | full export validation |
| DATA-003 | Gold 来源 | Hard gate | 所有 answer 可追溯至 evaluator 和 snapshot | lineage audit |
| DATA-004 | Split 泄漏 | Hard gate | train、validation、test 的 query fingerprint 交集为空 | split audit |
| DATA-005 | 实体泄漏策略 | Hard gate | manifest 声明实体隔离规则，实际交集符合该规则 | split audit |
| DATA-006 | 重复样本 | Hard gate | 同 split 内规范化 question/query/transition 主键无重复 | dedup report |
| DATA-007 | 隐藏推理 | Hard gate | 导出不包含模型隐藏 chain-of-thought 或原始私密 prompt | field allowlist audit |
| DATA-008 | Manifest | Hard gate | 记录代码、快照、模型、prompt、validator、label、utility 和 split 版本 | manifest schema |
| DATA-009 | 可重复导出 | Hard gate | 相同输入 artifact 和导出配置产生相同文件内容 hash | export replay test |
| DATA-010 | 类别分布 | Monitoring metric | 记录 stop labels、action kinds、题型、hop、实体类型和 source 分布 | dataset statistics |

## 6. 系统级发布门禁

### 6.1 Gate A 契约与组件完成

进入集成阶段前必须满足：

- 新增契约可导出为版本化 JSON Schema；
- 旧 `PlannerTrajectory` 与新 `PlannerRollout` 无法被彼此的 loader 误读；
- Query Compiler、evaluator、validator、repository 和 exporter 单元测试全部通过；
- Golden Suite 中所有 Hard gate case 100% 通过；
- schema migration 有向前读取测试和失败提示。

输出证据：`component-acceptance-report.json`。

### 6.2 Gate B Fake 端到端闭环

使用 deterministic fake model、固定小图和固定文献数据运行：

```text
Candidate
    -> LogicalQuery
    -> ValidatedTask
    -> QuestionVariant
    -> QuestionSpec
    -> PlannerRollout
    -> PlannerTrainingSample
    -> Export
```

必须满足：

- 至少覆盖一个 1-hop、一个 2-hop、一个 ambiguous、一个 shortcut 和一个 insufficient case；
- artifact 级 Hard gate 全部通过；
- 中断后 resume 不重复外部调用、transition 或训练样本；
- 相同输入运行两次，所有确定性 artifact 的内容 hash 相同；
- fault injection 不产生被误标为有效的数据。

输出证据：端到端 workspace、manifest、trace 和 `fake-e2e-acceptance-report.json`。

### 6.3 Gate C 真实快照 Pilot

使用第 4.2 节 Pilot Set 验收：

- 所有已发布 task 和 question 满足 artifact Hard gate；
- Task Validator 对抗 false acceptance 满足 TASK-008；
- 题面自然度和审核一致性满足 TEXT-008、TEXT-009；
- stopping label 人工一致性满足 LABEL-010；
- split collision 为 0；
- 所有未覆盖能力明确记录为 `not_evaluated`，不能记录为 passed；
- 运行失败、模型拒绝和 provider 错误具有独立计数。

输出证据：`pilot-acceptance-report.md`、机器可读 JSON 报告、人工审核记录和全部 manifest hash。

### 6.4 Gate D 正式数据发布

正式发布批次必须满足：

- Gate A、B、C 均为 passed；
- 自 Gate C 之后影响契约、validator、模型、prompt、快照或标签的改动已重新执行相关验收；
- 导出目录通过 DATA-001 至 DATA-009；
- 发布负责人签署数据卡中的已知限制；
- 数据集有唯一 release ID 和不可变 manifest；
- 能从 release 中任意抽取一条样本，追溯到 question、task、query、snapshot、rollout 和 label policy。

## 7. 非功能验收

### 7.1 Resume 与幂等性

对每个外部调用边界注入中断，覆盖：

- Candidate Source 调用前后；
- evaluator 调用前后；
- Writer 调用前后；
- semantic/blind verifier 调用前后；
- Planner action 执行前后；
- artifact 持久化前后；
- export 写入期间。

通过标准：

- 不产生重复物理工具调用，除非调用被明确标记为 retry；
- 不产生重复 artifact ID；
- 不产生半写入 JSONL；
- ledger cost 与实际调用次数一致；
- 恢复后的最终结果与不中断运行在规范化内容上等价。

### 7.2 性能与成本

性能阈值不能在没有目标运行环境的情况下写死。正式验收前必须由负责人在 manifest 中设置：

- 单个 candidate 编译和确定性验证的 P50、P95 时延上限；
- 单题 Writer 和 verifier 的 P95 时延与 token 上限；
- 单个 rollout 的最大步骤、工具调用、token、时延和成本；
- 单个 accepted task 的平均生成成本上限；
- 批量任务的最大失败率和 retry 放大率。

系统必须报告分阶段成本，至少拆分为 candidate generation、validation、realization、verification 和 rollout。不得只报告整个 episode 的总成本。

### 7.3 可观测性

每个阶段至少记录：

- 输入和输出 artifact ID；
- component、policy、model 和 prompt 版本；
- 开始、结束时间和状态；
- reason code；
- token、工具调用和成本；
- retry 次数；
- snapshot/environment ID。

日志不得包含 API key、数据库凭证、完整隐藏 prompt 或模型隐藏思维链。

## 8. 人工审核协议

### 8.1 审核对象

Pilot 至少对以下对象进行人工审核：

- 随机抽取 100 个可回答 QuestionSpec；
- 全部 insufficient QuestionSpec，最多 100 个，超过时随机抽取 100 个；
- 全部 validator 与 blind verifier 意见不一致的样本；
- 每个 Candidate Source 至少 30 个 accepted task；
- 每种支持的 relation composition 至少 10 个任务，不足时全审。

### 8.2 审核方式

- 两名审核者独立判断，不看对方结论；
- 审核者可查看题面、规范 query、固定快照证据和 evaluator answer；
- 审核者不能只根据 Writer 或 Agent 的理由作结论；
- 分歧由第三人裁决；
- 保存原始判断、reason code、修改建议和最终裁决。

### 8.3 审核维度

每题至少判断：

- 生物医药语义是否成立；
- 题面是否等价于规范 query；
- 答案是否正确且基数合理；
- 证据是否足够；
- 是否泄漏答案或必要中间实体；
- 是否存在明显 shortcut；
- 是否适合用于目标 Planner 训练；
- 是否需要改写、拒绝或升级证据。

## 9. 验收报告格式

每次验收输出一个机器可读 JSON 和一个面向人的 Markdown 报告。报告至少包含：

```yaml
acceptance_id: acceptance-YYYYMMDD-N
architecture_version: string
source_revision: string
environment_manifest_hash: string
dataset_manifest_hash: string
started_at: datetime
completed_at: datetime
result: passed | failed | conditional | not_evaluated
gates:
  gate_a: passed | failed | not_evaluated
  gate_b: passed | failed | not_evaluated
  gate_c: passed | failed | not_evaluated
  gate_d: passed | failed | not_evaluated
hard_gate_failures: []
quality_targets: {}
monitoring_metrics: {}
known_limitations: []
artifact_paths: []
approvals: []
```

报告中的每项能力只能取以下状态：

- `passed`：具备完整证据且达到阈值；
- `failed`：已执行但未达到阈值；
- `conditional`：只有明确风险签字时可用于有限试运行；
- `not_evaluated`：没有执行或环境不满足。

禁止把 `not_evaluated`、skipped test 或 provider 不可用写成 passed。

## 10. 缺陷分级与发布处理

| 级别 | 示例 | 处理 |
|---|---|---|
| P0 | gold answer 错误、用局部唯一冒充全集唯一、跨 split 泄漏、rollout 看到 gold、错误标注 insufficient | 立即阻止发布，已发布批次撤回或作废 |
| P1 | query 与题面不等价、shortcut 漏检、标签 reason 无法复核、resume 重复样本 | 阻止正式发布，可保留隔离开发数据 |
| P2 | 自然度不足、分布失衡、成本超目标、候选通过率过低 | 阻止放量或降级发布范围 |
| P3 | 报告展示、非关键日志或开发体验问题 | 记录后可按计划修复 |

任何 P0 或 P1 缺陷修复后必须：

1. 增加能复现问题的回归 case；
2. 重新运行受影响 hard gate；
3. 识别并重新验证可能受影响的历史 artifact；
4. 更新验收报告和 release manifest。

## 11. 最小完成定义

第一版目标架构只有在以下条件全部满足时才可以标记为完成：

- Gate A、Gate B 和 Gate C 全部通过；
- PathQuerySource 和 AgentQuerySource 均能产生通过统一门禁的任务；
- 每个已发布 QuestionSpec 都绑定 ValidatedTask 和 LogicalQuery；
- 每个 gold answer 都由完整验证世界复算，且报告保留 slice answers 与 world answers 的差异；
- 题面 round-trip 和 blind verification 是发布硬门禁；
- PlannerRollout 从空 EvidenceStore 开始且通过 gold 隔离测试；
- stopping labels 在 Golden Suite 上 100% 正确；
- utility 不再只等于新增 evidence 数量；
- generation trace 与 planner rollout 的 schema、loader 和存储完全分离；
- 正式导出不存在 dangling reference、split collision 或未版本化 policy；
- Pilot 人工审核达到本文阈值；
- 验收报告明确列出未覆盖能力和已知限制。

只完成问题生成、只完成多跳探索、只保存出题 Agent 的动作轨迹，或只通过现有 baseline-v1，都不能视为该架构验收完成。


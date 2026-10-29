# 客观医学题目方法论

> 契约版本：`objective-question-v1`  
> 实施计划：[证据先行题目空间改造计划](../../plans/evidence-first-question-derivation-refactor-plan-2026-10-28.md)  
> 产品说明：[整体方案阅读版](../../plans/evidence-first-question-derivation-reader-version-2026-10-29.md)

## 1. 方法论目的

本项目的任务不是从知识图谱或检索结果中抄出问题，而是从可回读材料中发现候选事实，再用独立证据确认该事实能够形成题面自包含、答案稳定且可复核的客观题。

因此必须持续区分四个对象：

| 对象 | 可以证明什么 | 不能证明什么 |
|---|---|---|
| `EvidenceMaterial` | 某个来源在某次检索中返回了内容 | 现实世界中的事实已经成立 |
| `EvidenceAnchor` | 某段原文或字段确实位于材料中 | 该段内容足以独立确认答案 |
| `CandidateClaim` | 模型或规则提出了什么解释 | 该解释已经是客观真值 |
| `AnswerResolution` | 独立验证计划覆盖完成后的事实判断 | 题面表达没有改变原意 |

数据源、LLM 和主 Agent 都不能直接把候选升级为可发布题目。正式发布只能由确定性门禁根据结构化 `ObjectiveValidationReport.PASS` 决定。

## 2. 处理流程

```text
目标实体
  -> 多来源材料与 anchor
  -> 实体消歧和候选事实
  -> 候选问题与候选答案
  -> 盲态独立求解、竞争答案和反例检查
  -> AnswerResolution
  -> FrozenQuestionContract
  -> 最终题面
  -> 公开题面审查 + 答案证据审查
  -> ObjectiveValidationReport
  -> public package / audit package
```

问题设计阶段可以同时提出问题和候选答案。候选答案只能保存在 `QuestionHypothesis.proposed_answer_values` 中，不能进入 `BlindAnswerQuery`，也不能被发布代码当作 gold answer。

## 3. 硬性不变量

1. discovery source 不等于 truth source。
2. 隐藏 metadata 不能修补不完整的公开题面。
3. source absence 不能推导现实世界中的否定结论。
4. 一个 supporting record 不能证明答案集合完整。
5. LLM 输出必须经过严格 schema 校验和独立复核。
6. 题面生成不能改变冻结契约中的语义、条件、基数或关系强度。
7. 只有 `ObjectiveValidationReport.PASS` 且 `release_allowed=true` 才能发布。
8. public package 不包含内部 source filter、ID、anchor 或隐藏答案信息。
9. audit metadata 从材料检索开始持续持久化，不能在发布前补造。
10. provider failure、未评估和 coverage 不完整必须保持 `INDETERMINATE`。
11. 推导链只保存可复现的输入输出映射和版本，不保存自由形式 chain-of-thought。
12. 没有合格事实时返回 `COMPLETED_WITH_ZERO_QUESTIONS`，不得降级造题。
13. 子 Agent 只提交 artifact，正式状态只能由确定性协调器推进。
14. 一次运行的模型、prompt、skill、schema、policy 和 provider 版本必须固定。
15. operation 重试和重复投递不得重复写 artifact 或重复扣减预算。

## 4. 事实类型和验证政策

| 事实类型 | 可接受依据 | 额外条件 |
|---|---|---|
| 实体身份、基因或蛋白定位 | 有定义权的规范来源，必要时交叉确认 | 实体、物种和版本明确 |
| 分类和命名 | 具有定义权的标准或本体 | 会改变答案的标准版本进入公开题意 |
| 封闭选项 | 题面公开的完整候选集合 | 每个选项都检查，答案唯一或基数明确 |
| 确定性计算 | 公开输入、单位、公式和版本 | 计算可以重放 |
| 权威枚举 | 有正式定义权的枚举 | 保存 authority、版本、适用范围和定义权依据 |
| 观察性关联、风险和因果 | 多来源直接证据与冲突审查 | 首期默认不自动发布 |

PrimeKG、CTD、普通 RAG 索引或一次检索的收录范围不能成为 `completeness_basis`。`NOT_ESTABLISHED` 不允许形成正式单答案或 exact-set 题。

## 5. 发布门禁

公开题面审查只接收最终公开题面，必须检查：

- `self_contained`
- `source_independent`
- `asks_underlying_fact`
- `entity_unambiguous`
- `cardinality_clear`
- 必要公开条件完整
- 关系强度没有被升级

答案证据审查在公开题面审查结果固化后执行，必须检查：

- 答案由独立材料直接支持；
- 答案唯一，或封闭集合完整；
- 竞争答案和冲突证据已经检查；
- 题面、答案与证据方向一致；
- 验证 coverage 完整且 blind answer 已比较；
- 验证来源不只是 discovery artifact。

任一阶段为 `REJECT`、`INDETERMINATE`、超时、非 JSON 或 digest 失效时，最终结果都不能是 `PASS`。

## 6. 状态和恢复

`QuestionHypothesis` 只能按以下主路径推进：

```text
PROPOSED
  -> VERIFICATION_PLANNED
  -> VERIFYING
  -> ANSWER_VERIFIED
  -> QUESTION_REALIZED
  -> RELEASE_REVIEWED
  -> PUBLISHED
```

候选业务终态是 `REJECTED` 或 `INDETERMINATE`。运行级状态另外区分：

- `COMPLETED_WITH_ZERO_QUESTIONS`：正常完成但没有合格题目；
- `RECOVERY_REQUIRED`：checkpoint、外部调用或其他运行时完整性需要人工处理；
- `VERSION_INCOMPATIBLE`：无法加载固定的 runtime bundle；
- `CANCELLED`：明确取消，不能伪装成失败或证据不足。

`operation_key` 在重试间保持稳定，`attempt_id` 每次调用唯一。超时且外部结果未知时，必须先按 provider capability 查状态；无法确认时不得直接重放。

## 7. 实现边界

本阶段的稳定 Python 契约位于：

- `generation/evidence_provider.py`
- `generation/objective_question_contracts.py`
- `generation/objective_question_persistence.py`
- `generation/subagent_contracts.py`
- `generation/fact_exploration.py`
- `generation/question_design.py`
- `generation/answer_verification_subagent.py`
- `generation/objective_question_realization.py`
- `generation/release_review_subagent.py`
- `generation/objective_question_runtime.py`
- `generation/main_agent.py`
- `generation/subagent_dispatch.py`
- `generation/material_retrieval_subagent.py`
- `generation/agent_loop.py`
- `generation/objective_question_release.py`
- `generation/objective_provider_adapters.py`

新链路使用独立 schema family 和 feature flag。旧 `TaskCandidate`、CTD task 和 v2 release 保持原语义，不会因读取新代码而自动升级为 `PASS`。

契约详细字段以 Pydantic 模型为准，外部消费者使用 `schemas/v1` 中导出的 JSON Schema。

材料原文按 SHA-256 内容寻址保存为不可变 blob。anchor 的 offset、引用文本和 quote hash 必须能对回读原文重新校验。provider attempt 只能追加，`operation_key` 标识逻辑操作，`attempt_id` 和 `retry_ordinal` 标识物理尝试；同一 retry ordinal 不能被另一 attempt 复用。

子 Agent 只通过版本化 `SubagentTask` 调用。事实理解角色只能看到成功材料、可回读 anchor、实体消歧和事实类型约束；候选题、proposed answer 和 discovery anchor 均不在其可见字段中。问题设计角色只读取已固化 candidate claim、anchor 位置和公开产品约束，不能回写 claim 或把 proposed answer 标记为 verified。

PubMed、RAG、网页和数据库文本统一放在 `trust_boundary.material_policy=untrusted_data` 内。材料中的指令、工具请求或“发布答案”等文本始终按普通引用数据处理，不能改变角色的 system prompt、工具白名单或输出契约。

答案验证固定执行三个独立阶段：盲态求解、在盲态结果固化后比较 proposed answer、再检查竞争答案和反例。deterministic host 会核对 plan、attempt 与 coverage 的 ID 和终态；empty、provider failure、预算耗尽或 coverage 不完整均为 `INDETERMINATE`。只有 verification attempt 直接提供的独立 anchor 能支持答案，discovery anchor 自身不能完成验证。

Writer 使用独立的 realization 调用，只接收 `FrozenQuestionContract`。输出必须携带相同 question intent、scope、answer values、answer cardinality、claim strength 和必要公开条件；出现 answer/scope/strength 漂移、source-relative 或 meta-evidence 措辞、因果或治疗语义升级时，deterministic gate 直接拒绝。

发布复核由两个独立模型调用完成。第一阶段只接收 `PublicQuestionPackage`，不能访问答案、冻结契约、evidence 或内部 metadata；结果和 digest 固化后，第二阶段才接收 `AnswerEvidenceAuditPackage`。deterministic merger 会重新核对题面、contract、独立证据和 skill content hash。任一阶段非 `PASS`、硬门禁 false、超时、解析失败、模型拒绝或 digest 过期时，最终报告均不可发布。

运行态采用状态快照单写。每次 commit 都生成新的 `state_revision` 和单调 `commit_sequence`，并把状态、artifact、budget、operation terminal 和聚合结果放入同一不可变 commit 文件。`operation_key` 相同的已提交 operation 直接复用，不重复扣预算；相同 operation key 携带不同 input digest 时冲突。并行 child 使用独立 slot revision，不因 run revision 变化被丢弃；同一 parent 的 child 聚合只产生一个 commit。主 Agent 的 action、目标角色和 state digest 由 dispatcher 校验，不能直接发布。

材料检索子 Agent 每个实例只接受任务白名单中的一个 provider。成功结果先校验 provider、material ID、anchor ID 和原文 hash，再通过 `ObjectiveQuestionPersistence` 落地；empty、FAILED、UNKNOWN 分别保留终态并统一返回 `INSUFFICIENT`，不会生成 negative claim、候选答案或 gold。现有 PrimeKG、CTD、PubMed 与 RAG 到该端口的正式迁移仍属于后续实施项。

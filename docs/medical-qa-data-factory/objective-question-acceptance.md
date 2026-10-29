# 客观医学题目验收标准

> 验收契约：`objective-question-acceptance-v1`  
> 可执行案例：`tests/fixtures/objective_questions/acceptance-cases.json`  
> 方法论：[客观医学题目方法论](objective-question-methodology.md)

## 1. 验收原则

“能生成题目”不是通过条件。验收关注系统是否能够拒绝 source-relative、meta-evidence、隐藏范围、答案不唯一、不完整集合和语义升级，并把失败原因稳定记录为结构化 artifact。

每个验收案例必须包含：

```text
case_id
scenario
expected_verdict
expected_reason_code
```

`case_id` 唯一，`expected_verdict` 和 `expected_reason_code` 必须非空，reason code 使用大写常量。技术运行时的 `RECOVERY_REQUIRED` 和 `VERSION_INCOMPATIBLE` 不得聚合成 0 题完成。

## 2. 必须覆盖的案例组

| 范围 | 案例 | 核心证明 |
|---|---|---|
| 客观题语义 | `OBJ-01` 至 `OBJ-15` | 系统能拒绝数据源相对、元证据、隐藏范围、多答案、不完整集合和因果升级，并能放行独立确认的稳定事实 |
| 运行时一致性 | `OPS-01` 至 `OPS-10` | 重复投递、stale revision、unknown timeout、checkpoint 恢复、版本固定、循环和 bundle 不兼容具有明确终态 |
| 架构评估 | `EVAL-01`、`EVAL-02` | 单 Agent、共享上下文 Workflow 和隔离式多 Agent 必须按预注册规则比较 |

完整 27 个案例存放在可执行 JSON fixture 中。fixture 在 Step 1 只冻结验收身份和预期结果；后续 Step 4、Step 6、Step 7 和 Step 10 必须分别实现并回归对应行为。

## 3. 客观题通过条件

一道题只有同时满足以下条件才可进入正式数据集：

1. 公开题面自包含，不依赖隐藏 provider filter、snapshot、PMID 集合或内部候选集合；
2. 问题是 underlying fact，不是“数据库中有什么”或“研究是否支持”；
3. 实体、物种、人群、时间、剂量和定义等会改变答案的条件完整；
4. 答案唯一，或者公开选项/权威枚举/确定性计算已经建立闭合范围；
5. 竞争答案、反例和冲突已经检查；
6. verification coverage 完整，所有 expected attempts 到达成功终态；
7. 答案有独立于 discovery artifact 的直接证据；
8. 关系强度和题面表达一致，没有把关联升级为因果、治疗、诊断或预后；
9. `FrozenQuestionContract` round-trip 通过；
10. 公开题面审查和答案证据审查都返回 `PASS`；
11. 最终 `ObjectiveValidationReport` 为 `PASS + release_allowed=true`；
12. public 和 audit package 完成字段隔离。

任一条件未满足时：

- 已明确发现事实或语义问题：`REJECT`；
- 证据不足、coverage 不完整或无法判断：`INDETERMINATE`；
- 运行时无法可靠恢复：对应的 run 进入 `RECOVERY_REQUIRED` 或 `VERSION_INCOMPATIBLE`。

## 4. 0 题验收

以下场景可以正常返回 `COMPLETED_WITH_ZERO_QUESTIONS`：

- 没有产生 hypothesis；
- 所有 hypothesis 都是 `REJECTED`；
- 所有 hypothesis 都是 `INDETERMINATE`；
- 预算或 provider 限制使候选全部终止，但 run 本身没有运行时完整性阻塞。

0 题结果必须保存 stop reason、候选阶段计数、拒绝原因和 provider attempt。不得触发自动降低门槛、重复尝试到偶然 `PASS` 或写入伪题目。

## 5. 自动化门禁

Step 1 的最低验证命令：

```powershell
$env:PYTHONPATH='src'
& 'D:\Anaconda\envs\qa_env\python.exe' -m pytest tests/test_objective_question_contracts.py tests/test_objective_question_fixtures.py tests/test_evidence_provider.py tests/test_schema_export.py -q
```

通过标准：

- 全部验收案例可被 `ObjectiveAcceptanceSuite` 解析；
- 27 个计划案例无遗漏、无重复；
- `BlindAnswerQuery` schema 不包含 proposed answer 或 discovery anchor；
- 新契约以严格 schema 导出到 `schemas/v1`；
- 旧 schema 和旧 task 语义不改写。

后续行为实现必须新增测试，不得用 fixture 中声明了预期结果来代替实际验证、门禁和恢复代码的测试。

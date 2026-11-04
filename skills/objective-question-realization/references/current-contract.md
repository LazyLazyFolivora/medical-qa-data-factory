# 当前题面生成契约与接入边界

对应 `generation/objective_question_realization.py` 和 `generation/objective_question_contracts.py`。本 skill 尚未改变这些实现。

## 输入与输出

当前 LLM payload 包含 `frozen_question_contract` 和简短 `instruction`。输入没有原始文献，也没有明确传入的语言、难度、受众或风格字段；不能靠延长 Prompt 获得缺失的任务规格。

输出为 `ObjectiveQuestionDraft`：

- `draft_id`：宿主接收后重设为基于契约的 ID。
- `contract_id`：必须对应输入契约。
- `question_text`：实际公开题干。
- `answer_values`：与冻结答案一致。
- `concise_explanation`：隐藏答案解释。
- `semantic_readback`：意图、范围、答案类型和值、数量、关系强度及必要公开条件。

## 已有检查

宿主检查契约 ID、答案值及回读字段的一致性，并通过关键词阻止部分来源依赖和元证据措辞。EXACTLY_ONE 草稿需只有一个答案。

这些检查能保护部分结构一致性，不等于验证实际题干已表达所有条件。回读来自同一生成模型，不能替代独立语义审核。

## 接入时需对齐

- 原 instruction 一律禁止题面含任何答案值，与选择题和某些语义上非泄漏的字符串出现不兼容。支持哪些题型、怎样检查泄漏应与本 skill 同步确定。
- 现有 `_validate_semantics` 没有实际检查答案值是否出现在题干中；也没有逐项验证真实题干满足必要条件和措辞约束。不能声称这些要求已被程序硬性保证。
- 当前关键词匹配可能漏掉改写，也可能误拦包含相同子串的合法术语。语义上的来源依赖与纯字符串命中应区分。
- `allowed_wording_constraints` 和 `forbidden_semantic_upgrades` 已传入契约，但现有检查没有逐项核验实际题面。冲突的通用禁止项需要上游修正，不能让写作者自行改变已验证关系。
- 输出 schema 只表达成功草稿，没有契约不足或冲突的诊断分支。接入需为这类结果提供明确通道；不能输出假题填满字段。
- 当前主循环遇到题面语义违规会拒绝假设，没有专门的局部重写流程。若增加重写，应区分措辞缺陷与需要重新验证的语义变更。

遵守调用方输出格式，不自行新增字段或对外发布。先检查固定契约上的题面自包含性、答案提示、语义保持和 Planner 查询风格，再做运行时接入。

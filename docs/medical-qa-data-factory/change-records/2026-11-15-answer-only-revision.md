# 答案修订保留原问题

## 观察到的问题

真实运行 `apoe-20261115-161927` 第二步，主 agent 要求修正 NHE6 答案中没有直接证据的机制解释。反馈的 repair_issues 均针对答案，生成器却将问题改为两种干预的观测效果。改题后的文本更容易匹配现有段落，却偏离原信息需求。

代码 payload 允许“narrow the question when warranted”，而出题 skill 已要求答案问题只修答案。先统一文字指令，但相同保存输入的真实回放 `revision-intent-20261115-162337` 仍改问效果。因此不把 prompt 修改视为修复成功。

## 实现

- `react_call_contracts.py`：`revise_candidate` 增加 `revision_target=answer|question_and_answer`。默认旧行为以兼容历史调用。由主 LLM 明确选择目标，不通过关键词判断科学内容。
- `react_main_agent.py`：工具说明解释两种选择。缺少关键证据应补查，不通过替换疑问来绕过。
- `agent_loop.py`：将选择传入 `revision_context`，保留实际旧候选、反馈和来源。
- `question_design.py`：answer 模式复用已有 grounded-answer 路径；原问句放入 approved_question。动态输出 schema 将 question_intent 固定为原句，最多返回一个候选；允许空列表及具体缺口。不静默替换模型文字，不自动认定答案正确。
- 已有 approved_question 的初次回答也使用固定问句 schema。普通题干修订仍可修正文句，新的题干仍需评审和验证。

生成后的新候选沿用原有 review/verify/publish 流程；这里的 approved_question 表示当前回答任务的固定输入，不是新问题通过事实审核的证明。

## 真实验证

从 `apoe-20261115-161927/step-02-before.json` 重放完整主 agent 单步，生成 `apoe-20261115-162637`：

1. 主 agent 自行选择 `revise_candidate`、`revision_target=answer`。
2. 调用实际 `objective_grounded_answer`，而不是重新出题。
3. 模型保留旧 question_intent，答案移除未经支持的“逆转结构不稳定”解释，只保留内体功能恢复和缺失干预的斑块观察。
4. 产生新候选 `hypothesis.e7444e3b4a3fd00a3a110d42`；没有沿用旧验证结果或直接发布。

实际代码已审查。启动检查曾发现新增 Literal 未导入，已修正；失败启动发生在模型请求前，不计入真实回放结果。

后续 `apoe-20261115-162820` 完成三步：review_candidate、verify_answer、publish_question，新候选实际发布。题干保持原句，未经支持的“逆转结构不稳定”机制未再进入候选答案。这个结果证明答案修订可以完整走通后续链路，而非仅通过局部 schema 检查。

质量仍未完全通过人工检查：原问句把 NHE6 缺失的效果归入 ApoE4 介导病理，而所引段落对缺失只写 ApoE，不能由此直接确定亚型；“机制或表现”也混合了不同信息需求。系统的候选评审和最终验证未明确处理这些差异，不能将这项发布作为全部质量要求达成的证据。

## 边界

本例原题本身含“机制或表现”的混合需求，仍有选题和范围疑虑。固定问句只能保证修答案不擅自换题，不能让不合格的原题变合格；主 agent 识别到具体题干缺陷时应选择 question_and_answer，或重新探索有意义的领域问题。

旧模型请求、prompt、响应和状态都保存在各实验目录；默认模型与配置未变。本次没有新增评审层，也没有宣称全部出题质量问题解决。

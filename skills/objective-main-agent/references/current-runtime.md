# 当前调度协议与接入限制

对应 `generation/main_agent.py` 和 `generation/agent_loop.py`。本说明不修改运行时，也不把未来能力当作现有动作。

## 现有决策格式

`MainAgentDecisionProposal` 包含 action、target_subagent_role、input_artifact_ids、requested_budget、expected_information_gain、action_parameters 和 stop_reason。只有八个动作：retrieve_materials、request_more_evidence、understand_facts、design_questions、verify_answer、realize_question、review_release、stop。

每次使用 `available_actions` 决定合法动作。检索的两个动作共用实现。当前参数支持 provider_id、purpose、query 和若干检索上限；自由字典中填入新的需求字段，不意味着子 Agent 会收到它。

## 当前门槛

- understand_facts 需要未处理材料和锚点。
- design_questions 需要候选事实、无未处理材料，以及没有活跃假设等状态条件。
- verify_answer 需要未解决的活跃假设和成功的 verification provider 结果；这个检查没有证明材料独立或直接相关。
- realize_question 需要未生成草稿且关联假设已验证的冻结契约。
- review_release 需要未审核且关联假设已生成题面的草稿。
- stop 只有已发布数量达到目标才合法。`question_count_upper_bound` 在当前流程实际被当作必达目标使用。

## 恢复和停止缺口

当前没有显式重写、重开终止假设、请求人工诊断或失败停止动作。题面校验失败可能拒绝假设；发布失败也会转为拒绝或不确定。不能仅靠 skill 指示就在原契约上重新写作或重新验证，需要同步状态机和版本策略。

宿主会在达到最大动作次数、连续空检索阈值或连续 provider 失败阈值时结束运行，默认连续阈值为 3。空检索计数依据返回是否有材料，不是语义上是否新增有价值证据。

因此“无新有用材料”和“宿主空结果阈值”不是同一概念。不要故意制造空检索来触发终止。未达题目目标而无合理动作时，需要宿主增加明确失败或阻塞退出途径。

系统 Prompt 当前仍允许零题停止，而动作验证禁止未达目标的 STOP；应在接入时同步，不让模型在相互冲突的提示间猜测。

## 输入交接缺口

- 检索任务当前将 purpose 直接作为 evidence_need，没有承载具体关系、条件或证据缺口。
- 部分审核报告和失败产物在主 Agent 中只有空内容摘要，难以按真实缺陷选择修复。
- 当前没有完整的 Planner 环境、知识源可达性与逐题难度要求传递；发布审核提出的相关缺口不能通过重复 review 消除。
- requested_budget 是提案字段，不代表本次动作已被实际预留或执行完整预算校验。
- 独立 skill 的存在不会自动让调用模型看到内容。除已接入的发布审核外，其他新 skill 需要显式装载及相应输入、输出通道。

## 接入复核案例

- 只有实体时选择合理来源和初始方向，而不是直接设计题目。
- 有未处理材料时推进事实理解，再依合法状态设计。
- 验证材料与候选不相关时明确补查目标，不因 provider 成功就宣布足够。
- 审核指出漏条件时定位写作缺陷；如果当前状态不支持重写，报告能力缺口，不伪造动作。
- 环境信息缺失时不重复审核相同输入期待通过。
- 有有效短路径时接受结果并重新判断难度，不通过增加检索步骤维持困难标签。
- 数量未达但证据耗尽时不降低事实标准，也不假装 STOP 当前合法。

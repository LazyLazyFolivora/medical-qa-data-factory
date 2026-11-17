# 出题前显式说明材料为何足够

主 Agent 调用 design_questions 时新增必填参数 evidence_sufficiency_reason（1–1600 字符），原文选段模式和事实 ID 模式都适用。说明应包含拟问的问题及答案边界、所选来源支持的关键点、剩余缺口及其不影响本题的原因。单篇足够就直接说明，不以篇数代替信息贡献。

该字段由动作 schema 和本地参数解析共同要求。程序只检查结构，不用关键词判断研究是否充分，不替模型写理由。原始响应继续完整留存。

传递链：ReactChoice.parameters → 主决策记录 → agent_loop 的出题任务 → objective_question_design 输入。理由同时进入 question_design_notes，标记 controller_judgment_not_verified_evidence。出题模型需对照原文检查该判断，不能把理由当证据；现有独立验证不变。

41 项相关测试通过，包括缺字段拒绝、两种出题模式 schema、理由传入真实出题流程及持久化。

真实观察：用 first-action-8e68f66e39cd-success-replay 第 7 步 before.json 固定状态，重放输出保存在 data/runs/design-readiness-step7-replay-20261110。模型没有选择出题，而是选择 read_artifacts，读取 16354869、23821327、19124237、20836682 四篇此前未读的材料，理由是寻找更具体的比较、耐药或适应症信息。此次输入 20956 tokens，输出 344 tokens。

这是一次随机生成的观察，不能断言由新增要求导致，更不能声称已观察到真实出题理由字段。必填及传递行为已由回归测试验证；真实模型选择出题时还需观察其说明质量。原第 7 步候选及主运行未覆盖、未推进。

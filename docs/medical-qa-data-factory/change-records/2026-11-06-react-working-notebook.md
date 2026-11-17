# ReAct 工作笔记接入

## 问题与方案

自由文本 research_note 缺少来源、条件、探索状态，长任务容易重复检索或遗忘材料。增加结构化 working_notebook，在每次主 LLM 决策调用中，先总结上一工具的实际结果，再选择动作。无需额外总结模型调用，不改变工具路由控制权。

字段：current_goal、findings（statement/status/source_ids/scope）、last_step_progress、open_questions、explored_directions、next_step。supported 仅指有来源支持，不代表独立验证。笔记不是证据，也不是私有推理过程。

## 代码及持久化

- generation/react_main_agent.py：有长度上限的笔记模型，读取最新笔记并注入决策输入；返回笔记写入 action_parameters.working_notebook。
- 现有 agent_loop 保存 decision 及 react_step.parameters，不增加独立存储状态；恢复时从最新含笔记步骤读取。stop 的最终笔记保存在决策记录。
- 历史 prior_searches 去掉旧笔记正文，最新版本独立保留，不逐轮累积进 prompt；原始历史仍保存在运行记录。
- 工具请求无效时保留笔记并标记无法解析的来源引用。来源存在不等于语义正确，仍须检查原文。
- skills/objective-react-agent/SKILL.md：要求每轮更新完整笔记，区分待执行与已执行、事实与推测。

查看：运行目录 debug_trace/*-action.json 的 decision.action_parameters.working_notebook；决策模型请求 payload.working_notebook 是传入的上一版本。原始 prompt、响应继续由 trace 保存。

为兼容旧运行/脚本，字段可缺省；缺省时沿用上一笔记，实际输出是否更新需查看 trace。笔记不输入独立答案验证。若工具执行后直接达到 max_actions 或被暂停，最后工具尚未被总结，恢复后的下一轮会处理；不谎称生成了结束总结。

## 验证与局限

真实运行 react-egfr-notebook-20261106，限定 3 个动作，实际为 retrieve_materials → read_artifacts → tool_feedback。三轮均输出结构化笔记；后续笔记带来源 ID，记录定位信息与机制缺口。结果保存在 data/runs/react-egfr-notebook-20261106/working-notebook-observation.json。max_agent_actions 是本次观察设置的边界，未发布题目，不构成质量提升证据。

观察发现模型把未执行的动作混入 explored_directions，因此加强提示：首次没有已执行工具，进展只能来自上一轮工具输出。该提示加强后通过回归，但未再做完整真实出题对照。

回归覆盖笔记保存及恢复、旧历史不重复携带笔记、未知来源警告、参数错误保留笔记、字段长度，以及原有 ReAct/检索链路。

# 实际 Agent 链路与调试日志

## 入口与调用链（路径相对项目根目录）

Web 新建：src/medical_qa_data_factory/web/app.py 的 _run_objective_web_generation 组装 ReactMainAgent；恢复入口同样组装它。CLI：generation/objective_cli.py 的 run_objective_generation_command。

1. generation/agent_loop.py：run -> _run，读取快照、调用主 Agent、验证参数、分发动作、保存工具结果。
2. generation/react_main_agent.py：ReactMainAgent.decide 组装目标、工具、历史与最新观察，调用 objective_react_agent，生成动作。
3. generation/react_observations.py：ObservationView，查看模型看到的内容卡片和最新结果。
4. agent_loop.py 的 _retrieve_materials / _understand_facts / _design_questions / _verify_answer：各动作的输入组装和执行。
5. generation/react_tools.py：阅读、候选评审、显式发布、参数错误反馈。
6. generation/question_design.py、answer_verification_subagent.py、difficulty_assessment.py：具体的出题、验证与难度 LLM 请求。
7. llm/objective_skills.py 的 load_objective_skill：角色与实际加载的 skill/reference 对应关系。
8. llm/providers/openai_compatible.py：_build_body 拼装系统提示与用户 payload；generate_structured 处理压缩/重试/解析；_attempt 真正发送请求。

## 日志位置

新运行和恢复运行在其 runtime workspace 下自动生成 debug_trace/。例如 Web 通常是 data/runs/objective-web/objective_runs/<run-id>/debug_trace/；以实际 runtime.root 为准。CLI 则在 --workspace 指定目录下。

每个事件一个 UTF-8 JSON 文件，按文件名时间戳排序。关联字段：run_id、state_revision、action、decision_id（执行动作时）、invocation_id（逻辑模型调用）、attempt_id（物理请求及其响应）。主 Agent 决策时 action=decide，尚未产生 decision_id；后续 action 事件给出选中的完整决策。

- run_start / run_end / run_error：运行开始、结束或异常。
- action：执行哪一步，全部参数、引用、理由。
- model_request：body.messages 为真正发送的系统提示和用户内容，body 同时保留模型参数及响应 schema。记录在上下文压缩、skill 拼接及修复提示之后，调用之前。
- model_response：HTTP 状态、耗时、完整 raw_response；用 attempt_id 与发送配对。
- model_result：通过 schema 校验的结构化输出、用量；用 invocation_id 与请求配对。
- model_error / transport_error：逻辑调用失败、物理传输失败。
- tool_result：成功提交的工具结果及 artifacts，包含检索状态、证据、候选题、评审意见等。材料正文仍可按 raw_content_ref 在 workspace 中找到。

请求重试会产生新的物理请求事件；历史摘要也以 objective_history_summary 角色单独记录。日志不记录 Authorization、API key 配置或请求/响应 headers。它是本地完整调试记录，包含实际题目和证据正文；不要直接提交 data/ 到版本库。

## 真实冒烟验证

data/runs/trace-smoke-20261106/debug_trace/ 已有真实模型和 PrimeKG 执行记录：1 次主 Agent 调用、2 条 messages 共约 10137 字符，选中 retrieve_materials，返回 22 个 artifacts（含材料、锚点、provider_result、react_step），最后因 max_actions=1 保留恢复状态。这是刻意限制的一步日志验证，不是题目质量测试。

旧任务以前未记录的 prompt 无法补回。重启后端加载新代码后，新调用才会写这些日志。上下文使用 ContextVar 隔离不同运行；每个事件使用独立文件，避免多线程追加同一日志行。日志写入失败会发出 RuntimeWarning，不静默伪称已保存，也不阻断业务。

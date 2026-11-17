# 执行状态与模型解释分离；验证前置条件补救

## 问题

EGFR 上轮模型把未执行的验证请求称为验证失败，又把缺少候选独立检索结果说成系统关联故障，反复重试后关闭候选。工作笔记会延续这种错误解释。

## 改动

- 新增 generation/react_execution_state.py：execution_state 从已持久化步骤、结果、来源状态和错误生成，不读取模型笔记；原始网络错误保留在 search_trace。
- agent_loop._commit_child 在 react_step 保存 execution_record：宿主执行状态、业务结果、错误类型及原工具是否执行。兼容旧步骤时明确标记旧记录缺少执行状态。
- ReactMainAgent 输入并列包含 execution_state（宿主事实）、working_notebook（模型解释）、verification_readiness（逐候选入口条件）。提示模型以执行事实纠正笔记。笔记不覆盖事实，也不作为答案证据。
- verify_answer 缺少独立检索结果或传错 ID 时明确 NOT EXECUTED。工具反馈附 remediation，指出当前候选、缺少内容、request_more_evidence 参数要求、从公开题干制定查询及之后的验证调用方法。
- readiness 只说明可以进入验证，不代表证据相关、独立充分或答案正确。provider_result 必须关联同一候选且 purpose=verification。宿主不选择来源、查询或下一动作。
- skills/objective-react-agent 更新状态区分与补证说明。保留原工具调用自主权。

## 验证

31 项定向测试通过。新增覆盖：缺少证据 → 可读反馈 → 候选补证 → 真正验证；虚假的模型进展不改变宿主状态。代码评审通过。

真实实验复制 react-egfr-notebook-full-20261106 的 checkpoint 87（第二题 KEEP 后、验证前），存入 data/runs/react-egfr-verification-repair-20261106。为维持原提交链完整性，内部 run_id 沿用父任务；新 trace 与后续提交只写实验目录，原任务未变。网络访问使用允许联网的环境。

实际变化：第一动作 request_more_evidence；成功返回 10 条材料；之后进入 verify_answer。验证发现材料不相关，模型再次 request_more_evidence，没有将前置条件误判为系统故障并关闭题目。之后多次 PubMed 查询 EMPTY，模型简化查询、回读已有材料并探索另一机制方向。未证明出题质量提高；检索相关性与查询制定仍待改进。

查看实验目录 debug_trace/*-action.json、*-tool_result.json 和 execution-repair-report.json。此实验保留旧候选及历史，不是完全从零的对照试验。

实验结束：新增 12 步后达到 max_agent_actions，状态 RECOVERY_REQUIRED，发布 0 道。目标候选未被错误关闭。完整动作序列：request_more_evidence → review_candidate → verify_answer → request_more_evidence → retrieve_materials → retrieve_materials → retrieve_materials → retrieve_materials → read_artifacts → read_artifacts → understand_facts → read_artifacts。

# 整合、出题与评审的产物依赖

## 问题

综合研究结果只能通过 claim_ids 分支进入出题，需要额外提取事实。
直接原文出题不能携带 research_id。研究范围和未解决的问题没有按候选题持久化，后续评审容易失去上下文。

## 实现

- design_questions 增加 research_id 单独入口，仍需 evidence_sufficiency_reason。
- 宿主加载该次整合保存的原文片段，对照持久化原文和偏移检查后创建证据锚点；不把模型整合文字当成原文。
- source_sections 直接出题和 claim_ids 事实出题保留。research_id 单独入口不需要 understand_facts；不与 source_sections 混用。
- 每个新候选保存 candidate_research_context 产物，含研究 ID、问题焦点、答案边界、剩余缺口及选中原文。
- review_candidate 与最终验证复核加载该候选的上下文。盲答不接收设计上下文。
- 只有索引时不调用整合模型，返回选段提示；沿用现有可恢复失败机制，下一轮仍由主 agent 选择动作。
- 发布继续要求同一候选版本的评审和验证；没有新增固定出题顺序，也不按模型 ready_for_design 的布尔值强制决定出题。

## 与 LangGraph 的关系

本次入口为 generation/agent_loop.py 的 ObjectiveQuestionAgentLoop._run，使用 Python 循环、工具分派和 ObjectiveRuntimeRepository 持久化。
这些依赖是应用层产物引用和参数契约，未使用新的 LangGraph 节点、ToolNode、Command 或条件边。
仓库 generation/react_graph.py 另有 StateGraph、add_node、add_edge、add_conditional_edges 及 compile(checkpointer=...) 实现，不能把它当成本次修改实际经过的调用链。

## 验证

tests/test_research_dependencies.py 覆盖整合直接出题、断点继续、评审和最终验证上下文、盲答隔离，以及索引不足后选择原文继续整合。
同批回归包括 research_tools、direct_source_generation、react_call_contracts、react_main_agent。
测试使用脚本化模型检查实际调用和持久化；不代表真实模型的出题质量已经改善。

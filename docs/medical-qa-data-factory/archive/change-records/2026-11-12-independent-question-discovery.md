# 独立问题发现 agent

## 问题与职责

原先问题发现 skill 作为 companion 直接附加在主 agent 的同一次决策中，没有独立输入输出。现改为由主 agent 调用 `discover_questions`，单独请求模型角色 `objective_question_discovery`。

- 主 agent 选择材料、调用发现工具、选择返回的机会、调度补查和出题。
- 问题发现 agent 阅读选定原文，返回信息需求、价值、支持观察、必须保留的范围、具体缺口及是否可以出题的建议。
- 出题 agent 接收所选机会及对应原文，形成候选题干与答案。
- 证据整合工具保留，用于围绕已选研究问题进一步整合。发现 agent 不负责检索、验证、发布或停止任务。

它是一次独立、受限的模型调用，不是额外的自主检索循环。主 agent 自行决定何时调用；没有加入强制每篇材料必经的阶段，也没有新增 LangGraph 节点。

## 输入、输出与实现

入口：`generation/question_discovery.py::discover_questions`。

输入是当前 goal、可选 focus、1–6 个材料的选定原文（总计不超过16000字符）、已有候选题及状态、最近的研究笔记。长材料必须指定段落，索引不能充当原文。研究笔记与已有候选只用于了解任务和避免重复。

输出最多四个机会，可返回空列表。每项包含 information_need、value、source_ids、supported_observations、answer_boundaries、missing_evidence、readiness、next_information_target。没有最终答案字段。

每项机会保存成独立 research_id，保留宿主提供的原文及偏移。`design_questions(research_id=...)` 复用已有证据交接；机会说明仍为模型判断，不是验证结论。补查后可再次调用发现工具。模型调用保留独立的 role、skill_version、invocation_id、请求和响应，工具输出写入 runtime artifact 与 debug_trace。

接线包含角色/提示词注册、skill 加载、动作枚举、工具schema、参数检查、执行分发和角色能力注册。问题发现 skill 不再注入主模型；主 skill 改为调度独立 agent。其他出题、评审、验证 skill 没有改写。

## 真实运行与修复

运行目录：`data/experiments/egfr-independent-discovery-20261112`。默认 Qwen，EGFR、free策略、目标3题，五个来源可用。没有运行单元测试。

已观察到独立发现请求、结果持久化，以及主 agent 使用具体 opportunity research_id 进入真实出题和评审。第一次以 HGNC 身份记录发现缺口，随后以 PrimeKG 关联边发现多个研究方向。

运行暴露同一原文片段按“完整材料”和“p0段落”传递时，anchor ID 相同但显示标签不同，导致 IdempotencyConflictError。修复 `source_question_inputs.py`：两条出题路径使用共同的 `_design_anchor`，新标签只使用偏移；旧记录在其余所有证据字段一致时复用原标签，仍拒绝真实内容冲突。用实际失败断点的6个片段重新保存无冲突，随后恢复同一任务继续运行，没有清库或丢弃记录。

独立发现的质量仍需讨论：图谱边能提出追问线索，但不能证明疾病关联具有特异性。发现结果曾明确标记 needs_evidence，主 agent 却继续围绕图谱直接出题或反复调用发现工具，没有先补文献。职责拆分让这个问题可定位，但没有自动消除它。本轮不据此宣称稳定出题。

最终在工具边界主动暂停该观测任务，避免继续重复图谱改写：31个决策，6次实际独立发现模型请求，7条候选记录，0题发布。发现动作选择7次不等于成功模型调用7次；具体记录见 inspection.json 和 debug_trace。恢复后的出题交接不再出现锚点冲突。系统暂停状态为 RECOVERY_REQUIRED、stop_reason=user_paused；这是本次由开发助手调用暂停接口，不是用户点击暂停，也不是自动探索完成。该运行只证明独立调用和交接可执行，不证明题目质量已经合格。

代码导入/schema构建及语法检查通过；独立代码审查、角色注册增量审查、锚点修复审查未发现阻塞问题。旧的 companion 加载断言已同步为独立角色断言，但按用户要求未运行测试套件。

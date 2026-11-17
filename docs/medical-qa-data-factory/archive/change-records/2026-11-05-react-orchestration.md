# 将出题编排切换到 ReAct

## 遇到的问题

原运行入口使用 HierarchicalMainAgent：程序决定先理解材料、再出题、优先处理候选以及补证轮数。即使检索规划使用了 LLM，整个流程仍是固定调度；局部模型失败还可能触发自动选取材料。与“模型根据观察决定下一步”的预期不一致。

## 本次方案和改动

Web 新建、Web 断点恢复及 objective CLI 使用 ReactMainAgent（objective-react-v1）。每轮模型观察目标、策略、候选状态、材料索引、工具结果、检索错误、预算和研究便签，选择一个动作。宿主执行后将结果持久化，再交回模型。

可选动作：retrieve_materials、read_artifacts、understand_facts、design_questions、review_candidate、request_more_evidence、verify_answer、publish_question、close_candidate、stop。

- 检索实体、方向、数据源和查询由模型选择，附数据源查询格式。材料未处理或存在活动候选不再强制阻断继续探索。
- read_artifacts 支持按 ID 分页读取持久化材料原文及工具结果；索引预览不能冒充证据。
- 出题由模型选择 claim_ids 和 design_instruction；revision_of 指向待改写候选，旧候选保留，需要模型显式关闭。
- review_candidate 仅返回评审意见，不自动改题或拒绝。验证失败返回具体缺口，不套用旧的两轮补证关闭规则。
- 验证通过生成发布提案，独立难度评估保留。publish_question 要求当前候选验证通过且最新质量评审为 KEEP；草稿不进入已发布列表。
- 非法工具请求返回 tool_feedback，不替模型选另一个业务动作。新流程检索模型异常不再自动选取全部材料。
- 工具参数、理由、结果引用和简短研究便签存入 react_step。历史使用既有压缩机制，单独保存于 react 目录；最近工具观察有体积限制，详情可按 ID 读取。

## 边界与兼容

ReAct 指外层业务动作选择，并非废除工具内部的流程：事实抽取、盲答复核、难度评估仍是已有专用工具。宿主保留状态一致性、证据发布门槛、暂停恢复、行动步数及工具调用范围限制。预算字段沿用原系统；本次未重做全局模型调用和费用记账。

旧调度器保留供旧协议和测试使用。恢复时已持久化的待执行决策继续按其版本执行，之后新决策由 ReactMainAgent 产生；旧 manifest 不重写。新任务标记 objective-react-v1。

本次不重启服务，不提交 Git，不修改已有任务数据。运行中的进程需要重启才能加载新入口。

## 验证方式

新增 test_react_main_agent.py 覆盖模型逐步选择动作、跨实体继续搜索、分页阅读、显式发布与恢复、错误回传、检索异常、质量拒绝阻止发布、三轮缺证后继续探索。Web 测试检查发布前草稿不出现在发布列表。

这些是可复现的假模型集成测试，证明控制权和状态流转；不证明真实模型已经稳定产出困难题。真实运行需另行观察模型是否合理选择方向、读取材料及及时修订候选。

## 相关入口

- src/medical_qa_data_factory/generation/react_main_agent.py
- src/medical_qa_data_factory/generation/react_tools.py
- skills/objective-react-agent/SKILL.md

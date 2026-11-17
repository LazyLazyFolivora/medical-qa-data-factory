# PubMed 查询调用修复及真实验证

## 问题与改动

前次 BRCA1 任务两次想检索 PubMed，但字符串 query 和不存在的候选 ID 阻止了请求执行。

- ReAct 接口把 PubMed 字符串 query 原样包装为 `{"term": 原字符串}`，不改词、不换 provider、不改动作。
- 工具描述提供实际参数示例，明确候选形成前用 retrieve_materials 调查线索；request_more_evidence 需要真实候选 ID。
- 错误分别指出 query 格式、evidence_need 缺失、PubMed term 为空、候选不存在。错误观察保存原始 attempted_tool_call，供主 LLM 修复。
- 未自动把补证改成探索，也未自动重写复杂检索词。
- 联网验证发现另一个成功分支故障：PubMedMaterialProvider.retrieve 仍引用已删除的 `queries` 变量。将调用统计修为 2（一次 search_result 和一次 fetch_result），消除有结果时的 NameError。

## 验证

项目环境运行 ReAct、材料检索和 EvidenceSearchAgent 测试，共 28 项通过。新增覆盖字符串查询抵达 provider、无候选时保留原调用并反馈、空查询拒绝，以及成功返回摘要的路径。代码审查无阻断问题。

真实数据结果位于 `data/runs/react-live-pubmed-request-replay-20261106/`：

1. 重放原任务第 7 步模型输出，通过真实 agent 分发到 PubMed provider；受限执行环境最初报 WinError 10013。此项使用记录的模型选择，不是新的自主选择。
2. 放开本次只读联网测试后，原查询 `BRCA1 gene mutations AML leukemia Miller-Dieker Fanconi anemia clinical features` 返回 EMPTY，而非格式错误。保存为 original-query-network-result.json。
3. 手工连通性对照 `BRCA1 DNA repair` 返回 SUCCEEDED，取回 PMID 15546503、15829967 的标题和摘要。保存为 connectivity-control-network-result.json。该查询仅用于验证适配器，不代表 agent 自主选出了它。

另从原任务 state_revision=20 复制断点，进行 12 步真实 LLM 自主续跑，目录 `data/runs/react-live-brca1-queryfix-20261106/`，保留原内部 run_id。因 max_agent_actions 结束，发布 0 题。该实验不能证明出题质量已修复；完整决策见 debug_trace。此次修复确认的是查询接口和 PubMed 成功返回链路。

后端需重启才能加载代码；本次未操作现有后端服务。

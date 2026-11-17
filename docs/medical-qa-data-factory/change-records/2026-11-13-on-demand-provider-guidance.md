# 按需加载检索来源指导

## 问题与方案

此前主 agent 与研究 agent 都一开始收到来源参数结构，模型一次选择来源并生成查询；没有在选择来源后补充详细指导。EGFR 的 mention-only 查询只返回前 10 条组织边。

现在主 agent 用 research_evidence 提交 information_need 和可选 preferred_provider。研究 agent 先调用 load_provider_guide，下一轮拿到该来源指导与参数结构后再生成 retrieve_materials。已加载指导保存在研究 checkpoint，恢复时继续可用；未加载来源的查询被反馈，不执行。来源选择仍由模型决定。

PrimeKG 指导额外读取当前实体真实的关系分组、目标类型、方向和行数。它是可查询能力，不作为生物学证据。关系清单查询复用只读连接、参数化 SQL 和查询超时。指导加载的图谱错误返回研究反馈。

主 agent 新请求中移除直接检索动作及来源参数结构，底层旧工具仍支持已有断点。候选补证经 research_evidence.hypothesis_id 委托，内部沿用 request_more_evidence 的验证元数据路径。新研究仍保留阅读和正文工具。

## 修改位置

- generation/react_main_agent.py：主 agent 工具和请求 schema、委托说明。
- generation/react_call_contracts.py：研究委托增加 preferred_provider、hypothesis_id。
- generation/research_agent.py：加载指导动作、持久化上下文、调用前置条件与候选补证连接。
- generation/react_execution_state.py：补证入口提示同步为研究委托。
- generation/evidence_search_agent.py：PrimeKG 参数补齐 entity_types。
- knowledge_graph/providers/primekg.py：relation_inventory。
- skills/objective-react-agent/SKILL.md、skills/objective-research-agent/SKILL.md：职责分工。
- skills/objective-research-agent/references/primekg.md：按实体与信息需求选择关系的指导。

## 真实观察

1. egfr-provider-guidance-20261113：主 agent 真实选择 PrimeKG 委托；实验驱动 record_decision 参数写错，尚未执行研究，已修正驱动。
2. egfr-provider-guidance-20261113-b：重新运行主 agent 后自主选择 PubMed；研究先加载指导再生成查询。网络被当前环境 WinError 10013 阻止，不能判断检索质量。
3. egfr-provider-guidance-20261113-primekg：复用第一轮真实 PrimeKG 委托，研究自主加载指导、生成关系过滤。native_id 1956 在图谱中同时属于 EGFR 和解剖实体，未指定类型导致解析歧义与空结果。另一次跳过 PubMed 指导的调用被程序反馈拦住。
4. egfr-provider-guidance-20261113-primekg-v2：补齐实体类型参数指导，再次复用同一委托。模型加载指导后，自主生成疾病、药物、蛋白互作三组查询，均携带 entity_types=[gene/protein]，每组成功返回 10 条材料，共 30 条。原始 system/user prompt、模型响应、指导和工具结果保存在 debug_trace，汇总为 prompt-and-output.md。

## 验证边界与遗留观察

只做语法、导入、schema 检查和真实模型调用，没有运行单元测试。代码经过静态评审，候选补证连接未做真实端到端验证。

本次只限定三个研究回合，未生成或发布题目。v2 第三回合的说明声称要查 PubMed，实际参数却重复 PrimeKG 疾病查询；没有把解释当作执行事实。这说明分阶段参数生成已可工作，但后续研究一致性仍需单步观察。其他来源目前按需加载既有简短说明与 schema，尚未重写专门的来源方法论。旧 EGFR 单步任务未被改写或推进。

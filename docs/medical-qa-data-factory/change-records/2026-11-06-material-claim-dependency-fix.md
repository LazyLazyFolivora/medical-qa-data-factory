# 材料与事实依赖修复

## 问题

上一轮 react-live-brca1-queryfix-fresh-20261106 中，主模型将 material ID 当成 claim_ids，9 次收到相同的模糊错误，未调用事实提取就停止。输入字段 facts 混入了材料索引，工具说明没有明确持久化输入依赖，校验反馈未区分没有事实与 ID 类型错误。

## 改动

- react_main_agent：facts 仅含 candidate_claim；其他材料和结果放在 artifact_index。新增 design_inputs，明确列出当前可用 claim ID 和数量。
- understand_facts/design_questions 工具说明明确输入输出关系：阅读材料不创建 claim；出题只接受持久化事实 ID。
- 校验分别说明无事实、参数格式错误、引用了材料/其他类型/不存在 ID，给出如何修复依赖的说明。仍由模型选择动作，不自动插入事实提取或改写研究路线。
- exploration_memory：artifact_index 纳入历史压缩；当前 design_inputs 保留，避免压缩后丢失可用 ID。
- objective-react-agent skill 同步说明这些工具依赖。

## 测试

ReAct、探索记忆和 agent_loop 共 38 项测试通过。新增回归覆盖：材料误作事实→明确反馈→模型选择理解→错类型反馈→使用事实成功出题；以及材料索引压缩后仍保留当前事实 ID。代码审查无阻断发现。

## 真实运行

新任务 react-live-brca1-claimfix-20261106，BRCA1 / 672，hard，目标 1 题，最多 30 动作、100 次模型调用；显式开放 PrimeKG、PubMed、HGNC、NCBI Gene、Ensembl，并允许只读联网。没有注入动作序列。

- 成功执行 6 次 understand_facts，3 次 design_questions，生成 2 道候选；没有出现本次针对的“把材料作为 claim_ids”错误。
- 主模型从图谱转向 PubMed；首次查询 EMPTY 后自行改写，取回 10 条文献材料，再提取事实。
- 无候选时误用 request_more_evidence 的问题再次出现，但模型依据具体反馈改成 retrieve_materials，继续探索。
- 后续完成候选评审、PubMed 独立补证和一次 verify_answer。
- 总计 41 个 candidate_claim、49 个材料、2 道候选、0 道发布。30 步到达 max_agent_actions，保留 RECOVERY_REQUIRED。
- 仍有 8 次 tool_feedback，包含长 ID 抄错、重复选择已处理材料以及验证前缺少独立证据等；不应宣称所有工具使用问题已解决。

修订候选题为：“在PARP抑制剂针对BRCA1缺陷细胞的合成致死机制中，哪一种关键的DNA复制分子特征在耐药性产生后表现出减少，并在重新敏感化后恢复？”

候选预期答案为 replication gaps，独立盲答给出 DNA replication fork stability；复核生成 INSUFFICIENT_VALIDATION_EVIDENCE 缺口。这里只记录模型结果，不将其医学解释或拒绝理由视为已人工核实的结论。原始来源与独立证据、验证逻辑需另行分析。

完整结果：data/runs/react-live-brca1-claimfix-20261106/quality-test-summary.json；逐次 prompt、返回、工具执行见同目录 debug_trace。此轮确认输入依赖修复在真实链路中有效，不代表困难题质量或发布流程已全面达标。未重启现有后端。

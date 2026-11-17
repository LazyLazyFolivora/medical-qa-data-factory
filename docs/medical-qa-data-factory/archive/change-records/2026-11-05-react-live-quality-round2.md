# 移除隐式判断后的真实出题复测

模型为当前配置 Qwen3.6-FP8，真实调用模型、图谱与外部数据库。两次均要求 1 道 hard 题、hard 生成策略；测试期间未修改业务代码、未人工向运行注入材料或决策。均以独立 CLI 执行，未重启用户后端。

## 结果

| 实体 | 决策次数 | 完成工具动作 | 候选 | 发布 | 终止原因 |
|---|---:|---:|---:|---:|---|
| NRF1 | 25 | 25 | 0 | 0 | max_agent_actions，保留恢复状态 |
| BRCA1（672） | 10 | 9 | 0 | 0 | 模型主动 stop，COMPLETED_WITH_ZERO_QUESTIONS |

本次没有题目可逐题评估，因此不能声称题目正确率或困难题质量提高。可确定的是：这两个样本的端到端产出均失败。结果也不能估计所有实体上的成功率。

## NRF1：身份确认后仍漂移、工具协议使用不稳定

开局查 HGNC 确认 NRF1（HGNC:7996），并获得 Ensembl ENSG00000106459，随后转向功能资料。但后续又查询 NFE2L1，出现未经可靠映射的 native_id 以及 gene/protein 类型使用混乱。25 个动作全部用完，没有调用出题。

外部障碍确实存在：PubMed 和 NCBI Gene 返回 HTTP 500。图谱还有 native_id 查询缺少 mention 导致 EntityLookup 校验错误；模型传入 gene/protein 等筛选的具体取值与图谱类型协议不匹配，空结果不能直接当成缺少科学关系。

## BRCA1：新结果已经存在，模型仍用旧结论停止

先查 PrimeKG 得到 20 条材料，其中是组织分布关系，随后读取、抽取。读取过程中两次工具请求因 ID/数量等参数问题返回反馈。最后模型主动把查询改为 target_types=[disease, drug]、material_limit=50。

最后一次 provider_result.decision.8e23c84e661c9f540fa5 显示 SUCCEEDED、material_count=50，包含新疾病关联。例如：

BRCA1 --disease_protein--> Fanconi anemia

这是本地图谱记录，用于证明返回了不同类别的材料，不在此将图谱边额外解释为临床或因果结论。

但下一步模型直接 stop，理由仍是“只有 anatomical 关系”“可用来源已耗尽”。它没有读取或理解这 50 条新材料，也没有尝试其他提供者。因而这次停止理由与实际工具输出不符；不是宿主固定调度器强制结束。

## 代码与观察输入中的具体问题

react_main_agent.py 的通用索引 preview 截取序列化 payload 前 360 字符。对材料和证据锚点，开头主要是长 ID、content_hash、quote_hash 等，关键 quoted_text 往往尚未进入预览。

latest_observations 当前不直接包含 evidence_material/evidence_anchor/provider_result 内容。检索的状态和 material_ids 在 search_results 中，原文可以 read_artifacts 获取，但主 Agent 需要先识别新结果并主动读。这种输入设计没有把“最新查到了什么、与上轮有什么变化”突出展示，可能放大模型沿用旧研究便签的问题；不能仅凭本次试验认定它是唯一原因。

## 本次确认与未确认

已确认新检索路径每次只执行一个给定查询，不再调用隐式多轮检索规划/词频补选；由模型主动选择后续动作。没有生成候选，因此本轮未触发题目评审、答案验证、难度评估或发布，不能据此评价这些模块是否已经改好。

优先需要改进的是可读的检索观察、实体身份与工具协议说明，以及要求模型基于新结果更新工作记录的 skill。应让 LLM 看清事实后决定，而非重新写死“有材料必须下一步出题”。

## 原始运行

- data/runs/react-live-nrf1-20261105-c/objective_runtime/commits
- data/runs/react-live-brca1-20261105-d/objective_runtime/commits

每个运行目录中另存 quality-test-summary.json，CLI 发布目录后缀为 -release。两个 CLI 子进程均已正常退出，没有留下测试后台任务。

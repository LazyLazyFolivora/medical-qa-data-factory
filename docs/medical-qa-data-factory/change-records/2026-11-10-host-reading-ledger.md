# 执行记录与研究笔记分开

## 问题

固定运行 first-action-4624fd4b1081 的第 6 步前，历史压缩把“已取得全文”概括为“已分析全文”，并混淆不同论文的内容归属。主模型同时返回下一动作和 last_step_progress，也会把待执行动作写成已完成。LangGraph 的持久化不会辨别这些模型描述的真假。

## 修改

- `react_observations.py` 从已保存的材料和读取工具输出生成 reading_ledger，记录来源、材料类型、返回过的段落和完整性。下载、索引预览、失败输出、模型笔记不计作正文阅读。该记录仅说明工具返回情况，不表示理解或正文仍在当前上下文。
- 材料卡片的 provider_id、source_record_id、material_type 放到预览之外，正文截断不再截掉来源身份。
- `research_tools.py` 返回明确的 returned_paragraph_ids；完整选中摘要 p0 也算完整读取该材料，但不表示取得论文全文。兼容旧记录，按段落覆盖计算完整性。
- `react_main_agent.py` 不再要求模型返回 last_step_progress；兼容旧输出但不带入新笔记。决策仍由模型选择，程序不自动改选工具。原始 HTTP 响应继续完整保存。
- ReAct 模式中，压缩只覆盖 facts、candidate_reviews、research_history。执行历史、材料身份、reading_ledger 保持程序生成的结构。旧混合摘要保留在原文件，新记忆使用 research-v2 文件，不沿用旧摘要。其他 workflow 的压缩范围不变。
- 材料索引不再重复列出每个 evidence_anchor 和整份读取结果。历史步骤保留结果材料 ID、其他结果 ID 和省略的锚点数量；完整锚点仍在存储中。最新工具结果继续直接进入上下文。
- 产品 skill 同步移除 last_step_progress 要求，明确摘要与全文、存储与返回的区别。

## 固定材料重放

新增 `scripts/replay_react_decision.py`：读取保存的 before.json，调用一次真实主模型；仅当选择 read_artifacts 时执行本地读取，其他动作只记录。原运行不写入、不推进。

输入：`data/runs/first-action-4624fd4b1081/single_steps/006/before.json`，revision 27。

第一次尝试保存在 `data/runs/reading-ledger-step6-replay-20261110`：程序预检发现上下文仍超限，没有发出模型请求。原因是结构化执行历史重复携带大量锚点 ID；随后改为保留材料/其他结果 ID 和锚点计数。

成功重放保存在 `data/runs/reading-ledger-step6-replay-20261110-b`：Qwen3.6-FP8，HTTP 200，输入 16276 tokens，输出 787 tokens。本次无需历史摘要。完整 system prompt、请求、原始响应在 debug_trace，agent-input.json 是结构化业务输入，decision.json 和 outputs.json 分别保存决策与实际工具结果。

模型选择读取 29455669、41150710、34558640 三篇摘要的 p0，三个实际结果都是 literature_abstract，content_complete=true。模型明确称其为 abstracts，AXL 内容归属 41150710，没有沿用原错误摘要的跨论文归属。

限制：其中两篇摘要此前已读；research_note 仍有“Reviewed three key abstracts”的宽泛过去式描述。因此此次改善了来源区分和执行状态的表达边界，不能据此宣称消除了幻觉、提高了最终题目质量，或证明重复阅读已解决。研究笔记始终是模型解释，不是执行事实。

## 验证

52 项相关测试通过：读取记录、笔记兼容、历史压缩、批量阅读、执行状态、研究工具、直接来源出题和 schema export。未运行完整实体出题流程；原任务仍停在原位置。

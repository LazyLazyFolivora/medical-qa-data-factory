# 在 32768 窗口内准确计数与原文去重

## 问题及验证

first-action-8e68f66e39cd 第 3 步曾被本地预检拦截，估算 29796 tokens，压缩后 29232，输入预算 27648。请求没有发给生成接口。

发现当前模型服务提供同源 `/tokenize`。使用第 2 步实际 messages 与 chat_template_kwargs 验证，返回 17764，与生成接口的 prompt_tokens 完全一致。对原第 3 步保存的待发送请求重新计数为 20093；所以该次拦截主要是字符估算法高估，而不是服务端真实窗口耗尽。

## 修改

- 模型配置新增可选 tokenization_url，必须与生成接口同源，避免把同一认证信息发送到其他主机；当前本地配置已启用，示例默认 null。
- 历史压缩阈值与发送前预检统一使用服务端 tokenizer，包含实际 messages、模型、chat_template_kwargs、工具信息及生成前缀。response_format 是解码约束，不作为正文重复计入；这一选择已用当前服务真实 usage 对齐。其他服务需另行验证，未配置计数接口时仍使用原保守估算。
- 输出预留 4096、安全余量 1024、总窗口 32768 不变，输入上限仍是 27648。计数结果按请求内容缓存（最多 256 项）；计数失败记录 trace 并回退保守估计，同一个模型实例不反复请求失败接口。
- 读取结果的模型视图只在 content 中保留一份原文。excerpts 保留段落与位置元数据；已返回段落的索引不重复预览，未读段落的导航预览保留。磁盘原始工具结果不改变。
- 通用压缩不再用旧 planner 的字段列表重写 ReAct prior_searches，防止抹掉 action、sequence、result_ids 和实际参数。
- 单步脚本兼容“预检异常后仍处于活动状态”的断点，不再无条件二次 resume；终态仍拒绝继续。保存旧失败目录，在 003-attempt-2 中记录重试。

## 实际单步结果（到第 5 步停止）

| 步骤 | tokenizer / 实际输入 | 模型动作 | 实际结果 |
| --- | --- | --- | --- |
| 3（重试） | 19728 / 19728 | request_more_evidence，引用 temp_hypothesis_egfr_drug_nature | ID 不存在，反馈给模型，没有执行检索 |
| 4 | 20691 / 20691 | read_artifacts，读 Cetuximab、Erlotinib | 成功，但重复了第 2 步已完整读取的图谱边，没有新内容 |
| 5 | 17169 / 17169 | retrieve_materials，PubMed 查询 Cetuximab EGFR mechanism clinical indication | 沙箱网络 WinError 10013，UNKNOWN，无材料 |

第 5 步随后用完全相同的 EvidenceProviderRequest 在允许网络的环境中只重试工具，没有再次调用模型，也没有推进第 6 步。返回 SUCCEEDED、9 条材料，保存在 single_steps/005/network-recheck-result.json。该诊断结果未覆盖原 outputs.json，也未注入运行状态；运行中第 5 步仍记录原网络失败。后续若要排除网络影响，应显式建立重放分支或让 Agent 接收新的真实检索结果，不能默默替换审计记录。

每步保存 before/after、decisions/outputs、实际 HTTP trace、system-prompt.txt、user-prompt.txt、agent-input.json、model-response.txt。原失败 003 保留。第 3～5 步均未触发上下文压缩，实际输入加输出均低于窗口。

## 验证与限制

95 项相关测试通过，覆盖 tokenizer 请求、缓存、失败回退、32768 总预算、旧 ReAct 执行记录保留、原文去重且不改存储，以及已有模型适配器、配置、主决策和读取工具。

这次修复解决了当前误拦截，不等于无限历史可装入 32K。未来接近真实上限时仍需按预算选择材料和压缩研究记忆。没有删除核心 skill，也没有裁掉可选工具或修改模型研究路线。当前还观察到候选 ID 编造及重复阅读，属于另一个决策质量问题。

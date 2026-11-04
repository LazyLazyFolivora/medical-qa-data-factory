# 模型调用上下文管理

## Ensembl 展开记录的事实提取

Ensembl 的展开响应可能包含大量转录本及外显子，单条记录即可超出窗口，不能仅靠按材料篇数分批。事实提取使用已有引用锚点中的 display_name、description、biotype、seq_region_name、start、end、strand、assembly_name、canonical_transcript 字段构造输入视图；完整记录保留在持久化层。视图明确标注只是选定字段，不把未提供的转录本、外显子或交叉引用解释成不存在。锚点文字、哈希和相对于原始记录的位置保持不变，模型只能引用实际提供的锚点。没有可用字段锚点时保留原路径并明确报告超限，不伪造空结果。

2026-11-03 MAN1B1 任务 web-b28a9779-3212-4182-9b8e-23fc72943a79：原始单条记录 179219 字符，失败请求估算 186931 tokens；修复后的完整请求只读预检估算 9656，预算 27648。此结果仅验证输入预算，未调用真实模型，未恢复任务。

所有通过 OpenAICompatibleModel 的主 agent 和子 agent 调用，在每次 HTTP 请求前（包括格式修复和重试）执行 ContextManager.prepare。

配置位于 config/models.json 的每个 provider 内：

- context_window_tokens：服务端实际部署窗口，当前 Qwen 配置 32768；不能仅修改客户端就扩大服务端窗口。
- output_reserve_tokens：默认输出上限 4096，同时以 max_tokens 发给服务端。request_parameters 中显式配置的 max_completion_tokens 或 max_tokens 优先，并参与输入预算计算。
- context_safety_tokens：1024。
- compact_trigger_ratio：0.85。
- compact_target_ratio：0.75。

输入预算 = 窗口 - 输出上限 - 安全余量，默认 27648。触发阈值约 23501，整理目标 20736。目标是尽力达到；保留必要资料后未达到目标但没有超过输入预算，可以继续调用。

完整组装后的请求参与估算，包含 system prompt、skill、payload、schema 和修复提示。当前采用 ASCII 每 3 字符约 1 token、其他字符每字符 2 token，加固定开销的启发式估算，不是模型原生 tokenizer，不保证绝对不超限。调用结果 usage.context_management 记录该次请求整理前后估算、预算、整理轮数。

整理主要作用于决策上下文中的 artifact_summaries 和 retrieval_history：减少预览、历史记录和搜索摘要。原始持久化数据不变。出题和验证 payload 中的 candidate_claims、evidence_excerpts 等关键资料不进行通用字符串截断。

两轮整理仍超过预算，抛出不可重试 BAD_REQUEST，不发送 HTTP。已有 agent loop 将其转成 RECOVERY_REQUIRED，保留现场。服务端仍报告超限时，保留有限的进一步整理与重试；耗尽后同样进入恢复状态。

候选设计阶段已接入自适应分批：上下文超限时按事实及来源关联拆成子批，保留每条事实对应的完整证据，通过独立请求 ID 调用后汇总候选。优先在来源组之间拆分；不会截断证据。单条事实连同不可分证据仍超限时明确失败。事实提取阶段按完整材料预先拆成每批至多 5 篇；若完整请求仍超限，继续二分。各批仅携带自己的完整材料和引用，全部成功后汇总事实；单篇仍超限会明确失败。其他阶段尚未实现通用自动分批，也未接入原生 tokenizer。


2026-11-03 TTC33 只读预检重放：web-ede416f7-5c94-4a49-8a6f-26afa6cec421 的 20 条事实请求重现 30216 token 估算，超过 27648 输入预算。自动拆为两个 10 条事实子批，估算分别 21914 和 19912。此为完整请求构建与预算检查，不是模型生成质量评测，没有恢复或改写原任务状态。

2026-11-03 NRF1 只读预检重放：web-17b4f18b-77b7-4767-b9b3-d73804ca2001 原失败发生在 understand_facts，记录估算 28024、预算 27648。修复后原 10 篇材料拆成两批 5 篇，在当前配置及模板下估算分别为 20024、15202。仅检查完整请求预算，未调用模型或修改原任务。证据引用字段同时关闭首尾空白自动清理，保持引用文字、偏移和哈希一致。

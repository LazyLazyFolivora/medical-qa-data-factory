# 主 Agent 动作参数结构约束

## 问题

运行 first-action-132b8acf4f85 第一轮返回 retrieve_materials，但完全没有 parameters。此前 response_format=json_object，ReactChoice 的 parameters 默认空字典，只有 action/rationale 必填。适配器视其为合法响应，直到工具执行前才产生 Select an available provider_id。原始失败运行保留。

## 修改

- 新增 react_call_contracts.py，按动作定义参数结构：单源检索必填 provider_id/query/evidence_need，多源必填 searches 及每项参数；补证还需 hypothesis_id。其他动作声明对应候选 ID、研究问题、原文段落或事实 ID 等结构。材料存在性、权限、发布条件等仍由运行时检查。
- ReactChoice 的响应 schema 按 action 分支约束 parameters。本地解析使用同一组 Pydantic 参数模型，使漏参数触发现有模型适配器的有限格式修复，而不是将一个空调用交给工具。程序不填造 query，不替模型选择来源；保留旧 PubMed 文本 query 的表示兼容。
- 支持 structured_output_modes_by_role 配置，并参与配置摘要。当前本地模型仅 objective_react_agent 使用 json_schema，其他角色保持 json_object；supports_strict_json_schema 已启用。服务端不支持时仍遵循原有限 fallback 流程，本地参数检查仍生效，trace 标记实际模式和重试。
- 严格模式下主 Agent schema 只放 response_format，不再完整复制到 user 消息；fallback 为 json_object 时仍把 schema 放到文本，避免重复占用上下文。

## 服务探测与限制

strict-call-probe-20261110：当前服务接受 json_schema，HTTP 200，但初版分支 schema 的响应漏掉根层必填 rationale。本地检查识别错误，未执行工具。不能仅凭 HTTP 200 判断严格约束完整有效。

随后各分支显式包含公共字段和 required，strict-call-probe-20261110-b 一次返回完整检索参数和 rationale。仍保留本地检查与格式修复，不把一次成功解释为绝对无误。query 内部的数据源语义、ID 存在性及互斥业务要求仍需运行时验证。

## 实际链路验证

新隔离测试运行 first-action-35a4e35aa25b，只执行一次主决策和工具动作。没有推进或改写失败的原运行。

输入：EGFR，free 策略，目标 1 题，无历史或材料；相同五个数据源和完整主 Agent skill。实际请求使用 json_schema。模型 Qwen3.6-FP8，输入 6334 tokens，输出 182 tokens。

模型返回 retrieve_materials，parameters 中包含 provider_id=primekg、query={mention:EGFR}、evidence_need 和 purpose=discovery。模型未返回 material_limit，使用原有运行时默认值 10，未篡改模型输出。PrimeKG 实际 SUCCEEDED，返回 10 个 knowledge_graph_edge 材料。此时未生成题目，停在第一步。

运行目录保存 system-prompt.txt、user-prompt.txt、agent-input.json、output-schema.json、model-response.txt、result.json；debug_trace 保留实际 HTTP 请求和响应及执行事件。output-schema.json 是独立的协议约束，不在 user-prompt.txt 内重复出现。

## 测试

99 项相关测试通过（模型适配器、配置、动作参数、主决策、工作笔记、统一读取、研究工具），并完成一次真实第一步测试。新增回归覆盖原来的漏参数响应、批量参数、服务不可靠时在同一次模型调用内修复、按角色选择模式及 fallback 携带 schema。

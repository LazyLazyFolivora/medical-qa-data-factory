# 多来源并行检索、深入阅读与证据整理

## 需求与设计

主 LLM 不应每轮只能选择一个来源。它可以根据研究目的，为多个合适来源分别制定查询；互不依赖的查询并行，依赖新发现的后续查询仍由下一轮决定。材料是否具有足够信息量由 LLM 评估，不按字数、条数或来源数量硬打分。

保留单来源调用，同时为 retrieve_materials / request_more_evidence 增加 searches 数组（1–6 项），每项包含 provider_id、query、evidence_need 和可选 material_limit、fetch_full_text、target_entity。补证批次统一关联顶层 hypothesis_id。

远程请求最多 4 个并发；同一 provider 实例串行，SQLite 本地来源留在控制线程，同时与远程请求重叠。线程内只读取、不写共享持久化库；所有结果返回后顺序保存，一次提交整个动作。失败、空结果与成功分别保留，不把单源失败当成批次失败。

## 新能力

- provider_guide：描述 HGNC、NCBI Gene、Ensembl、PrimeKG、PubMed、CTD 的适用信息与局限，配合已有 query schema。
- read_source_details：按已保存 PubMed 材料 ID 请求可用文章正文，无需重复搜索。正文生成独立内容版本，不覆盖摘要。保存截断、不可用与 fallback 信息；目前受全文接口和长度上限限制，不承诺表格/图注/补充材料齐全。
- synthesize_evidence：向真实 LLM 提供所选材料的原始内容及证据，整理每个来源的信息贡献、条件、范围差异、冲突、缺口和问题机会。它是研究判断，不是新证据，不创建 claim，不替代答案验证。
- 整理结果作为非证据的研究提示供出题参考；事实提取强调保留研究身份、人群、条件、终点与不确定性，避免把不同研究强行拼接。
- 主 skill 明确区分概览、深入读取、跨来源整理及正式事实提取；摘要不足时应指出缺少哪种细节并继续读，而不是用常识补全。

核心代码：generation/research_tools.py、react_main_agent.py、agent_loop.py、react_tools.py；模型角色 objective_evidence_synthesis 已注册。

## 验证结果

59 项针对性测试通过，覆盖真实线程重叠、重复命中、单源失败保留兄弟结果、批次调用前参数验证、补证候选归属、正文不覆盖摘要、重复正文读取、整理不冒充事实，以及现有 ReAct/agent_loop/上下文与 schema。代码审查无阻断发现。

### 工具能力实测（动作序列明确指定）

目录：data/runs/research-tools-live-check-20261106。

固定执行“两来源并行→文章详情→真实 LLM 整理”，不是自主规划表现测试。HGNC 与 PubMed 的 search_start 相隔约 1 毫秒，两者完成前都已启动；分别返回 1 条身份记录和 2 条论文材料。所选 PMID 31564718 未返回可用实质正文，明确记录 EMPTY 和 detail_status，没有将摘要伪装为全文。

整理 LLM 实际读取 HGNC 和 PubMed 摘要，区分身份资料与耐药研究概览，指出缺少量化结果、特定条件和全文细节。机会列表是研究建议，不能当作已证实的问题或答案。结果见 research-path-summary.json 与 debug_trace。

### 自主出题实测（不指定动作）

目录：data/runs/react-live-egfr-multisource-20261106。EGFR，hard，目标 1 题，五来源开放，30 动作上限。

模型仍主要逐个来源检索，未主动使用并行批次或证据整理工具。它主动调用过一次 read_source_details（PMID 29141884），但没有拿到可用正文，fallback 的 JSON 解析错误保留在返回信息中。已完成图谱/文献检索、事实提取、设计和评审，生成 3 道候选、未发布；后续一次 understand_facts 对单条过大材料分批仍超出上下文窗口，进入 model_request_requires_repair。最后一次失败输入估计 31692 tokens，预算 27648。

本轮证明新增工具链可执行，不证明模型已稳定学会选用它，也不证明材料深读和困难题质量已解决。单条长材料的按段阅读/提取仍是后续需要处理的限制。未为本轮测试更改或重启现有后端服务；后端重启后加载新代码。

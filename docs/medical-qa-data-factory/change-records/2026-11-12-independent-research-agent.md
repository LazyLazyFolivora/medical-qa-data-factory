# 独立研究 agent：围绕信息需求循环检索和阅读

## 为什么增加这个角色

此前问题发现 agent 可以指出具体缺口，但搜索词、来源选择、正文获取、段落阅读和补查都由主 agent 承担。新增研究 agent 接收一个信息需求，在有限步数内自行调查，将证据和未解决缺口交回主 agent。它不出题、不验证、不发布，也不停止整个任务。

## 当前接口与调用链

主 agent 选择 `research_evidence`，参数为 `information_need`、可选 `max_steps`（默认8，上限16）；可附带已知材料ID作为线索。

`agent_loop` → `execute_react_tool` → `research_agent.research_evidence` → 独立角色 `objective_research_agent`。

每轮由模型选择 `retrieve_materials`、`read_source_details`、`read_artifacts` 或 `finish_research`。工具复用生产检索、正文获取、材料持久化与原文片段读取实现，保留多查询批量检索能力。独立来源可并发；同一来源仍遵守现有 provider 锁策略。

研究 agent 的skill为 `skills/objective-research-agent/SKILL.md`，只注入该角色。主skill只增加委托与接续指导；未改写其他出题、评审、验证skill。没有新增LangGraph节点，接入当前实际运行的Python ReAct循环。

## 输入输出与证据

每轮输入委托问题、实体、可用来源与查询schema、材料索引、最近工具结果、研究摘要和剩余步数。材料使用短别名，存储仍保留完整ID。长正文先看索引，再按真实段落ID读取。

返回 `research.<parent decision ID>`，包含研究摘要、有来源与范围的发现、具体缺口、实际工具错误、获取和阅读过的材料ID、选择的原文片段及完成原因。报告只属于研究指导；原文仍独立保存。主agent可把返回的材料及段落交给问题发现agent。子任务预算耗尽不表示整个实体无题可出。

`max_steps` 限制这个子循环的轮数，不等于模型服务端重试次数。原系统全局模型调用预算统计未在本轮重构，不据此宣称全局计费统计已完善。

## 记录、暂停和恢复

- 每个研究委托有 `objective_research/<parent decision ID>.json`，记录已完成工具、材料、当前摘要和待执行动作。
- 内层工具结果先写自己的checkpoint，结束后统一交回主runtime，避免第一次检索完成就把整个研究委托标成完成。
- 在模型调用和工具调用边界检查暂停；保留pending父决策，恢复时继续同一checkpoint。
- 模型调用报错时进入恢复状态，不提交FAILED child slot，否则父循环会跳过原研究断点。
- debug_trace保留独立角色的prompt/响应，以及 `research_action`、`research_tool_result`、`research_tool_feedback`。
- 语义充分性由模型判断；宿主只限制工具权限、预算边界及原文引用。返回片段必须来自本次研究实际读取过的段落。

## 真实运行观察

未运行单元测试。使用真实默认Qwen和生产来源，开发助手给定“查清EGFR与急性肾损伤关联的实验模型、干预、结果和时间条件”这一委托；之后的查询、论文选择、阅读和返回由模型自行决定。不能将这次委托入口描述成主agent自主发现的方向。

第一轮 `data/experiments/egfr-research-agent-live-20261112`：检索成功，但模型反复遗漏顶层材料ID、猜测不存在的段落ID。已补齐schema中的读取ID约束、兼容旧嵌套参数形式，并明确先读材料/索引再选择段落。旧代码运行在工具边界暂停，记录保留。

修正后运行 `data/experiments/egfr-research-agent-fixed-20261112`：研究agent自主检索并请求三篇论文正文。PMID40379634获取成功，保存正文78903字符，实际读取p5、p19、p13；另外两篇正文未获取，读取了其摘要并继续针对干预和时间条件补查。获取全文与实际读过哪些段落分开记录。

最终该研究委托完成6次工具操作（检索、获取正文、读取、读取、补检索、补检索）后主动finish，原文片段和未解决缺口均交回主runtime。主agent随后实际调用问题发现、出题、评审与验证，生成2条候选；在本次12个外层动作上限结束，发布0题。外层出现6次工具反馈，包含缺发布提案和混用research_id/source_sections等已有问题，本轮没有扩展修改出题与发布编排。最终状态RECOVERY_REQUIRED，原因max_agent_actions，不能描述为完整数据集生成成功。

该次finish的summary为空，但保留了findings与remaining_gaps，因此最后将研究summary字段改为必填且非空；这处小改动只做schema检查，没有再次执行整轮模型。后续应继续观察摘要质量，不能将字段非空等同于总结准确。

代码导入与响应schema构建检查通过。审查指出的模型报错丢失恢复入口已修复并复审通过。正文阅读证明研究循环能执行，不代表已有证据结论、最终出题质量或跨实体稳定性已被验证。

# PrimeKG SQLite 导航适配与真实运行记录

## 目的

让研究 agent 先了解图谱有哪些方向和对象，再自主选择具体关系。所有操作仍属于 primekg provider；程序负责 SQL、分页与去重，模型负责研究方向。

## 实现

- 新增 `knowledge_graph/primekg_browser.py`：overview、search_nodes、list_neighbors、get_relations。
- overview 自动随 PrimeKG 指引加载，提供实体身份及各关系的对象数量。
- list_neighbors 每类关系独立分页，返回名称、类型、ID、总数和游标；默认合并两种存储方向，按对象身份去重。
- get_relations 按选定对象查实际边，保留存储方向和来源字段，返回材料与分页状态。
- agent_loop / research_tools 同时支持单次及 searches 批量导航；导航结果作为 observation，不能冒充证据材料。
- research_agent 的来源指引使用 primekg-navigation-v2，旧断点恢复时更新旧指引。
- evidence_search_agent 的参数说明与 PrimeKG skill 同步更新。

参考 https://github.com/K-Dense-AI/scientific-agent-skills/blob/main/skills/primekg/scripts/query_primekg.py 的接口思路，未复制 pandas 实现。当前 SQLite 只有 head/relation/tail、两端 ID/type 和 source，没有原始节点来源命名空间与 display_relation；不能凭空恢复这些语义。此次未实现两跳查询。

## 实际验证

直接 SQLite 探针：`data/experiments/primekg-sqlite-navigation-20261113`。
EGFR 目录分别有疾病 183、药物 31、分子功能 23 个对象。三类独立取第一页和第二页，每页各 10 个，页间无重复。选定对象关系查询成功。反向存储记录不能算独立证据。

真实 Qwen 定向实验：`data/experiments/egfr-primekg-navigation-focused-20261113-v2`。
这是人工限定“只调查图谱”的检索委托，不是完整出题流程。模型自主进行了：

1. list_neighbors 查询疾病、药物、分子功能，每类返回 20 个对象。
2. get_relations 选择腺癌、急性肾衰竭、Cetuximab、Erlotinib、Gefitinib、EGF 激活的受体活性。
3. finish_research。

第二次查询返回 10 条存储关系，已覆盖上述六个对象，仍有下一页。模型没有继续翻页；不能据此声称全部关系已读完，但也不必为了取重复反向记录而强制翻页。

首轮定向实验曾因程序拒绝批量入口中的导航而连续失败；已修复兼容性，v2 的两次工具执行均成功。原始单步调试任务未修改。完整 prompt 和输出保存在各实验的 `prompt-and-output.md`。

## 暴露的后续问题

- 模型选择理由把目录中的 adenocarcinoma 扩写成 Lung Adenocarcinoma；最终总结改回 Adenocarcinoma，但中间理由仍属于未经支持的限定。
- finish_research 没有调用材料阅读工具；其 findings 中出现自行构造的 overview 来源 ID。工具调用成功不等于研究报告的引用与解释合格。
- 名称排序的第一页可能影响模型的方向选择；目录已明确不是相关性排序，尚未证明模型能跨页主动探索。

结论：SQLite 适配和导航到具体关系的链路已运行成功；完整证据阅读、报告忠实度以及最终出题质量尚未由这次实验验证。

代码复核无高、中严重性问题；按复核意见提升指引版本。未运行单元测试，以真实数据库调用和真实模型轨迹核对为主。

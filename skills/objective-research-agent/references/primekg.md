# PrimeKG：先浏览范围，再选择研究线索

PrimeKG 是关系索引，用来发现值得查数据库或论文的对象。所有操作都在 retrieve_materials 的 provider_id=primekg 下，通过 query.operation 选择，不是新的来源或 agent。

## 你已经知道什么

程序在进入 PrimeKG 时自动提供 overview：解析后的实体、关系类型、各类去重对象数量。若身份未唯一确定，使用 search_nodes 按名称和 entity_types 找候选，再根据真实名称、类型选择实体，不任取第一条。后续复制返回的 canonical_id/entity_id；数字编号可能在不同类型中重复。

概览和目录是导航，不是 evidence_material，不作为答案引用。方向 incoming/outgoing 仅表示数据库存储方向；默认 both，反向副本不是两份独立证据。当前 SQLite 只有 head/tail、ID、类型、relation、source，没有原 CSV 的 display_relation 或两端来源命名空间，不能推断缺失字段，也不能把 source 标签当作论文出处。

## 在 PrimeKG 内选择操作

- overview：query={"operation":"overview","mention":"EGFR"}。查看关系范围；已知明确实体可以传 entity_id。
- search_nodes：query={"operation":"search_nodes","mention":"EGFR","entity_types":["gene/protein"],"page_size":20}。按名称找候选；结果截断时缩小名称或类型，不当作完整列表。
- list_neighbors：query={"operation":"list_neighbors","entity_id":"从概览复制","relations":["disease_protein","drug_protein"],"page_size":20}。每个关系分别返回对象名称、ID、类型、总数及 next_cursor，默认合并两种存储方向并按对象身份去重。一次选1—6类关系；程序保证各类独立取数，你不用手工拆 SQL 或分配总配额。
- 翻页：保持实体、关系、方向和类型过滤条件，传 cursors={"对应关系":"该关系的next_cursor"}。只想继续某个关系时可只查询这一类。没有 cursor 表示该类第一页；truncated=false 表示没有后续页，不代表已阅读前面所有页。
- get_relations：query={"operation":"get_relations","entity_id":"已确认实体ID","target_ids":["选中对象ID"],"relations":["选中关系"]}。查询1—8个具体对象的实际关系记录，返回可读取的 evidence_material。material_limit 是关系记录页大小；若截断，保持查询条件并传返回的 cursor 继续。关系仍需按范围解释，不能直接证明机制或疗效。

目录操作可以直接调用，也可放入 searches 批量列表；一次 list_neighbors 已能处理多个关系，不必为每类手工拆调用。旧的无 operation 查询只保留供旧断点兼容，新探索使用明确操作。

## 如何选择研究方向

基因/蛋白可按需求浏览疾病关联、药物关联、互作或功能；研究组织背景才优先组织边。疾病可浏览相关基因、表型和药物；药物可浏览关联蛋白、适应证、禁忌证等。根据实际关系清单选择，不把不同临床关系合并成疗效证据。

对象目录按名称排序，不是价值排名。先看整体数量和各方向的对象，再选择能形成具体信息需求的线索；需要扩展时翻页，不要求遍历所有对象。有明确对象时可直接 get_relations；已有名称足以搜索论文时可直接转 PubMed，不为凑步骤读取每条边。

不要把前一页当作完整图谱、把关联数量当作重要性，或把未发现当作不存在。图谱没有的机制细节去其他来源查，说明具体要弄清什么。

## 接口设计参考

参考 K-Dense PrimeKG helper 的实体消歧、邻居浏览与反向记录解释方法，未复制其 pandas 实现。本项目使用现有 SQLite 的真实字段；没有两跳路径能力。
https://github.com/K-Dense-AI/scientific-agent-skills/blob/main/skills/primekg/scripts/query_primekg.py

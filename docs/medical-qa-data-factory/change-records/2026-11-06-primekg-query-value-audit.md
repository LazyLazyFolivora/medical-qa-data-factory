# 图谱对查询的作用：本地 PrimeKG 实测

只读本地图谱，未出题、未改代码。样本以 EGFR 为主，补查 Gefitinib、Erlotinib、ERBB2；不把单实体测试推广为全图覆盖保证。

## 数据与实测

EGFR 解析为 primekg:gene/protein:1956。读取其邻接原始行 2294 条（查询上限 20000，未触顶）。包含 9 类关系；各关系的不同邻居数：protein_protein 623、disease_protein 183、anatomy_protein_present 130、bioprocess_protein 93、pathway_protein 35、drug_protein 31、cellcomp_protein 28、molfunc_protein 23、phenotype_protein 1。原始行数包含正反方向记录，不可当成独立证据数。

默认 neighbors(limit=10) 全部为 anatomy_protein_present，底层返回 truncated=true 和 next_cursor。按真实关系名过滤可以取得药物、通路、互作等其他结果。生成层 PrimeKgMaterialProvider 将图谱结果转换为材料时只保留 provider_calls/rows_examined，丢失 truncated/next_cursor；没有向主 agent 提供关系分布或图谱分页接口。默认排序样本不能代表全部研究方向。

EGFR 的 pathway_protein 返回 35 个不同通路节点，例如 Signaling by EGFR、EGFR downregulation、GRB2 events in EGFR signaling、Clathrin-mediated endocytosis。

从 EGFR 的 drug_protein 发现 Gefitinib，再查其 drug_protein，得到 EGFR、ABCB1、ABCG2、ALB、CYP3A4 等 11 个关联蛋白。可用于进一步检索各关系的具体性质；不能把所有关联一律解释成药物抑制靶点。

EGFR 与 ERBB2 的 drug_protein 邻居交集为 8 个图谱药物节点：AV-412、Afatinib、Brigatinib、Fostamatinib、IGN311、Lapatinib、Varlitinib、Zanubrutinib。两次查询均未截断。此结果只证明本快照中两组关联的交集，不证明均为双靶抑制剂或适应证相同。

## 能做与不能做

可以：实体 ID 对齐；按关系定位查询方向；产生可追查的实体对；逐跳扩展；对有限候选做快照内交集/差集。差集只能说明当前图谱未记录，不能直接证明现实中无关系。

不能仅凭本地边得到：激活/抑制方向、直接结合、实验剂量/细胞系/时间、适用人群、突变特异性、因果净效应。当前 edges 表仅 head/relation/tail/head_id/tail_id/head_type/tail_type/source，无论文 PMID/DOI 或实验条件列。建库代码将 x_source 写入 source，因此该列通常表示实体命名空间（如 NCBI），不是原始论文出处。

底层 medical-qa 图谱能力明确声明 path_query=false，paths() 禁用；可通过多次 filtered neighbors 做有边界扩展，但不能宣称原生完整路径查询已可用。query-planner 有另一套 PrimeKG 适配接口，需要另行验证，不可与生成端接口混同。

## 建议下一实验

先按关系展示候选方向，再从图谱结果选 2–3 个实体对/通路，进入原始来源查明确关系性质和条件；记录图谱是否缩小搜索范围、是否带来原本未想到的线索、是否最终支持结论。此轮只验证图谱查询能力，尚未做与无图谱检索的效果对照。

原始结果：data/experiments/primekg-query-value-20261106/egfr-graph-audit.json、relationship-probes.json。

# 修订工具可显式选择补查证据

BRCA1 实跑获取全文后，后续修订仍只引用原 NCBI 摘要。原 revise_candidate 只复用旧锚点；使用新证据需切换 design_questions + revision_of。该替代路径已存在，但模型没有稳定使用。

为减少路由歧义，在 revise_candidate 加入可选 source_sections。省略或 null 时行为不变；显式提供时代表完整替换证据集合，需要保留的旧段落也应列入。运行时检查保存的材料 ID，复用 design_source_anchors 的原文偏移、源身份、预算及新锚点持久化。新候选保留修订链接，不继承旧批准。代码审查通过。

真实单步诊断 `brca1-revision-source-probe-20261115-042550`：从已结束的 brca1-20261115-041007 检查点复制隔离运行，强制一个 revise_candidate 动作，选择已保存 PMID32094664 全文 p2/p3；设计模型正常调用。

请求 trace 证实 evidence_excerpts 引用了新全文，不是旧 NCBI 摘要；生成新候选 a3586175c66bedd30a563fc6：“BRCA1蛋白与其必需伙伴BARD1通过哪种特定的蛋白质结构域相互作用形成异二聚体？”相比全体互作目录，信息需求更明确。尚未独立验证答案，不计为合格发布。

该动作是诊断用指定动作，不是主 agent 自主决策成功的证据。自主续跑 `brca1-20261115-042252` 在此 Python 修改之前启动，包含阅读分页但不包含这次新增参数；需按具体请求版本区分。

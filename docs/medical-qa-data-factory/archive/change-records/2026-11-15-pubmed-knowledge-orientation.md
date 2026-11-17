# 从实体知识关系组织PubMed材料

## 起点对照
initial-direction-20261115使用空材料初始snapshot，对HFE和PCSK9分别调用正式主skill与精简实验skill。正式版本也能提出功能/互作方向；精简版本未显示明显优越性，HFE仍夹带临床患病比例需求。未替换正式主skill。

## 实施
更新真正由研究agent加载的skills/objective-research-agent/references/pubmed.md，而非未参与当前链路的旧material_retrieval skill。

新增宽泛实体探索的选材方法：从功能概览/综述理解对象关系与条件，再沿具体关系查原始研究；综述不自动证实机制。明确临床意义不等于收集分组比例和P值；按信息需求读取背景、讨论、方法或结果，区分背景知识、样本观察、作者解释和假说。已有具体缺口可直接查询，不强制综述或多来源。保留原参数契约和全文读取方法。

修改前内容保存在pubmed-orientation-20261115/before-pubmed.md。

## 固定研究上下文回放
取真实HFE首次已加载PubMed指导的请求，其他上下文保持一致。
- baseline输出read_artifacts试图读取图谱实体ID的p1，同时解释又说应先搜索；未执行该无效动作。
- 新版输出retrieve_materials，批量PrimeKG/NCBI/PubMed，PubMed检索式仍为HFE gene mutation C282Y H63D Hereditary Hemochromatosis review。

这只能说明实际加载及动作差异，不能把一次回放的动作正确性当选材质量提升。完整请求响应与输入保存。

## 完整新实体验证
- pcsk9-20261115-054540，目标2题，最多20动作。
- cftr-20261115-054626，目标2题，最多20动作。

两次隔离真实链路运行；保存源码/skill哈希及每步prompt/响应，不进入生产题库。启动后冻结本轮代码和skill，需等待实际题目与答案审计后评价效果。先检查题目独立性，再核对证据是否支持实际答案，不以发布计数代替质量。

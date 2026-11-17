# PubMed摘要旁的正文入口与固定材料决策对比

## 实现

ObservationView在PubMed摘要卡片的预览之外添加paper_access；read_artifacts返回的摘要视图也携带同一入口。该字段不受preview字符截断影响，ID沿用现有别名翻译。

- current_content明确为abstract_only。
- full_text_status区分saved、not_acquired_on_last_attempt、not_confirmed；not_confirmed不承诺可获取，也不声称论文没有全文。
- saved_full_text_ids列出同一PubMed记录已保存正文；有正文时引导read_artifacts，不重复下载。
- 无保存正文时给出read_source_details和具体摘要材料ID，evidence_need由模型根据缺口填写。
- 索引/获取状态不等于已阅读或理解；已保存材料优先于早先失败观察。

provider_result提示不再笼统说“读材料获取完整内容”，明确完整摘要不等于完整论文。
主agent skill补充“相关论文缺细节时深入该论文；方向不符、需独立支持或互补信息时另搜”，以及按方法、结果、图表、讨论选择阅读段落。
通用material_retrieval skill开头的“尚未接入”旧说明已修正，说明角色加载与ReAct直接查询的区别。

## 固定材料实验

目录：data/experiments/pubmed-guidance-fixed-20261112。
使用brca1-ten-questions-20261112第4个动作完成后的同一持久化快照，含5篇已返回的PubMed摘要。修改前先保存before-input.json，修改后从同一快照构造after-input.json。

当前配置模型、输出schema及采样设置不变；两组均不注入跨轮memory历史。每个条件两次，交错调用。保存完整system/user输入和原始API响应，不执行选出的工具、不发布题目。

| 条件 | 修改前选择正文 | 修改后选择正文 |
|---|---:|---:|
| 原始自由出题状态 | 0/2 | 0/2 |
| 相同材料+明确方法学缺口 | 2/2 | 2/2 |

自由状态修改前两次选择understand_facts；修改后一次read_artifacts、一次design_questions。
明确缺口条件对两组加入相同任务：核实PMID15829967中BRCA缺陷及对照细胞模型、PARP抑制干预条件、细胞死亡测量方法。未指定必须调用哪个工具。四次均选择read_source_details，目标M0006。

这些是模型的单步动作选择；虽然ReactChoice结构校验均通过，不等于已通过所有运行时前置检查或真正执行工具。某次read_artifacts把ID放在parameters内，不能算成功阅读；部分evidence_need出现猜测性的药名/细胞系例子，不能当成原文事实。

## 判断

本组小样本没有显示入口提示提高正文选择率。它说明当前模型在明确方法学信息缺口下会选择正文工具；自由出题时倾向于认为摘要已足够，并直接提取/出题。不能把人工给定缺口的成功称为自主形成研究目标的成功，也不能由2次抽样推断统计稳定性。

入口改动解决了发现与使用工具的呈现问题；下一步更值得检验的是：模型能否自主形成有价值的问题机会并具体识别材料缺口，而不是不断从现有摘要中选最容易回答的内容。

## 测试

35项相关测试通过，覆盖入口不被截断、材料别名、正文已保存时转阅读、失败状态以及原有读取/检索链路。

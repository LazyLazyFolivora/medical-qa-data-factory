# 研究问题发现 skill

## 本次实现

新增 skills/objective-research-question-discovery/SKILL.md，独立于材料检索、最终出题和答案验证。
提供差异、条件、机制、联系、分歧、区分六类可选思考方向。要求从有来源的具体发现出发，识别自然信息需求、已有观察和具体未知点；不强制遍历、不预设答案存在、不强制全文或难题。

发现结果使用主agent现有working_notebook字段承载：current_goal记录研究问题与价值，findings保留来源与范围，open_questions记录缺口，next_step说明动作及信息目标。没有新增模型调用、工具、固定流程节点或响应字段。

llm/objective_skills.py通过ROLE_COMPANION_SKILLS只为objective_react_agent加载该独立skill。主skill和伴随skill合并计算版本摘要，沿用请求幂等键和trace记录；修改伴随skill也会改变版本。正文不会自动附加给出题模型或盲答模型。

## 测试

41项通过（objective_skill_loading、react_main_agent、react_working_notebook）。新测试覆盖：主agent加载、出题/盲答不加载、文件修改改变版本、缺失文件显式失败。

## 真实模型固定材料实验

目录 data/experiments/discovery-skill-fixed-20261112。
复用此前BRCA1第4步完成的快照，已有5篇PubMed摘要；before使用上一轮已加入paper_access但未加入发现skill的请求。after从相同状态构造。程序比较确认payload和response_format相同，只改变skill说明及版本；未人工指定研究缺口。两组均不注入额外历史memory。

修改前后各两次、交错调用当前配置模型，保存全部输入和响应，只观察单步选择，不执行工具。

| 组别 | 第一次 | 第二次 |
|---|---|---|
| 修改前 | design_questions | understand_facts |
| 修改后 | read_artifacts（ID错放parameters中） | design_questions |

修改后两次没有返回working_notebook，没有明确提出新的研究问题与信息缺口，也未选择正文；重读摘要不能算已深入研究，更不能算已经成功执行。修改前某次用临床生存差异解释“为什么预后差”，仍有观察向机制升级风险。

结论：skill已接入，但这组小样本未显示自主问题发现改善。不能把skill加载、长rationale或调用次数当作研究进展。单步测试不能代表完整链路或统计稳定性；未生成或发布任何新题。

后续若继续：重点观察主agent是否在工作笔记中形成具体研究目标、缺口及更新依据，而不是只统计是否调用全文。当前schema允许省略working_notebook，模型本轮确实省略了它；是否加强输出契约需要单独评估，不在本次悄悄改成强制工作流。

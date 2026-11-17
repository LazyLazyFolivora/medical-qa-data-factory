# 沿综述追到原始证据

## 问题与事实核对

前几轮只用 PMID35298921 综述段落，模型将“低 pH”和“进一步酸化恢复功能”解释为矛盾，并倾向删去机制、改问观测结果。这只能说明当前段落解释不足，不能认定机制错误。

原始研究提供了缺失条件：

- [PMID30375977](https://pubmed.ncbi.nlm.nih.gov/30375977/)：讨论早期内体 pH 接近 ApoE4 等电点、溶解性与受体回收，以及改变内体 pH 的实验；分子解释中仍有提出的机制假说，不能全部改写为人类中已确立的事实。
- [PMID34617884](https://pubmed.ncbi.nlm.nih.gov/34617884/)：NHE6 缺失降低斑块的结果并不限于 ApoE4，也研究了含人类 ApoE4 的模型。此前“所引综述没有明确亚型”的判断不能升级为“原始研究未研究 ApoE4”。更准确的核验需求是区分适用模型和是否具有亚型特异性。
- 药理抑制、遗传缺失、细胞受体回收和动物斑块负荷仍是不同条件与终点，不能自动互相替代。

独立人工核对的两篇 Europe PMC XML 和段落保存在 `data/experiments/question-first-lab/nhe6-primary-audit-20261115/`。这些文件没有注入研究 agent，避免把人工搜到的答案当作 agent 自主发现。

## 真实研究 agent 实验

`apoe-20261115-163458`：以旧运行断点为基础，在实验主 prompt 增加明确补证委托，要求调查 NHE6/pH/模型边界，不提供 PMID、论文标题或答案。生产 skills 未替换。此实验不是自主选题能力的证明。

主 agent 调用 research_evidence，研究 agent 自主搜索 Xian/NHE6/ApoE 等关键词，找到 PMID30375977 与 PMID34617884，取得两篇全文并读选定段落，8 个研究工具步骤后返回。报告解释了等电点与受体运输关系，并识别有人类 ApoE4 的动物模型。它的摘要仍有将机制解释说得过强的问题，摘要仅作为笔记，不代替原文。

接着在 `apoe-20261115-163922` 移除人工研究提示，恢复原实验主 skill，完成 8 个外层动作，没有新增发布。发现关键交接错误：主 agent 的 design_instruction 说使用原始论文，但选择了旧 discovery 的 research_id；实际 objective_grounded_answer 输入仍只有 PMID35298921 综述。随后又在旧综述上核验 pH，不能用这轮结果判断新增原始论文是否足够。

`research_id` 是固定材料集合，后续研究不会自动更新它。更新 `react_main_agent.py` 的 design_questions 工具说明，移除已经过时的“新问题可直接 source_sections/revision_of”描述，明确新问题需以新材料重新 discovery；已有候选可通过 revise_candidate 的 source_sections 指定新证据。`research_agent.py` 的研究交付说明同步提醒。没有自动合并来源或放宽 reader 门禁，代码已审查。

从相同研究结束断点回放 `apoe-20261115-164545`：主 agent 本次改选 discover_questions，但选择的是旧综述中另一组星形胶质细胞相关段落，并非新原始论文。因此尚不能宣称新证据交接行为已经稳定纠正；这轮只证明没有再走同一旧研究 ID 回答动作，具体新题仍需检查。

该单步已结束，返回三个问题机会：apoER2/内体机制、细胞类型间脂滴/应激比较、MAMs 与应激的联系。前两个被标 ready_for_design，但同时列出了可能属于题目核心的机制或因果证据缺口；第三个为 needs_evidence。它们尚未形成新发布题，不能因为 reader ACCEPT 就计算为合格问答。当前全部实验进程已终止，无遗留试跑。

## 阅读参数丢失修复

真实研究步骤 3、4 将要读的段落放入 ResearchTurn 顶层 source_sections，而工具只收到 parameters，返回了目录而非正文。步骤 5 也未传入有效段落选择；步骤 6 才使用 parameters.source_sections 读到内容。

`research_agent.py` 现明确顶层字段用于 finish_research 交付；read_artifacts 中若段落选择误放顶层，程序移入参数再执行。两处非空选择不同则拒绝，不猜测。原有材料别名翻译、段落校验和阅读预算保留。改动已经代码审查。

用保存的两个实际错误响应回放解析，均恢复原段落选择。再经过真实别名翻译与 material_passages 读取，第一篇返回 4 段、6579 字符，第二篇返回 2 段、2245 字符，记录在 `recovered-reading.json`。没有把未执行的阅读记成已完成，也没有将第三次未提供选择的调用宣称已修复。

## 对目标的意义

这轮证明系统工具能够取得原始证据，且明确的信息缺口能引导研究 agent 找到有用内容。仍待改善的是主 agent 何时选择补证，以及新证据进入后是否真正重审旧假设。不能仅靠增加评审强度、缩短答案或改变问题来替代这一步。

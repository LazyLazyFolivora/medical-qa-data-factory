# 完整观察与新增事实实验

当前版本已于 2026-11-02 冻结为 [fact-pair-v1](baselines/fact-pair-v1/README.md)。
两份 skill 与本轮原始实验资料已独立保存，文件校验值见该目录 manifest.json。
后续复测以此基线为准，暂不修改提示或开展新实验。

随后已按用户要求新增[完整出题 CLI](question-pipeline-cli.md)，将检索到写作串联。
冻结提示保持原样；CLI 自动读取冻结的事实选择与关系题写作文本。

## 本轮改变了什么

前两轮证据整理把角色拆成 entity、intervention 等字段，却持续填错干预对象。
本轮采用新的 `pilot-fact-pair` skill：先写一条完整观察，再选择一项相关新增事实，
说明信息增量；允许 pair、single、insufficient，不先指定题目难度。

输入仍是人工选定的 PNMA1/BCL2 信息需求，以及保存的完整摘要和 UniProt 注释。
没有新检索、图谱调用、换模型或独立求解。示范与该病例具有相近的关系结构，存在针对案例调优的局限。

## 运行及原始记录

新增入口 `src/medical_qa_data_factory/fact_pair_pilot.py` 只选择事实，不自动写题。

```powershell
$env:PYTHONPATH='src'
$env:PYTHONIOENCODING='utf-8'
D:\Anaconda\envs\qa_env\python.exe -m medical_qa_data_factory.fact_pair_pilot --input data/experiments/pnma1-fixed-writing-01/writing-materials.json --output data/experiments/pnma1-pair-new --trials 3 --config config/models.json --provider qwen-self-hosted
```

实际输出位于 `data/experiments/pnma1-fact-pair-01/`：

- `input.json` 与 `origin.json`：事实选择的输入和来源。
- 各 trial 下 `fact-pair.request.json`、`fact-pair.result.json`：选择过程及原始结果。
- `review.md`：三组事实及模型的组合理由。
- 各 trial 下 `writing-materials.json`、`delivery.request.json`、`delivery.result.json`：后续写作全过程。
- `writing-review.md`：三道未经手工改写的题目与答案。

三次选择完成后，助手逐项阅读事实与原始来源，再调用已有的 `invoke` 和 `Delivery` 写作。
写作 skill 沿用 `pilot-focused-composition`，未修改。写作输入映射为：
assignment.information_need 使用原需求；relation_evidence 使用 observation；
property_evidence 使用 added_fact；property_source_species 从原 UniProt organism 复制。
三个 trial 的模型事实都原样交接，没有修改成标准答案，也没有只保留某次最好结果。

## 观察结果

三次都选出：

1. Bcl2 过表达抑制 PNMA1 诱导的神经元凋亡。
2. BCL2 的一般功能涉及通过控制线粒体膜通透性来调节细胞死亡。

第一、三次的完整观察直接以 Bcl2 过表达为主语；第二次先写 PNMA1 诱导凋亡，
再写该效应被 Bcl2 过表达抑制，角色仍然清楚。第二次所附单句引文没有覆盖其中文摘要的全部限定，
不过完整输入摘要支持 PNMA1 异位表达促进凋亡和神经元背景；后续若做独立证据卡，应补足相应原句。

第二事实不再只是“抗凋亡”这个重复标签。三个 trial 均有相应 UniProt 原文依据。
但三个 information_gain 的解释都倾向于用一般功能解释具体保护效应，第二次尤其明显。
因此写作交接只包括两项事实、对应原文、来源范围和原始信息需求，
不把 information_gain、reader_need 这些选题解释作为医学证据。
这是人工阅读后采用的交接决定，不能称为自动识别并修复了错误解释。

后续三次写作均保留过表达、PNMA1、神经元背景，以及线粒体膜通透性这一新增信息。
没有出现此前的自噬机制拼接。第二、三次明确区分一般功能与原实验具体机制。
第一题的 shortcut 仍把两项事实缩成容易误读为具体机制的一句话；第三题难度说明又使用
“跨物种映射”，而实验物种并未确定。题干通顺不意味着所有训练元数据均可直接采用。

## 一道原始输出

trial-2 题干：

> 举出一种过表达可抑制 PNMA1 诱导的神经元凋亡的蛋白，并说明该蛋白在人类细胞凋亡调控中的一般作用。

答案识别 Bcl2，并描述控制线粒体膜通透性的一般作用，另明确该作用不等于已经确定
PNMA1 实验中的具体机制。完整答案及证据保存在 trial-2/delivery.result.json。

## 可保留的方法与局限

值得继续测试的方法是：

1. 用完整观察句理解关系，避免先把语义拆散。
2. 以“比第一项多知道了什么”选择第二项事实；没有新增内容则接受简单题。
3. 区分用于决定选题的解释与真正支持答案的证据，写作只接收后者及任务范围。
4. 先形成最小充分答案，再形成独立可读的问题。

这只是同一案例三次选择和三次写作的观察，不证明普遍稳定性，也不能归因于单一改动。
题目是否中等、是否存在短解，仍未通过独立求解测量。
当前只建立了值得迁移测试的候选流程，未替换生产代码。

下一轮应冻结当前 skill，换实体和关系做少量迁移测试，并包含第二事实没有信息增量的对照，
观察模型是否真能选择 single，而非总是给出 pair。优先做这一步，不继续只针对 PNMA1 堆叠规则。

脚本真实运行完成，原有三项 pilot 测试通过，新 skill 格式检查通过。这些不等于事实或题目质量验收。

# 图谱探索出题：方法实验与观察

日期：2026-11-02。目标是探索适合未来 query-planner 数据集的出题方法，
不以当前 Planner 能运行作为前提。本轮接入真实 PrimeKG 和公开摘要，
用三份实验 skill 替代生产主循环，观察不同指导带来的出题行为。

## 流程及 skill

1. 输入实体的 UniProt 快照，并读取本地 PrimeKG 邻域。
2. `skills/pilot-entity-exploration/SKILL.md`：发现值得深入的方向，选择最多两项查询。
3. 执行查询，返回真实资料。
4. `skills/pilot-question-opportunities/SKILL.md`：形成信息需求、必要事实和依赖，选择最多两项补查。
5. 执行补查。
6. `skills/pilot-question-composition/SKILL.md`：根据已有材料形成至多三题、答案依据与参考求解结构。
7. 人工查看实际结果，修订指导方法。

工具包括：PrimeKG 邻域、UniProt 基因概览、PubMed 查询与摘要读取。
没有全文读取能力，pubmed_fetch 不会把已读摘要升级为全文。
图谱只作为线索，不自动证明路径的生物学含义；生成问题均是实验草稿。
读取最多 1000 条图谱边，模型看到每种关系最多 24 个示例，明确标注省略数量。
全部返回边保留在材料文件；这是有限探索，不是穷尽候选集。

入口：`src/medical_qa_data_factory/graph_question_pilot.py`。
使用项目现有模型配置。三次逻辑模型调用，底层适配器可能按配置重试。
不会自动发布，也没有新增模型审核环节。

## 运行

项目根目录 PowerShell：

```powershell
$env:PYTHONPATH='src'
$env:PYTHONIOENCODING='utf-8'
D:\Anaconda\envs\qa_env\python.exe -m medical_qa_data_factory.graph_question_pilot --snapshot data/experiments/pnma1-method-03/source.json --graph ../../datasets/PrimeKG/primekg.sqlite --output data/experiments/pnma1-graph-new
```

输出目录须为新目录；`--prepare-only` 只准备种子材料，不调用模型。
可用 `--config`、`--provider` 选择模型配置。

固定材料重做题目设计，不重复检索：

```powershell
D:\Anaconda\envs\qa_env\python.exe -m medical_qa_data_factory.graph_question_pilot --replay-run data/experiments/pnma1-graph-method-03 --output data/experiments/pnma1-graph-replay
```

请求文件保存实际 skill 正文及摘要版本；原始响应、搜索结果、材料及
review.md 保留，便于区分真实变化与模型随机性。本轮没有重复采样统计。

## 本轮结果

实验结果均在 `data/experiments/`：

| 运行 | 方法变化与结果 |
|---|---|
| pnma1-graph-method-01 | 首次请求 HTTP 400，未产生有效结果。后续缩减重复材料。 |
| pnma1-graph-method-02 | 三阶段跑通，但长 PubMed 查询全部为空；重复查种子 UniProt；生成缺资料题和未经支持的正常细胞比较。 |
| pnma1-graph-method-03 | 补充查询方法后找到凋亡和生殖功能摘要；也查到不相关文献。补查实际读取 PMID 20936693 和 DISC1 图谱邻域。最终响应 JSON 未闭合，未生成报告。 |
| pnma1-graph-method-04 | 复用 03 材料，缩减图谱呈现后完成；仍把不足的机制比较写成题，并擅自限定为人神经元。 |
| pnma1-graph-method-05 | 同一材料，增加选题示范；未解决的机制方向留在后续探索，最终三题均为直接查询。仍有来源依赖措辞和时间条件改写问题。 |

建议先看 `pnma1-graph-method-05/review.md`，再对照 04。
两份报告沿用 03 的探索历史，不代表重新执行了检索。

## 对方法的判断

### 已观察到的进展

- 从单条实体注释扩展到了实验现象材料，题目内容不再局限于家族和表达。
- 机会阶段能提出事实需求和连接缺口，图谱能帮助识别扩展对象。
- 具体示范后，最后一轮把缺证据的机制问题留在 remaining_opportunities。
- 最后一轮明确承认单篇摘要可直接回答，三个最终问题均标为直接查询。

### 仍未解决

- 机会普遍选成宽泛机制解释，再因证据不足退回单事实题。需要教模型从
  现有的两三个事实构造合理连接，避免总是把问题升级成尚未解决的研究课题。
- 检索语法仍不稳定：OR 未加括号、附带主题过多，出现不相关文献。
  应继续用具体案例讲清短查询、别名组合、阅读后细化的方法。
- 一次补查重复读取已在上下文的摘要，并声称意图获取全文细节。
  已在机会 skill 补充“摘要工具能力和重复查询”指导，尚未整轮复测该修改。
- 05 题 1/3 仍以 PMID 定义题目。题干应公开真实实验条件，而把来源留在隐藏依据中。
- 05 题 2 把摘要的“by six months”变成“几个月开始”，观察时间不能证明精确起病时间。
- 早期报告含“正常细胞”“人神经元”等未经当前片段支持的限定，不能直接入库。
- 摘要中同名论文可能是同项研究的不同版本，本轮没有做版本整合，不能视为独立支持。

结论：已有可运行、可回放的方法实验；尚未证明稳定生成关系连接或困难题。
不应把发现了图谱路径、读了多篇摘要等同于最终题目需要多步求解。

## 下一轮应测试的具体假设

保持这三项职责，先精化“机会选择”而非增加审核层：

1. 给机会 skill 一个完整的事实连接示范：发现互作对象，补查对象属性，
   明确公开条件，再形成有事实答案的查询。示范也展示条件不足时怎样缩小问题。
2. 同时保留“已有材料足以回答”和“值得补查”的机会，避免全都挑成研究机制问题。
3. 选择材料能支持的连接对象，补查它的具体属性；对于原始实验中直接给出的
   结果接受其为简单题，不人为增加查找步骤。
4. 固定材料比较自然性、事实范围与实际依赖，再换一个实体确认是否可迁移。

本轮两项脚本行为测试通过，三份 skill 格式检查通过。它们不构成题目质量验收。

## 第二次方法迭代：06—08

本轮先在机会 skill 增加一个完整的假想示范：实验中识别干预对象 R，
再补查 R 的相关功能，最后形成需要两项事实的自然问题。写作 skill 同步
增加保持关系目标、物种范围及观察时间的示范。

新增机会说明 `bridge_fact / next_entity / lookup_property`，用于把补查思考
明确为“已有关系—具体对象—所需属性”，不是质量评分或发布门禁。
旧实验缺少这些说明时保持空字符串，以便回放。

| 实验 | 改动 | 实际观察 |
|---|---|---|
| 06 | 增加完整关系题示范，复用 03 的初步探索材料 | 仍提出很宽的比较与机制问题；重复查询 PNMA1，并把 `PNMA1 ABHD17A interaction` 当作图谱实体名。 |
| 07 | 增加三个具体思考位置和工具用法说明 | 没有执行补查，选择已有直接事实；仍出现跨实验和跨实体混合。 |
| 08 | 相同 skill，明确本轮 focus 为 relationship | 实际补查 ACTB、ABHD17A 的 UniProt 注释；写题仍退回直接事实，把 ACTB 属性脱离 PNMA1 单独提问。 |

查看 08 的 `review.md` 可以看到补查与写题之间的脱节。它不是成功的关系题示范。
具体问题还包括：题 1 擅加“人类神经元”，将结构域删除替换为抑制操作；
题 3 把 ACTB 的来源写成 PNMA1 的 accession；任务要求最多一道关系题加一道
简单对照，输出仍为三题。因此这些结果仅用于方法诊断，不应收入数据集。

观察支持的判断：显式题型目标在这一轮改变了查询行为，但没有让最终题目
保留目标。只增加一个关系示范还不够；整包材料交给写作模型时，它会再次选题。
由于单次采样且多个改动并存，不能据此声称有统计上的改善。

下一组值得检验的流程：机会设计阶段选定一个有依据的需求，并只交接该需求、
其实际补查材料及所需事实，让写作模型完成这一个任务。比较它与当前批量写题
是否更能保留关系结构。暂不再堆叠笼统禁令。

### 重做机会与补查

现在可复用已有探索结果，重新执行机会阶段及最多两项补查：

```powershell
D:\Anaconda\envs\qa_env\python.exe -m medical_qa_data_factory.graph_question_pilot --replan-run data/experiments/pnma1-graph-method-03 --graph ../../datasets/PrimeKG/primekg.sqlite --focus relationship --output data/experiments/pnma1-replan-new
```

`--focus open` 为开放出题，`--focus relationship` 用于关系题实验。两者仅改变
任务目标，不指定医学答案。`--replan-run` 与 `--replay-run` 互斥。
重做机会时清除旧机会和旧补查，保留初始探索；新的完整运行记录可以继续回放。
早期 04/05 回放目录未保存探索响应，重放它们时应使用原始 03 目录。

本轮两项脚本测试和两份修改 skill 的格式检查通过，真实模型运行 06—08 均完成。

## 单机会交接实验：focused 01—02

新增 `focused_question_pilot.py` 和两个实验 skill：`pilot-focused-opportunity`、
`pilot-focused-composition`。流程为：复用原始探索摘要 → 模型选择一个机会 →
查一次所选对象的 UniProt → 仅交接该任务、选中的原始摘要、补查结果 → 一道题或缺口。
没有手工指定 BCL2，也没有对输出做自动事实修复。原始模型输出完整保存。

该实验复用 03 的文献探索结果；本轮没有再次调用图谱，也没有测试由图谱边构造题目。
一次人类 UniProt 查询是本实验的能力边界，不能代表完整检索方案。

```powershell
$env:PYTHONPATH='src'
$env:PYTHONIOENCODING='utf-8'
D:\Anaconda\envs\qa_env\python.exe -m medical_qa_data_factory.focused_question_pilot --source-run data/experiments/pnma1-graph-method-03 --output data/experiments/pnma1-focused-new --config config/models.json --provider qwen-self-hosted
```

| 运行 | 选择与补查 | 实际表现 |
|---|---|---|
| pnma1-focused-method-01 | 自动选择 BCL2，补查 P10415 | 最终保留“抑制 PNMA1 诱导的凋亡→该蛋白一般功能”；但擅加 human、遗漏过表达条件、暗示唯一答案、声称两次检索必需。不能直接入库。 |
| pnma1-focused-method-02 | 加具体交接示范后，重新选择 PNMA1，补查 Q8ND90 | 退回种子自身的功能；需求描述先把效应方向写反，后续草稿又换回；最终把抑制加强为阻断，混用人类注释与摘要条件，并把摘要原句标作 UniProt 引句。仍不能入库。 |

这两次运行说明单任务交接能保留一次关系结构，但尚不能稳定选到有价值的连接。
第二轮不是成功修复。示范变化与采样波动并存，不能把差异归因为某一条规则。
两轮中模型均对实验物种有越界推断；第二轮参考结构域的事实原摘要本身已有，
增加 UniProt 查询不意味着题目具有中等难度。

后续应拆开两个实验变量：

1. 固定一个人工审阅过的机会与原始证据，重复测试写作能否保持对象、操作、效应和范围。
   这测试写作能力，不能称为自动选题成功。
2. 另测机会发现：先读出具体观察（对象、操作、效应、条件），再决定哪里有值得补查的未知。
   对比“已经在原摘要中回答的重复属性”和“另一对象的新信息”，用具体样本教方法。

先做到一个真实的信息需求有可靠表达，再扩大题型与实体覆盖。暂不迁移进生产流程。

## 固定机会后的写作对照

后续已完成三组各三次真实调用，固定审阅后的 BCL2 机会，分别测试原写作 skill、
增加具体写作步骤、缩小补查证据片段。详见
[固定机会实验](fixed-opportunity-writing-experiment.md)。

九次均保留关系目标，但前两组仍出现条件遗漏或无依据的机制拼接。
第三组人工整理相关原文后，三次在这些主要观察点上表现更好；尚未证明自动整理或跨实体稳定性。
当前 `pilot-focused-composition` 包含第二组加入的写作步骤；各次实际使用的 skill 保存在请求记录中。

后续新增 `pilot-evidence-preparation` 与独立实验入口，测试自动整理能否替代人工精选证据。
两轮共六条流水线已运行；最终题目通顺，但中间观察仍混淆干预对象，第二问信息增量不足。
详见[自动证据整理实验](evidence-preparation-experiment.md)，当前尚未形成稳定的自动出题流程。

后续 `pilot-fact-pair` 改用完整观察和新增事实，两项事实经阅读后仅将证据交给写作者。
同一 PNMA1 案例的三次选择、三次写作均保留了具体调控信息；选题解释与部分元数据仍有问题。
详见[事实组合实验](fact-pair-experiment.md)，尚待跨实体及无需组合的对照测试。

# Web 实体到可信问题生产实施计划

> 状态：待实施  
> 制定日期：2026-10-28  
> 目标：用户只在现有 Web 页面选择一个 PrimeKG 实体，系统实时搜索、验证并发布合格问题，同时展示每一步真实动作。  
> 本计划不以“代码完成”“测试通过”作为最终成功标准；最终成功标准是 Web 真实运行能够产生通过本文题目门禁的问题。

## 1. 这次到底交付什么

第一轮只交付一条可以被真实验收的纵切：

```text
用户在 Web 选择种子实体
  -> 初始化 ExplorationState
  -> ReAct 根据当前 observation 选择图谱、CTD、PubMed 或实体解析动作
  -> 执行器保存真实 observation 并更新证据、候选和 EvidenceGap
  -> 信息可能足够时，Agent 提出结构化 TaskCandidate
  -> TaskCompiler 冻结候选的公开 QuestionScope
  -> TaskValidator 验证证据、竞争答案和基数
       ├─ 可补救：把 EvidenceGap 返回 ReAct，继续针对性查询
       ├─ 不可补救：拒绝候选，探索其他方向
       └─ 通过：生成 ValidatedTask
  -> Writer 生成自然问题
  -> 独立 Verifier 回译并核对题目语义
  -> 合格题进入现有问题、证据和审核页面
  -> 达到 max_questions、搜索饱和、候选终结或预算耗尽时停止
  -> Web 展示每个真实动作、状态、结果、gap 和拒绝原因
```

用户不需要选择目标疾病、关系、PMID 或 evidence kind。用户只选择种子实体和题目数量等产品参数。

第一轮正式验收只要求以下题型真正发布：

```text
study_grounded_directional_association
```

它询问某个已明确界定的研究中，种子实体与哪个生物医学实体存在有方向的关联，例如风险升高或降低。这只是首轮发布验收范围，不是 ReAct 的固定搜索路线：Agent 仍可探索 therapeutic、marker/mechanism、多跳图谱或其他方向，但尚无完整任务契约和验收用例的候选只能进入研究队列，不能冒充正式发布题。

这一纵切不能只靠一个已知实体通过。第一阶段至少固定：

- 1 个开发可见正例；
- 1 个实现期间不可见答案和题面的 held-out 正例；
- 1 个多答案负例；
- 1 个文献不完整或 provider 失败负例。

两个正例必须来自不同 seed。否则只能证明单例被跑通，不能证明平台具备从实体生产问题的能力。

## 2. 明确不做什么

- 不新建另一个 Web MVP；继续使用现有 `web/app.py`、模板和审核页面。
- 不要求每个实体都产生题目；证据不足时允许 0 题。
- 不让用户预先提供目标答案或目标疾病。
- 不把 CTD 字段、PrimeKG 边或 PMID 本身写成知识问答。
- 不把生成过程轨迹当成 Query Planner 的正确动作监督。
- 不伪造 action gain、best action 或 optimal trajectory。
- 不以 mock、fixture、固定模板或 fake verifier 的运行结果替代真实验收。
- 不以单元测试数量、页面可打开或流水线完成作为产品验收结论。

## 3. 当前代码事实

现有 Web 已具备实体选择、生成表单、进度轮询、问题列表、证据查看和审核页面，应保留。

当前错位是：

1. Web 的 `_run_workbench_generation()` 只允许 `KNOWLEDGE_GRAPH`，并将正式 literature policy 设置为 `FORBIDDEN`；
2. CTD/PubMed enrichment 被隐藏在旧 `build_claims` 节点内，没有逐次动作记录；
3. 严格的 `PrimeKGCtdPubmedTaskService` 只接在 CLI，且要求调用者先指定目标关系；
4. 正式 task release 与 Web 使用的 `LocalArtifactRepository` 尚未形成统一发布路径；
5. 现有 Web 测试明确断言只允许 knowledge graph，因此测试通过证明的是旧行为。

### 3.1 ReAct 运行时的四个核心契约

1. `ExplorationState`：实体、累计 evidence/claim、CandidatePool、EvidenceGap、已尝试动作、provider 状态、预算和已发布问题；
2. `AgentDecision`：`continue_search/propose_candidate/abandon_direction/stop`，以及 next action、expected information、affected gap 和 reason；
3. `TaskCandidate`：候选问题意图、候选答案/变量、关系、限定、study anchor 和支持证据；
4. `ValidationFeedback`：`validated/recoverable_gap/terminal_rejection`，以及返回 ReAct 的 gap 或稳定拒绝码。

LLM 可以选择下一动作、提出候选和建议停止，但 action/attempt 状态、预算、provider 结果、`ValidatedTask` 和发布状态全部由程序生成。

## 4. 题目发布硬门槛

下列规则全部是阻塞条件。任意一项失败，该题只能进入 rejection/audit，不能进入公开问题集。

### Q0. 题目范围和竞争全集必须先形式化

每个候选由 Agent 提出后、进入正式答案与竞争集验证前，必须先冻结版本化的 `QuestionScope`，至少包含：

```text
seed entity/type
answer entity type
relation/evidence kind
PublicStudyAnchor
population/species/model
time/dose/exposure definition
PrimeKG relation/direction/source policy
CTD record selection predicate
PubMed corpus closure rule
candidate enumeration algorithm/version
```

`PublicStudyAnchor` 必须能从题面公开恢复到同一研究，例如“第一作者 + 年份 + 队列/研究设计”。只在隐藏字段保存 PMID、题面写“一项研究”不算公开 scope；无法自然公开唯一 study anchor 时，拒绝 study-bounded 单答案题。

竞争答案全集由 `QuestionScope` 的确定性算法产生，不能由生成器在看到答案后选择最窄集合。`world_answer_ids` 必须能由冻结 scope 独立重算。删除题面中的任一判别限定后，应重新计算答案集合或语义；无作用限定不得作为制造难度的装饰。

### Q1. 问的是生物医学事实，而不是数据库元数据

必须询问疾病、基因、蛋白、通路、表型、药物作用、暴露关联或研究结论。

以下内容直接拒绝：

- “在 CTD 记录中……”；
- “PrimeKG 的 tail 节点……”；
- “哪种疾病具有直接机制研究证据……”；
- “根据当前纳入的证据……”；
- “哪个 PMID……”或“哪篇论文……”，除非产品明确建设文献检索题型。

### Q2. 问题必须独立、自然、明确

问题必须：

- 明确给出种子实体；
- 明确询问的关系和答案类型；
- 公开所有改变答案集合的真实限定条件；
- 不依赖前文、表格、隐藏 scope 或生成界面上下文；
- 不出现“某药物”“化学物 A”“疾病 D”“特定人群”等占位符；
- 不出现内部 ID、PMID、claim ID、evidence ID、source filter 或生成路线；
- 不泄漏答案名称、别名或可逆提示。

### Q3. 题面强度不能超过证据

- association 只能写成“相关”；
- risk decrease 只能写成“风险降低相关”，不能写成“预防”；
- therapeutic research 只能写成“被研究用于/被评估用于”，不能写成“有效治疗”；
- marker/mechanism 不能升级成因果；
- 动物、细胞或体外研究不能改写成人体临床结论；
- population、年龄、species/model、剂量、时间和研究类型只允许来自可定位原文。

对于 risk increase/decrease，还必须结构化保存并验证：

- `effect_measure` 与 reference/comparison group；
- effect estimate；
- uncertainty/置信区间及其是否跨无效值；
- adjusted 或 unadjusted；
- prespecified、subgroup 或 post-hoc analysis；
- 单一暴露或共同/混合暴露；
- 作者原文的结论强度。

“odds 更低”不能无条件改写成一般人群“风险降低”；非显著、探索性、仅 subgroup、共同暴露无法拆分或作者只写 suggestive/possible 时，必须使用更弱且准确的表达，无法形成自然单答案题则拒绝。关系强度由确定性 policy 裁决，不能由 Writer 自报。

### Q4. Gold answer 必须有可回读的直接证据

至少满足：

- 答案是固定 PrimeKG snapshot 中的 canonical entity；
- PrimeKG 关系或路径能够复算；
- PrimeKG 与 CTD 映射状态是唯一、明确的 resolved；
- `EvidenceCorpusManifest` 声明的全部预期 PMID 均已得到终态 assessment；
- 至少一篇 PubMed 记录提供支持结论的原文 quote、字符 span 和内容 hash；
- quote 明确支持关系、方向和题面中出现的限定条件；
- provider failure、无摘要、解析失败或未评估不得解释成“不支持”。

`EvidenceCorpusManifest` 至少保存：CTD snapshot、record locator、chemical/disease/evidence-kind selection predicate、去重后的 expected PMID set、实际取得的 PMID set、每个 PMID 的终态、corpus digest 和 closure status。只有 expected set 与 terminal set 完全相等时，`assessment_complete=true`。

第一次运行必须实时请求 PubMed，并在许可范围内冻结本次 HTTP response 的原文/摘要、HTTP status、获取时间、response hash 和 cache 状态。本次验证只能读取这份冻结 corpus。以后重新联网必须创建新 corpus/version，不得覆盖旧任务或悄悄改变 gold。

### Q5. 单答案问题必须真的只有一个答案

单数问法发布前必须：

- 声明可复现的 `ValidationEnvironment` 和公开 scope；
- 公开 scope 必须与 Q0 的 `QuestionScope` 一致；
- 完整枚举该 scope 中的候选答案，不只检查生成器首先发现的答案；
- 为每个候选保存 `matched/not_matched/ambiguous/unreadable/provider_failed/not_evaluated`；
- `world_answer_ids` 严格只有一个；
- 没有 `ambiguous`、`provider_failed` 或 `not_evaluated` 竞争者；
- 不能靠题面没有公开的 source filter 制造唯一答案。

已知多答案时，第一轮直接拒绝，不自动把单数题改成不完整的集合题。

### Q6. 题面必须通过独立语义回验

Verifier 输入不得包含 gold answer、Writer rationale 或“建议通过”的结论。Verifier 必须从题面恢复：

- seed entity；
- answer type；
- predicate/intent；
- polarity；
- study qualifiers；
- scope；
- cardinality。

恢复结果必须与 `ValidatedTask` 一致。Verifier 不确定、遗漏限定或改变关系强度时拒绝；不能让 Verifier 反向修改 gold task。

### Q7. 问题必须具有训练价值

人工审核必须同时回答“是”：

1. 这是一个人可能合理提出的生物医药问题吗？
2. 答案提供的是生物医药知识，而不是系统内部信息吗？
3. 问题不是只靠实体名称猜答案吗？
4. 题面中的限定条件确实影响答案或结论吗？
5. 对目标 Query Planner 而言，问题对应可执行的信息需求，但没有泄漏固定执行路线吗？

任一答案为“否”，以 `low_question_value` 拒绝。

## 5. 题目正反例

### 5.1 目标正例句式

下面只是目标句式。只有实际证据、完整竞争集和 Verifier 都通过后才能成为 gold：

```text
[第一作者]等于 2016 年发表的一项针对美国工人的病例对照研究显示，
在 65 岁以下人群中，
职业性二氯甲烷暴露与哪种疾病的风险降低相关？
```

预期答案候选：`amyotrophic lateral sclerosis`。

它可以通过的必要原因是：

- 问的是研究发现，不是 CTD/PubMed 字段；
- 种子实体、研究设计、人群、年龄范围、暴露和方向均公开；
- 使用“相关”而非“预防”；
- 问题能够脱离生成界面独立阅读；
- 第一作者、年份和研究设计能够唯一恢复公开 study anchor；
- 对应 PubMed 原文必须逐项支持上述信息；
- 在该公开 scope 中必须只有一个符合条件的疾病。

这里的 `[第一作者]` 不是允许发布的占位符，而是提醒 Step 0 必须先从真实记录确认并实例化公开 study anchor。如果第一作者、年份、研究设计、人群、年龄、统计结论、共同暴露状态或唯一性与真实记录不符，这个候选必须拒绝或重写，不能因为句子自然就通过。

另一类可通过的结构是：

```text
在[明确的人群/模型和实验条件]中，哪种[答案类型]被发现与
[已命名 chemical/disease]之间的[明确机制关系]有关？
```

但 marker/mechanism 题不属于第一条实现纵切，不能用这个句式宣称第一阶段完成。

### 5.2 必须拒绝的题目

| 题目 | 拒绝原因 |
|---|---|
| `在当前证据范围内，阿司匹林使用与哪种癌症的风险降低相关？` | scope 不可复现，可能多答案 |
| `阿司匹林已知作用于哪种蛋白？` | 关系过宽，通常多答案 |
| `哪种神经系统疾病具有关于二氯甲烷的直接机制研究证据？` | 问的是 evidence metadata，不是机制事实 |
| `二氯甲烷能预防哪种疾病？` | 把风险相关升级成预防因果 |
| `二氯甲烷与哪种疾病有关？` | relation、方向、人群和研究范围都不明确 |
| `CTD 中二氯甲烷对应哪种疾病？` | 数据库字段查询，没有独立生物医学价值 |
| `某化学物暴露与疾病 D 有何关系？` | 占位符未实例化 |
| `与 BRCA1 相关的蛋白参与哪种生物过程？` | 多层开放变量，通常多答案 |

## 6. Web 产品验收标准

### W1. 用户路径

- 用户能够搜索并选择 PrimeKG 实体；
- 用户只需选择实体、最多题数、语言和允许的题型/难度；
- 点击开始后，API 立即返回已持久化的 task ID 和 `202 Accepted`；
- 页面持续轮询任务，不因生成请求耗时而一直占用提交请求；
- 页面刷新后仍能恢复任务进度；
- 任务结束后同一页面展示合格问题或明确的 0 题结论。

题数参数命名为 `max_questions`，只表示上限，不是生产配额。达到上限可以停止；不足上限绝不允许补造或放宽门禁。

任务必须先持久化再返回 202。后台 worker 以 task ID/idempotency key claim 任务，同一任务只能有一个 owner；浏览器断开不取消任务。应用重启后，running/orphan task 必须可恢复，或明确标记 `interrupted`，不能只依赖进程内全局 lock。

### W2. 动作级可观测性

每次外部或关键确定性操作必须保存并显示：

```text
action_id
candidate_id（如适用）
action_type
provider
公开的 request summary
status: started/succeeded/empty/retrying/failed/rejected
result count/summary
artifact references
elapsed_ms
retry_count
reason_codes
```

日志区分两层：

- logical action：一次业务动作及其最终 outcome；
- provider attempt：该动作下每次不可变的 HTTP/数据库尝试，包含 request digest、HTTP status/error、elapsed 和 retry ordinal。

`retrying` 不是终态。logical action 只能在全部 attempt 归并后进入 `succeeded/empty/failed/rejected`。恢复时必须 reconcile 遗留的 `started` attempt。UI 默认显示 logical outcome，并允许展开 attempts。

Web 至少逐项展示：

1. PrimeKG 种子解析；
2. PrimeKG 候选关系枚举；
3. 候选实体和关系；
4. PrimeKG↔CTD mapping；
5. CTD 记录和 PMID 数量；
6. 每个 PMID 的 PubMed 请求状态；
7. 每个 PMID 的 assessment；
8. 竞争答案枚举及 outcome；
9. Writer 生成；
10. blind verifier；
11. 发布或拒绝原因。

不得用一个“增强证据并构建主张”节点代替这些动作。

### W3. 真实来源

- 正式验收运行必须使用真实 PrimeKG snapshot、真实 CTD index 和实时 PubMed provider；
- 首次验收运行不得由 mock、预制 HTTP response 或手写 corpus 冒充；
- 缓存允许使用，但 UI 和记录必须明确标记 cache hit、原始 snapshot/hash 和首次获取时间；
- 网络失败必须显示 `provider_unavailable` 或更具体错误，不得显示为“没有文献”。
- 真实请求证明必须来自 provider endpoint kind、不可变 HTTP attempt 和 response hash，不能只相信 UI 文案。

### W4. 页面结果

每道发布题在现有 Web 页面中必须可查看：

- 自然语言问题；
- gold answer；
- validation level；
- PrimeKG 关系/路径；
- CTD lineage；
- PubMed quote/span；
- 全部竞争候选及 outcome；
- Writer/Verifier 结果；
- rejection 不进入公开问题列表，但可在任务审计页面查看。

## 7. 下一步实施顺序

### Step 0：冻结范围契约和真实验收集

**目标**：在改代码前先固定题目 scope、corpus closure 和不会被单例硬编码通过的真实验收集。

**工作**：

1. 定义并版本化 `QuestionScope`、`PublicStudyAnchor` 和 `EvidenceCorpusManifest`；
2. 固定 PrimeKG、CTD 版本和 PubMed 获取时间；
3. 人工核对 `Methylene Chloride -> amyotrophic lateral sclerosis` 的原文、统计限定、共同暴露状态和竞争答案；
4. 如果它不能满足本文门槛，记录失败原因并选择另一个开发可见正例；
5. 由验收者另行保存一个 held-out 正例，答案和题面不进入实现 prompt、规则或 fixture；
6. 固定一个多答案负例和一个文献/provider 不完整负例；
7. acceptance manifest 只保存预期事实断言、必须接触的最小 provider 集合、预期 evidence coverage 和最终发布/拒绝结论，不规定最佳动作、动作顺序或调用次数。

**验证**：人工逐项签署 Q0-Q7；正例不能由项目代码自己宣布。  
**退出标准**：两个不同 seed 的确认正例（其中一个 held-out）和两个不同失败模式的负例。  
**回滚**：无代码变更。

### Step 1：建立统一动作日志

**目标**：每个真实查询和验证动作都能被持久化并由现有 Web 轮询读取。

**主要位置**：

- `generation/contracts.py`
- `generation/repository.py`
- `generation/attempt_ledger.py`
- `web/view_models.py`
- `web/app.py`
- `web/static/app.js`
- `web/templates/generation.html`

**工作**：

1. 建立上述 W2 字段的 action/attempt contract；
2. 复用现有 append-only repository，不新建第二套日志目录；
3. 为 PrimeKG、CTD、PubMed、Writer、Verifier 建立统一事件写入接口；
4. `/api/generation/{task_id}` 返回动作明细；
5. 前端按 candidate 分组展示动作、耗时、返回数量和 reason code；
6. 日志只存公开摘要和证据引用，不存模型隐藏思维链。
7. task claim、幂等键、owner、heartbeat/orphan reconciliation 进入持久化状态，不以全局 `generation_lock` 作为唯一一致性机制。

**验证**：started→terminal、logical action/attempt 聚合、失败、空结果、重试、重复 POST、进程中断、刷新恢复和并发读取测试。  
**退出标准**：用 stub 动作运行时，Web 能逐条实时展示且刷新不丢失。  
**回滚**：保留旧 node trace 只读展示，新 action trace 可 feature flag 关闭。

### Step 2：实现“只给种子实体”的 ReAct 探索循环

**目标**：把目前要求显式目标关系的 formal service，改造成由 observation 驱动、能够自主选择异质动作并提出候选的循环。

**主要位置**：

- `generation/service.py`
- `generation/state.py`
- `generation/actions.py`
- `generation/executor.py`
- `generation/react_graph.py`
- `generation/primekg_ctd_pubmed_task.py`
- `generation/task_candidates.py`
- `generation/ctd_world_evaluation.py`
- `knowledge_graph/`

**工作**：

1. Web 只传 `UpstreamEntitySeed`、`max_questions`、允许工具和预算；
2. 实现 `ExplorationState`、`AgentDecision`、`TaskCandidate` 和 `ValidationFeedback`；
3. 每轮由 repository 投影当前 observation，LLM 不维护未持久化隐藏状态；
4. Agent 根据 observation 选择 PrimeKG 邻居/路径、CTD、PubMed 搜索/读取、实体映射、提出候选、换方向或停止；
5. Executor 只执行 policy gate 接受的动作，动作顺序、检索词和调用次数不由固定 Pipeline 规定；
6. 新 observation 更新 evidence/claim/candidate/gap 后，下一轮决策必须能发生变化；
7. Agent 认为信息可能足够时只提交 `TaskCandidate`，不能创建 gold；
8. `TaskCompiler` 在候选提出后构造并冻结公开 `QuestionScope`；
9. `TaskValidator` 返回 validated、recoverable gap 或 terminal rejection；recoverable gap 回到 ReAct，terminal rejection 关闭候选；
10. 到达 `max_questions`、所有候选终结、搜索饱和、预算耗尽或 provider failure 时使用不同 stop reason；
11. `EntityGenerationService` 是 Web/CLI 唯一任务级 orchestrator；`PrimeKGCtdPubmedTaskService` 降为无任务生命周期和无独立发布副作用的单候选 evaluator；
12. `ValidatedTask` 是唯一 canonical truth；Web view 与 public/hidden release 都按同一 canonical task ID 投影，不允许先导出一份 formal release 再复制成另一份 Web 真值。

**验证**：单候选、多候选、多答案、无候选、分页、截断、候选失败后换方向、recoverable gap 补搜、过早 stop 拒绝和 observation 改变下一动作。  
**退出标准**：调用方只给种子实体即可运行；至少一个真实 episode 出现两轮以上由 observation 改变的异质动作，并形成候选及完整 rejection/gap ledger。  
**回滚**：保留显式 edge 的底层单候选 API，移除旧 Web 直接调用路径。

### Step 3：接入真实异质工具和候选验证反馈

**目标**：ReAct 能真实调用 PrimeKG、CTD、PubMed 等工具；每个候选得到可回读的 evidence assessment 和可反馈给 Agent 的验证结果。

**主要位置**：

- `evidence_enrichment/ctd.py`
- `evidence_enrichment/pubmed.py`
- `evidence_enrichment/pubmed_evidence.py`
- `generation/evidence_fusion.py`
- `generation/primekg_ctd_pubmed.py`
- `generation/primekg_ctd_pubmed_task.py`

**工作**：

1. CTD mapping 必须 resolved；
2. 对 CTD 返回的全部 PMID 请求 PubMed；
3. 区分无摘要、404、空结果、429、timeout 和解析失败；
4. 对每个 PMID 保存 supports/contradicts/context_only/uncertain；
5. 保存 quote/span/hash 并从持久化文本独立回读校验；
6. 保存 qualifier 和关系强度；
7. 任何未知竞争者或未终结 PMID 阻止发布。
8. 生成并冻结 `EvidenceCorpusManifest`；expected/terminal PMID set 不闭合时 fail closed。
9. 对 directional association 提取 effect measure、estimate、uncertainty、comparison、analysis type、co-exposure 和 author strength。
10. 将缺文档、缺 qualifier、未评估竞争者等标成 recoverable gap；将已知多答案、metadata question、强度夸大等标成 terminal rejection。
11. provider failure、预算不足和无证据保持不同状态，不得由 Agent 合并解释。

**验证**：真实 provider smoke 加覆盖全部错误分支的 fixture 测试。  
**退出标准**：Golden 正例的全部 PMID 都有可回读 assessment，负例按预期拒绝。  
**回滚**：provider 失败只产生未完成任务，不允许回退成 KG-only 正例。

### Step 4：题目生成与独立回验

**目标**：只从 `ValidatedTask` 生成自然题面，并执行 Q0-Q7。

**主要位置**：

- `generation/prompt_writing.py`
- `generation/task_compiler.py`
- `generation/task_validation.py`
- `generation/blind_verification.py`
- `generation/semantics.py`
- `generation/task_release.py`

**工作**：

1. Writer 只接收已冻结的公开语义，不接收答案泄漏字段；
2. 每个 task 可生成多个候选表达，但仅保留语义等价者；
3. 独立 blind verifier 不读取答案和 Writer rationale；
4. 确定性检查占位符、内部术语、答案泄漏和关系强度；
5. 一次受控重写后仍不通过则拒绝；
6. 加入人工 question-value disposition，不由模型自报价值。

Blind Verifier 只负责从题面重建 Q6 的结构化语义；答案 alias、占位符、单复数、禁止词和内部术语由确定性校验器负责。Verifier 输入契约、输出 schema、resolver 权限、拒绝阈值和模型/prompt 版本必须记录。若 Writer 与 Verifier 使用同一基础模型，仍必须使用隔离上下文和不同 prompt family，并通过 held-out adversarial set 证明不会因语言流畅而放行错误题。

人工价值审核使用冻结 rubric：第一遍只看题面，按自然性、独立性、真实提问可能性各评 0/1/2；第二遍再看证据，判断正确性、强度、限定和唯一性。审核者看不到生成器的 pass/reject 建议；分歧进入 adjudication，并记录 reviewer/rubric version/reason。

**验证**：本文全部正反例及已有 PCQ-Q/PCQ-W fixtures。  
**退出标准**：Golden 正例生成的最终问题逐项通过 Q0-Q7；任何已列反例均不能发布。  
**回滚**：保留 ValidatedTask 和证据，失败题面可重新生成，不能绕过门禁。

### Step 5：接回现有 Web 和统一发布仓库

**目标**：不再存在“CLI 正式结果”和“Web 旧问题”两套互不相通的产物。

**主要位置**：

- `web/app.py`
- `web/view_models.py`
- `web/templates/generation.html`
- `web/templates/review.html`
- `web/static/app.js`
- `repository.py`
- `generation/task_release.py`

**工作**：

1. `/api/generate` 创建持久化任务并立即返回 202；
2. 后台执行统一 EntityGenerationService，不再调用旧 KG-only Web spec；
3. formal published task 写入现有问题、证据、审核读取模型；
4. 页面证据来源改为 `PrimeKG + CTD + PubMed`；
5. 0 题时展示候选数、拒绝分布和真实 stop reason；
6. 旧 Web KG-only 生成路径标为 legacy，不能产生 formal 状态。
7. canonical task、Web question/evidence projection 和 public/hidden release 通过原子写入或可恢复 journal 保持一致，禁止半发布双轨状态。

**验证**：API、刷新恢复、0 题、有题、证据详情和审核页面 E2E。  
**退出标准**：Golden 正例从 Web 点击开始到问题/证据页面全程完成；不运行额外 CLI。  
**回滚**：可关闭 formal generation 按钮，但不得让旧路径冒充 formal。

### Step 6：真实 Web 验收和发布门禁

**目标**：用产品路径证明系统确实能造出合格问题。

**必须执行**：

1. 开发可见正例和 held-out 正例分别真实运行至少一次；
2. 多答案负例和 provider/文献不完整负例分别真实运行至少一次；
3. 正例逐条展示 PrimeKG、CTD、每个 PubMed、竞争集、Writer、Verifier；
4. 负例发布 0 题并展示预期拒绝原因；
5. 人工盲审最终问题，不向审阅者展示生成器“建议通过”；
6. 保存截图/运行 ID、数据版本、问题、答案、证据、动作记录和验收表；
7. 然后再运行完整自动化回归。
8. 运行 qualifier leave-one-out、第二答案注入、隐藏 source filter、空摘要、重复/乱序 PMID、部分 429/timeout、共同暴露、非显著 subgroup、答案多语言 alias 泄漏、frozen corpus 重放、Web/release ID 一致和中断恢复测试。

**退出标准**：满足第 8 节 Definition of Done。  
**回滚**：验收失败则标记未完成，保留失败运行，不修改标准迁就输出。

## 8. Definition of Done

只有下列项目全部勾选，才能对用户说“第一条真实生产链已经完成”：

- [ ] 用户在现有 Web 中只选择一个实体即可启动任务；
- [ ] Web 不要求用户提供目标答案、疾病、关系或 PMID；
- [ ] ReAct 不使用固定 `PrimeKG -> CTD -> PubMed` 动作序列；
- [ ] 至少一个真实 episode 包含两轮以上由新 observation 改变的异质动作；
- [ ] Agent 只能提交 `TaskCandidate`，不能创建 `ValidatedTask` 或正式发布；
- [ ] recoverable `EvidenceGap` 能返回 ReAct 并触发针对性动作；
- [ ] terminal rejection 关闭候选但不强制结束整个实体探索；
- [ ] stop 决策保存 stop reason、未解决 gap 和可审计依据；
- [ ] 题数是 `max_questions` 上限，不是必须填满的配额；
- [ ] 任务创建立即返回，刷新后可继续查看；
- [ ] 重复提交、浏览器断开和应用重启不会产生重复任务、重复题或半发布状态；
- [ ] 每次 PrimeKG、CTD、PubMed、Writer 和 Verifier 动作均可见且有终态；
- [ ] logical action 与每次 provider attempt 可分别审计；
- [ ] 正式 Web spec 不再设置 `literature_policy=FORBIDDEN`；
- [ ] PubMed 首次验收运行使用真实网络 provider；
- [ ] 每次验证绑定冻结且闭合的 `EvidenceCorpusManifest`；
- [ ] 网络失败、无记录、无摘要和不支持被正确区分；
- [ ] 开发可见正例和 held-out 正例两个不同 seed 均在 Web 产生至少一道题；
- [ ] 这些题逐项通过 Q0-Q7，而不是只通过 schema/test；
- [ ] 多答案负例和 provider/文献不完整负例均发布 0 题并给出正确原因；
- [ ] 单答案题有完整竞争集且 `world_answer_ids` 严格为 1；
- [ ] `QuestionScope` 的公开字段能从题面恢复并独立重算竞争集；
- [ ] 所有支持 quote/span/hash 可从持久化文献独立回读；
- [ ] directional wording 与统计量、不确定性、analysis type 和共同暴露状态一致；
- [ ] 题面没有数据库元数据、隐藏 scope、占位符、答案泄漏或强度夸大；
- [ ] blind verifier 看不到 gold answer 和 Writer rationale；
- [ ] 正式结果出现在现有问题、证据和审核页面；
- [ ] 正式结果能进入 public/hidden 分离的 dataset release；
- [ ] 人工盲审判定答案正确、题面自然、问题有价值；
- [ ] 完整自动化测试通过，真实集成测试没有被 deselect 后冒充通过；
- [ ] 生成轨迹没有被导出成 Query Planner 的 correct action 标签。

## 9. 第一轮验收报告必须长什么样

报告必须直接给出：

```text
Seed:
Run ID:
PrimeKG snapshot:
CTD snapshot:
PubMed retrieval time/snapshot:
QuestionScope ID/digest:
PublicStudyAnchor:
EvidenceCorpusManifest ID/digest/closure:
Candidates discovered:
Candidates rejected by reason:
Questions published:

Final question:
Gold answer:
Public scope/qualifiers:
World answer IDs:
Supporting PMID(s):
Supporting quote/span/hash:
Contradicting/uncertain PMID(s):
Effect measure/estimate/uncertainty/comparison:
Analysis type/co-exposure/author strength:
Blind verifier reconstruction:
Human Q0-Q7 decision:
Web evidence URL/path:
```

如果任一正例的 `Questions published: 0`，报告不能写“功能完成”。只有开发可见正例和 held-out 正例都成功发布合格问题，第一轮才能通过。

## 10. 依赖顺序

```text
Step 0 Golden Case
  -> Step 1 Action Journal
  -> Step 2 Observation-dependent ReAct Loop
  -> Step 3 Real Heterogeneous Tools + Validation Feedback
  -> Step 4 Writer + Blind Verification
  -> Step 5 Existing Web Integration
  -> Step 6 Real Acceptance
```

Step 1 的前端展示可以和 Step 2 的候选发现局部并行，但数据契约必须先冻结。Step 3、4、5 不得跳过依赖提前宣布完成。

## 11. 反模式

出现任一项即视为没有完成：

- 新建第三套 generation service，却不迁移现有 Web 和正式 task service；
- 把固定 `PrimeKG -> CTD -> PubMed -> finish` 顺序包装成 ReAct；
- Agent 每轮不读取新 observation，或 observation 改变后仍机械执行同一动作；
- 预先写死查询词、调用次数、候选答案或停止轮次；
- Web、CLI release 和审核页各自保存一份互不一致的题目真值；
- 把 `build_claims` 一个节点包装成“展示了 PubMed 动作”；
- 只记录模型描述“我查询了 PubMed”，没有真实 provider attempt；
- 用 mock/fixture 生成一题后声称具备真实生产能力；
- 用户仍需先指定答案或目标疾病；
- LLM 说“信息够了”后直接发布，不经过独立 Compiler/Validator；
- Validator 发现可补救 gap 后直接拒绝，不把 gap 返回探索循环；
- LLM 说“没有必要继续”但没有结构化 stop reason、未解决 gap 和边际价值依据；
- 一发现支持答案就停止，没有检查竞争答案；
- 在看到候选答案后缩小 `QuestionScope` 或使用题面未公开的 study/source filter；
- 用隐藏 PMID 绑定研究，但题面只写无法唯一定位的“一项研究”；
- expected PMID set 与 terminal set 未闭合仍宣布 corpus 完整；
- 实时 PubMed 响应没有冻结，重跑时悄悄覆盖原任务证据；
- 将非显著 subgroup、混合暴露或 suggestive 结论写成一般风险降低；
- 把 provider failure 当作无证据或负例；
- Writer 直接从原始边自由发挥，不经过 ValidatedTask；
- Verifier 看到 gold 后复述 gold 语义；
- 题面自然但证据/唯一性不合格仍发布；
- 证据合格但题目是数据库字段查询仍发布；
- 只报告 pytest 通过数，不展示真实最终问题；
- 只让 Methylene Chloride 单例通过，没有 held-out 正例；
- 为了让现有输出通过而降低验收标准。

## 12. 计划变更协议

如果真实证据证明 Golden Case 不成立，允许替换正例，但必须追加：

```text
### Plan Mutation YYYY-MM-DD / <id>
- Evidence:
- Why the original case cannot pass Q0-Q7:
- Replacement case:
- Preserved acceptance rules:
- New risks:
- User approval required: yes/no
```

不得通过计划变更取消 Q0-Q7、真实 provider、竞争集验证、动作可观测性或现有 Web 用户入口。

## 13. 执行与验证方式

当前仓库有本地 Git，但没有可用的 GitHub CLI/远端工作流，因此本计划采用 direct mode：每个 Step 完成后先展示变更、运行该 Step 的定向测试，再运行累计回归；未经用户要求不自动创建 PR。

基础验证命令：

```powershell
& 'D:\Anaconda\envs\qa_env\python.exe' -m pytest tests/test_web.py -q
& 'D:\Anaconda\envs\qa_env\python.exe' -m pytest tests/test_primekg_ctd_pubmed_task_service.py -q
& 'D:\Anaconda\envs\qa_env\python.exe' -m pytest -q -m "not primekg_integration"
& 'D:\Anaconda\envs\qa_env\python.exe' -m pytest -q -m primekg_integration
```

自动化测试之后必须执行 Step 6 的真实 Web 验收。任何 real-provider 测试被 skip/deselect、任何正例发布 0 题，或任何 Q0-Q7 未签署，都不能写“验收通过”。

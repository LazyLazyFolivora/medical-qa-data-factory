# CTD 正式接入开发计划

> 状态：待实施
>
> 更新日期：2026-10-27
>
> 适用项目：`medical-qa-data-factory`
>
> 第一阶段范围：`chemical/drug -> disease` 的公开研究上下文限定单实体问题

## 1. 文档定位

本文给出 CTD（Comparative Toxicogenomics Database）进入正式
`TaskCandidate -> ValidatedTask -> release` 主链的实施计划。

它是以下文档在 CTD 纵切上的可执行细化：

- [项目开发架构](development-architecture.md)；
- [项目验收标准](acceptance-criteria.md)；
- [生成问题的题型与语言规范](generated-question-types-and-language-criteria.md)；
- [自然问答与证据增强计划](evidence-enrichment-natural-qa-plan.md)。

发生冲突时，前三份当前实施依据优先。本文取代
`evidence-enrichment-natural-qa-plan.md` 中关于 CTD 正式接入的实施顺序，但不取代其中的
背景说明。

配套验收标准见：

- [CTD 正式接入验收标准](ctd-formal-integration-acceptance-criteria.md)。

## 2. 开发目标

把现有但彼此分离的两套能力合并成一条正式生产纵切：

```text
现有 CTD / PubMed enrichment
  +
现有 task-first 完整世界验证和发布
  ↓
CTD-backed ValidatedTask
```

第一阶段完成后，系统应能从一个已解析的 chemical/drug 实体出发：

1. 在固定 PrimeKG 与 CTD 快照中发现 chemical-disease 候选；
2. 定位 CTD 原始记录、直接证据类型、推断信息和 PMID；
3. 回读固定版本的 PubMed 文本，形成可定位、可复核的规范 claim；
4. 在声明的验证范围中枚举所有可能竞争的 disease 答案；
5. 只把答案严格唯一、语义没有夸大、证据与版本完整的任务提升为
   `ValidatedTask`；
6. 分离发布 Planner 可见问题和隐藏答案、claim、evidence 与验证报告。

目标题型命名为：

```text
study_context_qualified_disease
```

第一阶段只启用：

```text
seed: chemical/drug
answer: disease
answer_type: entity
cardinality: exactly_one
```

## 3. 非目标

本阶段不做：

- 不接入 CTD 全部数据表；第一阶段只使用 chemical-disease associations；
- 不强制所有图谱题经过 CTD；蛋白、通路等不适用题型保持原流程；
- 不把 CTD 记录本身当作 gold answer；
- 不把 `therapeutic` 直接解释为临床有效；
- 不把 inference score 或 inference gene 单独作为自然医学结论；
- 不把 association 写成 causality、prevention 或 efficacy；
- 不生成诊断、治疗或用药建议；
- 不把“具有某类证据”“被当前证据收录”作为疾病答案的主要判别条件；
- 不用隐藏的 evidence scope 代替题面中公开、可理解的生物医学限定；
- 不在本阶段生成 boolean、自由文本或开放集合答案；
- 不实现 Query Planner 的动作策略、rollout 或 cost/gain 标签；
- 不因接入 CTD 而把核心任务契约硬编码成 CTD 专用结构。

`disease -> chemical/drug` 反向题、CTD inference gene 多跳题和其他 CTD 表属于后续扩展。

## 4. 当前基线

### 4.1 已有能力

当前仓库已经存在：

- `evidence_enrichment/ctd.py`：CTD CSV 解析、SQLite sidecar 和精确 pair 查询；
- `evidence_enrichment/pubmed.py`：PubMed 搜索与摘要获取；
- `evidence_enrichment/service.py`：PrimeKG CTD 边、CTD 记录、PubMed 文本和
  `NormalizedEvidenceClaim` 的旧链路；
- `generation/task_contracts.py`：`TaskCandidate`、`TaskSpec`、
  `ValidatedTask`、`EvaluationTarget`；
- `generation/world_evaluation.py` 与 `task_validation.py`：完整 PrimeKG 快照复算；
- `generation/task_release.py`：`public/` 与 `hidden/` 物理分离发布；
- `generate validated`：当前正式任务入口。

### 4.2 当前断点

现有 `generate validated` 不消费 CTD enrichment 的 claim，旧 enrichment 也不经过新的
任务认证和正式发布门禁。因此：

- CTD claim 没有绑定到 `InformationRequirement`；
- `TaskSpec` 没有正式 gold claim/evidence 依赖；
- 当前 CTD 查询只能核对 pair，不能枚举并证明完整候选集合；
- PubMed 文本尚未形成稳定的支持 span 契约；
- CTD/PubMed 冲突与未评估竞争答案不能进入统一 `ValidationReport`；
- 当前正式发布的问题不属于 CTD-backed task。

## 5. 产品输出

### 5.1 正例的语义

正例必须询问真实的生物医学关系。CTD/PubMed 是后台发现、核验和追溯手段，不是题面所问的对象。

所有用于区分答案的条件必须以自然语言公开给答题者，例如：

- 研究人群或遗传背景；
- 职业、地区或其他暴露场景；
- 暴露方式、剂量和持续时间；
- study type、样本类型或随访条件；
- 疾病类别或器官系统；
- association、candidate biomarker、therapeutic research、safety 或风险方向；
- 会改变结论的时间范围和其他 qualifier。

固定 CTD release、PubMed corpus 和 evidence bundle 仍记录在隐藏验证环境中，但不能成为题面唯一的消歧条件。

### 5.2 第一阶段允许的正例结构

以下示例只定义语言和任务结构，不声明其中占位实体对应的医学事实已经成立。
正式数据必须把 A、S、D 等占位符替换为已解析的具体实体和有证据支持的具体条件。

**流行病学风险关系**：

```text
在携带遗传特征 S、并接受至少两年剂量 D 药物 A 的成年人长期随访中，哪种消化系统癌症的发生风险被报告降低？
```

**职业或环境暴露关系**：

```text
在长期接触化学物 A 的职业人群中，哪种血液系统恶性疾病的发生风险被报告升高？
```

**候选生物标志物关系**：

```text
在[明确患者人群 P]的血清研究中，代谢物 A 的水平变化被研究作为哪种神经退行性疾病的候选生物标志物？
```

**治疗研究对象**：

```text
在具有分子特征 S 的受试者中，药物 A 被临床研究用于改善哪种炎症性疾病？
```

**安全或不良风险关系**：

```text
在接受疗程 D 的人群中，长期使用药物 A 被报告与哪种肝胆系统疾病的风险升高相关？
```

发布必须同时满足：

- 题面询问的是疾病风险、候选标志物、治疗研究对象或安全关联等领域事实；
- CTD 与 PubMed 支持题面中的关系语义和公开 study context；
- 疾病类别、研究人群、暴露、时间、剂量等公开限定均有证据来源；
- 仅根据题面公开条件定义 query scope 后，答案集合严格为一个；
- 去掉关键公开限定后答案集合扩大，证明限定不是装饰；
- 所有可能竞争的候选都得到确定结果。

### 5.3 不应发布的问题

```text
在当前纳入的研究证据中，哪种癌症与化学物 A 暴露后的风险降低相关？
```

原因：“当前纳入”是隐藏数据集范围，答题者无法据此确定候选世界。

```text
在当前证据范围内，哪种神经系统疾病具有关于化学物 A 的直接机制研究证据？
```

原因：询问的是内部 evidence label，而不是具有独立意义的生物医学事实。

```text
化学物 A 与哪种疾病有关？
```

原因：通常存在多个合法答案，且问题范围开放。

```text
化学物 A 是否能预防疾病 B？
```

原因：答案形式不符，且可能把关联研究升级成预防结论。

```text
CTD 显示化学物 A 能治疗什么疾病？
```

原因：数据库术语题价值低，`therapeutic` 也不等于临床疗效。

### 5.4 机制题的后续边界

真正的机制题应该询问实际机制实体：

```text
化学物 A 与疾病 B 的关联被报道可能涉及哪个基因？
```

```text
化学物 A 暴露影响的哪条信号通路可能参与疾病 B 的发生过程？
```

这类任务需要验证 `chemical -> gene/pathway -> disease`，答案类型也从 disease 改为 gene/pathway。它属于后续 CTD + PrimeKG + 文献多跳题型，不属于第一阶段。

## 6. 核心架构

```text
Resolved chemical/drug entity
        |
        v
CTD Study-context Candidate Source
  - enumerate chemical-disease records
  - preserve snapshot and row identity
        |
        v
Evidence Enrichment Router
  - CTD raw records
  - PubMed read-back
        |
        v
Source-neutral EvidenceRecord + NormalizedClaim
        |
        v
Evidence Fusion / Conflict Gate
        |
        v
Evidence-qualified TaskCandidate
        |
        v
CTD Validation World Evaluator
  - enumerate full declared candidate universe
  - evaluate every possible competitor
  - compute cardinality and completeness
        |
        v
TaskValidator
  - evidence / semantics / scope / value
        |
        v
ValidatedTask -> Writer -> semantic verifier
        |
        v
public/ planner input + hidden/ evaluation target
```

CTD 是 `EvidenceProvider` 和 `CandidateSource` 的一种实现，而不是核心任务契约的一部分。

## 7. 验证世界与唯一性

### 7.1 ValidationEnvironment

CTD-backed task 的验证环境至少固定：

- PrimeKG provider、snapshot、schema 和 manifest hash；
- CTD release、源文件 hash、sidecar schema 和 index manifest hash；
- PubMed provider、检索/回读日期、文档 content hash 与本次 corpus manifest；
- normalizer、fusion、validator、Writer 和 prompt/model 版本；
- public task scope，例如 disease category、population、exposure、study type 和 temporal scope；
- hidden evidence scope，例如 CTD release、PubMed corpus manifest 和检索日期。

### 7.2 操作性完整世界

“唯一”仅表示在上述固定环境和题面公开条件定义的 public task scope 中唯一，不表示现实世界绝对唯一。hidden evidence scope 负责复现和审计，不能凭空缩小公开问题的候选语义。

对一个拟发布的 chemical -> disease 任务，Validator 必须：

1. 从题面公开的 population、exposure、disease category、intent、polarity 和其他 qualifier 构造 public task scope；
2. 枚举该 chemical 在 CTD 快照和 public task scope 内的全部 disease 记录；
3. canonicalize disease identity 并合并别名；
4. 对所有可能满足题面语义的竞争项执行同等证据判定；
5. 只有全部竞争项均得到 `matched` 或 `not_matched` 的确定结果时，才令
   `result_complete=true`；
6. 如果任一竞争项为 `unreadable`、`unmapped`、`ambiguous`、`provider_failed` 或
   `not_evaluated`，则唯一性未知，候选不得发布；
7. 最终 `matched` disease canonical ID 必须严格等于一个。

禁止只增强 Agent 首先选中的一对 chemical-disease，然后把其余记录视为不存在。

## 8. 证据和语义规则

### 8.1 CTD 的职责

CTD 用于：

- 定位原始 chemical-disease association；
- 提供 `DirectEvidence`、`InferenceGeneSymbol`、`InferenceScore` 和 PMID；
- 提供结构化候选和检索线索；
- 固定记录身份与数据快照。

CTD 的 `DirectEvidence` 和 source metadata 不能直接成为题目的主要 predicate；它们只负责路由、证据强度判断和 provenance。

CTD 单独不能证明：

- 风险增加或风险降低；
- 临床疗效；
- 因果关系；
- 特定人群、剂量或时间条件；
- 现实世界的完整候选集合。

### 8.2 PubMed 的职责

PubMed read-back 用于核验：

- relation intent；
- polarity；
- study type；
- population；
- dose/exposure；
- temporal qualifier；
- 支持、反对或不确定结论。

模型可以从已提供文本提取结构化判断，但不能创建来源、PMID、quote 或实体。

### 8.3 第一阶段允许的 intent

| Intent | 发布条件 |
|---|---|
| `association_research` | 原文明确支持研究关联，不包含因果升级 |
| `biomarker_research` | 原文明确支持候选 biomarker 语义，不宣称可用于临床诊断 |
| `therapeutic_research` | 只能表述“治疗研究/被研究用于”，不能表述有效 |
| `risk_increase` | PubMed 支持 span 明确给出方向和对象 |
| `risk_decrease` | PubMed 支持 span 明确给出方向和对象 |
| `safety` | 原文明确支持药物安全或不良疾病风险语义 |

`mechanism_research` 不作为第一阶段 disease-answer 题型；它应在后续转化为 gene/pathway 等机制实体答案。`insufficient`、`conflicting`、`mixed`、`unknown polarity` 与 inference-only 候选不得以更强语义发布。

## 9. 契约改造

### 9.1 Source-neutral `EvidenceRecord`

正式链路使用统一结构，而不是直接把 `CtdEvidenceRecord` 放进 Task：

```yaml
evidence_id: "..."
provider_id: "ctd" | "pubmed" | "primekg"
source_type: database_record | literature_passage | kg_edge
source_locator: {}
snapshot_id: "..."
content_hash: "..."
retrieved_at: "..."
span: null | {start: 0, end: 120}
metadata: {}
```

CTD 专属字段保存在 source-specific record 或 metadata 中。

### 9.2 Source-neutral `NormalizedClaim`

将旧 `NormalizedEvidenceClaim` 迁移或适配为：

```yaml
claim_id: "..."
subject_id: "chemical-id"
predicate_family: "risk_decrease"
object_id: "disease-id"
qualifiers:
  epistemic_status: associated
  population: null
  temporal_scope: null
  dose_or_exposure: null
supporting_evidence_ids: []
contradicting_evidence_ids: []
normalizer_version: "..."
```

claim 必须能由通用 requirement matcher 消费，不能要求 matcher 理解 CTD 私有字段。

### 9.3 Task 契约

需要补齐：

- `TaskCandidate.candidate_claim_ids`；
- `TaskCandidate.public_qualifiers` 或等价的结构化 study context；
- `TaskSpec.candidate_claim_ids`；
- `TaskSpec.public_task_scope`，只能由题面可表达的限定构成；
- 显式、确定性的 `CompletionRule`；
- `ValidationReport.matched_claim_ids`；
- `ValidationReport.supporting_evidence_ids`；
- `ValidationReport.competitor_outcomes` 或等价的隐藏验证引用；
- `ValidationReport.evidence_dependency_digest`；
- `ValidatedTask.gold_claim_ids` 和 `gold_evidence_ids`；
- `EvaluationTarget.completion_rule`、gold claim/evidence 引用和 evidence catalog ref。

这些字段只进入 hidden view；Planner 可见 view 不得泄漏。

## 10. 分阶段实施

每个步骤应形成独立 Git commit。当前没有远程仓库，先采用本地 `main` 上的小步提交；如果后续配置远程，再将每一步映射为独立 PR。

### Step 0：冻结纵切契约与 fixtures

**目的**：先把第一阶段问题类型、验证世界和错误语义固定下来。

**主要修改**：

- 新增 CTD 正例、冲突、多解、推断-only、缺 PMID、映射歧义 fixtures；
- 为 `study_context_qualified_disease` 建立 task fixture；
- 为隐藏 evidence scope 造成伪唯一的候选建立必拒 fixture；
- 将 `biomarker_research` 作为版本化 `QuestionIntent` 变更加入契约，并为旧 schema 提供明确迁移或兼容策略；
- 固定 reason code 与 competitor outcome 枚举；
- 明确 schema migration 策略，不直接破坏旧 run reload。

**建议文件**：

- `tests/fixtures/ctd/`；
- `tests/test_ctd_task_contracts.py`；
- `generation/task_contracts.py`；
- `evidence_enrichment/models.py`。

**退出条件**：失败 fixtures 先以预期失败测试表达；契约可以序列化、reload 并拒绝非法组合。

**回滚**：删除新增 schema version 和 fixtures，不迁移旧数据。

### Step 1：CTD index 与映射审计

**目的**：把 pair lookup 扩展为可证明候选范围完整的只读查询能力。

**主要修改**：

- 增加按 `chemical_id` 完整枚举 disease records；
- 增加 canonical disease ID/name crosswalk 与映射状态；
- 状态至少包含 `mapped_exact`、`mapped_crosswalk`、`ambiguous`、`unmapped`；
- 增加 index manifest、row count、source hash 和 schema 校验；
- 增加 audit CLI，报告所有 PrimeKG CTD 边的明确映射状态；
- 所有查询确定性排序、无静默截断。

**建议文件**：

- `evidence_enrichment/ctd.py`；
- `knowledge_graph/providers/primekg.py`；
- `cli.py`；
- `tests/test_ctd_index.py`。

**退出条件**：给定 chemical ID 能完整、稳定、去重地枚举声明范围内的全部 CTD disease 记录；每条审计输入都有明确状态。

**回滚**：保留旧 index reader；新 schema 使用版本化 sidecar，不原地破坏现有 index。

### Step 2：统一 EvidenceRecord 与 NormalizedClaim

**目的**：让 CTD、PubMed 和 PrimeKG observation 进入同一个 source-neutral matcher。

**主要修改**：

- 定义正式 `EvidenceRecord` 与 `NormalizedClaim`；
- 为现有 `CtdEvidenceRecord`、`PubMedEvidenceRecord` 和 `KGEvidence` 建 adapter；
- stable ID 必须包含 provider、snapshot、locator 和 content hash；
- claim stable ID 必须包含语义字段和 qualifier；
- 支持 supporting 与 contradicting evidence；
- 为旧 enrichment schema 提供显式兼容读取或迁移层。

**建议文件**：

- 新增 `contracts/evidence.py` 或现有结构中的等价模块；
- `evidence_enrichment/models.py`；
- `evidence_enrichment/service.py`；
- `generation/task_contracts.py`；
- `tests/test_evidence_contracts.py`。

**退出条件**：相同 observation 重放得到相同 ID；异质来源的等价 claim 能由同一 matcher 判定。

**回滚**：adapter 双写新旧结构，待正式链路通过后再删除旧结构。

### Step 3：PubMed 固定回读与支持 span

**目的**：把“获取过摘要”提升为“能独立复核支持句”。

**主要修改**：

- PMID 批量 fetch、重试、速率限制和错误分类；
- 保存 title、abstract、content hash、retrieval time 和 snapshot/corpus manifest；
- 抽取支持 span 的字符偏移；
- 对 span 边界和 quote hash 做 read-back；
- 模型引用未知 PMID、伪造 quote 或越界 span 时 fail closed；
- provider failure 与 empty result 分开。

**建议文件**：

- `evidence_enrichment/pubmed.py`；
- `evidence_enrichment/service.py`；
- `tests/test_pubmed_evidence.py`。

**退出条件**：每个 literature supporting evidence 都可由 PMID + snapshot + span + hash 独立重建。

**回滚**：无法产生 span 的旧摘要只能用于候选发现，不能用于正式 validation。

### Step 4：证据融合与生物医学语义门禁

**目的**：得到可进入 Task 的规范 claim，而不是模型自由结论。

**主要修改**：

- CTD DirectEvidence 与 PubMed assessment 分层保存；
- 处理 support、contradict、context-only、uncertain；
- 合并重复文献和重复 CTD 记录；
- 保留 population、study type、dose/exposure、temporal scope；
- intent/polarity 只能从 allow-list 产生；
- inference-only、conflicting、mixed 或 unsupported 强度输出拒绝原因；
- 模型只负责受约束抽取，确定性代码负责引用与一致性验证。

**建议文件**：

- `generation/evidence_fusion.py`；
- `evidence_enrichment/service.py`；
- `generation/semantics.py`；
- `tests/test_ctd_evidence_fusion.py`。

**退出条件**：同一固定 evidence bundle 重放得到相同可发布状态；冲突或夸大语义不能形成 eligible claim。

**回滚**：关闭 CTD formal candidate source，不影响现有 PrimeKG constrained task。

### Step 5：Study-context Candidate Source 与完整竞争集验证

**目的**：生成有价值的任务候选，并证明答案在声明范围内唯一。

**主要修改**：

- 新增 `CtdStudyContextCandidateSource`；
- 先枚举 candidate universe，再构造问题，而不是先选答案；
- 只使用可公开表达且有证据支持的 population、exposure、study type、disease category、polarity 和其他 qualifier 构造候选；
- 禁止使用“是否有 CTD/文献证据”作为主要 answer discriminator；
- 为每个竞争 disease 记录 outcome；
- 支持可验证的 disease category、intent、polarity 和 qualifier constraints；
- 任一可能竞争项状态未知时 `result_complete=false`；
- 执行 cardinality、constraint-value 和 shortcut gate；
- 候选答案只能由完整评估结果产生，不能直接复制单条 CTD record。

**建议文件**：

- `generation/task_candidates.py`；
- 新增 `generation/ctd_world_evaluation.py`；
- `generation/task_validation.py`；
- `generation/task_compiler.py`；
- `tests/test_ctd_candidate_source.py`；
- `tests/test_ctd_world_evaluation.py`。

**退出条件**：正例严格单答案；多解、未评估竞争项、无解和无价值限定均确定性拒绝。

**回滚**：用 candidate source registry feature flag 禁用 CTD source。

### Step 6：接入正式 TaskService、CLI 与发布

**目的**：CTD-backed task 进入唯一正式生产入口。

**主要修改**：

- `TaskGenerationService` 装配 CTD provider、PubMed provider、normalizer 和 validator；
- `generate validated` 支持 CTD candidate profile；
- 保存 episode 摘要、搜索完成情况、EvidenceGap 和 stop reason；
- hidden evidence catalog 写入 CTD row、PubMed span、claim 和 competitor report；
- public manifest 不引用 hidden 文件；
- immutable release 绑定所有 provider/schema/model/prompt/version hash。

**建议文件**：

- `generation/task_service.py`；
- `generation/task_release.py`；
- `cli.py`；
- `tests/test_ctd_task_service.py`；
- `tests/test_ctd_task_release.py`。

**退出条件**：一条命令能生成 CTD-backed 正式 release；旧 `generate entity` 不再是该题型的正式入口。

**回滚**：保留现有 `generate validated` PrimeKG profile；CTD profile 可独立禁用。

### Step 7：Writer 与题面语义回环

**目的**：只把已验证 claim 写成自然、准确、不泄漏答案的问题。

**主要修改**：

- Writer 只读取 `ValidatedTask`；
- 为六类允许 intent 建受控骨架；
- 题面必须公开所有承担消歧作用的 study context；
- 禁止使用“当前纳入的研究证据中”“当前证据范围内”等未定义范围兜底；
- 禁止用“具有直接机制证据”“CTD 中记录”等 evidence metadata 作为主要问题语义；
- population、dose、time 和 study type 等关键 qualifier 不得丢失；
- 独立 semantic verifier 回译实体、predicate、polarity、qualifier、scope 和 cardinality；
- 增加答案/别名泄漏、内部 ID 泄漏、CTD 数据库术语题、近重复和模板占比门禁；
- 正式题面禁止残留 A、B、S、D、`某疾病`、`特定人群` 等未实例化占位表达；
- LLM 只能润色，不能修改 Task。

**建议文件**：

- `generation/task_realization.py`；
- `generation/writing.py`；
- `generation/verification.py`；
- `tests/test_ctd_question_realization.py`。

**退出条件**：题面与 Task round-trip 等价；改写造成语义漂移时拒绝而不是修正 gold。

**回滚**：退回确定性模板，保持任务和验证结果不变。

### Step 8：真实 pilot、质量报告与文档收口

**目的**：证明系统不仅能跑，而且能稳定产出有价值的真实问题。

**主要修改**：

- 运行至少 20 个 chemical/drug seeds；
- 覆盖正例、多解、无解、冲突、推断-only、映射歧义和 provider failure；
- 输出候选漏斗、拒绝原因、题型/intent 分布、重复率和模板占比；
- 完成人工盲审；
- 更新 running guide、current gaps 和 schema export；
- 保存可复现命令与 snapshot manifest。

**退出条件**：通过配套验收标准全部 P0 项。

**回滚**：pilot release 不覆盖旧 release；不通过则保留 hidden 审计产物并禁止分发 public 数据。

## 11. 依赖顺序

```text
Step 0
  |
  +--> Step 1 ----+
  |               |
  +--> Step 2 --> Step 3 --> Step 4 --> Step 5 --> Step 6 --> Step 7 --> Step 8
```

Step 1 和 Step 2 可在契约冻结后并行设计，但 Step 5 必须依赖 Step 1-4。Step 6-8 串行，以避免把未认证 claim 提前发布。

## 12. 建议提交序列

| Step | 建议 commit message |
|---|---|
| 0 | `test: define CTD formal task fixtures` |
| 1 | `feat: add complete CTD candidate enumeration` |
| 2 | `feat: unify evidence and claim contracts` |
| 3 | `feat: make PubMed evidence independently replayable` |
| 4 | `feat: add CTD evidence fusion and semantic gates` |
| 5 | `feat: validate CTD evidence-qualified tasks` |
| 6 | `feat: publish CTD-backed validated tasks` |
| 7 | `feat: realize and verify CTD-backed questions` |
| 8 | `docs: record CTD pilot acceptance results` |

每个提交前至少运行该步骤定向测试；Step 5 以后运行完整默认回归；Step 8 运行真实集成测试。

## 13. 反模式

以下实现即使“能出题”也不接受：

1. 只 lookup 选中的 chemical-disease pair，不枚举竞争答案；
2. 用 CTD `therapeutic` 直接生成“有效治疗”；
3. 用 inference score 直接生成机制结论；
4. 把完整 abstract 当作支持 span，且不能定位具体结论；
5. 模型返回 `supported` 就通过，不验证 PMID、span 和实体；
6. 把 provider timeout 当成 empty result；
7. 将 CTD 专属字段写进 source-neutral requirement matcher；
8. 将生成时使用的 provider 路线写成 Planner 的正确动作；
9. 为了得到单答案而静默丢弃第二答案；
10. 把“固定证据世界唯一”写成“医学上唯一”。
11. 用隐藏 CTD/PubMed corpus 边界制造单答案，却不把有效限定公开到题面。
12. 询问“哪个疾病具有某类证据”，把 evidence metadata 当成领域知识。

## 14. 计划变更规则

实施中允许拆分或重排 Step，但必须满足：

- 不得跳过完整竞争集验证、claim/evidence 绑定和 public/hidden 隔离；
- scope 扩大到其他 CTD 表或其他答案形式时，先更新契约和验收标准；
- 新增 provider 不得改变 source-neutral claim 与 requirement 语义；
- 暂时跳过的步骤必须记录影响、替代门禁和恢复条件；
- 所有 P0 验收项只能通过实现满足，不能通过降低阈值或修改样本规避。

## 15. 完成定义

CTD 正式接入只有在以下事实同时成立时才算完成：

1. CTD/PubMed claim 已进入 `ValidatedTask` 主链，而非只存在于旧 enrichment；
2. 每题能够从固定快照重新计算答案、cardinality 和完成状态；
3. 每个 gold claim 能追溯到具体 CTD record 和 PubMed span；
4. 所有可能竞争答案均有确定 outcome，未知竞争项会阻止发布；
5. Writer 没有改变 claim 强度、方向和 qualifier；
6. public 数据不泄漏 gold、requirements、claims、evidence 或路线；
7. 真实 pilot、自动化回归和人工盲审均通过；
8. 不适用 CTD 的现有 PrimeKG 正式题型保持回归通过。
9. 每道题的单答案范围由题面公开的 study context 定义，而不是由隐藏 evidence scope 定义。

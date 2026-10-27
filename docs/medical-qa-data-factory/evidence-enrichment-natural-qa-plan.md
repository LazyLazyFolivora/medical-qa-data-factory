# Evidence-Enriched Natural QA Expansion Plan

> 实施说明：本文保留为证据增强的背景设计。CTD 进入正式 task-first 主链的当前执行计划和验收门禁，分别见 [CTD 正式接入开发计划](ctd-formal-integration-development-plan.md) 与 [CTD 正式接入验收标准](ctd-formal-integration-acceptance-criteria.md)。

## 1. 背景

当前 Web 的自然语言出题只依赖 PrimeKG 一条边，例如：

```text
Aspirin --[exposure_disease]--> prostate cancer
source = CTD
```

PrimeKG 只保存实体、关系、端点和来源，不保存 CTD 的原始证据类型、推断方式和
PMID。因此系统只能知道“数据源记录过这条边”，无法可靠判断：

- 是风险增加、风险降低，还是仅存在研究关联；
- 是治疗、机制、生物标志物还是推断；
- 是直接证据还是通过基因等信息推断；
- 是否存在相互矛盾的研究结论。

如果直接把边改写成自然问题，很容易产生：

```text
阿司匹林和前列腺癌是什么关系？
阿司匹林这种暴露因素与哪种疾病有关？
阿司匹林和前列腺癌是否存在关联？
```

这些句子要么是数据查询语言，要么过于笼统，不符合自然语言问答用户。

本方案引入独立的证据增强层。PrimeKG 只负责定位候选实体对，CTD 和 RAG 负责补回
语义与原文证据，LLM 最终只负责润色用户表达。

## 2. 目标

1. 从 CTD、文献 RAG 或其他来源恢复关系的真实语义。
2. 生成能在现有答案契约下被唯一回答的自然语言问题，避免开放候选集。
3. 根据证据决定问题意图，不允许 Writer 自行猜测。
4. 保留完整 provenance：PrimeKG 边、来源记录、文本片段、摘要和快照。
5. 证据不足或冲突时明确拒绝，不使用含糊问法兜底。
6. 保持 PrimeKG 和外部来源数据只读。

## 3. 非目标

- 不修改 PrimeKG 原始 SQLite 或 CSV；
- 不把 CTD 的 `therapeutic` 直接解释为临床有效；
- 不允许 LLM 自行补充医学事实；
- 不生成个性化诊断、处方或治疗建议；
- 不取消或静默修改现有 `QuestionSpec`、`GoldAnswer`、`QAItem` 和导出契约；
- 不在第一版处理所有 PrimeKG 来源；
- 不自动把机器候选标记为 gold。

## 4. 核心原则

```text
图谱负责“候选是谁”
证据负责“关系实际意味着什么”
确定性 Planner 负责“问什么”
Style Skill 负责“像什么用户”
LLM 负责“怎么说”
Verifier 负责“是否仍然是同一事实”
```

自然问答模式禁止开放候选题，例如：

```text
A 和哪种疾病有关？
A 有哪些相关药物？
哪些实体与 A 存在关系？
```

自然问题必须固定精确 A/B 或精确路径，且问题意图由证据确定。

“精确 A/B”是题面和证据约束，不代表可以改变现有答案形式。任何证据意图如果必须依赖
新的答案形式才能表达，都不能进入 V1 数据集。

## 5. 总体架构

```text
PrimeKG A/B edge
    |
    v
Enrichment Router
    |-- CTD adapter
    |-- RAG adapter (PubMed / WeKnora / other)
    |-- source-specific adapter
    |
    v
NormalizedEvidenceClaim
    |
    v
Evidence Fusion + Conflict Detection
    |
    v
Question Intent Resolution
    |
    v
Deterministic Question Skeleton
    |
    v
Style Skill + LLM Polish
    |
    v
Answer-blind Verification + Deterministic Gates
    |
    v
Draft QA Item
```

## 6. 组件设计

### 6.1 Enrichment Router

根据 PrimeKG 边的 `source`、`relation`、实体类型和 provider capability 选择增强器：

| PrimeKG 来源/关系 | 默认增强方式 |
|---|---|
| `source=CTD` | CTD 原始记录，再按 PMID 获取摘要 |
| 文献型关系 | PubMed、WeKnora 或其他 RAG |
| DrugBank 关系 | DrugBank 原始文本，再按需 RAG |
| MONDO ontology | 只恢复 ontology 语义，不直接生成临床问题 |
| NCBI 蛋白互作 | NCBI/DrugBank 结构化证据 |
| 只有推断分数 | 默认不得生成自然医学问题 |

### 6.2 CTD Adapter

输入：

- PrimeKG CTD 边；
- CTD chemical-disease association 原始文件；
- CTD release manifest。

输出：

- CTD 原始记录；
- `DirectEvidence`；
- `InferenceGeneSymbol`；
- `InferenceScore`；
- `PubMedIDs`；
- PrimeKG 到 CTD 的映射状态。

### 6.3 RAG Adapter

第一批支持：

- PubMed E-utilities；
- 现有 WeKnora provider；
- 后续可增加 Europe PMC 或其他语料 provider。

RAG 必须支持：

1. 按精确 A/B、别名和关系关键词检索；
2. 独立 read-back；
3. 返回 PMID、chunk ID、文本、偏移和 snapshot；
4. 保存内容 hash；
5. 标记无法回读或快照不一致的结果。

### 6.4 Claim Normalizer

把 CTD 元数据、RAG 片段和来源专用数据统一成 `NormalizedEvidenceClaim`。

### 6.5 Evidence Fusion

合并多个来源，处理：

- 重复 claim；
- CTD 元数据与摘要结论冲突；
- 多篇文献方向冲突；
- 研究类型和人群差异；
- 直接证据与推断证据优先级。

### 6.6 Intent Resolver

只从证据支持的集合中选择问题意图，不允许自由生成意图。

### 6.7 Question Skeleton Planner

根据意图生成确定性骨架。A/B 名称和答案形式由服务端填写。

### 6.8 Style Skill + LLM Polisher

继续使用现有版本化 Style Skill：

- `layperson-search-v1`
- `patient-caregiver-v1`
- `medical-learner-v1`

Skill 和 LLM 不得修改：

- 实体 A/B；
- 关系方向；
- 风险增加或降低；
- 治疗、机制、安全性等意图；
- 研究类型和人群范围；
- 答案形式；
- 证据集合。

### 6.9 Verifier

Verifier 不读取 Writer 的候选答案和理由，只依据问题、意图和冻结证据复核：

- 意图是否被证据支持；
- 是否存在方向冲突；
- 是否只有一个答案；
- 是否出现答案泄漏；
- 是否把关联升级为因果；
- 是否改变了 A/B 或人群。

## 7. 统一证据契约

### 7.0 现有答案契约保持不变

Evidence Enrichment 只产生证据和 eligibility 判断，不重新定义数据集答案。

以下现有契约继续作为唯一数据产品接口：

- `QuestionSpec`
- `GoldAnswer`
- `QAItem`
- 机器校验、人工确认和导出门禁

CTD/RAG 字段只能进入附加的 evidence metadata 或 `query_intent`，不能直接覆盖
`gold_answer`。

如果现有答案契约不能表达某个候选：

1. 默认拒绝该候选；
2. 新答案形式必须通过显式、向后兼容的新契约版本引入；
3. 不得在某些样本中临时使用字符串、布尔值或其他非契约结构。

自然语言表达也不能改变答案形式。Writer 和 Style Skill 只能润色题面，不能把已有
答案契约转换为其他形式。

### 7.1 RawEvidenceRecord

```json
{
  "provider": "ctd",
  "source_id": "ctd:D001241:prostate-cancer",
  "snapshot_id": "ctd-2026-09",
  "raw_fields": {},
  "content_hash": "..."
}
```

### 7.2 NormalizedEvidenceClaim

```json
{
  "subject_entity_id": "primekg:exposure:D001241",
  "object_entity_id": "primekg:disease:8315",
  "relation": "exposure_disease",
  "claim_type": "risk_association",
  "polarity": "decrease",
  "study_type": "observational",
  "population": "adult men",
  "claim_text": "Aspirin use was associated with lower prostate cancer risk.",
  "support_level": "literature_supported",
  "sources": [
    {
      "kind": "ctd",
      "source_id": "...",
      "metadata": {}
    },
    {
      "kind": "pubmed",
      "source_id": "pmid:12345678",
      "quotes": [
        {
          "text": "...",
          "start": 100,
          "end": 220,
          "content_hash": "..."
        }
      ]
    }
  ]
}
```

### 7.3 QuestionIntent

第一版允许的意图：

```text
association_research
therapeutic_research
mechanism_research
risk_increase
risk_decrease
contraindication
safety
insufficient
conflicting
```

`insufficient` 和 `conflicting` 不允许生成自然问题。

## 8. 本地存储

```text
data/
  external/
    ctd/
      CTD_chemicals_diseases.csv.gz
      release.json
  cache/
    ctd-evidence.sqlite
    literature/
      pubmed.sqlite
      weknora.sqlite
```

### 8.1 `ctd_records`

```text
chemical_id
chemical_name
disease_id
disease_name
direct_evidence
inference_genes
inference_score
pubmed_ids
content_hash
```

### 8.2 `primekg_ctd_map`

```text
primekg_edge_id
primekg_head_id
primekg_tail_id
ctd_chemical_id
ctd_disease_id
mapping_method
mapping_status
```

`mapping_status`：

```text
exact
crosswalk
grouped
ambiguous
unmatched
```

只有 `exact` 和 `crosswalk` 默认允许进入自然 QA。

### 8.3 `literature_evidence`

```text
pmid
title
abstract
doi
chunk_text
offset_start
offset_end
content_hash
snapshot_id
retrieved_at
```

## 9. CTD 映射规则

1. 化学品优先使用 ID：

   ```text
   PrimeKG head_id = CTD ChemicalID
   ```

2. 疾病优先使用 ontology crosswalk：

   ```text
   MONDO -> MeSH / CTD DiseaseID
   ```

3. 无 crosswalk 时使用规范化疾病名称精确匹配。
4. `MONDO_grouped` 不得把某一条子疾病证据无条件归到整个组合节点。
5. 无法唯一映射时标记 `ambiguous` 或 `unmatched`，不得自动出题。

## 10. RAG 检索规范

查询必须同时锚定精确实体：

```text
("Aspirin" OR "acetylsalicylic acid")
AND
("prostate cancer" OR "prostatic neoplasm")
```

CTD 有 PMID 时优先读取这些文献；没有时再执行独立检索。

证据片段必须：

- 来自固定 snapshot；
- 可独立 read-back；
- 保存原文、偏移和 hash；
- 明确来自哪个文档；
- 不得由 LLM 摘要代替原文。

## 11. 问题意图与证据门禁

本节中的意图只表示“证据支持什么语义”，不等于自动允许某种新的答案形式。

| 证据情况 | 允许的问题 |
|---|---|
| CTD `therapeutic` + 摘要支持 | 研究是否探讨过 A 用于 B 的治疗 |
| CTD `marker/mechanism` + 摘要支持 | A 是否被研究与 B 的机制有关 |
| 摘要明确支持风险降低 | A 是否与 B 风险降低有关 |
| 摘要明确支持风险增加 | A 是否与 B 风险增加有关 |
| 摘要明确无关联 | 不生成“风险增加/降低”题 |
| 只有推断、无摘要 | 不生成自然问题 |
| 多篇证据冲突 | 不生成，标记 `conflicting` |
| CTD/RAG 映射不到唯一 A/B | 不生成 |

## 12. 自然问题规范

以下是用户意图示例，不一定能成为 V1 数据集题面：

```text
阿司匹林是否被研究用于治疗前列腺癌？
服用阿司匹林是否与前列腺癌风险降低有关？
有研究探讨过阿司匹林与前列腺癌发生机制的关系吗？
```

禁止：

```text
阿司匹林和前列腺癌是什么关系？
阿司匹林和哪种疾病有关？
阿司匹林这种暴露因素与前列腺癌有关吗？
阿司匹林和前列腺癌之间是否存在笼统关联？
```

问题必须包含精确 A/B。若意图是开放候选查询，例如“哪种疾病”，则直接拒绝，不进入
Style Skill。

V1 当前实际使用的答案是实体答案。最终题面必须能够在 `answer_mode=entity` 下被唯一
回答，并且候选实体数量必须为 1。例如，如果 RAG 明确支持“阿司匹林使用与前列腺癌风险
降低相关”，V1 可以接受这种实体答案题面：

```text
阿司匹林使用与哪种癌症的风险降低相关？
```

答案必须唯一对应 `prostate cancer`。下面的是/否题只能作为证据意图描述，不能直接进入
当前实体答案数据集：

```text
服用阿司匹林是否与前列腺癌风险降低有关？
```

除非未来显式升级答案契约，否则这类候选必须拒绝。

## 13. 答案契约与自然问题

现有答案契约是数据集目标，不因证据增强或风格改造而取消。

约束如下：

1. `QuestionSpec.gold_answer` 和 `QAItem.answer` 继续使用现有版本化结构。
2. CTD `DirectEvidence`、RAG 摘要和问题意图只用于：
   - 判断关系能否支持题目；
   - 约束题面方向和措辞；
   - 提供 evidence provenance。
3. 题目必须能在现有答案契约下被唯一回答，否则拒绝。
4. 不得把实体答案静默改成布尔值、短句或自由文本。
5. 如果未来确实需要新的答案形式，必须：
   - 单独提出 schema 变更；
   - 提升契约版本；
   - 提供迁移和导出兼容策略；
   - 新旧样本明确区分。

示例规则：

```text
现有契约要求实体答案
→ 只能生成答案可由唯一实体确定的自然问题
→ 如果 RAG 只支持“风险降低”的布尔结论
→ 只有能把该结论转成答案唯一的实体问题时才保留
→ 否则该候选在现有契约下拒绝
→ 不自动改为 boolean
```

## 14. 分阶段任务

### 阶段 0：契约与 fixture

交付：

- `RawEvidenceRecord`
- `NormalizedEvidenceClaim`
- `QuestionIntent`
- `EnrichmentDecision`
- CTD/RAG fake provider
- 字段级单元测试

退出标准：不接入网络也能跑通 CTD → RAG → Claim → Intent 的 fake pipeline。

### 阶段 1：CTD 快照与映射

交付：

- CTD 文件下载和 SHA256 manifest；
- `ctd_records` 解析器；
- `primekg_ctd_map`；
- 映射状态统计命令；
- 只读 sidecar。

退出标准：9,336 条 PrimeKG CTD 边全部有明确映射状态。

### 阶段 2：PubMed RAG

交付：

- PMID 批量获取；
- 摘要 read-back；
- 文本偏移和 hash；
- snapshot manifest；
- rate limit、重试和错误语义。

退出标准：给定 PMID 能独立回读摘要片段，失败不会伪造证据。

### 阶段 3：证据融合与意图

交付：

- Claim 去重；
- CTD/RAG 冲突检测；
- 研究类型分类；
- support level；
- intent resolver，但不得改变 `GoldAnswer.answer_mode`；
- 不合格边拒绝原因。

退出标准：冲突或推断证据不会进入自然问题 Writer；意图无法映射到现有实体答案契约时
直接拒绝。

### 阶段 4：问题骨架与 LLM 润色

交付：

- 精确 A/B 骨架；
- 证据意图槽位；
- 禁止开放候选；
- Style Skill 只润色；
- 实体、方向、意图不变检查；
- 候选去重。

退出标准：LLM 无法把治疗研究改成疗效结论，也无法扩大候选集合。

### 阶段 5：Web 与实时进度

交付：

- 面向前端显示当前证据增强阶段；
- CTD mapping、RAG retrieval、fusion、Writer、Verifier 进度；
- 拒绝原因可查看；
- 无证据时不展示伪造问题。

### 阶段 6：评测与灰度

先选取：

- 20 对 CTD 实体；
- 20 对非 CTD 文献实体；
- 若干冲突和缺失证据负例。

人工盲评：

- 是否像真实用户；
- 是否正确保留 A/B；
- 是否夸大关系；
- 是否过大候选集；
- 答案是否被证据支持。

## 15. 测试要求

### 单元测试

- CTD 文件字段解析；
- ID/名称/crosswalk 映射；
- grouped/ambiguous 拒绝；
- PMID 解析和回读；
- hash、偏移、snapshot；
- CTD/RAG 冲突；
- 意图映射；
- 开放候选拒绝；
- 实体和方向漂移拒绝。

### 契约测试

- 所有 claim 必须引用来源；
- 所有自然问题必须引用 claim；
- `insufficient`/`conflicting` 不得生成问题；
- 自然问题的 A/B 与 claim 一致；
- 答案形式与现有 `answer_contract_v1` 一致；
- 意图无法转成唯一实体答案时不出题；
- `QuestionSpec.gold_answer` 到 `QAItem.answer` 的投影无损。

### 集成测试

- CTD 原始数据 → PrimeKG 映射；
- PMID → 摘要 → claim；
- fake RAG + fake CTD；
- 真实 CTD + 少量真实 PMID；
- 真实 Web structured model smoke。

## 16. 验收标准

- 自然问题中开放候选集问题为 0；
- 未找到原文却生成的医学问题为 0；
- 答案泄漏为 0；
- 实体或关系方向被润色改变为 0；
- CTD/RAG 冲突仍出题为 0；
- 现有 `QuestionSpec`/`GoldAnswer`/`QAItem` 契约兼容性回归为 100%；
- 至少 90% 的机器候选通过人工初筛；
- 每个 QA Item 可追溯到：
  - PrimeKG edge；
  - CTD raw record；
  - PMID / RAG chunk；
  - content hash；
  - source snapshot；
  - question intent；
  - style skill version。

## 17. 风险与缓解

| 风险 | 缓解 |
|---|---|
| CTD 与 PrimeKG 疾病分组不一致 | crosswalk + ambiguous 拒绝 |
| CTD 类型不等于临床结论 | 摘要复核，禁止直接升级 |
| RAG 检索到同名不同实体 | 精确 A/B 和别名约束 |
| 多篇文献结论冲突 | conflict gate |
| LLM 润色改变问题意图 | 结构化槽位 + Verifier |
| 外部服务不稳定 | 固定快照、缓存、可重试错误 |
| 题量显著下降 | 接受质量优先；先扩大来源，不降低门禁 |

## 18. 推荐落地顺序

```text
1. CTD 快照与映射
2. PubMed RAG
3. Evidence Claim 融合
4. Intent + 精确 A/B 骨架
5. Style Skill 润色
6. Web 状态与人工评测
```

第一阶段只覆盖 PrimeKG `source=CTD`，验证稳定后再扩展到其他 source 和 RAG provider。

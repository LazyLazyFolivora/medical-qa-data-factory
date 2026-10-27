# PrimeKG、CTD 与 PubMed 联合证据验证设计

> 状态：实施设计  
> 日期：2026-10-27  
> 适用对象：PrimeKG 实体候选、CTD direct evidence、PubMed 文献验证和题目生成

题目是否可发布，以
[PrimeKG + CTD + PubMed 题目验收标准](primekg-ctd-pubmed-question-acceptance.md)
为准。

## 1. 目标

目标架构是：

```text
PrimeKG-first
CTD-validated
PubMed-supported
```

PrimeKG 始终是实体身份、答案集合和图关系验证的权威来源。CTD 和 PubMed
不能替换 PrimeKG entity，只负责验证 PrimeKG 候选关系是否有可靠的生物医学证据。

当前简化路径的问题在于：

```text
PrimeKG source=CTD 边只被当作来源标签
或
CTD direct record 直接成为答案世界
```

这两种做法都绕过了统一流程。它们没有证明：

- 最终答案实体是否存在于固定 PrimeKG snapshot；
- PrimeKG 实体是否正确映射到 CTD；
- CTD 引用的论文是否可读；
- 论文是否真的支持该关系；
- 论文中的研究条件是什么；
- CTD 标签是否被夸大；
- PrimeKG 完整候选世界中答案是否唯一；
- 能否生成自然的生物医学问题。

本设计把 PrimeKG 图验证、CTD 结构化证据和 PubMed 原文验证串成一条链路。

最终目标是：

```text
PrimeKG edge / candidate
  -> PrimeKG canonical entities
  -> identity crosswalk
  -> matching CTD records
  -> CTD DirectEvidence / inference status
  -> frozen PubMed corpus
  -> verified PubMed support spans
  -> PrimeKG-bound normalized claim
  -> complete PrimeKG candidate universe
  -> ValidatedTask
  -> prompt-constrained Writer
  -> semantic verifier
  -> public / hidden release
```

### 1.1 当前实现与目标实现的区别

当前代码中有两条中间状态路径，均不能作为最终正式题目口径。

| 当前路径 | 当前行为 | 问题 | 允许用途 |
|---|---|---|---|
| `generate validated --profile primekg-ctd` | 用 PrimeKG `source=CTD` 边发现候选，在 PrimeKG 中验证 | 没有读取 CTD DirectEvidence/PMID/PubMed | 图结构探索 |
| `generate ctd-direct` | 枚举 CTD direct evidence，并用 CTD disease ID 作为答案 | 没有 PrimeKG canonical entity，也没有 PubMed 原文验证 | CTD 审计/候选发现 |
| 本设计目标路径 | PrimeKG entities + CTD evidence + PubMed span + PrimeKG final world | 尚未实现 | 正式数据生成 |

不允许把前两条路径的 public release 直接当作本设计完成后的正式 gold。

## 2. 设计边界

### 2.1 本项目负责

- 从 CTD 记录读取 PMID；
- 固定 PubMed 文献集合；
- 保存标题、摘要/全文、snapshot 和 content hash；
- 调用受约束模型抽取关系；
- 验证引用 PMID、原文 span 和 quote hash；
- 验证 support、contradiction、context-only、uncertain；
- 保留研究条件；
- 判断关系强度是否被夸大；
- 把已验证证据重新绑定到 PrimeKG canonical entities；
- 在固定 PrimeKG snapshot 中验证实体存在、关系/路径和候选全集；
- 将 CTD/PubMed evidence references 绑定到 PrimeKG Task；
- 只有实体、图关系、证据和语义全部通过时才发布；
- 发布 public/hidden 分离的数据。

### 2.2 本项目不负责

- 修改 PubMed 原文；
- 根据摘要自动补充作者没有写出的研究条件；
- 把数据库标签直接解释为临床共识；
- 把 PubMed 摘要等同于完整论文；
- 用 LLM 判断代替确定性引用检查；
- 把“有一篇论文提到”写成“现实世界已经确立”。

## 3. 核心原则

1. **CTD 是候选来源，不是最终证据。**
2. **PubMed PMID 是定位线索，不是支持证明。**
3. **PrimeKG canonical entity 是答案身份的唯一来源。**
4. **CTD DiseaseID、MeSH ID 和 PubMed 中的名称不能直接替代 PrimeKG ID。**
5. **模型只能提出 verdict、claim 和 quote。**
6. **代码决定 PMID、span、hash、映射和引用是否真实。**
7. **所有 CTD 引用 PMID 都必须被评估，不能只挑支持文献。**
8. **未知、不可读、映射歧义和 provider failure 都不能当作负例。**
9. **题目只能从最终 ValidatedTask 生成。**
10. **题面不得依赖未公开的 source filter、CTD 子集或证据边界制造唯一性。**

## 4. 文献来源策略

### 4.1 首选 CTD 给出的 PMID

对于每条 CTD direct record：

```text
PubMedIDs = pmid1 | pmid2 | ...
```

必须获取全部 PMID，并逐个评估。

不允许：

- 只取第一个 PMID；
- 只保留标题看起来相关的 PMID；
- 用补充搜索结果替换 CTD 原引用；
- 把搜索不到摘要解释为论文不存在。

### 4.2 补充搜索只用于发现

当 CTD 没有 PMID，或需要发现更多证据时，可以调用 PubMed ESearch。

补充搜索结果：

- 必须记录查询式、时间、命中数和排序；
- 不能冒充 CTD 原始 citation；
- 不能提高已有 CTD claim 的验证等级；
- 需要单独标记来源为 `supplementary_search`。

### 4.3 摘要与全文的优先级

第一阶段：

```text
PubMed abstract
    > open-access PMC full text
    > title only
```

具体规则：

- abstract 可用于正式支持验证；
- PMC/其他开放全文可作为补充或替代，但必须固定完整文本和 section；
- title only、snippet、搜索结果高亮片段只能用于候选发现；
- 无摘要但正文可读时，必须记录全文来源、版本、section 和字符偏移；
- 无摘要且无全文时，该 PMID 状态为 `unreadable`。

BioDSA 的 `fetch_paper_content_by_pmid` 可以作为全文获取 adapter，但其结果必须先转换成本项目的固定文献记录，不能直接作为 gold evidence。

## 5. PrimeKG 候选绑定与数据流

### 5.1 PrimeKG 实体身份

候选首先来自 PrimeKG edge 或 PrimeKG graph query。边两端使用 PrimeKG
canonical entity ID：

```text
primekg:exposure:C005445
primekg:disease:2909
```

最终答案必须保留 PrimeKG entity ID。CTD DiseaseID/MeSH ID 只能作为
evidence binding 和审计字段。

例如：

```text
PrimeKG:
  triphenyl phosphate --exposure_disease--> hyperglycemia
  source = CTD

CTD:
  C005445 + MESH:D006943
  DirectEvidence = marker/mechanism
  PMID = 32679393
```

正确绑定是：

```text
PrimeKG chemical C005445
  -> CTD C005445

PrimeKG disease 2909
  -> crosswalk
  -> CTD MESH:D006943
```

错误做法是把最终答案改成 `ctd:disease:MESH:D006943`。CTD ID 只出现在 hidden
evidence catalog 和 dependency digest 中。

### 5.2 Entity mapping 状态

疾病映射至少包含：

```text
resolved
ambiguous
unmapped
provider_failed
```

规则：

- `resolved` 且唯一：可以继续；
- `ambiguous`：不能自动选择，阻止发布；
- `unmapped`：不能构造正式 claim，阻止发布；
- `provider_failed`：搜索未完成，阻止发布；
- 同名异实体不得合并；
- 多 CTD record 映射到同一 PrimeKG entity 时，必须保留 alias/record lineage；
- `MONDO_grouped` 不能无条件视为其所有子疾病。

### 5.3 数据流

```text
PrimeKG edge / graph candidate
  |
  +--> PrimeKG canonical entities
          |
          +--> identity crosswalk
          |      +--> matching CTD chemical
          |      +--> matching CTD disease
          |      +--> mapping status
          |
          +--> CTD record resolution
          |      +--> DirectEvidence
          |      +--> inference-only status
          |      +--> PubMedIDs
          |
          |
          +--> PubMed fetch
          |      +--> abstract/full text
          |      +--> raw XML hash
          |      +--> corpus manifest
          |
          +--> constrained model extraction
          |      +--> per-PMID verdict
          |      +--> exact quote + offsets
          |      +--> qualifiers
          |
          +--> deterministic verification
          |      +--> PMID coverage
          |      +--> span bounds
          |      +--> quote equality
          |      +--> content/quote hashes
          |      +--> semantic upgrade checks
          |
          +--> PrimeKG-bound evidence fusion
                 +--> CTD direct evidence layer
                 +--> PubMed evidence layers
                 +--> support/contrast/context
                 +--> PrimeKG canonical answer ID
                 |
                 +--> complete PrimeKG candidate universe
                        +--> relation/path validation
                        +--> qualifier/evidence validation
                        +--> cardinality validation
                        |
                        +--> ValidatedTask or rejection
```

## 6. PubMed 固定快照

### 6.1 单篇文档

建议契约：

```python
class PubMedDocument:
    pmid: str
    pmcid: str | None
    doi: str | None
    title: str
    abstract: str
    full_text: str | None
    full_text_sections: list[PubMedTextSection]
    source_kind: Literal["abstract", "pmc_full_text", "pubtator"]
    snapshot_id: str
    raw_response_hash: str
    content_hash: str
    retrieved_at: datetime
```

`content_hash` 至少覆盖规范化的 title、abstract 和 used full text。

### 6.2 Corpus manifest

```python
class PubMedCorpusManifest:
    manifest_id: str
    snapshot_id: str
    requested_pmids: list[str]
    records: dict[str, PubMedDocumentRef]
    supplemental_queries: list[PubMedSearchTrace]
```

要求：

- 相同 snapshot、PMID 和文本生成相同 manifest ID；
- 缺少 PMID、额外 PMID 和重复 PMID 必须显式记录；
- 同一 PMID 文本发生变化时，不能沿用旧 span；
- manifest 不得只包含支持文献。

### 6.3 全文 section

如果使用全文：

```python
class PubMedTextSection:
    section_id: str
    section_type: str
    heading: str | None
    text: str
    start: int
    end: int
    content_hash: str
```

span 必须相对于明确的 section，不能相对于运行时随意拼接的全文。

## 7. 模型抽取契约

模型对每个 PMID 必须返回且只返回一个 assessment：

```json
{
  "pmid": "31229880",
  "verdict": "supports",
  "intent": "therapeutic_research",
  "polarity": "neutral",
  "support_level": "literature_supported",
  "claim_text": "...",
  "citation": {
    "section_id": "abstract",
    "start": 120,
    "end": 245,
    "quote": "verbatim text"
  },
  "qualifiers": {
    "population": null,
    "species_or_model": null,
    "study_type": null,
    "dose_or_exposure": null,
    "duration": null,
    "sample_type": null
  },
  "reason_codes": []
}
```

允许的 verdict：

```text
supports
contradicts
context_only
uncertain
```

模型不得输出：

```text
proven
effective treatment
causes
correct_action
best_route
```

`claim_text` 可以是对原文的受约束概括，但 `citation.quote` 必须是逐字原文。

## 8. 确定性校验

### 8.1 PMID 校验

- 模型只能引用 CTD 提供的 PMID；
- 每个输入 PMID 必须有一致结果；
- 未知、重复或遗漏 PMID 均拒绝；
- 不允许用补充搜索结果替代缺失的原引用。

### 8.2 Span 校验

```text
0 <= start < end <= len(section.text)
section.text[start:end] == citation.quote
sha256(citation.quote) == quote_hash
```

同时检查：

- record content hash；
- corpus manifest ID；
- section hash；
- 原始响应 hash；
- retrieved snapshot。

### 8.3 Qualifier 校验

模型可以提出以下 qualifier：

- population；
- species/model；
- study type；
- dose/exposure；
- duration；
- sample type；
- temporal scope；
- additional qualifiers。

但所有进入题面的 qualifier 必须满足：

1. 在原文中有明确依据；
2. 有可回读 span；
3. 与支持结论的语义一致；
4. 不得根据常识补齐；
5. 不得把动物/细胞条件写成患者条件。

### 8.4 关系强度校验

#### therapeutic

允许的最强措辞：

```text
被研究用于
被研究作为治疗手段
在治疗研究中被评估
```

不允许仅凭 CTD 标签生成：

```text
能够治疗
有效治疗
治愈
预防
优于其他药物
```

#### marker/mechanism

允许：

```text
与某 marker 变化相关
在某机制研究中被研究
可能涉及某通路
```

不允许：

```text
证明因果
阐明唯一机制
直接导致
```

#### association

只能使用“相关、共同出现、被研究与……存在关联”等中性措辞。

#### risk increase/decrease

进入该题型的前提是原文明确报告方向和比较对象。仅凭 inference score、疾病名称或 CTD direct 标签不得生成风险方向。

### 8.5 引用完整性

每个正式 claim 至少需要：

- 一个 CTD direct record 引用；
- 一个可回读的 PubMed support span；
- 一个稳定的 claim ID；
- 一个固定的 PubMed corpus manifest；
- 一个明确的 ValidationEnvironment。

Literature context-only 记录可以进入 evidence catalog，但不能单独满足 claim。

## 9. 多 PMID 和冲突处理

### 9.1 全部评估

CTD 提供 N 个 PMID，就必须产生 N 个 verdict：

```text
PMID A -> supports
PMID B -> contradicts
PMID C -> context_only
PMID D -> uncertain
```

不能只发布 A。

### 9.2 冲突状态

同一公开研究条件下：

```text
supports + contradicts -> conflicting
```

以下情况可以保留为分层证据：

- 不同人群；
- 不同物种或模型；
- 不同剂量；
- 不同时间范围；
- 不同研究类型；
- 原文明确解释结论差异。

如果无法通过 qualifier 解释冲突，题目不得发布。

### 9.3 缺失和失败

状态必须区分：

```text
pubmed_empty
pubmed_no_abstract
pubmed_unreadable
pubmed_timeout
pubmed_rate_limited
pubmed_network_error
pubmed_provider_error
pubmed_malformed_response
pubmed_quote_mismatch
pubmed_span_out_of_bounds
unknown_pmid_reference
```

这些状态都不能被解释成“没有论文支持”。

## 10. CTD 与 PubMed 的分层

### 10.1 CTD direct layer

保存：

```text
ChemicalID
DiseaseID
DirectEvidence
InferenceGeneSymbol
InferenceScore
PubMedIDs
CTD record locator/hash
```

### 10.2 PubMed layer

保存：

- 每个 PMID 的 verdict；
- 支持/反对/上下文 span；
- 原文 quote；
- qualifier；
- access/snapshot 信息。

### 10.3 融合规则

- inference-only 不能升级为 direct claim；
- CTD direct 标签负责发现和 provenance，不代替 PubMed 文本；
- PubMed 文本负责证明论文实际说了什么；
- 模型不能改变 PrimeKG canonical chemical/disease IDs；
- CTD ChemicalID/DiseaseID 只作为证据绑定和 hidden lineage；
- 任何 CTD/PubMed 结果都必须重新绑定到 PrimeKG Task；
- 最终 claim 的 predicate 由受控语义映射产生；
- CTD 和 PubMed 的位置、hash、snapshot 都必须进入依赖 digest。

## 11. PrimeKG 实体、路径与联合竞争集验证

### 11.1 PrimeKG 验证什么

PrimeKG 负责：

- 固定 snapshot 中的 canonical entity 是否存在；
- 题目定义的 relation 和 direction 是否成立；
- 两个实体之间是否存在符合题面语义的路径；
- 哪些实体是题目约束下的潜在答案；
- 完整 PrimeKG 候选集是否严格单答案。

PrimeKG 不负责判断：

- CTD 中是否具有 DirectEvidence；
- PubMed 论文是否支持；
- population、dose、time 等研究条件；
- 医学关系是否被语义夸大。

“完整”仅相对于声明的 PrimeKG snapshot、relation、direction、source filter
和 query scope，不表示现实世界绝对完整。

### 11.2 双实体问题示例

假设题面涉及：

```text
起点实体：chemical X
答案实体：disease D
额外约束实体：protein P
```

候选路径是：

```text
X --exposure_disease--> D
D --disease_protein--> P
```

最终验证不能只确认：

```text
D 存在，且 X -> D -> P 这条路径存在
```

还必须枚举 PrimeKG 中 X 的全部 exposure disease：

```text
A, B, C, D, E
```

并分别检查它们是否满足与 P 的关系：

```text
A -> P
B -> 不满足
C -> P
D -> P
E -> 未完成/不可读
```

如果结果是：

```text
matched = {A, D}
```

则题目有多个答案，拒绝。

如果 E 未完成：

```text
result_complete = false
```

同样拒绝。

只有：

```text
result_complete = true
world_answer_ids = {D}
```

才能继续发布。

### 11.3 路径规则

验证器必须：

1. 统一 relation direction；
2. 枚举完整分页；
3. 对同一答案实体去重；
4. 保留所有满足题面的 evidence path；
5. 区分直接边和间接路径；
6. 不在题面未说明时选择某条生成路径作为“正确路线”。

如果 PrimeKG 中同一 relation 由于不同 source 出现方向不一致，必须先应用
versioned relation-direction policy。没有统一方向时，不能宣称竞争集完整。

### 11.4 联合证据验证

PrimeKG 枚举出的每个候选答案都需要执行同一套证据检查：

```text
PrimeKG disease
  -> identity crosswalk
  -> CTD record status
  -> DirectEvidence/inference status
  -> PubMed document availability
  -> PubMed evidence assessment
  -> matched/not_matched/ambiguous/unknown
```

任一候选无法完成映射或证据验证，都不能当成 `not_matched`。

### 11.5 Source filter 必须公开

如果题目只允许 PrimeKG 的 CTD 来源边：

```text
source = CTD
```

这个范围必须进入题面可理解语义或明确的任务契约，不能作为隐藏过滤条件。

如果题面问的是普通 PrimeKG 或生物医学世界问题，则不能偷偷排除：

```text
MONDO
MONDO_grouped
其他 source
```

否则单答案只是由隐藏 source filter 制造出来的。

### 11.6 发布门槛

```text
PrimeKG entity 存在
AND identity mapping resolved
AND graph relation/path matches question semantics
AND CTD direct evidence status is known
AND all CTD PMIDs have known verdicts
AND all supporting spans/hashes are replayable
AND complete PrimeKG candidate universe is evaluated
AND result_complete == true
AND world_answer_ids == one PrimeKG canonical entity
AND text semantic verification passes
```

任何 `unmapped`、`ambiguous`、`not_evaluated` 或 `provider_failed` 都阻止发布。

## 12. 题目生成

只有 `ValidatedTask` 能进入 Writer。

第一阶段可以允许：

```text
primekg_ctd_pubmed_therapeutic_research_disease
primekg_ctd_pubmed_marker_mechanism
primekg_ctd_pubmed_association
```

Writer 必须使用 Task 中的：

- chemical；
- disease；
- intent；
- polarity；
- public qualifiers；
- validation scope；
- answer cardinality。

### 12.1 不使用程序固定题面

正式 Writer 不得通过 `if intent == ...: question = "固定字符串"` 的方式生成题面。

程序固定字符串会带来：

- 模板占比过高；
- 题目机械且重复；
- 无法自然表达研究条件；
- 数据集容易学习句式而不是学习信息检索；
- 新增题型时必须修改 Writer 代码。

固定句面只能存在于测试 fixture 或明确隔离的历史兼容路径，不能作为正式数据集的唯一 Writer。

### 12.2 Prompt 约束负责表达

Writer 接收结构化 Task，并由受版本控制的 Prompt 约束表达：

```text
role = question_writer
version = ctd-pubmed-writer-v1

输入：
  ValidatedTask
  question intent
  public qualifiers
  answer type
  forbidden disclosures
  desired language/style

输出：
  QuestionCandidate
  question_text
  reconstructed semantics
```

Prompt 约束至少包括：

- 不得改变 chemical、disease、gene/pathway 或 answer cardinality；
- 不得改变 predicate、polarity 或证据强度；
- 必须公开所有承担消歧作用的 study qualifiers；
- `therapeutic` 只能表达“被研究用于/治疗研究中被评估”；
- 不得写成“有效治疗/治愈/预防”；
- `marker/mechanism` 不得写成因果；
- 不得提 CTD/PubMed 字段、PMID、claim、evidence 或内部 ID；
- 不得泄漏答案名称、别名或可逆占位符；
- 不得使用“当前证据范围内”等未公开范围；
- 不要求题面永远使用同一种句式。

Prompt 约束不是答案模板。它规定“可以怎么说、不能怎么说”，不规定唯一句子。

### 12.3 多候选与风格

每个 ValidatedTask 可以生成多个候选：

- 研究型表达；
- 医学学习者表达；
- 普通检索表达。

只有在不改变任务语义时才允许选择不同风格。

程序和数据集必须记录：

- prompt role/version/hash；
- model config；
- style profile；
- task semantic digest；
- verifier version。

### 12.4 语义回验不能比较固定字符串

Verifier 不能要求题面等于 Writer 的某条固定模板。

正确流程是：

```text
question_text
  -> semantic reconstruction
  -> QuestionSemanticView
  -> compare with ValidatedTask
```

`QuestionSemanticView` 至少包含：

```text
chemical/entity ids or canonical names
predicate/intent
polarity
public qualifiers
scope
answer cardinality
forbidden leakage checks
```

以下任一情况拒绝：

- reconstruction 不确定；
- 实体不一致；
- predicate 或 polarity 不一致；
- qualifier 丢失或新增；
- 研究范围发生漂移；
- 答案泄漏；
- 关系强度升级。

### 12.5 示例结构

下面的内容只说明信息结构，不是程序必须拼接出的固定字符串：

```text
在[研究中明确公开的人群/样本/模型]中，
[chemical] 在[明确研究条件]下，
被研究用于改善哪种[有证据支持的疾病类别]？
```

如果原文只支持宽泛的 therapeutic 关系，不能自动补成具体人群、剂量或疾病类别。

题面禁止：

- 隐藏的 CTD 子集；
- “当前证据范围内”；
- “CTD 字段中”；
- evidence metadata trivia；
- 答案或别名；
- claim/evidence/PMID；
- 因果或疗效升级。

## 13. 发布产物

建议新增：

```text
hidden/pubmed_corpus_manifest.json
hidden/primekg_ctd_identity_map.snapshot.json
hidden/pubmed_documents.jsonl
hidden/pubmed_spans.jsonl
hidden/ctd_pubmed_fusions.jsonl
hidden/pubmed_assessments.jsonl
```

最终 release：

```text
public/
  planner_questions.jsonl
  dataset_manifest.json

hidden/
  evaluation_targets.jsonl
  validated_tasks.jsonl
  validation_reports.jsonl
  evidence_catalog.jsonl
  competitor_reports.jsonl
  generation_episodes.jsonl
  rejected_candidates.jsonl
  pubmed_corpus_manifest.json
  pubmed_documents.jsonl
  pubmed_spans.jsonl
  pubmed_assessments.jsonl
  quality_report.json
  dataset_manifest.json
```

## 14. BioDSA 接入方式

BioDSA 已有：

```text
fetch_paper_content_by_pmid
pubmed_api_search_papers
pubtator_api_fetch_paper_annotations
pubtator_api_find_related_entities
```

本项目不直接依赖 BioDSA agent/tool wrapper，而增加一个 adapter：

```python
class BioDSAPubMedPort:
    def fetch(self, pmid: str) -> PubMedDocument: ...
```

Adapter 负责：

- 调用 BioDSA 的底层 Python 函数；
- 保留 title、abstract、full text 和来源；
- 生成 raw response hash；
- 转换成本项目固定的 PubMedDocument；
- 统一错误码；
- 禁止把 BioDSA 的格式化字符串直接当成 evidence。

如果 BioDSA 返回当前在线全文而没有固定 snapshot，adapter 必须在本次运行开始时冻结内容并保存 hash。

## 15. 失败策略

必须 fail closed：

- 无 PMID；
- PMID 未请求或重复；
- PMID 缺失；
- 文档无摘要且无全文；
- PMID 文本无法定位；
- quote 不是逐字原文；
- span 越界；
- 模型修改实体；
- 模型引用未知来源；
- 模型把研究关系升级成因果或疗效；
- qualifier 没有证据；
- support/contradict 无法解释；
- 竞争集不完整；
- corpus/snapshot hash 不一致。

## 16. 测试策略

### 16.1 单元测试

- EFetch 多段 AbstractText；
- 无摘要；
- 多 PMID；
- 缺失/额外 PMID；
- span 边界；
- quote hash；
- 同文本稳定 ID；
- 文本变化后旧 span 失效；
- malformed XML；
- timeout/rate limit/network/provider error。

### 16.2 Fixture 测试

- 摘要明确支持 therapeutic research；
- 摘要只说共同出现，不支持 therapeutic；
- 摘要支持 marker/mechanism；
- 摘要报告相反结果；
- 摘要没有人群信息，模型不得补；
- 动物实验不得写成患者；
- CTD direct 与 PubMed 不一致；
- 多篇文献冲突。

### 16.3 BioDSA adapter 测试

- abstract-only；
- PMC full text；
- PubTator full text；
- empty/failed；
- fixed snapshot/hash；
- 不依赖 agent wrapper；
- 不把 sandbox 输出当成证据。

### 16.4 真实 smoke

至少运行：

- 一个 CTD direct therapeutic 候选；
- 一个 marker/mechanism 候选；
- 一个无摘要 PMID；
- 一个冲突/不支持案例；
- 一个未知 PMID。

真实 smoke 只证明链路可用，不自动代表完整 pilot 验收。

## 17. 实施顺序

### Step A：PrimeKG ↔ CTD identity binding

- 固定 PrimeKG、MONDO/MeSH 和 CTD mapping snapshot；
- 以 PrimeKG canonical entity 为主键；
- 建立 resolved/ambiguous/unmapped/provider_failed；
- 同名异实体不得合并；
- 所有正式 claim 保留 PrimeKG object ID；
- CTD DiseaseID 只写入 hidden lineage。

### Step B：固定 PubMed adapter

- 新增 PubMed document/corpus 契约；
- 接入现有 `PubMedClient`；
- 可选接入 BioDSA full-text port；
- 保存 hash 和 manifest。

### Step C：结构化抽取

- 逐 PMID 调用受约束模型；
- 输出 verdict、quote、span 和 qualifiers；
- 未知/缺失 PMID 直接拒绝。

### Step D：确定性验证与融合

- 验证 span/hash；
- 生成支持、反对、上下文和不确定 evidence；
- CTD/PubMed 分层；
- 把已验证 evidence 重新绑定到 PrimeKG canonical claim；
- 冲突和语义升级门禁。

### Step E：接入 PrimeKG+CTD service

- PrimeKG 生成候选和 canonical entities；
- mapping 解析对应 CTD records；
- 只有 PubMed 验证通过后才构造 ValidatedTask；
- 发布前回到完整 PrimeKG 世界验证 relation/path 和 cardinality；
- 保留当前 CTD-only release 作为审计对照，不进入正式 public data。

### Step F：真实 PubMed smoke

- 使用固定 PrimeKG、CTD 和 mapping snapshots；
- 固定 PMID corpus；
- 保存查询、响应、hash、模型配置和 prompt 版本；
- 报告 passed/failed/not reproducible，不把网络失败写成通过。

## 18. 验收门槛

该阶段完成必须满足：

1. 候选实体和最终答案都来自 PrimeKG canonical IDs；
2. PrimeKG↔CTD 映射有 resolved/ambiguous/unmapped 状态；
3. CTD 引用 PMID 全部有确定状态；
4. 每个 supporting span 能从固定文献重新读取；
5. quote hash、document hash、corpus manifest 全部一致；
6. 模型无法引用未知 PMID 或伪造 quote；
7. CTD direct 不被 inference-only 替代；
8. therapeutic 不被升级为疗效；
9. qualifier 没有原文支持时不进入题面；
10. support/contradict/context/uncertain 分层保存；
11. 已验证证据重新绑定到 PrimeKG canonical answer ID；
12. 固定 PrimeKG snapshot 中 entity、relation/path 和完整候选集均通过；
13. 同一证据重放结果稳定；
14. 竞争集不完整时禁止发布；
15. 题目语义回验通过；
16. public/hidden 数据物理隔离。


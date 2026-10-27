# PubMed 证据验证设计

> 状态：实施设计  
> 日期：2026-10-27  
> 适用对象：CTD direct evidence 候选、PubMed 文献验证和后续题目生成

## 1. 目标

当前 `ctd-direct` 题只证明：

```text
固定 CTD snapshot 中，某个 chemical 的 DirectEvidence
只对应一个目标疾病。
```

它没有证明：

- CTD 引用的论文是否可读；
- 论文是否真的支持该关系；
- 论文中的研究条件是什么；
- CTD 标签是否被夸大；
- 能否生成自然的生物医学问题。

本设计增加 PubMed 证据验证层，把 CTD 记录从“候选”变成“有可复核文献证据的 claim”。

最终目标是：

```text
CTD direct record
  -> PubMed 固定文献
  -> 原文支持 span
  -> 规范 claim
  -> 完整竞争集
  -> ValidatedTask
  -> 自然问题
```

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
- 把已验证证据送入 CTD 竞争集验证；
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
3. **模型只能提出 verdict、claim 和 quote。**
4. **代码决定 PMID、span、hash 和引用是否真实。**
5. **所有 CTD 引用 PMID 都必须被评估，不能只挑支持文献。**
6. **未知、不可读和 provider failure 都不能当作负例或“无证据”。**
7. **题目只能从最终 ValidatedTask 生成。**
8. **题面不得依赖未公开的 CTD 子集边界制造唯一性。**

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

## 5. 数据流

```text
CTD EvidenceRecord
  |
  +--> PubMedIDs
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
          +--> CTD evidence fusion
                 +--> direct evidence layer
                 +--> PubMed evidence layers
                 +--> support/contrast/context
                 |
                 +--> CTD complete competitor set
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
- 模型不能改变 CTD ChemicalID 或 DiseaseID；
- 最终 claim 的 predicate 由受控语义映射产生；
- CTD 和 PubMed 的位置、hash、snapshot 都必须进入依赖 digest。

## 11. 竞争集验证

PubMed 只证明“某个候选是否有文献支持”，不能证明答案唯一。

最终仍要执行：

```text
CTD fixed snapshot
  -> enumerate all applicable disease competitors
  -> apply the same public relation and qualifiers
  -> matched / not_matched / ambiguous / unreadable / provider_failed / not_evaluated
```

发布门槛：

```text
result_complete == true
world_answer_ids == exactly one
所有 competitor 都有确定 outcome
所有 matched claim/evidence 均可回放
```

任何 `not_evaluated` 或 `provider_failed` 都阻止发布。

如果题面没有公开足够条件，而不能在完整 CTD/PubMed 世界中证明唯一性，应拒绝候选。

## 12. 题目生成

只有 `ValidatedTask` 能进入 Writer。

第一阶段可以允许：

```text
ctd_pubmed_therapeutic_research_disease
ctd_pubmed_marker_mechanism
ctd_pubmed_association
```

Writer 必须使用 Task 中的：

- chemical；
- disease；
- intent；
- polarity；
- public qualifiers；
- validation scope；
- answer cardinality。

示例结构：

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

### Step A：固定 PubMed adapter

- 新增 PubMed document/corpus 契约；
- 接入现有 `PubMedClient`；
- 可选接入 BioDSA full-text port；
- 保存 hash 和 manifest。

### Step B：结构化抽取

- 逐 PMID 调用受约束模型；
- 输出 verdict、quote、span 和 qualifiers；
- 未知/缺失 PMID 直接拒绝。

### Step C：确定性验证与融合

- 验证 span/hash；
- 生成支持、反对、上下文和不确定 evidence；
- CTD/PubMed 分层；
- 冲突和语义升级门禁。

### Step D：接入 CTD direct service

- `ctd-direct` 先筛候选；
- 只有 PubMed 验证通过后才构造 ValidatedTask；
- 保留当前 CTD-only release 作为审计对照，不进入正式 public data。

### Step E：真实 PubMed smoke

- 使用固定 CTD snapshot；
- 固定 PMID corpus；
- 保存查询、响应、hash、模型配置和 prompt 版本；
- 报告 passed/failed/not reproducible，不把网络失败写成通过。

## 18. 验收门槛

该阶段完成必须满足：

1. CTD 引用 PMID 全部有确定状态；
2. 每个 supporting span 能从固定文献重新读取；
3. quote hash、document hash、corpus manifest 全部一致；
4. 模型无法引用未知 PMID 或伪造 quote；
5. CTD direct 不被 inference-only 替代；
6. therapeutic 不被升级为疗效；
7. qualifier 没有原文支持时不进入题面；
8. support/contradict/context/uncertain 分层保存；
9. 同一证据重放结果稳定；
10. 竞争集不完整时禁止发布；
11. 题目语义回验通过；
12. public/hidden 数据物理隔离。


# PrimeKG + CTD + PubMed 题目验收标准

> 状态：实施依据  
> 日期：2026-10-27  
> 适用范围：以 PrimeKG 为实体世界、以 CTD/PubMed 为证据来源的正式题目

## 1. 结论标准

一道题只有同时满足以下条件才算验收通过：

```text
答案是 PrimeKG 中的真实实体
AND 题目在图中的关系/路径成立
AND 对应 CTD 记录能够唯一映射
AND CTD evidence 类型明确
AND 所有 CTD PMID 都有确定 assessment
AND supporting quote/span/hash 可回读
AND 研究条件没有被丢失或编造
AND 完整 PrimeKG 候选世界严格单答案
AND 题面与 ValidatedTask 语义等价
AND 没有答案、路线或内部数据泄漏
AND 发布文件通过 public/hidden 和版本门禁
```

题目通过自动校验后仍是：

```text
machine_validated
```

对正式 gold 发布，还必须通过对应的人工审核等级。

## 2. 当前中间产物不自动合格

以下当前路径不能直接产出正式正例：

| 路径 | 当前产物 | 验收状态 |
|---|---|---|
| `generate ctd-direct` | 用 CTD disease ID 作为答案，未读 PubMed | 仅审计/候选发现 |
| `generate validated --profile primekg-ctd` | 只用 PrimeKG `source=CTD` 标签，未读 CTD/PubMed | 仅图结构探索 |
| 固定题面模板 | 如“X 在 CTD 中对应哪种疾病” | 仅 smoke/test |

联合流程基础纵切现已实现，但生产 crosswalk、真实 PubMed corpus 和 pilot
人工盲审尚未完成。现有 CTD-only 或 PrimeKG-标签 public release 仍不得作为
本验收标准下的正式 gold 数据集。

## 3. 第一阶段允许的正式题型

```text
primekg_ctd_pubmed_therapeutic_research_disease
primekg_ctd_pubmed_marker_mechanism
primekg_ctd_pubmed_association
```

约束：

- 题目的起点、答案和约束实体必须来自 PrimeKG；
- therapeutic/association 的第一阶段答案可为 disease；
- marker/mechanism 优先以 gene/pathway 等机制实体为答案，不以“有机制证据的疾病”为答案；
- `ctd_direct_evidence_disease` 和固定 CTD lookup 模板不在正式题型中。

## 4. 实体与映射验收

| ID | 通过条件 |
|---|---|
| PCQ-I01 | 起点、答案和约束实体都使用固定 PrimeKG snapshot 中的 canonical ID |
| PCQ-I02 | 最终答案 ID 来自 PrimeKG，不从 CTD/PubMed 名称临时生成 |
| PCQ-I03 | PrimeKG entity → CTD record 的映射状态为 `resolved` |
| PCQ-I04 | 一个 PrimeKG entity 对应多个 CTD 实体时标记 `ambiguous`，不得自动选择 |
| PCQ-I05 | 无对应 CTD entity 时标记 `unmapped`，不得构造正式 claim |
| PCQ-I06 | 同名异实体不得合并；alias 合并必须有明确 ontology/record 依据 |
| PCQ-I07 | `MONDO_grouped`、疾病组和父子节点不得被无条件压成同一答案 |
| PCQ-I08 | entity、mapping 和 snapshot version 都进入依赖 digest |

## 5. PrimeKG 图验证

| ID | 通过条件 |
|---|---|
| PCQ-G01 | 题目涉及的每个 PrimeKG entity 在固定 snapshot 中存在 |
| PCQ-G02 | relation semantics 与题面表达一致 |
| PCQ-G03 | relation direction 使用固定 direction policy |
| PCQ-G04 | 直接关系题必须有对应直接边；间接路径题必须满足声明的路径长度 |
| PCQ-G05 | 完整分页枚举，无静默截断 |
| PCQ-G06 | 同一答案的多条路径可以保留 lineage，但答案实体去重 |
| PCQ-G07 | source filter 必须属于公开任务语义，不能用于隐藏造唯一答案 |
| PCQ-G08 | 题面未说明 source 时，不得静默排除 MONDO/MONDO_grouped/其他来源 |

“完整”只相对于 `ValidationEnvironment` 中声明的 PrimeKG snapshot、relation、
direction、source filter 和 query scope，不表示现实世界绝对完整。

## 6. CTD 证据验收

| ID | 通过条件 |
|---|---|
| PCQ-C01 | CTD chemical 与 PrimeKG chemical 通过 CTD ChemicalID 精确对齐 |
| PCQ-C02 | CTD disease 与 PrimeKG answer 通过 mapping snapshot 对齐 |
| PCQ-C03 | CTD record 保存 snapshot、locator、content hash 和 PubMedIDs |
| PCQ-C04 | `DirectEvidence` 与 inference 分层保存 |
| PCQ-C05 | inference-only 不能支持 therapeutic、直接机制、风险方向或疗效题 |
| PCQ-C06 | therapeutic 最强只允许“被研究用于/治疗研究中被评估” |
| PCQ-C07 | marker/mechanism 不得升级为因果、预防或疗效 |
| PCQ-C08 | CTD 中已知多疾病关系不得靠静默选择一条记录变成单答案 |
| PCQ-C09 | CTD mapping 失败、记录缺失或提供者失败均不能当作负例 |

## 7. PubMed 证据验收

| ID | 通过条件 |
|---|---|
| PCQ-P01 | 评估 CTD 给出的全部 PMID，不只保留支持文献 |
| PCQ-P02 | 每个 PMID 有 `supports/contradicts/context_only/uncertain` 之一 |
| PCQ-P03 | 正式支持证据有可回读的逐字 quote 和字符 span |
| PCQ-P04 | PMID、document hash、section hash、quote hash 和 snapshot 全部一致 |
| PCQ-P05 | 模型引用未知 PMID、伪造 quote 或越界 span 时 fail closed |
| PCQ-P06 | 标题、搜索 snippet 或无定位摘要不能作为正式 support |
| PCQ-P07 | population、species/model、study type、dose、time 等 qualifier 必须有原文依据 |
| PCQ-P08 | 模型不能补写原文没有的人群、剂量、时间或临床结论 |
| PCQ-P09 | support 与 contradict 无法通过 qualifier 解释时标记 conflicted |
| PCQ-P10 | timeout、rate limit、network/provider failure、空结果和无摘要必须区分 |
| PCQ-P11 | 相同 PMID/snapshot/text 重放得到稳定 document/span/claim ID |

## 8. 答案与竞争集验收

| ID | 通过条件 |
|---|---|
| PCQ-A01 | 从固定 PrimeKG snapshot 枚举题目 scope 内全部候选答案 |
| PCQ-A02 | 每个候选有明确 outcome：matched/not_matched/ambiguous/unreadable/provider_failed/not_evaluated |
| PCQ-A03 | 任一 not_evaluated、ambiguous 或 provider_failed 都使 `result_complete=false` |
| PCQ-A04 | 单数题最终 `world_answer_ids` 严格为 1 个 PrimeKG entity |
| PCQ-A05 | Candidate 提议答案与 world answer 不一致时拒绝 |
| PCQ-A06 | 已知多答案不得静默保留一个 |
| PCQ-A07 | 删除关键条件后答案集变化，证明该条件有判别价值 |
| PCQ-A08 | 候选路径存在只是必要条件，不是唯一性证明 |
| PCQ-A09 | 双实体题必须检查另一个实体是否存在、路径是否成立，以及全部竞争实体 |
| PCQ-A10 | 任何未映射到 CTD 且无法完成 PubMed 验证的竞争者都会阻止发布 |

## 9. 题面语义验收

| ID | 通过条件 |
|---|---|
| PCQ-Q01 | Writer 只接收 ValidatedTask |
| PCQ-Q02 | 题面保留相同 PrimeKG entity identities |
| PCQ-Q03 | 题面保留相同 predicate/intent、polarity 和 evidence strength |
| PCQ-Q04 | 题面公开所有承担消歧作用的 study qualifiers |
| PCQ-Q05 | 单数问法只对应 exactly_one |
| PCQ-Q06 | therapeutic 不得改写成有效治疗、治愈或预防 |
| PCQ-Q07 | marker/mechanism 不得改写成因果结论 |
| PCQ-Q08 | 题面不出现 CTD/PubMed 字段、PMID、claim/evidence ID 或生成路线 |
| PCQ-Q09 | 题面不出现答案 canonical ID、名称、alias 或可逆描述 |
| PCQ-Q10 | 题面不出现“当前证据范围”“根据现有资料”等未公开范围 |
| PCQ-Q11 | 题面不出现“某种证据对应什么疾病”等 evidence metadata trivia |
| PCQ-Q12 | 题面不得残留 A、B、S、D、某疾病、特定人群等占位符 |
| PCQ-Q13 | 题面独立可读，不依赖上一段、表格或内部记录 |
| PCQ-Q14 | 独立 verifier 能恢复 entity、predicate、polarity、qualifier、scope、cardinality |
| PCQ-Q15 | verifier 不确定或不一致时拒绝，不能反向修改 gold task |

## 10. Writer 与多样性验收

| ID | 通过条件 |
|---|---|
| PCQ-W01 | 正式 Writer 不依赖单条程序固定题面 |
| PCQ-W02 | Prompt version、model config、style profile 和 hash 进入 manifest |
| PCQ-W03 | 同一 Task 可生成多个自然候选，但语义不变 |
| PCQ-W04 | 单题答案泄漏、内部术语、占位符检查为 0 |
| PCQ-W05 | 数据批次报告模板占比和近重复率 |
| PCQ-W06 | 不在 pilot 阶段用单一句式占满某一题型 |
| PCQ-W07 | 风格差异不能改变研究范围、答案集合或证据强度 |

固定模板仅允许用于：

- 测试 fixture；
- 历史兼容路径；
- 明确标记的 development smoke；
- 模型不可用时的失败诊断。

固定模板不得作为正式 gold 的唯一 Writer。

## 11. 正例结构

这些是语义结构，不是程序必须拼接的固定字符串。

### 11.1 Therapeutic research

```text
在[有原文支持的研究人群或模型]中，
[有原文支持的暴露/干预和时程]下，
[PrimeKG chemical] 被研究用于改善哪种疾病？
```

合格前提：

- CTD mapping resolved；
- CTD therapeutic record；
- PubMed 原文支持“被研究用于”；
- qualifier 完整；
- PrimeKG 完整候选集唯一；
- 题面没有暗示临床疗效。

### 11.2 Marker/mechanism

```text
在[有原文支持的实验/研究条件]中，
哪项 marker、基因或通路被研究为与
[PrimeKG chemical] 和 [PrimeKG disease] 的关系相关？
```

注意：

- 疾病不能仅因为“有机制证据”而成为题目答案；
- 机制题优先让 gene/pathway 作为答案；
- 因果和唯一机制声明需要更强证据。

### 11.3 结构化实体约束

```text
哪个 PrimeKG 实体同时满足：
[证据支持的 relation A]
AND
[证据支持的 relation B]？
```

合格前提：

- 两个 relation 都有语义；
- 两个条件都影响答案集；
- 完整 PrimeKG 候选集已评估；
- 不是把生成路径直接写成 Planner 正确动作。

## 12. 必须拒绝的题

```text
triphenyl phosphate 在 CTD 记录中被研究用于哪种疾病？
```

拒绝原因：

- 主要语义是 CTD 字段查询；
- 没有 PubMed 原文验证；
- 题面价值是数据库 metadata trivia。

```text
在整张 PrimeKG 中，与 chemical X 暴露相关且与 protein P 相关的疾病是哪一种？
```

如果未枚举完整候选集，拒绝。

```text
哪种疾病具有 chemical X 的直接机制研究证据？
```

拒绝原因：

- 问的是 evidence metadata，不是生物医学机制；
- 不应以 disease 为答案。

```text
chemical X 能够有效治疗 disease Y。
```

除非存在独立、足够强的疗效证据契约，否则拒绝。

```text
在特定人群中，chemical X 与 disease Y 相关。
```

如果 population 没有原文支持，拒绝。

```text
与 chemical X 暴露相关，并且与 protein P 相关的疾病是哪一种？
```

如果 world answers 包含两个疾病，或者存在未评估竞争者，拒绝。

## 13. 自动验收与人工验收

### 13.1 自动验收

必须由代码完成：

- entity/mapping closure；
- graph traversal；
- PMID coverage；
- span/hash；
- conflict state；
- cardinality；
- leakage；
- placeholder；
- schema；
- public/hidden isolation。

### 13.2 人工验收

必须由医学/产品审阅者判断：

- 题目是否自然、独立可读；
- 论文是否真的支持该关系；
- 研究条件是否被正确理解；
- 题面是否存在医学误导；
- 题目是否具有信息价值；
- 竞争答案是否被合理处理。

### 13.3 Pilot 通过阈值

真实 pilot：

- 错误答案：0；
- 伪造证据：0；
- 语义夸大：0；
- 已知多答案：0；
- 答案泄漏：0；
- 隐藏 source/scope 制造唯一：0；
- 自然且明确：至少 90%；
- 被认为具有实际信息价值：至少 80%。

未达到阈值时，状态写“pilot 未通过”，不得通过修改阈值规避。

## 14. 必需 reason code

```text
primekg_entity_missing
primekg_snapshot_mismatch
primekg_relation_mismatch
primekg_path_mismatch
primekg_candidate_universe_incomplete
ctd_mapping_ambiguous
ctd_mapping_unmapped
ctd_direct_evidence_missing
ctd_inference_only
pubmed_document_missing
pubmed_record_unreadable
pubmed_span_invalid
pubmed_quote_mismatch
pubmed_contradiction
pubmed_assessment_incomplete
qualifier_not_supported
claim_strength_exaggerated
unknown_competitor
cardinality_mismatch
hidden_scope_dependency
missing_public_disambiguator
database_metadata_question
answer_leakage
question_semantics_mismatch
uninstantiated_placeholder
template_concentration_exceeded
```

## 15. 必测 fixtures

1. PrimeKG answer 严格单答案正例；
2. 两个 PrimeKG 合法答案；
3. answer 不存在于 PrimeKG；
4. relation direction 错误；
5. source filter 隐藏后导致伪唯一；
6. CTD mapping resolved；
7. CTD mapping ambiguous；
8. CTD direct evidence；
9. CTD inference-only；
10. PubMed 明确支持；
11. PubMed 只说共同出现；
12. PubMed 相反结论；
13. quote/span/hash 错误；
14. qualifier 缺失；
15. qualifier 无原文支持；
16. therapeutic 被升级为疗效；
17. answer alias 泄漏；
18. 固定 CTD 字段题；
19. template-only question；
20. public/hidden 泄漏。

## 16. 发布完成定义

一道题可进入正式 release 必须同时具有：

```text
PrimeKG canonical answer
PrimeKG graph/path validation
resolved CTD mapping
CTD direct evidence lineage
complete PubMed corpus
verified PubMed spans
complete PrimeKG competitor outcomes
ValidatedTask
prompt-generated QuestionVariant
passed semantic reconstruction
passed leakage/diversity gates
```

缺少任一项时：

```text
not_accepted
```

不得写“部分通过”后进入正式 public 数据集。


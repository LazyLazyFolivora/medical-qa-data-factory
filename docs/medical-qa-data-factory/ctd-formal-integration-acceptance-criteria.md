# CTD 正式接入验收标准

> 状态：待实施
>
> 更新日期：2026-10-27
>
> 适用范围：第一阶段 `chemical/drug -> disease` 的 `evidence_qualified_entity` 题型

## 1. 验收结论定义

只有满足本文全部 P0 条件，才可以声明：

> CTD 已正式接入 `medical-qa-data-factory`，能够生成 CTD/PubMed 支持、在声明验证世界中严格单答案、可审计发布的 chemical/drug -> disease 问题。

以下情况不能称为“正式接入”：

- CTD index 可以 lookup，但结果没有进入 `ValidatedTask`；
- 旧 `generate entity` 可以生成问题，但没有经过 task-first Validator；
- 只验证一对实体，没有检查完整竞争答案集合；
- 只保存摘要文本，没有支持 span、hash 和 snapshot；
- 只完成 fake fixture，没有真实 CTD/PubMed pilot；
- 问题能生成，但 public/hidden 没有隔离。

## 2. P0：数据源与映射

| ID | 通过条件 |
|---|---|
| CTD-D01 | CTD source file、release/schema、SHA-256、row count 和 index manifest 可复核 |
| CTD-D02 | sidecar 只读；构建和查询不修改 CTD 或 PrimeKG 原始数据 |
| CTD-D03 | 按 chemical ID 枚举结果无静默 limit、无静默分页截断、排序确定 |
| CTD-D04 | 同一 canonical disease 的别名记录合并；同名异实体不合并 |
| CTD-D05 | 每条待审计 PrimeKG CTD 边都有明确映射状态，不允许静默丢失 |
| CTD-D06 | ambiguous、unmapped 和 crosswalk 失败均进入明确 reason code |
| CTD-D07 | CTD record 可以通过 snapshot + stable locator + content hash 独立重建 |

自动化负例至少覆盖：重复记录、同名异实体、缺失 DiseaseID、名称归一化碰撞、旧 index schema 和 source hash mismatch。

## 3. P0：PubMed 证据可复核性

| ID | 通过条件 |
|---|---|
| CTD-P01 | 每条正式 literature evidence 具有 PMID、snapshot/corpus manifest、content hash 和 retrieved_at |
| CTD-P02 | 支持结论具有合法字符 span，read-back 与 quote hash 一致 |
| CTD-P03 | 模型引用未知 PMID、未知文档、越界 span 或伪造 quote 时 fail closed |
| CTD-P04 | provider timeout、rate limit、网络失败、空结果和无摘要是不同状态 |
| CTD-P05 | provider failure 不能被解释为“无竞争答案”或“没有冲突证据” |
| CTD-P06 | 相同 snapshot 和相同文本重放产生相同 evidence ID |

只有标题、只有不可定位摘要、只有搜索 snippet 的 observation 可以用于探索，但不能作为正式支持证据。

## 4. P0：Claim 归一化与证据融合

| ID | 通过条件 |
|---|---|
| CTD-C01 | 每个 eligible claim 都有 subject、predicate、object、qualifier、supporting evidence 和 normalizer version |
| CTD-C02 | CTD 与 PubMed observation 先转换为 source-neutral claim，再进入 requirement matcher |
| CTD-C03 | association 不得升级成 causality、prevention 或 efficacy |
| CTD-C04 | `therapeutic` 最强只允许生成“治疗研究/被研究用于”，除非独立契约未来明确支持更强结论 |
| CTD-C05 | risk increase/decrease 必须由可定位 PubMed span 明确支持，不能只从 CTD 字段推断 |
| CTD-C06 | population、study type、dose/exposure、time 等会改变结论的 qualifier 不得丢失 |
| CTD-C07 | support 与 contradict 同时存在且无法按 qualifier 解释时，状态为 conflicting，不得发布 |
| CTD-C08 | inference-only 记录不能生成直接机制、风险方向或疗效问题 |
| CTD-C09 | 模型只能从 allow-list 选择 intent/polarity；未知值和不一致引用被拒绝 |
| CTD-C10 | 相同固定 evidence bundle 重放得到相同 eligible/rejected 结果 |

允许第一阶段发布的 intent 仅为：

- `association_research`；
- `mechanism_research`；
- `therapeutic_research`；
- `risk_increase`；
- `risk_decrease`。

## 5. P0：完整竞争集与答案正确性

这是 CTD 题型的核心门禁。

| ID | 通过条件 |
|---|---|
| CTD-A01 | Validator 从固定 CTD snapshot 枚举 query scope 内全部 disease 候选，而不是使用 Candidate 提供的局部列表 |
| CTD-A02 | 所有可能满足题面语义的竞争项均有持久化 outcome |
| CTD-A03 | `matched`、`not_matched`、`ambiguous`、`unreadable`、`provider_failed`、`not_evaluated` 等状态可区分 |
| CTD-A04 | 只有不存在 unknown competitor 时 `result_complete=true` |
| CTD-A05 | 正式题的 canonical `world_answer_ids` 严格为一个 |
| CTD-A06 | Candidate proposed answer 与权威 world answer 不一致时拒绝 |
| CTD-A07 | 已知多答案不得静默保留一个；必须增加真实限定、改契约或拒绝 |
| CTD-A08 | scope statement 明确限定 CTD/PubMed 版本和 query scope，不宣称现实世界绝对唯一 |
| CTD-A09 | qualifier/category 约束必须有证据且有判别价值；删除关键约束后应扩大答案集或改变匹配结果 |
| CTD-A10 | provider failure、映射歧义、证据截断和预算耗尽均不能生成 passing report |

必须具备以下自动化 fixtures：

1. 严格单答案正例；
2. 两个合法 disease 的多答案反例；
3. 第二候选未完成 PubMed 回读的 unknown competitor 反例；
4. disease alias 合并后仍为单答案；
5. 同名异 disease 不能合并；
6. 删除约束后仍单答案的 shortcut 反例；
7. candidate answer 与 world answer 不一致；
8. CTD snapshot 与 Task snapshot 不一致。

## 6. P0：Task 与完成条件

| ID | 通过条件 |
|---|---|
| CTD-T01 | 每题至少有一个机器可判定的 `InformationRequirement` |
| CTD-T02 | requirement 使用 source-neutral claim pattern，不写 `search_ctd`、`search_pubmed` 等动作名 |
| CTD-T03 | 至少一个 `AcceptableEvidenceSet` 可由已验证 claim 满足 |
| CTD-T04 | 存在显式、确定性的 `completion_rule` |
| CTD-T05 | `ValidationReport` 保存 matched claims、supporting evidence、competitor report 和 dependency digest |
| CTD-T06 | `ValidatedTask` 保存 gold claim/evidence refs，并校验 report、query、environment 和 dependency digest 一致 |
| CTD-T07 | 给定同一 EvidenceStore，requirement matcher 的 satisfied set 与 completion state 完全确定 |
| CTD-T08 | 任一 gold claim/evidence 缺失、hash 变化或快照失效时，任务不能继续视为有效 |

最低 completion 语义为：

```text
存在一个 AcceptableEvidenceSet，且其全部 required requirements 被满足；
答案合同匹配 exactly_one；
验证报告完整且依赖未失效。
```

## 7. P0：题面语言与语义等价

| ID | 通过条件 |
|---|---|
| CTD-Q01 | Writer 只接收 `ValidatedTask`，不能接收未验证 Candidate |
| CTD-Q02 | 题面中的 chemical、disease category、predicate、polarity 和 qualifier 与 Task 一致 |
| CTD-Q03 | 单数问法只对应 `exactly_one` |
| CTD-Q04 | Writer 不得把“研究相关”改成“导致”，不得把“治疗研究”改成“有效治疗” |
| CTD-Q05 | 题面不泄漏答案 canonical ID、名称、别名或可逆占位符 |
| CTD-Q06 | 题面不出现内部 claim/evidence ID、生成路线或隐藏 rubric |
| CTD-Q07 | 题面独立可读；不得依赖“上文”“该记录”或未公开表格 |
| CTD-Q08 | 数据库字段翻译题和低价值 `CTD 显示……` 问法被拒绝 |
| CTD-Q09 | semantic verifier 能恢复相同实体、predicate、polarity、qualifier、scope 和 cardinality |
| CTD-Q10 | semantic verifier 不确定或不一致时拒绝，不能反向修改 gold task 适配题面 |

### 7.1 可通过的结构示例

示例只代表语言结构：

```text
在当前纳入的研究证据中，哪种癌症与化学物 A 暴露后的风险降低相关？
```

通过前提：`risk_decrease`、癌症分类和其他 qualifier 均被证据支持，且完整竞争集严格单答案。

```text
在当前证据范围内，哪种神经系统疾病具有关于化学物 A 的直接机制研究证据？
```

通过前提：`mechanism_research` 不被写成确定因果机制，答案严格唯一。

### 7.2 必须拒绝的结构示例

```text
化学物 A 与哪种疾病有关？
```

拒绝：开放、多答案风险高、限定不足。

```text
化学物 A 是否能预防疾病 B？
```

拒绝：boolean 契约未启用，且可能发生临床结论升级。

```text
CTD 中 therapeutic 字段对应的是哪种疾病？
```

拒绝：数据库 trivia，不是目标生物医学问题。

## 8. P0：发布隔离和可重放性

| ID | 通过条件 |
|---|---|
| CTD-R01 | `public/planner_questions.jsonl` 不含 answer、requirements、completion rule、claims、evidence 或 competitor outcomes |
| CTD-R02 | `public/dataset_manifest.json` 只列 public 文件，不引用 hidden 路径 |
| CTD-R03 | hidden view 包含 evaluation target、validated task、validation report、evidence catalog、rejected candidates 和 quality report |
| CTD-R04 | hidden evidence catalog 能解析所有 gold claim/evidence refs |
| CTD-R05 | release manifest 固定代码、schema、CTD、PrimeKG、PubMed corpus、模型、prompt 和 validator 版本 |
| CTD-R06 | 任何 manifest hash 不匹配都会阻止 replay 通过 |
| CTD-R07 | 输出目录不可覆盖；相同输入重放产生语义等价结果和稳定依赖 digest |
| CTD-R08 | 生成阶段的检索顺序不作为 Planner 正确动作标签发布 |

最低输出物：

```text
public/
  planner_questions.jsonl
  dataset_manifest.json
hidden/
  evaluation_targets.jsonl
  validated_tasks.jsonl
  validation_reports.jsonl
  evidence_catalog.jsonl
  competitor_reports.jsonl      # 可合并进 validation_reports
  generation_episodes.jsonl
  rejected_candidates.jsonl
  quality_report.json
  dataset_manifest.json
```

## 9. P0：停止语义与失败恢复

| ID | 通过条件 |
|---|---|
| CTD-S01 | `required_searches_completed` 仅在声明的 CTD 枚举和必需 PubMed read-back 全部完成后使用 |
| CTD-S02 | 目标题均通过最终验证后才能使用 `question_targets_met` |
| CTD-S03 | `budget_exhausted` 保留尚未评估竞争项，不能伪装成搜索完成 |
| CTD-S04 | 必需 CTD/PubMed 服务失败时使用 `provider_unavailable`，不能发布依赖该服务的题 |
| CTD-S05 | 针对候选的补搜记录明确 EvidenceGap：claim support、contradiction、qualifier、entity resolution 或 answer completeness |
| CTD-S06 | episode 结束时使用最终 EvidenceBundle 对全部拟发布任务统一重验 |

## 10. P0：自动化测试门槛

必须通过：

1. CTD parser/index/mapping 单元测试；
2. PubMed read-back/span/hash 单元测试；
3. claim normalizer/fusion/semantic gate 单元测试；
4. candidate source 与完整竞争集测试；
5. Task contract、requirement matcher 与 promotion 测试；
6. Writer/semantic verifier/leakage 测试；
7. release public/hidden 隔离测试；
8. 旧 PrimeKG constrained task 全量回归；
9. 真实 CTD + 少量真实 PMID integration smoke；
10. 默认完整测试集。

建议命令：

```powershell
D:\Anaconda\envs\qa_env\python.exe -m pytest `
  tests/test_ctd_index.py `
  tests/test_pubmed_evidence.py `
  tests/test_ctd_evidence_fusion.py `
  tests/test_ctd_world_evaluation.py `
  tests/test_ctd_task_service.py `
  tests/test_ctd_task_release.py -q

D:\Anaconda\envs\qa_env\python.exe -m pytest -q
```

真实 provider 测试应使用单独 marker，报告时必须区分 passed 与 deselected；不得把未运行写成通过。

## 11. P0：真实数据 pilot

pilot 至少包含：

- 20 个不同 chemical/drug seeds；
- 至少 5 个能够形成正式发布题的正例，用于证明纵切可用；
- 多答案、无答案、冲突、inference-only、映射歧义、无法回读和 provider failure 负例；
- 至少一个高关联度 chemical，用于验证完整枚举和性能；
- 至少一个 alias/crosswalk 案例。

这里的“至少 5 个正例”不是要求降低门禁凑题；如果真实数据找不到 5 个合格题，结论应为产品纵切暂不可用，而不是修改 gold 或忽略竞争答案。

质量报告至少包含：

- seed 数、CTD record 数和 canonical disease 数；
- Candidate 数、ValidatedTask 数和发布数；
- 各 reason code 拒绝数量；
- unknown competitor 数；
- 各 intent/polarity/qualifier 分布；
- 候选到发布的漏斗；
- 模板占比和近重复率；
- 每题证据数量与 provider 分布；
- provider failure、延迟和预算消耗；
- snapshot、manifest 和代码 commit。

## 12. P0：人工盲审

对全部 pilot 发布题进行盲审；若后续题量扩大，则按 intent 和来源分层抽样。

每题检查：

1. 问题是否自然、明确、独立可读；
2. gold answer 是否正确；
3. CTD record 与 PubMed span 是否真正支持 claim；
4. 是否丢失 population、dose、time 或 study type；
5. 是否把关联夸大成因果、预防或疗效；
6. 是否存在已知但遗漏的竞争答案；
7. 题目是否具有生物医学信息价值，而非数据库字段 trivia；
8. Planner 视图是否泄漏答案或路线。

通过阈值：

- 错误答案、伪造证据、语义夸大、已知多答案、答案泄漏：`0`；
- 自然且明确：至少 `90%`；
- 被审阅者认为具有实际信息价值：至少 `80%`；
- 所有失败项都有 reason code 或新增回归测试，不允许只手工删除样本。

## 13. 必需拒绝码

除现有通用拒绝码外，至少支持：

```text
ctd_record_unmapped
ctd_mapping_ambiguous
ctd_snapshot_mismatch
ctd_inference_only
pubmed_record_unreadable
pubmed_span_invalid
unknown_pmid_reference
claim_not_supported
claim_semantics_mismatch
conflicting_evidence_unresolved
qualifier_not_supported
unknown_competitor
cardinality_mismatch
scope_not_closed
shortcut_detected
evidence_dependency_mismatch
question_semantics_mismatch
answer_leakage
database_trivia
```

reason code 必须区分数据本身不合格、provider failure、实现错误和预算停止。

## 14. P1：接入完成后的扩展

以下不阻塞第一阶段 P0：

- `disease -> chemical/drug` 反向实体题；
- CTD inference gene + PrimeKG 的多跳问题；
- 更多 CTD 数据表；
- 多篇文献的更精细研究质量分层；
- Europe PMC、DrugBank 等其他 evidence provider；
- EvidenceGap 自动优先级和搜索饱和判断；
- 更丰富的自然语言风格；
- 大规模长期缓存、增量失效和 checkpoint。

P1 扩展必须复用同一 `EvidenceRecord + NormalizedClaim + InformationRequirement`
体系，不能产生 CTD 专用评估逻辑分叉。

## 15. 最终验收清单

发布负责人必须逐项确认：

- [ ] CTD 完整候选枚举和映射审计通过；
- [ ] PubMed span/hash/snapshot 可独立回读；
- [ ] source-neutral claim 与 requirement matcher 已接通；
- [ ] 所有竞争答案有确定 outcome；
- [ ] passing report 的 `result_complete=true` 且严格单答案；
- [ ] claim/evidence/completion rule 已进入 hidden EvaluationTarget；
- [ ] Writer 和 semantic verifier 通过；
- [ ] public/hidden 物理隔离通过；
- [ ] 默认回归与真实 provider smoke 通过；
- [ ] 20-seed pilot 和质量报告完成；
- [ ] 人工盲审达到阈值；
- [ ] release 绑定 Git commit 和全部 source manifests；
- [ ] 不适用 CTD 的现有正式题型无回归。

任一项未完成时，状态应写为“CTD 正式接入实施中”，不能写为“已完成”。

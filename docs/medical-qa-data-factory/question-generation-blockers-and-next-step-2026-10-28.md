# 题目生成阻塞问题诊断与下一步建议

> 文档类型：实现诊断与决策记录  
> 日期：2026-10-28  
> 适用范围：Entity Question Generation、PrimeKG + CTD + PubMed 正式生成链路  
> 当前结论：系统已经能够生成候选题并完成验证；发布数为 0 的核心原因已经从“流程故障”转变为“题型与任务价值门禁不匹配”。

## 1. 执行摘要

当前链路已经能够完成：

```text
PrimeKG 候选发现
  -> CTD 身份映射与直接证据检查
  -> PubMed 全文献获取
  -> 逐字引文与字符 span 验证
  -> 结构化 claim 融合
  -> TaskCandidate / TaskSpec
  -> 题面生成
  -> 完整竞争集与语义验证
  -> 发布或带原因拒绝
```

在真实运行中，`1-methylphenanthrene -> asthma` 已经产生题目并完成最终验证：

- `candidate_count = 1`；
- `validation_report_count = 1`；
- `validation_complete = true`；
- 最终因 `shortcut_detected` 未发布。

因此，“无法生成题目”已经不是准确描述。更准确的状态是：

> 系统能够生成题目，但当前正式题型以疾病实体为答案，在真实数据中经常可被 PrimeKG 单边直接回答，无法证明文献限定条件具有任务价值，因此被质量门禁拒绝。

## 2. 问题分层

### 2.1 状态与停止原因曾经失真

原流程会把“没有提出候选题”和“所有候选均被硬拒绝”统一记录为：

```text
all_candidates_hard_rejected
```

这会掩盖真实失败阶段，使调试者误以为题目已经进入审核。

已完成修正：

- 新增 `no_candidate_proposed`；
- 保留并区分 `all_candidates_hard_rejected`；
- 保留内部 ReAct 的真实停止原因；
- 区分 `question_generated`、`generation_stopped`、`generation_failed`；
- 有已接受题目时记录 `evidence_sufficient`；
- 对仍需证据的候选保留 `budget_exhausted`、`evidence_exhausted` 等原因。

该问题属于可观测性缺陷，不是出题能力本身的缺陷。

### 2.2 PubMed 与模型供应商不稳定

真实运行中观察到：

- `pubmed_network_error`；
- HTTP 502：`Upstream service temporarily unavailable`；
- 代理连接断开；
- 大文献集请求在模型端超时或断开；
- 原适配器将所有非 401/403/429 的 HTTP 错误归为不可重试的 `model_internal_error`。

已完成修正：

- 本次运行通过进程级 `HTTP_PROXY`、`HTTPS_PROXY` 使用 `http://127.0.0.1:7890`；
- 新增 `model_provider_unavailable`；
- 5xx 被标记为可重试供应商故障；
- 网络错误、超时、429 和 5xx 使用 `max_retries` 自动重试；
- 网络重试不会被错误标记为 Schema repair；
- 每次物理调用仍保留独立账本身份。

仍存在的容量问题：

- Rotenone -> Parkinson disease 包含 22 篇 CTD 引用文献；
- 当前实现要求单次模型调用评估全部 PMID；
- 当前供应商在该请求规模下连续出现网络断开；
- 后续需要实现“分批提取、确定性合并、全 PMID 完整性校验”，不能简单截断文献。

### 2.3 模型无法稳定生成精确 PubMed 引文位置

原设计要求模型同时完成：

1. 从摘要中选择证据；
2. 逐字抄写 quote；
3. 计算字符 `start/end`；
4. 填写 PMID 和语义判断。

实际产生：

- `pubmed_quote_mismatch`；
- Unicode 标点或空白导致 offset 偏移；
- 模型轻微改写摘要原文；
- 引文存在但字符位置错误。

已完成修正：

- 程序预先把固定 PubMed 摘要切分为精确 span candidate；
- 每个候选包含 `candidate_id / pmid / text_field / start / end / quote`；
- 模型只选择候选并进行语义判断，不再自行计算字符位置；
- 对“逐字一致且在摘要中唯一出现”的 quote，程序可安全重锚定错误 offset；
- 改写文本、重复文本、未知 PMID、越界 span 继续 fail closed；
- quote hash、document hash、snapshot 和 read-back 校验保持不变。

该修正没有降低证据门槛，只把机械字符定位从模型职责移回确定性程序。

### 2.4 Golden Case 选择曾依赖逐个试错

部分种子虽然在 PrimeKG 中存在 CTD 来源边，但不满足正式发布所需的全部条件。

本轮已改为对以下三类数据做本地交集：

```text
PrimeKG source=CTD 的 exposure_disease 边
AND 冻结 identity snapshot 中 fully_mapped 的 chemical
AND CTD 中存在 marker/mechanism 直接证据和 PMID
```

交集得到 38 条真实候选。候选数量不等于可发布数量，仍需 PubMed claim 与任务价值验证。

## 3. 真实运行结果

| 候选 | 运行结果 | 主要原因 | 结论 |
|---|---|---|---|
| triphenyl phosphate -> hyperglycemia | 0 发布 | `no_supporting_pubmed_evidence` | CTD 有直接记录，但 PubMed 摘要没有形成可接受的直接支持 |
| Methylene Chloride -> ALS | 0 发布 | `pubmed_no_abstract`、`unknown_competitor` | 文献与竞争集不完整 |
| Beryllium -> chronic beryllium disease | CLI 前置失败 | identity snapshot 未覆盖该 chemical 的全部 PrimeKG 疾病答案 | 不满足完整竞争集要求 |
| Aflatoxin B1 -> hepatocellular carcinoma | CLI 前置失败 | identity snapshot 未覆盖全部答案 | 不适合作为当前冻结世界 Golden Case |
| Aspirin -> prostate cancer | 0 发布 | `ctd_direct_evidence_missing` | CTD 只有 inference 记录，没有要求的直接证据 |
| Rotenone -> Parkinson disease | 0 发布 | `model_network_error` | 22 篇文献的单请求超过当前供应商稳定容量 |
| propylene dichloride -> cholangiocarcinoma | 0 发布 | `mixed_supporting_claims` | 4 篇文献支持的 claim 范围或限定条件不一致 |
| 1-methylphenanthrene -> asthma | 生成并完成验证，0 发布 | `shortcut_detected` | 题目可以不读文献、直接通过 PrimeKG 单答案边回答 |

这些拒绝多数是合理的质量门禁行为，不能通过降低阈值解决。

## 4. 核心产品矛盾

### 4.1 当前正式题型

当前 PrimeKG + CTD + PubMed 服务的主要任务形态是：

```text
给定 exposure 和研究条件，回答对应的 disease entity。
```

答案使用：

```text
AnswerContract(
  answer_type="entity",
  canonical_answer_ids=[primekg:disease:...]
)
```

### 4.2 实际目标题型

当前产品目标更接近：

```text
study_grounded_directional_association
```

即：

```text
给定 exposure、disease、population、dose/exposure 和 study context，
回答文献支持的关联方向或结论强度。
```

可能的标准答案：

- `increase`；
- `decrease`；
- `neutral / association_only`；
- `insufficient`；
- 必要时单独表示 `conflicting`，但不能作为普通 gold 正例静默发布。

### 4.3 为什么疾病实体题会触发 shortcut

以 `1-methylphenanthrene -> asthma` 为例：

- PubMed 明确支持肥胖儿童中暴露增加与 asthma 的正向关联；
- 引文、PMID、claim、qualifier 均通过验证；
- 但 PrimeKG 中该 exposure 的题目 scope 只有一个疾病答案；
- 不读取 population、dose 或 PubMed，也能从图谱边直接得到 asthma；
- 删除任一研究限定条件后，答案集合仍不变化；
- 因此 `shortcut_detected` 是正确结果。

这说明问题不在 Writer，而在答案类型与任务目标不一致。

## 5. 不建议采用的修复方式

以下方式会制造表面发布数，但破坏任务价值：

- 将 `shortcut_detected` 从硬门禁中删除；
- 把 inference-only CTD 记录当作直接证据；
- 忽略未映射或 provider_failed 的竞争答案；
- 只选择支持结论的 PMID，跳过 contradictory/uncertain 文献；
- 放松逐字 quote/span/hash 校验；
- 从多篇 mixed claim 中静默挑选一篇；
- 为通过门禁而人工指定唯一疾病答案；
- 把 KG 中已经显式存在的 disease 边包装成“文献推理题”。

## 6. 推荐的下一步实现

### 6.1 将答案从疾病实体改为关联方向

新增或正式启用题型：

```text
study_grounded_directional_association
```

建议契约：

```text
answer_type = "categorical"
cardinality = "exactly_one"
canonical_answer_ids = ["association_direction:increase"]
accepted_aliases = ["增加", "正相关", "风险升高"]
```

其中 canonical category 需要版本化枚举，不能直接保存 Writer 自由文本。

### 6.2 调整公开题面

题干必须公开：

- exposure；
- disease；
- population；
- study type；
- dose/exposure definition；
- 必要 temporal scope；
- 只使用证据中逐字支持的限定条件。

示意形式：

```text
在肥胖儿童的分层模型中，随着 1-methylphenanthrene 暴露水平增加，
研究观察到其与哮喘之间呈现什么方向的关联？
```

答案不再是“哮喘”，而是“正向/增加关联”。

### 6.3 重写任务价值验证

新的 shortcut 验证应证明：

1. PrimeKG 边只能确认 exposure 和 disease 存在关系，不能决定方向；
2. 删除 PubMed supporting evidence 后，方向答案不可验证；
3. 删除关键 population/dose/study qualifier 后，claim 不再精确匹配；
4. contradicting 或 mixed evidence 会改变最终状态；
5. category answer 必须来自融合后的 claim polarity，而不是 KG relation 名称。

### 6.4 保留疾病实体题，但降级为不同产品类型

疾病实体题仍可保留，用于：

- KG lookup benchmark；
- constrained entity task；
- 图查询训练；
- smoke test。

但不能与 source-bounded、study-grounded 文献推理题混为同一正式类型。

### 6.5 为大文献集增加分批提取

对 Rotenone 这类多 PMID 候选，建议：

```text
固定 PMID 全集
  -> 每批 3-5 篇结构化 extraction
  -> 每篇恰好一个 assessment
  -> 按 PMID 确定性合并
  -> 检查无缺失、无重复、snapshot 一致
  -> 再执行 fusion 和 conflict 检测
```

分批不能改变“评估全部 CTD PMID”的验收要求。

## 7. 建议实施顺序

1. 为 categorical direction 定义版本化 canonical ID；
2. 扩展 `AnswerContract` 和发布 loader 对 categorical answer 的明确语义；
3. 新增 directional `TaskCandidate / TaskSpec` 编译路径；
4. 基于 verified PubMed claim polarity 生成 gold answer；
5. 实现不能由 KG 单独决定答案的新 shortcut gate；
6. 更新 Writer 和独立 semantic verifier；
7. 使用 `1-methylphenanthrene -> asthma` 作为第一个方向题 Golden Case；
8. 再加入 decrease、neutral、conflicting 对照样例；
9. 实现 PubMed 分批提取后，重新验证 Rotenone 大文献集；
10. 最后接入 Web 生成入口和公开/隐藏 release。

## 8. 当前验证事实

本轮分阶段回归结果：

- 状态与 ReAct 路径相关：`60 passed`；
- 模型适配器与账本：`40 passed`；
- PubMed span、evidence enrichment 和正式 pipeline：`43 passed`；
- 引文候选与相关服务：`75 passed`；
- 合并后的关键相关测试集：`144 passed`。

这些数字来自不同阶段的重叠测试集合，不能相加为总测试数。尚未在本轮运行全仓测试。

## 9. 当前决策结论

下一阶段不应继续通过更换 chemical/disease 种子碰撞疾病实体题的通过案例。

应正式确认：

```text
PrimeKG 用于实体与关系世界；
CTD 用于关系来源和 PMID 集合；
PubMed verified claim 用于方向、强度和研究限定；
方向性结论作为 categorical answer；
疾病实体不再同时充当题干目标和文献推理答案。
```

只有完成这一题型调整，系统才能稳定生成既通过门禁、又确实需要读取研究证据的问题。

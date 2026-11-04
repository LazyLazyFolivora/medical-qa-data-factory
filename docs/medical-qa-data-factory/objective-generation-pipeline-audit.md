# 主 Agent 出题链路审核说明

本文按当前代码描述“从点击开始生成到发布题目”的真实执行链路，包括材料截断、
模型重试、候选拒绝、整次运行终止和当前已知缺口。

## 1. 端到端总览

```text
Vue 选择实体与参数
  -> POST /api/objective/generate
  -> 校验实体、题数、难度、风格、providers、模型状态和并发锁
  -> 创建 manifest、goal、runtime commit
  -> 主 Agent observe -> decide -> validate -> dispatch -> commit
       -> 材料检索
       -> 事实理解
       -> 候选题设计
       -> 独立答案验证
       -> 最终题面生成
       -> 两阶段发布复核
  -> 主 Agent 停止或达到动作上限
  -> 计算 COMPLETED / COMPLETED_WITH_ZERO_QUESTIONS
  -> 导出 public + audit 包
```

Web 启动入口在 `src/medical_qa_data_factory/web/app.py:1688`，实际运行入口在
`src/medical_qa_data_factory/web/app.py:559`。

## 2. 启动与并发

### 2.1 请求校验

Vue 默认发送：

```json
{
  "run_id": "web-uuid",
  "entity_canonical_id": "primekg:gene/protein:9796",
  "entity_name": "PHYHIP",
  "entity_type": "gene/protein",
  "question_count": 3,
  "difficulty_targets": ["easy", "medium"],
  "question_style_ids": ["layperson-search-v1"],
  "max_actions": 100,
  "providers": ["hgnc", "ncbi_gene", "ensembl", "primekg", "pubmed"]
}
```

当前限制：

| 参数 | 限制 | 失败 |
| --- | --- | --- |
| `question_count` | 1–20 | HTTP 400 |
| `max_actions` | 2–100 | HTTP 400 |
| 难度 | 至少一项，只能是 easy/medium/hard，不重复 | HTTP 400 |
| 风格 | API 最多 3 项，不重复，必须是已存在 skill | HTTP 400 |
| providers | 至少一个，且只能来自 Web 支持的 5 个 provider | HTTP 400 |
| 实体 | `provider:type:id` 格式 | HTTP 400/422 |
| 模型 | 未就绪时拒绝 | HTTP 422 |
| 并发 | 每个 Web 进程同时只允许一个生成任务 | HTTP 409 |

Web 支持的 provider 是 `hgnc`、`ncbi_gene`、`ensembl`、`primekg`、`pubmed`。
CTD adapter 存在，但当前 Web 请求入口不允许选择 `ctd`。

Web 还把允许事实类型固定为：

```text
entity_identity
classification
gene_location
```

### 2.2 同步请求和前端轮询

`POST /api/objective/generate` 本身保持连接直到生成结束。Vue 不等待 POST 返回，
而是每约 900ms 请求轻量 summary：

```text
GET /api/objective/{run_id}/summary
```

任务结束后才请求：

```text
GET /api/objective/{run_id}/workbench
```

summary 超过 10 分钟没有新 runtime commit 时，只会把展示状态派生为 `stalled`，
不会主动终止后台线程，也不会修改历史 runtime state。

## 3. 状态机

运行层：

```text
CREATED
  -> MATERIALS_COLLECTED
  -> CLAIMS_PROPOSED
  -> HYPOTHESES_PROPOSED
  -> CANDIDATES_PROCESSING
  -> COMPLETED
     或 COMPLETED_WITH_ZERO_QUESTIONS
     或 PROVIDER_FAILED
     或 RECOVERY_REQUIRED
     或 VERSION_INCOMPATIBLE
     或 CANCELLED
```

候选题层：

```text
PROPOSED
  -> VERIFICATION_PLANNED
  -> VERIFYING
  -> ANSWER_VERIFIED
  -> QUESTION_REALIZED
  -> RELEASE_REVIEWED
  -> PUBLISHED

任一步可进入 REJECTED / INDETERMINATE
```

状态定义见 `src/medical_qa_data_factory/generation/objective_question_contracts.py:115`。

## 4. 主 Agent 循环

主循环在 `src/medical_qa_data_factory/generation/agent_loop.py:124`：

1. 从 append-only runtime snapshot 读取当前状态。
2. 把 goal、hypothesis 状态、artifact 摘要、检索历史、可用 provider、可用动作和预算
   发给主 Agent 模型。
3. 主 Agent 只能选择一个动作。
4. 宿主校验动作是否合法、状态 digest 是否最新、目标子 Agent 是否匹配。
5. 记录 decision。
6. 分派子 Agent。
7. 子 Agent 结果写入 child slot 和 artifact。
8. 推进候选题状态，进入下一轮。

### 4.1 动作

| 动作 | 作用 |
| --- | --- |
| `retrieve_materials` | 调用一个 provider 获取材料 |
| `request_more_evidence` | 与检索相同，但语义上表示证据不足后补充 |
| `understand_facts` | 从已保存的原文提取候选事实 |
| `design_questions` | 从候选事实生成候选题 |
| `verify_answer` | 盲答、比对候选答案、检查反例 |
| `realize_question` | 将冻结契约写成最终题面 |
| `review_release` | 两阶段发布复核 |
| `stop` | 请求结束主循环 |

### 4.2 主 Agent 决策纠错

主 Agent 返回非法动作时，宿主会携带：

```text
previous_invalid_decision
validation_error
```

再请求一次。最多两个决策响应；第二次仍非法则异常退出。实现位于
`src/medical_qa_data_factory/generation/main_agent.py:148`。

### 4.3 防止过早停止

`stop` 不是模型想停就能停。存在以下情况时会拒绝：

- 仍有合法未完成动作；
- 发布数低于上限，且仍有未使用的候选事实或允许继续设计的次数。

合法动作和停止限制见 `src/medical_qa_data_factory/generation/main_agent.py:266`。

## 5. 子 Agent 链路

### 5.1 材料检索

输入：

- 一个 provider；
- discovery 或 verification 目的；
- 模型生成的 query；
- 宿主固定的 `material_limit=10`。

输出：

- `evidence_material`；
- `evidence_anchor`；
- `provider_result` artifact。

材料检索现在由统一的多轮 `EvidenceSearchAgent` 执行：

1. 先执行主 Agent 提供的 provider-specific query；
2. 把候选材料 ID、来源和截断后的预览交给搜索模型；
3. 搜索模型选择相关材料，必要时最多再提出两个 query；
4. 最多执行 3 轮；
5. 只持久化最终选中的材料和 anchor；
6. 搜索模型失败、输出非法或选择未知 ID 时，回退到确定性排序。

搜索模型看不到问题、候选题或答案。候选预览按不可信数据处理。

原文按 SHA-256 content-addressed blob 保存；anchor 保存后会重新从原文读取，
检查字符偏移、quoted text 和 hash。见
`src/medical_qa_data_factory/generation/objective_question_persistence.py:49`。

Provider 行为：

- PrimeKG：实体解析后查双向邻居，最多 10 条。
- PrimeKG：支持 `direction`、`relations` 和 `target_types` 查询条件。
- PubMed：每轮最多 4 个内部 query 变体，依次为完整 query、前 6 个词、前 2 个词、
  第 1 个词；最终最多 10 篇。
- HGNC：使用搜索接口返回最多 10 个候选文档，供搜索 Agent 筛选。
- NCBI Gene：搜索最多 5 个 ID，并拉取全部候选摘要。
- Ensembl：优先按 symbol/ENSG ID lookup；失败时尝试 xrefs 候选。
- CTD：支持 chemical ID 和 disease name 过滤。

PubMed 的查询截断在
`src/medical_qa_data_factory/generation/objective_provider_adapters.py:149`。

### 5.2 事实理解

输入：

- 当前 run 中全部材料和 anchors；
- 每份材料的完整 UTF-8 原文；
- 目标实体和允许事实类型。

输出：

- 一组 `candidate_claim`；
- 一个和目标实体绑定的 `entity_resolution`。

边界：

- 模型看不到提出的问题、候选题或答案；
- claim 只能引用当前可见 anchor；
- claim 的 `status` 固定为 `PROPOSED`；
- claim 本身不是已验证事实。

当前没有对材料总字数或 claim 数量做硬截断。`CandidateClaimBatch` 只要求至少 1 条，
没有 `maxItems`。

### 5.3 候选题设计

输入：

- candidate claims；
- 允许事实类型；
- 剩余题数；
- 难度目标和风格目标；
- anchor 的位置，不发送原文。

输出：

- `question_hypothesis`，包括意图、公开范围、候选答案、答案类型、基数和来源 anchor。

确定性处理：

- claim 引用必须存在；
- `discovery_anchor_ids` 现在完全由被引用 claims 的支持 anchor 并集生成；
- 模型不能自由把一个无关 anchor 挂到候选题上；
- 候选题 ID 由语义内容 digest 生成。

`QuestionHypothesisBatch` 只要求至少 1 条，没有 `maxItems`。因此单批回复理论上可能
超过剩余目标题数，当前没有按返回列表长度截断。

### 5.4 独立答案验证

一次验证固定调用三次模型：

1. blind solve：只看问题意图、公开范围、答案类型、基数和关系强度；
2. compare：盲答结果冻结后，才加入候选题提出的答案进行比对；
3. counterexample review：查看验证材料 anchor，检查竞争答案和冲突。

验证门槛：

- provider coverage 必须完成；
- 必须存在独立支持 anchor；
- discovery anchors 被排除，不冒充独立验证证据；
- 不能有竞争答案；
- 不能有冲突证据；
- 候选答案必须与盲答一致；
- completeness basis 必须已建立。

结果映射：

| 条件 | 结果 |
| --- | --- |
| coverage 不完整或 blind uncertain | `INDETERMINATE` |
| 无独立验证证据 | `INDETERMINATE` |
| 出现竞争答案 | `REJECTED` |
| 冲突证据 | `REJECTED` |
| 盲答与候选答案不一致 | `REJECTED` |
| completeness 未建立 | `REJECTED` |
| 全部通过 | `VERIFIED` |

只有 `VERIFIED` 才生成 `frozen_question_contract`。

注意：当前宿主总是给 verification bundle 设置：

```text
completeness_basis = CANONICAL_IDENTITY
competing_answer_queries = 1
contradiction_queries = 1
```

这些并不是从实际检索过程逐项推导。审核时应重点确认这一设计是否过强。

### 5.5 最终题面生成

输入只有冻结契约，不提供原文、claim、提出答案或 discovery anchors。

硬检查：

- 题面不得出现 PrimeKG、CTD、RAG、database、knowledge graph 等来源措辞；
- 不得出现 “current research / existing evidence” 等元证据措辞；
- semantic readback 的 intent、scope、answer type、claim strength、cardinality、
  required conditions 必须与冻结契约完全一致；
- draft 和 readback 的答案值不得变化。

语义升级词 `cause/treat/diagnose/prognosis` 目前不再在题面生成阶段硬拒绝，
而是交给下一阶段发布复核判断。

如果题面生成抛 `RoleContractViolation`，当前只把该候选题标为 `REJECTED`，
主 Agent 继续，不再终止整个运行。

### 5.6 两阶段发布复核

第一阶段只提交题面，不提交答案、契约或 evidence。检查：

- 题目是否自包含；
- 是否依赖内部数据源；
- 是否询问底层事实；
- 实体是否明确；
- 答案基数是否清楚。

只有第一阶段 PASS，才进入第二阶段。第二阶段检查：

- 答案是否有独立支持；
- 答案集合是否唯一或封闭；
- 是否检查冲突；
- 问题是否保留原关系强度。

发布合并还要求：

- public question digest 一致；
- frozen contract digest 一致；
- independent evidence digest 一致；
- validation skill digest 一致。

任一 digest/skill 变化会得到 `INDETERMINATE`。

只有所有门禁通过才得到：

```text
verdict = PASS
release_allowed = true
```

## 6. 截断和上下文上限

### 6.1 实际截断

| 位置 | 上限 |
| --- | --- |
| 前端默认实体候选 | 前 10 条；当前选中实体始终保留 |
| 前端实体搜索 | 10 条 |
| 单次 provider 材料数 | 10 |
| PrimeKG 邻居 | 10 |
| PubMed 文献 | 10 |
| NCBI Gene search | 5 个 ID，只用第一个 |
| PubMed query | 超过 160 字符会退回目标实体名 |
| PubMed fallback | 完整 query、前 6 词、前 2 词、首词 |
| workbench 材料 preview | 320 字符，仅用于展示 |

Display preview 的 320 字符截断位于
`src/medical_qa_data_factory/web/app.py:291`，不会影响事实理解看到的原文。

### 6.2 没有截断的地方

- Fact understanding 会把当前 run 的全部材料原文发给模型；
- 主 Agent 会收到当前全部 artifact summaries 和完整 retrieval history；
- Candidate claim 列表没有最多条数限制；
- Hypothesis 列表没有最多条数限制；
- question 和 explanation 只有最小长度，没有最大长度；
- raw provider JSON 没有统一字符上限。

这些位置可能因材料或 run 变大而超过模型上下文。发生上下文/TLS/HTTP 错误后会进入
下述异常路径。

## 7. 模型重试

配置在 `config/models.json`：

```text
timeout_seconds = 180
max_retries = 3
```

对同一个结构化模型调用，最多 `max_retries + 1 = 4` 个 physical attempts。

- 429、5xx、timeout、network error：可能自动重试；
- 401/403：凭证错误，不重试；
- 其他 4xx：不重试；
- JSON parse/schema error：自动附加 repair prompt，在同一个 3 次上限内重试；
- repair 仍不合格：该模型调用抛出 `ModelError`。

每次 physical attempt 独立记录。主 Agent 的非法逻辑决策最多重试 3 次。
子 Agent 的 `ModelError` 或 `RoleContractViolation` 现在会写入
`subagent_failure` artifact，让主 Agent基于新状态继续决策，不再立即终止整个运行。

实现位于
`src/medical_qa_data_factory/llm/providers/openai_compatible.py:83`。

## 8. 失败处理矩阵

| 情况 | 当前行为 |
| --- | --- |
| Provider 返回 EMPTY | 记录 provider_result，无材料；空检索连续计数 +1 |
| Provider 返回 FAILED | 记录失败；空检索连续计数 +1 |
| Provider 返回 UNKNOWN | 记录 unknown；空检索连续计数 +1 |
| 连续 3 次无材料 | 主循环以 `no_new_evidence` 正常结束 |
| 主 Agent 决策非法 | 带校验错误再请求一次；仍非法则整次失败 |
| 主 Agent 持续重复决策 | loop guard 命中后整次失败 |
| 达到 max_actions | 正常结束，stop_reason=`max_agent_actions` |
| Claim 引用隐藏/未知 anchor | RoleContractViolation，整次失败 |
| 候选题引用未知 claim | RoleContractViolation，整次失败 |
| discovery anchor 不匹配 | 宿主自动按 claims 支持 anchor 归一化 |
| 验证覆盖不完整 | 该候选题 `INDETERMINATE` |
| 验证发现答案冲突/竞争/不匹配 | 该候选题 `REJECTED` |
| 题面生成硬契约违规 | 当前仅拒绝该候选题，主 Agent 继续 |
| 发布复核拒绝 | 该候选题 `REJECTED`，主 Agent 继续 |
| 发布模型输出无效 | 生成 INDETERMINATE 失败报告，不发布 |
| 子 Agent 模型错误或角色契约错误 | 写入 `subagent_failure`，主 Agent 继续决策 |
| 主 Agent 连续非法/无法产生决策 | Web 层把 run 标为 `RECOVERY_REQUIRED` |
| 进程被杀死/机器重启 | runtime 停留在最后 commit；10 分钟后 UI 显示 stalled，不会自动恢复 |
| release 导出失败 | 主循环可能已 completed，但 API 500，当前不会补写 RECOVERY_REQUIRED |

关键区别：

- 答案错误、公告冲突、发布复核拒绝，是**候选题级失败**；
- provider/task 契约错误、模型基础设施错误、未知 claim 等，多数仍是**整次运行级失败**。

Provider 失败目前主要记录为 retrieval history。主循环不会自动把运行状态改成
`PROVIDER_FAILED`；它通常通过连续空检索计数或后续缺少材料错误退出。

## 9. 结束与发布语义

`_finish()` 只判断是否存在至少一个 `release_allowed=true` 的验证报告：

- 有任意一道已发布题：`COMPLETED`；
- 一道都没有：`COMPLETED_WITH_ZERO_QUESTIONS`。

因此：

```text
目标 3 题，发布 1 题
-> 仍可能是 COMPLETED
```

`question_count` 的真实语义是上限，不保证达到。

正常结束后，release exporter：

- 再调用 `ObjectiveQuestionReleaseGate.assert_releasable()` 做最终检查；
- 写出 `public/questions.jsonl`；
- 写出 audit validation reports、runtime artifacts、main-agent decisions。

## 10. 当前审核风险清单

### P0：模型预算目前没有真正接入主循环

Web 创建了 `RuntimeBudget(max_cost=10, max_model_calls=...)`，但 objective agent loop
没有调用 `commit_operation()`，也没有绑定 `AttemptLedger` 或 `on_physical_call`。
当前实际有效的硬上限是 `max_actions`，不是 `max_cost` 和 `max_model_calls`。

### P0：部分候选题级错误仍会终止整次运行

目前只有 `realize_question` 中的 `RoleContractViolation` 被转为候选拒绝。事实理解、
候选题设计、答案验证、以及未捕获的模型/API 错误仍会向上抛，导致整次
`RECOVERY_REQUIRED`。

### P1：答案验证的 INDETERMINATE 被状态机写成 REJECTED

`AnswerVerificationSubagent` 能返回 `INDETERMINATE`，但 `_verify_answer()` 当前对
所有非 VERIFIED 都执行 `HypothesisState.REJECTED`，导致候选状态和 AnswerResolution
状态不一致。

### P1：题面生成被拒后没有留存拒绝原因

当前会将该候选题标为 REJECTED 并继续，但被拒 draft/具体校验原因没有作为独立
artifact 保存，后续 UI 只能看到“已拒绝”，难以审计。

### P1：难度和风格没有确定性闭环

- difficulty 只进入问题设计模型上下文，后续没有确定性难度校验；
- question style ID 被放入 `style_targets`，但 objective 问题设计流程没有加载对应
  `skills/question-styles/*/SKILL.md` 作为模型 instruction。

因此这两个前端控制项目前更接近提示，不是硬约束。

### P1：候选题数量可能超出目标

问题设计返回列表没有 `maxItems`，宿主也没有按剩余目标截断。单批可能提出超过
requested upper bound 的 hypothesis。

### P1：模型上下文没有总预算

主 Agent 和事实理解都使用当前累计的全部 artifact/material 数据。代码没有 token
预算、字段裁剪或 artifact 摘要窗口；随着动作增加，请求可能持续变大。

### P2：completed 容易被误解为目标已达成

`COMPLETED` 只表示主循环结束且至少发布一题；不是“达到了请求题数”。

### P2：stalled 只是展示状态

10 分钟无 commit 不会改变 runtime 为失败，也不会终止可能仍挂起的调用。

### P2：没有自动恢复/取消接口

存在 `RECOVERY_REQUIRED` 和 `CANCELLED` 状态，但 Web 当前没有 resume 或 cancel 操作。

## 11. 最近两次典型报错的准确位置

### `discovery_anchor_ids are not supporting anchors`

原位置在候选题设计的语义校验。现在已改为宿主根据引用 claims 的支持 anchor 并集
推导，不再信任模型填写。

### `SEMANTIC_UPGRADE`

原位置在最终题面的关键词硬门禁。现在关键词命中不再直接杀死主循环；题面会进入
两阶段发布复核。真正篡改冻结答案、基数、范围或 claim strength 仍会硬拒绝。


# 多源医学问答数据工厂：未完成事项清单

> 更新日期：2026-10-21  
> 当前判断：requirements-v2 的 P0 fake-model 闭环、不可变存储、审核、校验和分级导出
> 已实现；正式 `generate entity` 的 `fake` / `structured` 路径会运行 ReAct 与按需 RAG。
> 真实 structured 模型、真实 WeKnora、三路消融、可靠划分与正式 MVP 数据尚未验收。  
> `eval react` 已通过完整 ReAct 状态机在真实 PrimeKG 上验证观察依赖的 2/3-hop
> 可达性，但使用脚本 Planner，不应表述为真实模型 Agent 已验收。

## 2026-10-21 requirements-v2 P0 闭环

> 本节覆盖上文旧状态描述；唯一需求依据是
> `docs/entity-question-generation-implementation-requirements.md`。

- [x] `GenerationSpec` 与 `SafetyBudget` 分离
- [x] `QAItem`、`PlannerStep`、`PlannerTrajectory`、`PlannerTrainingSample`
- [x] 每轮动作前 state、selected action、policy、observation、evidence gain
- [x] evidence-need 绑定的正式 RAG action
- [x] search → read-back → hash/offset/snapshot 校验 → LiteratureEvidence
- [x] structured Writer 与独立上下文 Verifier
- [x] 按冻结证据计算 easy / medium / hard
- [x] fake-model + pinned fake-RAG 的 KG → RAG → Writer → Verifier 闭环
- [x] QAItem、Trajectory、TrainingSample 同 episode 落盘
- [ ] 真实 WeKnora 实机 smoke
- [ ] 获授权的真实 structured model smoke
- [x] Planner 正负样本正式 JSONL 导出
- [x] split-group 确定性划分与结构指纹泄漏门禁
- [ ] 正负样本采样比例与分层平衡
- [ ] 人工抽样质量与保留率报告

当前默认测试：777 passed，13 deselected；真实 PrimeKG 只读：13 passed。

## 2026-10-20 Step 0 基线冻结记录

施工依据：`plans/real-generation-agent-implementation-plan.md`（真实生成 Agent 实施文档，Step 0）。
该文件与 PRD v0.4 冲突时以它们为准；早先 roadmap/implementation-requirements 中
"PrimeKG-only""不实现真实文献 RAG"只描述已完成的历史最小闭环。

- [x] 记录默认测试基线与真实 PrimeKG 测试命令
- [x] 修复 revision 次数用尽后自动 accepted
- [x] 为受影响产物提供重新验证入口与迁移说明
- [x] 增加启动前依赖检查；`qa_env` 为推荐环境，非 `qa_env` 仅作提示不阻断

### 测试基线

- 默认：`python -m pytest -q -m "not primekg_integration"`，319 项通过、10 项真实 PrimeKG deselected
- 真实 PrimeKG 只读：`python -m pytest -q -m primekg_integration -o addopts=""`，10 项通过
- 唯一受支持环境：`D:\Anaconda\envs\qa_env\python.exe`（Python 3.12.15）；
  `generate` 前会预检直接依赖，缺失或过旧以 `environment_error`（退出码 3）失败，
  不写入任何产物；非 `qa_env` 的 Conda 环境只作为提示报告
- PrimeKG 数据库：`E:\Cursor\assumption\datasets\PrimeKG\primekg.sqlite`，只读

### 受影响产物的迁移说明

Step 0 之前已经生成并进入 `machine_validated` 或 `human_confirmed` 的问题，如果其 draft
曾被 verifier 判为 `needs_revision`，则该样本是在旧规则（revision 用尽后自动 accepted）下
放行的。这些样本**不会**被自动重判或删除：不可变仓库保留全部历史版本。处置方式：

1. 用 `validate run --workspace <ws>` 重新执行校验器，写入新版本校验记录；
2. 用 `validate status --workspace <ws>` 查看每条问题当前是否有适用的阻塞失败；
3. 导出前重新确认，凡不满足当前门禁的样本不会被导出，而不是被静默放行；
4. 人工发现确实需要改写的样本，走审核工作台的 revise/return/reject，生成新 draft 版本。

由于本项修复只收紧放行条件，不改变已有 artifact 的内容，因此不需要重跑历史 episode。

## 2026-10-20 Step 1 契约与账本冻结记录

施工依据：`plans/real-generation-agent-implementation-plan.md` Step 1。

- [x] 新增 GenerationPreparationRun、GenerationRunManifest、RequiredEvidenceMode
- [x] 新增 AttemptRecord 与以其为唯一事实来源的预算账本
- [x] 将 max_hops 改为受策略控制的整数（MVP 默认 3，系统上限 4）
- [x] 为 ValidationCheck 增加 question version 与 input digest 适用性
- [x] 定义 mode/run/episode/thread 隔离
- [x] 定义旧 JSON 与旧 check 记录的读取/拒绝策略
- [x] 拆分模型 proposal 与服务端 envelope 的契约边界

### 新增模块

- `generation/run_identity.py`：RequiredEvidenceMode、RunMode、GenerationPreparationRun、
  GenerationRunManifest、AttemptRecord
- `generation/attempt_ledger.py`：reserve/mark_running/complete/release/reconcile，
  预算总额只从 AttemptRecord 版本派生
- `validation_applicability.py`：校验结果的适用性判定、依赖 digest、版本祖先链

### 关键决定

- `RequiredEvidenceMode` 与消融算出的 `verified_route` 是两个独立字段，生成要求不可被
  消融结果反向放宽。
- AttemptRecord 状态机为 reserved → running → completed/failed/unknown。未知结果按预留
  上限计费，绝不按零计费；只有 provider 明确支持幂等键时才允许重试未知结果。
- 预算总额只从持久化的 AttemptRecord 派生，不保留第二份计数器，因此 resume 不会重置预算。
- 校验结果的复用条件：必须记录了 evaluated_question_version 和 input_digest，且版本是
  当前版本或其在 changes 父链上的祖先。版本祖先关系取自不可变 change 链，不猜测版本号。
- `verified_route` 不属于校验依赖字段：人工确认路线标签不会使证据正确性校验失效。
- 旧 check 记录仍可读取，但因缺少 provenance 永远不能放行门禁，报为 not_run 并给出原因。
- `rule-based`、`fake`、`structured` 的 episode id 互不相同，不再共用同一 episode、
  checkpoint thread 与产物目录。

### 测试基线

- 默认：383 项通过、10 项真实 PrimeKG deselected
- 真实 PrimeKG 只读：10 项通过

### 尚未完成（Step 2 及以后）

- N-hop 路径基础层（Step 3）与逐 hop 文献需求、真实 RAG 接入（Step 4）
- Planner / Policy Gate / Executor（Step 5）、有界 KG/RAG ReAct 与 Sufficiency（Step 6）
- 强类型 LogicalQuery、来源消融、分组排序、审核 UI 与三套导出等级（Step 7—10）

## 2026-10-20 Step 2 真实结构化模型适配器

施工依据：`plans/real-generation-agent-implementation-plan.md` Step 2。

- [x] 实现 OpenAI-compatible structured-output adapter
- [x] 配置只保存 `api_key_env`，不保存密钥；含内联密钥的配置文件被拒绝
- [x] 支持 timeout、429、一次 schema repair 与稳定错误分类
- [x] 每个 physical attempt 独立 reserve/charge 并记录 usage
- [x] 不支持严格 JSON Schema 的 provider fail fast
- [x] structured 模式从 registry 装配，不允许静默降级
- [x] 逐 role 的 prompt 模板与版本，writer 与 verifier 不共用模板

### 新增模块

- `llm/config.py`：ModelProviderConfig / ModelConfig，配置只存环境变量名
- `llm/errors.py`：ModelError 与稳定错误码，映射到 attempt outcome
- `llm/providers/openai_compatible.py`：真实 adapter，含严格 JSON Schema 请求、
  状态码分类、一次 schema repair、usage 记录
- `llm/registry.py`：ProviderRegistry，负责 provider 装配
- `llm/attempted_model.py`：LedgeredModel，把每次 physical attempt 记入账本
- `llm/prompts.py`：逐 role 的 prompt 模板、版本与哈希

### 关键决定

- schema repair 是两次 physical call，因此被计费两次。这要求 adapter 逐次物理调用
  上报，预算包装器据此分别 reserve，而不是按 generate_structured 调用次数计费。
- 预算耗尽抛出 BudgetExhaustedError 本身，不被包装成 provider 错误：一个预算拒绝
  和一个坏掉的 provider 必须是两回事。
- `structured` 模式现在从 registry 装配。缺配置报 `model_config_error`（退出码 3），
  配置存在但固定 DAG 尚无 model-driven planner 时报 `capability_error`。两者不混用。
- `model providers` CLI 命令只输出环境变量名与是否存在，绝不输出密钥。

### 测试基线

- 默认：431 项通过、10 项真实 PrimeKG deselected
- 真实 PrimeKG 只读：10 项通过

### 尚未完成

- 一次显式授权的真实模型 smoke：需要真实 provider 的 base_url 与密钥，本次未执行
- Step 3—10 同上

## 2026-10-20 Step 3 N-hop 路径基础层

施工依据：`plans/real-generation-agent-implementation-plan.md` Step 3。

- [x] 将 OrderedPath / EvidencePath 泛化为 N-hop
- [x] 实现有界 frontier expansion，不把 provider `path_query=false` 谎报为 true
- [x] 实现 evidence-first / path-second / reload-validation 原子流程
- [x] 环、重复边、回到起点、跨 snapshot、断链拒绝
- [x] fixture 覆盖 2/3/4-hop、正反向混合遍历和高连接度截断
- [x] 至少一条真实 3-hop path 可持久化并 reload 验证

### 新增模块

- `knowledge_graph/frontier.py`：FrontierBudget、NHopPathBuilder、select_frontier
- `generation/path_persistence.py`：PathPersister、build_evidence_path、
  validate_evidence_path

### 关键决定

- walk 从不原地修改。每对 (walk, edge) 产生一条新 walk，原 walk 保留，否则分支图会被
  拼成一条混合路径。早期实现原地追加，真实图上只产出 2 步路径。
- 下一层扩展的实体按 walk 的实际末端选取，而不是从原始边里挑。否则扩展出的实体
  没有任何 walk 到达，步骤接不上。
- 种子 walk 只能在第一跳起步。否则一条 walk 会看起来从它从未到过的实体开始。
- 快照在 walk 中途变化时记录 snapshot_drift 并丢弃该层，绝不把两个快照的步骤缝在一起。
- 路径契约层的校验先于证据比对，因此结构性错误（断链、位置不连续）在
  EvidencePath 构造时就被拒绝，证据比对只处理方向、关系与 provider 的一致性。

### 测试基线

- 默认：463 项通过、13 项真实 PrimeKG deselected
- 真实 PrimeKG 只读：13 项通过（含 3 项新的 N-hop 集成测试）

### 尚未完成

- Step 4：真实文献 RAG 接入与逐 hop 证据融合
- Step 5—10 同上

## 2026-10-20 Step 4 真实文献 RAG 接入与逐 hop 证据融合

施工依据：`plans/real-generation-agent-implementation-plan.md` Step 4。
接口契约见 `docs/weknora-rag-integration.md`，全部读自 `E:\Cursor\WeKnora` 源码。

- [x] 对旧 RAG 做只读 capability probe，契约取自源码而非推测
- [x] 新增 `LiteratureRetrievalPort` 与 provider registry
- [x] 实现 WeKnora adapter（search + read_chunk + preflight）
- [x] 从 path step / claim gap 确定性生成 `LiteratureEvidenceNeed`
- [x] 逐 hop 融合：回读一致才成为文本证据
- [x] 支持性判断独立保存，不覆盖原始证据
- [x] 空结果、超时、鉴权、限流、解析失败与 provider 错误分别区分
- [x] 配置只保存 secret 环境变量名

### 新增模块

- `literature/contracts.py`：LiteratureEvidenceNeed、LiteratureRetrievalRequest/Result、
  LiteratureSnapshot、RetrievedChunk、LiteratureChunkReadRequest/Result、
  LiteratureCapability
- `literature/providers/weknora.py`：WeKnoraProvider
- `literature/registry.py`：LiteratureRegistry、load_literature_config
- `generation/evidence_needs.py`：逐 hop 需求构造与检索式生成
- `generation/evidence_fusion.py`：回读校验、证据融合、支持性判断

### WeKnora 接口要点

- 检索：`POST /api/v1/knowledge-search`，鉴权 `X-API-Key`，返回
  `{success, data:[SearchResult]}`，不做 LLM 合成。
- 回读：`GET /api/v1/chunks/by-id/:id`，返回 `{success, data:Chunk}`，含
  `start_at`/`end_at` 字符偏移。
- `SearchResult` 不带 `content_hash`，hash 由本项目从取回文本计算。
- `match_type` 是数值线上契约，与服务端 iota 顺序一致，已按名映射。

### 关键决定

- 检索结果必须经同一 snapshot 上的独立回读，文本 hash 与字符偏移都一致，才成为
  文本证据。检索与回读是两个请求，回读失败或不一致一律记入 unverified。
- 支持性判断是独立记录，不改写证据原文。判断是确定性的：查两方实体名与关系措辞。
- 评估集合必须包含全部适用结果，支持、反对、不确定、仅背景、无关都进入，
  不能只挑支持片段。
- `LiteratureEvidenceNeed` 增加 `subject_name` / `object_name`：论文写的是名字而不是
  canonical id，只按 id 匹配会让所有真实段落都被判为无关。

### 测试基线

- 默认：514 项通过、13 项真实 PrimeKG deselected

### 尚未完成（实机联调）

本地 WeKnora 服务未运行（Docker daemon 未启动），因此真实知识库的检索与回读、
语料快照的可固定方式、许可字段、真实错误码表现均未实机验证。adapter 的请求与响应
形状全部来自源码，测试用本地 HTTP server 按同样形状回放。

- Step 5—10 同上

## 2026-10-20 Step 5 Planner、Policy Gate、Executor

施工依据：`plans/real-generation-agent-implementation-plan.md` Step 5。

- [x] 模型只能输出 proposal，不能直接执行工具
- [x] Policy Gate 校验并生成 SealedAction，非法 proposal 零工具调用
- [x] Executor 只执行 SealedAction
- [x] 观测结果反馈给 Planner，形成观察依赖的决策
- [x] Planner 上下文压缩且结构化，不含 SQL、路径、密钥与隐藏思维链
- [x] 不保存模型隐藏思维链，只保存结构化动作、简短依据、引用与停止原因

### 新增模块

- `generation/actions.py`：ModelActionProposal、ExplorationDecision、SealedAction、
  PolicyRejection、RunPolicy、PolicyGate
- `generation/planner.py`：PlannerContext、PlannerPort、StructuredModelPlanner、
  build_planner_context
- `generation/executor.py`：Executor、ExecutionOutcome
- `generation/exploration.py`：ExplorationLoop，把 propose→gate→execute→observe 串成一轮

### 关键决定

- 提案里不含 decision id、turn、action hash，模型也无法通过额外字段伪造：`extra=forbid`。
  这些身份全部由服务端生成。
- 拒绝不结束运行。模型会收到拒绝理由并可另选动作；连续两次被拒才以
  `no_valid_action` 停止。
- `finish` 在收集到任何证据之前会被拒绝。
- 文献动作必须服务一个真实存在的 need id，否则以 `need_not_found` 拒绝。
- 动作 hash 参与重复判定：同一 run 内完全相同的动作第二次会被拒。
- KG 边也计入"新证据"。此前只有文献证据被计入，导致连续查询同一实体被误判为
  "两轮无新证据"。

### 测试基线

- 默认：564 项通过、13 项真实 PrimeKG deselected

### 尚未完成

- Step 10：分组审核 UI/API 与导出语义


施工依据：`plans/real-generation-agent-implementation-plan.md` Step 6。

- [x] 把 ReAct 循环接入 LangGraph（observe→decide→gate→act→persist→assess）
- [x] 决策依赖上一轮观测，不是改名的 DAG
- [x] 新增 AnswerabilityProbe，在完整固定 snapshot 上确定性求值
- [x] Sufficiency Judge：模型提议，代码定夺
- [x] 断链、缺文本支持、未解决冲突、歧义、预算耗尽均阻止充分
- [x] 全部候选被拒但仍有预算时反馈失败摘要给 Planner

### 新增模块

- `generation/probe.py`：AnswerabilityProbe、SnapshotGraphEvaluator、ProbeExecution
- `generation/sufficiency.py`：SufficiencyJudge、CoverageState、SufficiencyEnvelope
- `generation/react_graph.py`：build_react_graph，把循环接进 LangGraph

### 关键决定

- probe 打破了循环依赖：先判充分再生成 LogicalQuery 的顺序会要求"证据是否够"依赖于
  证据本该确定的东西。probe 是从路径构造的查询骨架，不含自然语言问题和答案。
- probe 在完整 snapshot 上求值，不限于生成它的那条路径的证据。否则就是"用生成输入
  证明生成答案"的自证循环。
- 模型的充分性判定只是提议。确定性 guardrail 先行：缺文本支持、未解决冲突、
  probe 无答案或歧义、预算耗尽，都会覆盖模型说的 sufficient。
- `finish` 是停止信号，不是工具调用：它不产生观测，也不消耗工具预算。
- 状态里的每轮临时字段必须显式声明，否则 LangGraph 会丢弃未声明的键，循环会静默空转。

### 尚未完成

- Step 6 剩余：ReAct 循环尚未接入正式生成入口及真实 RAG provider 的端到端运行


## 2026-10-20 Step 6 有界 KG/RAG ReAct 与 Sufficiency

施工依据：`plans/real-generation-agent-implementation-plan.md` Step 6。

- [x] 把 ReAct 循环接入 LangGraph（observe→decide→gate→act→persist→assess）
- [x] 决策依赖上一轮观测，不是改名的 DAG
- [x] 新增 AnswerabilityProbe，在完整固定 snapshot 上确定性求值
- [x] Sufficiency Judge：模型提议，代码定夺
- [x] 断链、缺文本支持、未解决冲突、歧义、预算耗尽均阻止充分
- [x] 全部候选被拒但仍有预算时反馈失败摘要给 Planner

### 新增模块

- `generation/probe.py`：AnswerabilityProbe、SnapshotGraphEvaluator、ProbeExecution
- `generation/sufficiency.py`：SufficiencyJudge、CoverageState、SufficiencyEnvelope
- `generation/react_graph.py`：build_react_graph，把循环接进 LangGraph

### 关键决定

- probe 打破了循环依赖：先判充分再生成 LogicalQuery 的顺序会要求"证据是否够"依赖于
  证据本该确定的东西。probe 是从路径构造的查询骨架，不含自然语言问题和答案。
- probe 在完整 snapshot 上求值，不限于生成它的那条路径的证据。否则就是"用生成输入
  证明生成答案"的自证循环。测试 `test_the_evaluator_searches_the_whole_snapshot_not_only_the_path`
  钉住了这一点。
- 模型的充分性判定只是提议。确定性 guardrail 先行：缺文本支持、未解决冲突、
  probe 无答案或歧义、预算耗尽，都会覆盖模型说的 sufficient。
- `finish` 是停止信号，不是工具调用：它不产生观测，也不消耗工具预算。
- 状态里的每轮临时字段（proposal、sealed、rejection、outcome）必须显式声明，
  否则 LangGraph 会丢弃未声明的键，循环会静默空转。
- 模型出错时 stop_reason 由 `done` 节点补写，否则运行会停在无原因状态。

### 尚未完成

- Step 6 剩余：ReAct 循环尚未接入正式生成入口与真实 RAG provider 的端到端
  运行（需要一个能跑起来的 WeKnora 服务与真实模型）

## 2026-10-20 Step 7 多跳生成、独立验证与一次重写

施工依据：`plans/real-generation-agent-implementation-plan.md` Step 7。

- [x] 构造 subclaims：每跳一个 claim，各自绑定 KG 证据、文本证据与支持性判断
- [x] RelationCompositionPolicy：allow / neutralize / deny
- [x] 消费 probe 提升出强类型 LogicalQuery，校验 digest 链连续性
- [x] 全 hop leave-one-out 与更短路径 shortcut 检查
- [x] AnswerBlind 验证请求与确定性验证器
- [x] 最多一次改写，只允许改题面，不改答案、查询、路径、证据与 split 身份
- [x] 第二次仍需修改则 hard reject

### 新增模块

- `generation/logical_query.py`：LogicalQuery、QueryStep、SnapshotQueryEvaluator、
  promote_probe_to_query
- `generation/composition.py`：RelationCompositionPolicy
- `generation/shortcuts.py`：check_shortcuts、ShortcutReport
- `generation/blind_verification.py`：VerificationRequest、BlindVerificationResult、
  DeterministicBlindVerifier
- `generation/revision.py`：Subclaim、PathClaims、RewriteBudget

### 关键决定

- query 有自己的 digest（覆盖 aggregation、filters 等），但必须原样携带 probe 的 digest，
  并校验步骤序列与 probe 完全一致。query 不能悄悄回答另一个问题。
- 组合策略对未知链默认 neutralize，绝不静默升级为因果或治疗性读法。链式 claim 只断言
  "连接"，并显式声明不主张首关系对末实体的成立。
- shortcut 检查在完整 snapshot 上做。早期实现里 `_shortest_chain` 没有检查边是否与
  当前实体相接，导致 `migraine→pain` 的边被当成从 aspirin 出发的一跳捷径。
- 改写预算是按 lineage 计的：改写产生新 draft，但沿用同一条 lineage，因此花的是同一个预算。
- 验证请求里没有候选答案，只有权威答案集（用于基数与唯一性检查）。泄漏答案与泄漏
  中间实体是两个不同的 fault，分别记录。

## 2026-10-20 Step 8 来源消融与 verified route

施工依据：`plans/real-generation-agent-implementation-plan.md` Step 8。

- [x] 三个相互隔离的消融 episode：kg_only、literature_only、combined
- [x] 隐藏 gold answer、gold evidence 与 writer rationale
- [x] 每个 mode 独立的工具白名单与预算
- [x] 用确定性规则计算 verified_route，不由模型自报
- [x] verified route 与生成要求分开保存，消融结果不得反向降低生成要求
- [x] 路由标签或消融输入变化时精确失效旧结果
- [x] 扩展 AblationResult 记录依赖 digest、episode、白名单、快照、trace 与 gold 隐藏标志

### 新增模块

- `generation/ablation.py`：AblationEpisode、AblationReport、run_ablation、
  compute_verified_route、apply_verified_route
- `generation/ablation_runner.py`：IsolatedAblationRunner、GoldHidingSolver

### 关键决定

- 三个 episode 各自拿完整预算，互不继承对方的花费。episode id、工具白名单、快照全部独立。
- route 的判定规则是确定性的，覆盖全部组合：仅 combined 答出 → hybrid；仅一个单源答出
  → 该源；两个单源都答出 → redundant；都答不出 → unanswerable；某路失败 → 该路不算证据。
- `route_is_complete` 检查三路齐全、全部 gold_hidden 且 route 已确定。看过 gold 的
  运行不能用于验证 route。
- GoldHidingSolver 是双保险：内层 solver 本就拿不到 gold，万一泄漏就把答案 id、
  gold 证据 id 和 writer rationale 一并丢弃。
- 消融发现的 route 描述的是问题本身，不是运行的义务。`required_evidence_mode`
  单独记录，apply_verified_route 不动它。

## 2026-10-20 Step 9 实体—关系分组与可解释排序

施工依据：`plans/real-generation-agent-implementation-plan.md` Step 9。

- [x] QuestionSpec 增加 review_group_key、review_group_version、ranking_assessment、
  review_disposition、variant_of_question_id
- [x] 单跳按种子 + 关系 + 方向 + 答案实体类型分组
- [x] N-hop 按完整有序关系链 + 逐步方向 + 答案实体类型分组
- [x] 分组键由结构化 id 生成，不使用显示名称
- [x] 多路径问题的键不依赖路径发现顺序
- [x] ranking.v1 配置与因素明细，扣分原因可见
- [x] 排序不改 validation level、review disposition 或导出状态
- [x] `duplicate_detection` 从阻塞型改为 advisory
- [x] 新增阻塞型 `review_group_integrity`：只校验 group key 可复算
- [x] validator 版本 bump 到 validators.v2，旧 duplicate 记录不得被读成新语义

### 新增模块

- `generation/grouping.py`：GroupPayload、GroupStep、group_key、build_groups、
  is_recomputable
- `generation/ranking.py`：RankingSignals、rank_candidate、sort_candidates、explain

### 关键决定

- group key 只由结构化 id 与关系链生成。中间实体类型不进入 key，只作展示与筛选。
- `review_group_integrity` 只校验"key 可由结构复算"，不判断两个候选是否语义相同。
  没有分组结构的问题没有可复算的东西，检查通过；有结构却缺 key 才是缺陷。
- `duplicate_detection` 改为 warning。相似候选由人工决定是否两题都保留。
- 排序分数只改展示顺序。阻塞失败的候选仍可显示，但分数封顶在 20。
- answer_entity_type 必须显式记录在 blueprint 上，不能从 canonical id 的最后一段猜测。

## 2026-10-20 Step 10 处置命令、导出等级与仓库写入顺序

施工依据：`plans/real-generation-agent-implementation-plan.md` Step 10。

- [x] 处置命令：keep_independent、set_variant_of、keep_both、revise、return、reject
- [x] 持久化 disposition 只有四种：pending、keep_independent、variant_of、rejected
- [x] set_variant_of 的复合写入：同组、主问题须为根、禁自引用、禁环、禁 variant-of-variant
- [x] 变体继承主问题的 logical_question_id、split_group_id 与 dataset_split
- [x] ReviewOperation journal 结构与乐观版本检查
- [x] development / machine_validated / human_confirmed 三套显式导出等级
- [x] 各等级独立 eligibility predicate 与排除原因
- [x] manifest 记录等级、处置统计与待处置组数
- [x] 修复仓库"最新版本"判定：显式记录写入顺序

### 新增模块

- `generation/review_operations.py`：ReviewCommand、ReviewOperation、ReviewDisposition
  应用函数、check_expected_versions

### 关键决定

- 处置变更产生新不可变版本并写入 changes 链，使版本祖先关系保持完整。它不改变
  证据校验所依赖的字段，所以既有的证据校验记录继续适用。
- 人工确认（confirm）本身即"保留为独立问题"，会置 disposition 为 keep_independent。
- mock 检查按问题而非按工作区：没有导出问题引用的 mock bundle 不污染导出。
- machine 等级不要求 disposition 已解决（允许 pending 快照），human_confirmed 等级要求
  组内处置完成。表达变体不独立导出，只随主问题并入。
- 仓库的"最新版本"改为显式记录写入顺序。原先用 mtime 加版本号文本比较，
  同一文件系统 tick 内写入的两个版本无法区分，且 "v1" 的文本序排在 "v99" 之后。

## 2026-10-20 Step 10 补完：journal 持久化与分组审核页

- [x] ReviewOperation 持久化 journal：命令先入 journal，写完再置 committed
- [x] reconciliation：未完成的 operation 被判定为中断并回滚
- [x] 版本已变动的 operation 记为 version_conflict，不强行应用
- [x] 分组审核页面 /groups：并列展示问题、答案、KG 边、逐 hop 文本证据、阻塞失败、提示、排序因素
- [x] 分组审核 API：六个处置命令，含乐观版本检查
- [x] 界面不使用"系统已判定重复"一类误导表述

### 新增模块

- `generation/review_service.py`：ReviewService，命令执行 + journal + reconciliation
- `web/group_view.py`：GroupView、CandidateView、build_groups
- `web/templates/groups.html`：分组审核页面

### 关键决定

- journal 先写后做。命令在写任何记录之前先入 journal，因此中断留下的是一条记录了
  意图的 pending 条目，而不是"什么都不知道"的状态。
- reconciliation 保守：版本已变动 → version_conflict，不强行应用；版本未变但未完成
  → interrupted 并回滚。悬空变体比没有决定更糟。
- set_variant_of 会给主问题也产生新版本：身份不变（仍 keep_independent），
  但"多了一个变体"必须落到不可变版本上。
- 页面只呈现事实与排序因素，不做判断。文案明确写"是否重复由人工判断"。

### 测试基线

- 默认：744 项通过、13 项真实 PrimeKG deselected
- 真实 PrimeKG 只读：13 项通过

## 2026-10-20 Step 11 垂直验收与文档收口

施工依据：`plans/real-generation-agent-implementation-plan.md` Step 11。

- [x] 固定验收清单：4 个真实 PrimeKG 种子 + 快照 sha256 + policy/validator/group/ranking 版本
- [x] 固定 eval manifest 契约与读取/校验
- [x] 验收 runner 与报告：种子产出、路线分布、分组统计、处置统计、coverage gap、usage
- [x] 独立运行 rule-based 基线验收
- [x] 独立运行多跳可达性验收（脚本 planner，真实图）
- [x] 产出验收报告 docs/acceptance-report-v1.md
- [x] 产出运行手册 docs/running-guide.md
- [x] 更新 README，使能力声明与实际代码一致

### 新增模块

- `evaluation/manifest.py`：EvalManifest 与固定项校验
- `evaluation/runner.py`：AcceptanceReport、SeedOutcome、报告派生
- `evaluation/react_run.py`：真实图上的多跳可达性测量
- `evaluation/cli.py`：`eval run` 与 `eval react`
- `eval/manifests/baseline-v1.json`：基线验收清单

### 验收结果

- 结论 passed：4 个种子全部完成，产出 11 个问题、7 个审核分组，环境与清单一致。
- 多跳可达性：ibuprofen / warfarin / metformin 在真实图上产出 2-hop 与 3-hop 路径；
  aspirin 因 PrimeKG 中只有 2 条边只能产出 2-hop，与其度数一致。
- 未覆盖：文献证据与逐 hop coverage、三路消融、structured 真实模型、独立答案复算、
  人工处置统计。详见验收报告的"未覆盖项"。

### 本次运行修掉的问题

- 边预算刚好耗尽时整轮作废并丢弃已采集的证据。改为有证据时继续后续阶段。
- 报告的 question_count 只统计种子产出，与工作区实际问题数可能不一致。
- 报告的 route 分布最初借用 expected_route。改为使用 verified_route——未经过消融的
  问题就是 unknown，报告不替它借一个。

### 测试基线

- 默认：771 项通过、13 项真实 PrimeKG deselected
- 真实 PrimeKG 只读：13 项通过

## 2026-10-20 下一步

Step 0—11 的合同与骨架已全部落地。剩下的不再是"缺功能"，而是"缺一次真实联调"：

- 启动本地 WeKnora，准备一个有医学文献的知识库与 API key，补上文献 provider 的
  语料快照固定与 read_chunk 回读验证。完成后 `hybrid_per_hop` 的逐 hop 文本
  coverage、三路来源消融与 verified route 才能进入正式验收。
- 配置一个真实模型 provider，跑一次 `structured` 模式的端到端，验证 ReAct 循环
  在真实模型下的观察依赖决策。
- 在有真实候选的组上做一次人工处置，记录保留率与改排率。

## 当前基线

- [x] `GenerationTask`、`EvidenceBundle`、`QuestionSpec` Pydantic 契约
- [x] GenerationTask、EvidenceBundle、QuestionSpec JSON Schema 导出
- [x] 自动、手动、混合生成模式的数据字段
- [x] 不可覆盖的本地版本化 JSON 仓库
- [x] 任务状态转换与修改记录
- [x] KG、文献工具和问题生成 Agent 的接口协议
- [x] 固定价格成本模型与预算账本
- [x] Mock 种子到问题草稿的命令行流程
- [x] 本地审核工作台
- [x] 人工确认、退回、拒绝及 claim 判定
- [x] 开发快照和正式导出门禁框架
- [x] 可执行的自动校验器（11 项校验，含答案泄漏与重复检测）
- [x] `draft` → `machine_validated` → `human_confirmed` 状态流
- [x] 校验 CLI（`validate run` / `validate status`）
- [x] 现有自动化测试通过：316 项（默认非真实库）；真实 PrimeKG 只读集成测试 10 项单独执行
- [x] 运行依赖锁定：`requirements.txt` + `constraints.txt` + `pyproject.toml` 的 `dependencies`
- [x] 修复 `RuleBasedAnswerVerifier` 把「候选答案位于 edge.head」误判为 `answer_is_seed`
- [x] 分离 `rule-based` / `fake` / `structured` 三种装配，模式名称不再误导
- [ ] 受控 ReAct Observe → Decide → Act 循环
- [ ] Exploration Planner 的动态动作选择
- [ ] Evidence Sufficiency Agent
- [x] 有界二跳路径组装 `knowledge_graph/path_builder.py`（KG 层 `OrderedPath`，项目侧组装）
- [ ] 有序、可逐跳验证的二跳证据路径接入生成流程（依赖 Step 3/4/5）
- [ ] 多跳 claim 与多跳问题蓝图
- [ ] 真实结构化模型调用（`structured` 模式）
- [ ] 独立 Answer Verifier 答案复算
- [ ] 基于 verifier 反馈的真实重写

## P0：正式出数前必须完成

### 1. 接入真实证据工具

- [ ] 实现 PrimeKG 适配器，替换 `DemoKGTool`
- [ ] 明确使用 `primekg.sqlite` 还是冻结后的图快照作为默认数据源
- [ ] 为 PrimeKG 记录稳定版本、文件哈希、查询参数和 trace 信息
- [ ] 实现旧文献 RAG 适配器，替换 `DemoLiteratureTool`
- [ ] 核实旧 RAG 的调用方式、鉴权、返回字段、错误类型和索引版本
- [ ] 保留文献 PMID、DOI、标题、片段位置、检索查询和许可信息
- [ ] 区分空结果、超时、鉴权失败、解析失败和真实无证据
- [ ] 接入真实 claim／问题生成模型，替换 `DemoDraftAgent`
- [ ] 为模型调用保存模型版本、提示词版本、温度、token 用量和调用 ID

**完成标准**

- 一个真实种子可以同时返回 PrimeKG 和真实文献证据。
- 所有证据都包含稳定来源标识，且不含 `endpoint_kind=mock`。
- 工具失败不会被错误解释为“没有证据”。

### 2. 完成原始数据侦察和冻结快照

- [ ] 验证 BioHopR 的 hop 链能否在 PrimeKG 中查到
- [ ] 输出实体匹配率、关系覆盖率和链路可达率报告
- [ ] 明确实体规范 ID：PrimeKG ID、UMLS CUI 或多 ID 映射
- [ ] 从 BioHopR 涉及实体抽取 1～2 跳邻域
- [ ] 保留干扰边、死路和无答案案例
- [ ] 冻结图快照并记录生成脚本、参数、大小和 SHA256
- [ ] 禁止直接修改 `datasets/` 下的原始文件

**完成标准**

- 任意实验都可以从固定图快照重复运行。
- 快照可以追溯到原始 PrimeKG 文件及生成配置。

### 3. 实现真正的自动校验器

- [x] 定义每类正式导出必须存在的校验项
- [x] 实现 Schema 和跨对象引用完整性校验
- [x] 实现 gold answer 与 evidence requirement 一致性校验
- [x] 实现 claim 支持证据检查
- [x] 实现证据充分性检查
- [x] 实现问题答案泄漏检查 `question_leakage`
- [x] 实现近重复与完全重复检测 `duplicate_detection`
- [x] 实现问题变体语义等价检查
- [x] 实现自然度和模板化程度检查
- [x] 实现 taxonomy 一致性检查
- [x] 实现来源可引用性和文献标识有效性检查
- [x] 保存校验器版本、输入摘要、结果和明确失败原因
- [x] 增加执行全部校验的 CLI／服务入口

**完成标准**

- 不是只保存 `ValidationCheck`，而是能够真正运行并产生结果。
- 修改依赖字段后，相关校验自动失效并能够重新执行。
- 工作台能够显示每项校验的状态、版本和失败原因。

### 4. 修复确认与正式导出门禁

- [x] 定义正式导出所需校验项的固定集合
- [x] 校验记录缺失时必须视为 `not_run`，不能视为通过
- [x] 阻止存在 `failed` 阻塞校验的问题被确认为可正式导出
- [x] 检查 required claim 和 required evidence 是否真实存在于关联 bundle
- [x] 检查 gold claim 是否至少有一个明确支持证据
- [x] 检查非 mock 数据是否混入 mock evidence 或 mock snapshot
- [x] 决定正式导出是“整批冻结”还是“只导出合格子集”（只导出合格子集）
- [x] 让导出行为与 PRD 中“失败样本不阻塞其他样本”的原则一致
- [x] 为上述门禁增加正反测试

**完成标准**

- 不存在任何“未运行校验但可以正式导出”的路径。
- 正式导出的每条问题均满足同一套可解释、可复现门禁。

### 5. 实现来源消融

- [ ] 实现 KG-only 求解
- [ ] 实现 Literature-only 求解
- [ ] 实现 Combined 求解
- [ ] 保存每次消融的答案、证据覆盖率、停止结论、失败原因和成本
- [ ] 根据三路结果确定 `verified_route`
- [ ] 区分 `kg`、`literature`、`hybrid`、`redundant`、`unanswerable`
- [ ] 明确“充分回答”由规则、模型还是人工判定
- [ ] 防止直接把生成时使用的来源当作最终路线标签

**完成标准**

- 每条正式问题都有三路消融结果或明确的豁免原因。
- `verified_route` 可以由消融记录复算得到。

## P1：MVP 闭环必须完成

### 6. 补全审核工作台编辑能力

- [x] 修复“标准问题”输入框修改后未提交的问题
- [x] 前端提交真实 `changed_fields`
- [x] 支持编辑问题变体
- [ ] 支持编辑 gold answer 的结构化值、别名和解释（已支持解释与 short answer）
- [ ] 支持编辑 evidence requirements
- [ ] 支持编辑 taxonomy 和 query intent
- [ ] 支持选择或移除必需证据（已可提交，界面待补勾选控件）
- [ ] 支持 claim 文本、证据关联和判定理由的修订
- [ ] 编辑后生成新版本，禁止原地覆盖
- [x] 在界面中展示字段级修改摘要和父版本
- [ ] 增加浏览器级端到端测试

### 7. 实现机器校验状态流

- [x] 定义从 `draft` 到 `machine_validated` 的转换条件
- [x] 自动模式通过全部阻塞校验后写入 `machine_validated`
- [x] 自动流程不能写入 `human_confirmed`
- [x] 人工确认必须明确记录操作者、说明和时间
- [x] 支持导出 machine-validated 数据并在 manifest 中标明等级
- [ ] 区分机器数据可用于训练、调试还是正式评测

### 8. 实现可靠的数据划分

- [ ] 以 `logical_question_id`／`split_group_id` 为原子进行划分
- [ ] 同一逻辑问题的不同表达不得跨 train、validation、test
- [ ] 相同实体组合或近重复问题的跨集合泄漏需要检测
- [ ] 明确实体隔离、模板隔离和主题分层策略
- [ ] 固定划分随机种子与算法版本
- [ ] 在导出前运行跨 split 泄漏检查
- [ ] 输出每个 split 的路线、主题、答案形式和难度分布

### 9. 跑通 5 个种子、约 20 题的真实 MVP

- [ ] 选择覆盖药物、疾病、基因／蛋白等类型的 5 个种子
- [ ] 生成约 20 道候选问题
- [ ] 覆盖 KG、Literature、Hybrid 和 Unanswerable 路线
- [ ] 覆盖单事实、受约束列表、多跳、机制解释和证据验证
- [ ] 每个底层问题生成多个自然语言表达
- [ ] 运行全部机器校验
- [ ] 运行三路来源消融
- [ ] 完成人工审核、退回、修订和拒绝流程
- [ ] 导出带 manifest 的正式数据集
- [ ] 输出 MVP 质量报告和失败样本报告

### 10. 建立 MVP 质量指标

- [ ] 人工自然度通过率
- [ ] 医学事实拒绝率
- [ ] 来源消融通过率
- [ ] 近重复率
- [ ] 路线分布与问题类型分布
- [ ] 单题生成 token、耗时和金额
- [ ] 单题及 episode 平均 `PolicyCost`
- [ ] 人工平均审核时长
- [ ] 工具空结果率、失败率和重试率
- [ ] 在首批人工数据完成后确定质量阈值

## P2：MVP 后补齐

### 11. 补全公共数据契约

- [ ] 为 `DatasetManifest` 建立正式模型和 JSON Schema
- [ ] 为 `Trajectory`、`TrajectoryStep`、`CandidateAction` 建立模型和 Schema
- [ ] 为 `ChangeRecord` 建立可独立发布的 Schema
- [ ] 为 `UsageRecord`、`PolicyCost`、`CostModelSnapshot` 建立公共 Schema
- [ ] 增加 Schema 兼容性和版本迁移测试
- [ ] 明确 v0 到后续版本的兼容策略

### 12. 与 query planner 对接

- [ ] 实现 QuestionSpec 到 planner 输入对象的适配器
- [ ] 确保 gold answer 和 gold evidence 不进入运行时 PolicyState
- [ ] 从空证据状态生成轨迹
- [ ] 保存每步完整候选动作集合、选择结果和工具执行结果
- [ ] 实现参考动作／oracle 或 heuristic reference
- [ ] 对接冻结成本模型并计算 regret
- [ ] 支持轨迹回放和错误定位

### 13. 扩展到试验数据集

- [ ] 扩展到约 40 个种子
- [ ] 形成约 200 道审核后问题
- [ ] 系统性完成来源消融
- [ ] 完成问题分类、分布和失败分析报告
- [ ] 校准正式工具价格与预算
- [ ] 冻结试验数据版本、工具快照和成本模型

### 14. 工程化与发布准备

- [ ] 将项目纳入明确的 Git 仓库和版本发布流程
- [ ] 增加 CI：测试、Schema 漂移、静态检查和构建
- [ ] 增加依赖锁定或可复现环境文件
- [ ] 增加测试覆盖率工具和最低阈值
- [ ] 清理生成的 `__pycache__`、临时文件和本地 smoke 产物
  （`__pycache__` 已由 `.gitignore` 排除，不应作为项目产物或测试计数对象）
- [ ] 增加真实适配器的集成测试与可控 fixture
- [ ] 增加大文件、超时、断点恢复和并发写入测试
- [ ] 明确数据公开范围、文献版权、许可和隐私规则
- [ ] 补充运行手册、故障排查和数据发布说明

## 尚待决策

- [ ] 第一版目标用户是否只包含生物医学研究人员
- [ ] 文献 RAG 的正式接口与索引版本能力
- [ ] 文献证据允许使用的数据源、时间范围和许可边界
- [ ] 实体规范 ID 及实体链接置信度阈值
- [ ] 哪些样本必须人工确认
- [ ] 机器校验数据可用于哪些训练和评测场景
- [ ] 来源消融“充分回答”的判定方法
- [ ] 正式固定预算、价格表和成本分量权重
- [ ] 数据集是否公开以及公开字段范围

## 建议执行顺序


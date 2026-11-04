# 分层出题循环（2026-11-03）

Web 新建、恢复和 CLI 统一使用 HierarchicalMainAgent，原 StructuredMainAgent 保留供旧协议测试与兼容代码使用，生产入口不再用它自由选择所有阶段。

## 依赖

探索检索 → 理解本批新材料 → 候选设计及可用性判断 → 候选队列。

候选队列：定向独立取证 → 验证 → 冻结 → 写题 → 发布审核 → 入库。

- 可用性改写最多一次并复审；淘汰候选不进入队列。
- 初始取证由候选验证流程发起，只为当前问题寻找支持、竞争答案和反证。
- 验证给出具体 evidence_need 后，可以再次定向取证，然后返回同一道题的验证。验证材料不会被送回开放探索。
- 验证明确失败则退出。补证无新增锚点也退出，不重复验证同一份材料。
- 公开题干审核失败可以按冻结契约修改一次并复审；答案或事实审核失败直接退出。每版题干和报告独立保存。
- 同级候选按已执行工作量轮转；已有验证或写题结果的候选优先完成，不让缺证据的候选垄断队列。当前仍是串行执行，没有并发模型调用。

## 决策边界

LLM workflow planner 仅选择 exploration / verification_gap 内的检索方向或结束该循环；传入具体缺口、先前查询、结果和失败信息。阶段推进由宿主完成。题数是目标，允许有理由地少出题或零产出；探索自然结束标记 COMPLETED 或 COMPLETED_WITH_ZERO_QUESTIONS，运行级故障/总动作预算耗尽仍进入恢复状态。

探索响应允许 retrieve_materials / stop。局部补证使用单独 schema，仅允许 retrieve_materials / finish_search / search_exhausted；没有含糊的 stop。结束收集时，未验证的本候选证据先送答案验证；没有独立证据则以 no_independent_verification_evidence 关闭为 INDETERMINATE，已有证据验证后仍有缺口则以 unresolved_verification_gap 关闭。检索模型的“证据充分”文字不能直接成为候选关闭或通过的依据。

初次独立取证收到显式 initial_independent_verification 需求和发现阶段锚点列表，不再拿候选评审 KEEP 充当 evidence_gap，也不向局部检索传入历史评审。后续补证只使用验证阶段产生的缺口。探索模型同时收到 candidate_outcomes（当前状态、答案验证结果与原因、发布评审和关闭原因）及 published_question_count，历史 KEEP 不能覆盖这些最终结果。允许零产出，但停止理由应明确真实产出及是否仍有值得探索的方向。

## 初始预算

外层探索检索上限等于产出题数上限 × 3 × 难度系数。简单取 1，中等取 2，困难取 4。混合难度取最高系数，未指定难度取 1。恢复任务时，已经执行的外层检索仍计入预算。

每个候选最多 2 次定向取证（首次及一次补证），每次取证最多 2 轮搜索、每轮 2 个查询。候选阶段累计 2 次执行失败后退出。题干发布审核修改最多一次。候选设计对同一事实集合只运行一批，之后要有新增事实才再设计。以上是宿主初始上限，语义上无进展可提前结束，不要求耗满。

## 恢复与记录

次数从已提交 decisions/artifacts 推导，暂停恢复不会清零。pending decision 按原流程执行后再进入新调度。verification_gap、wording_repair_request、candidate_closure 保存回退原因；摘要给出 workflow_phase 和 active_hypothesis_id。旧任务已有产物不删除，恢复后使用新调度；已有终止候选不会自动复活。

## 验证范围

使用 fake 模型回归完整出题、自然停止、部分产出、验证补证回环、无新增证据退出、题干改写复审、候选轮转和暂停恢复。尚未进行真实模型长任务和跨实体质量评测。

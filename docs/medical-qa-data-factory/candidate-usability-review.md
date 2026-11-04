# 候选题可用性判断

生产入口：ObjectiveQuestionAgentLoop._design_questions。流程：设计候选 → 独立 LLM 可用性判断 → 必要时一次改写和复审 → 保存候选与判断 → 主 agent 补证或验证。

使用 skills/objective-candidate-review/SKILL.md；模型角色 objective_candidate_review。每批一次调用，仅发送本批候选、关联事实及完整证据摘录。判断不读取设计者的自评。

KEEP 进入常规验证；NEEDS_EVIDENCE 必须说明具体缺口，主 agent 根据判断补证；REJECT 在运行状态中标记为 REJECTED，保留审计，不进入活动候选；REWRITE 仅修改题干和公开范围，不改变答案或事实，复审仍要求改写则淘汰。

candidate_usability_review 保存最终判断及复审历史，主 agent 能读取理由。判断缺失、ID 不匹配、调用失败不会默认放行。现有重复候选控制仍然生效。

没有 embedding 筛选或禁词判断。简单题可以通过。单元测试覆盖流程路由和拦截，不代表真实模型的语义判断准确率已经评测。

接入仅作用于之后执行的候选设计；已存在的运行候选不会被后台追溯改写或淘汰。后端需重启加载新逻辑。

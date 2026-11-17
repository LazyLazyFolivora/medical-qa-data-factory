# 依赖接线后的 APOE 实际运行

## 设置与记录

生产 CLI / ObjectiveQuestionAgentLoop；当前默认 Qwen，131072 上下文；APOE，free 策略，题目目标 1，难度字段默认 easy；24 动作、70 模型调用预算；PrimeKG 和 PubMed。
没有手工指定文章、候选或动作，没有修改生成结果。

- 首轮：data/experiments/apoe-dependency-live-20261112。PubMed 请求遭 WinError 10013 权限阻断，11 个动作后 0 题结束。完整保留，不能用于判断出题能力。
- 联网重跑：data/experiments/apoe-dependency-live-network-20261112。9 个动作完成，发布 1 题。
- 两轮 debug_trace 保存请求、响应、工具产物；objective_runtime 保存状态；release 保存发布及审计结果。

## 实际路径

PrimeKG 检索 → 读取图谱材料 → PubMed 检索 → 读取摘要 → 原文直接出题 → 候选评审 KEEP → 答案验证通过 → 发布 → 停止。

未获取正文，未调用 synthesize_evidence，未产生 candidate_research_context；因此本轮没有覆盖新增 research_id 单独出题路径，不能归因于本次依赖改动，也不是改动前后受控比较。

## 原始题目

How do APOE isoforms differentially regulate neuroproteasome function and tau pathology in Alzheimer's disease?

原始答案要点：膜上神经蛋白酶体丰度 E2 > E3 > E4；ApoE4 神经元在蛋白酶体受扰后易积累 tau 聚集体，ApoE2 具有抵抗表现。

## 对保存材料的核对

核心来源 pubmed:42215643 摘要直接陈述上述丰度排序，以及 modest neuroproteasome disruption 后 ApoE4 与 ApoE2 神经元的差异；选择性抑制实验在 primary neurons 和 mouse brain 中观察 tau PHF。
另一个来源 pubmed:40275327 是 APOE 与 AD 综述，提供一般背景，对本题核心答案不是必需。不是必须多路整合才能回答的题目。

判断：核心回答有摘要支持，明显比无界限的组织罗列有信息价值；但题干的 how/regulate 容易让人期待更具体的调节机制，且没有明确实验背景。当前答案更适合“有什么差异、受抑制后出现什么表现”的问题。模型评审和最终复核均未要求收窄，不能把自动发布等同于已经完全合格。

本轮证明当前生产链路在联网正常时可以自主产生并发布一题；未证明新依赖提升出题质量或跨实体稳定性。尚需单独覆盖自主选择整合工具的真实运行，或者明确标注受控接线测试。

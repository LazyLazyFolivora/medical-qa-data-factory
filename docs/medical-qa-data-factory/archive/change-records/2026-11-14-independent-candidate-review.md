# 候选评审保持独立：真实对照与改动

当前仍未达到稳定生成合格问题的目标。上一轮有实际改动和真实模型结果，本轮继续检查运行结果并完成以下对照。

## 新发现

BRCA1 `brca1-20261114-015227` 正常结束为 COMPLETED，但实际只有 1 个 PUBLISHED、2 个 RELEASE_REVIEWED。主模型停止理由却称两题已发布；最后请求中 published_count 和 counted_toward_target 都明确是 1。不能把模型理由当实际产出，也不能将这个运行算两题成功。此问题本轮未修复。

简短出题提示词实验 `discovery-concise-20261114-015512` 使用同一原文和 focus，仍产生把通路写进题干又询问如何作用的问题。没有采用该实验提示词，正式 discovery skill 不因此重写。实验系统提示词和实际请求都保留。

## 候选评审对照

实验目录均在 `data/experiments/question-first-lab`。

- `candidate-unknown-20261114-020200`：当前 skill 重放原候选，REWRITE，正确发现答案重复题干。但建议改成“哪些实验证据支持”，重新偏向论文阅读题。
- 将 skill 的 REWRITE 条目改成要求修订题遵循同一领域问题标准，而非将解释缺口改为论文证据摘录。
- `candidate-unknown-20261114-020308`：仍然 KEEP，理由明确引用后台 research_context.answer_boundaries 来辩护公开题干遗漏模型条件。该结果不能说明修订规则有效。
- `candidate-independent-20261114-021032`：同一输入、同一 skill，只将 research_context 保留 source_passages，删除上游判断和作者范围说明。模型 REWRITE，指出重复题干和模型条件遗漏。但 revised_question 仍包含重复关系，修题质量尚未解决。

单次对照提示了干扰来源，不能证明普遍成功率改善。

## 正式改动

`react_tools.py` 的 review_candidate 调用只传递 research_context.source_passages；全部原文段落仍在，claims、evidence_excerpts、hypotheses、previous_review 均保留。上游 approved_question、question_review、answer_boundaries 等模型判断不再充当评审背景。它们仍存在持久化记录，不删除历史，不修改候选。

没有 candidate_context 的旧候选保留兼容行为，以空 source_passages 配合已有 evidence_excerpts 评审。静态审查发现的 None.get 回归已修复，最终静态审查通过。没有新增评审环节，没有靠字符串规则判断题目质量。

## 后续实际验证

从 LDLR `step-04-before.json` 隔离恢复完整生产链路，观察独立评审后是否真的修正公开问句、答案是否补充未知关系，再判断是否应保留此改变。保留每一步请求、响应和检查点。运行启动不等于验证完成。

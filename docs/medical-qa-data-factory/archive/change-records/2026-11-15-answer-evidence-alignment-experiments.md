# 答案强度对照：真实模型实验

## 问题

PCSK9 实际任务已形成独立的领域问题：PCSK9 结合 LDLR 后，为什么使受体降解而不回收？但答案把原文 `appears to involve` 的切割机制写成确定的因果步骤。HFE 任务曾将一次研究中的无统计学显著差异写成没有影响。两份模型答案一致不能排除这些错误。

本次判断的是给定原文是否支持答案措辞，不是宣称这些科学命题在所有文献中最终为真或为假。PCSK9 摘要结论比结果段更肯定，尤其需要保留这种内部强度差异。

## 实验与结果

全部使用配置中的真实 Qwen，实验目录位于 `data/experiments/question-first-lab/`。每次保留输入、prompt、schema、模型响应及结果。未更改正式模型配置、生产数据库或后端进程。

| 目录 | 方法 | 观察 |
| --- | --- | --- |
| answer-strength-pairs-20261115 | 单独判断 claim/source 是否支持，五个案例 | 五个都判支持，漏掉两个强度升级 |
| answer-strength-guided-20261115 | 明确说明未显著不等于无作用、可能机制不等于确定机制 | HFE 错误被发现，PCSK9 仍漏检；三个正确对照保留 |
| answer-strength-aligned-20261115 | 先输出直接原句、原文限定、主张限定，再判断 | 六个案例均符合人工预期：两个过强、三个有据、一个错误结合结构域无据 |
| actual-answer-aligned-20261115 | 换成实际完整 PCSK9 候选答案，以及保留限定的人工对照 | 原答案判过强，限定版本判支持；人工版本只是诊断对照，不算系统生成成功 |
| full-review-aligned-20261115 | 完整复核前增加结构化主张对照 | HFE 识别盲答问题并建议 revise，但 explanation 又写出没有影响；PCSK9 此次误将请求外层包装作为 payload，不用于公平比较 |
| full-review-aligned-corrected-20261115 | PCSK9 修正为真实 payload 后原样复测 | 候选与盲答的过强步骤均被指出，建议 revise，未把合理问题直接拒绝 |

## 限制与下一步

小样本不能证明稳定性，也不能凭一次正例把检查不断叠加。当前可行方向是在既有最终复核中先对齐原句与主张，再给出结论，而不是额外重复一次全能评审。原文缺失、答案错误和题目不适合必须分开处理。

同时，CFTR 真实链路出现了另一种浪费：独立读者已接受结合位点问题，后续候选评审仍因轻微冗余要求改写，建议题反而扩大到“突变型 CFTR（如……）”。应检查重复的题目评审职责，避免把普通润色当成阻断性错误。

上述完整运行结束后接入了正式验证器。不能把 PUBLISHED 状态作为质量认证。

## 接入及真实回放

- `answer_verification_subagent.py`：最终复核使用 `FinalEvidenceReview(claim_checks, review)`，仍为原来的一次模型调用。解包后保持原有 `FinalQuestionReview` 下游接口。负面检查与 accept 矛盾时走原有结构化修复；这是合同一致性，不是用程序判医学事实。检查引用同样必须属于本次可见锚点。
- `agent_loop.py`：完整 claim_checks 写入 final_question_review artifact；旧 Outcome 构造默认空 tuple。原始请求响应仍在 trace 中。
- `objective-final-verification/SKILL.md`：先原句与主张对照，再决定修订、补证或接受。明确原文限定、统计观察与无效应结论的差别。
- Python 变更经既有 reviewer 审查，通过；没有运行 mock 测试，以真实模型回放观测行为。

`production-final-review-20261115`：当前正式 skill/schema 在 HBB 位置正例上正常接受；HFE 仍漏检无显著差异到无影响的升级。结构化对照有改进迹象，但仍不可靠，不能宣称消除幻觉。

`pcsk9-20261115-061032`：从既有 step-06-after 验证前断点回放，发现答案过强并退回 revise；随后四次修订把机制问题改成“原文如何描述确定性”，均不符合领域问题目标，未成功发布。此进程初始载入的是包装改动版本，尚不含之后补充的跨字段一致性校验及 claim_checks 持久化，不能用它验证后两项。

据此在 `objective-react-agent`、`objective-question-design` 的既有修订段落明确：按 repair_issues.target 修订；仅答案有误时保留原 question_intent，修答案限定，不把证据判断改成新的阅读理解问题。

新进程 `pcsk9-20261115-061404` 从同一验证前断点检验当前完整版本：首次复核识别了过强主张，审计 artifact 已保存两条 claim_checks；新候选 `hypothesis.1b664a9677192d11bc632a2c` 保持机制问题，仅补充 LDLR 全称，答案恢复“似乎涉及”限定，候选评审为 KEEP。这是修订方向改善的实际证据。

但主 agent 下一步把 KEEP 当成已验证且已发布，选择 close_candidate，closure_reason 声称已发布。宿主没有发布该题，而是将其关闭为 INDETERMINATE。随后主 agent 转去发现第二方向。因此本轮仍未取得完整的自主成功；下一问题是状态误读与误用关闭工具，而非答案修订没有生效。还需验证该修订答案的最终证据结论，不能把评审 KEEP 当成完成。

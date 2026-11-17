# 明确的候选修订操作

## 故障依据
TP53 主 agent 在收到 REWRITE 及原样复用评审结果后，仍再次 review_candidate，明确说希望取得 KEEP。APOE 默认配置完整运行24动作结束时0发布，4个候选题干完全相同，评审要求修改范围而模型反复复评/复制。说明旧设计中修改藏于 design_questions 的 revision_of 参数，模型未可靠区分“评审建议”和“实际修改”。

## 改动
新增 revise_candidate 操作：参数 hypothesis_id 与 revision_instruction。程序取该候选已保存的原文锚点和最新验证/评审反馈，复用现有设计子 agent，明确请求生成1个修订候选。输出通过原有候选持久化，记录 revision_of/new_hypothesis_ids，须重新评审和验证。原题保留，不继承批准、不自动发布或关闭。

ReACT 的工具描述、解码契约、枚举、角色、执行分发已接通。保留 design_questions revision_of/source_sections 用于需要另选证据的修订。发布题不能走该候选修订工具；无原文锚点、超32000证据预算会报明确反馈。

## 真实验证
`tp53-revision-tool-20261115-034751`：在原TP53检查点克隆中固定一次工具调度，实际模型使用正式默认配置；生成 `hypothesis.0fed5bd08ba0c0b5ff2cec72`，移除Class II限定，原文锚点保留且新旧关联落盘。这证明工具执行链可用，不证明主 agent 会自主选对工具，也不证明新题最终合格。

代码审查通过。请求1个候选仍由既有模型输出语义控制，不新增截断输出规则；原文锚点按既有持久化信任边界复用。

APOE 原实验 `apoe-20261115-033415` 已正常结束，随后从其 after.json 继续10动作，使用新工具与正式默认模型，观察主 agent 自主决策。完整输入输出均保留。

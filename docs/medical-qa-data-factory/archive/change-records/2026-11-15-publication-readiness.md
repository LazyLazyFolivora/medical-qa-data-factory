# 将发布前提放在同一状态视图

TP53 实验 `tp53-20261115-025244` 在16动作上限结束，0发布。旧候选已通过答案核验，但后来被 candidate_review 要求 REWRITE；主 agent 仍回头尝试旧题。原 publication_readiness 只显示 reader acceptance 和 answer_verified，候选审查意见放在另一个数组里。

新增 publication_readiness 函数，统一返回现有发布前提：题目读者接受、实际答案发布提案、最新 candidate review、目标剩余容量，以及 publishable/blockers。没有改变发布规则，也没有新增语义判定。

真实检查点复算：旧题 reader=true/answer_verified=true，但 candidate_review=REWRITE，因此 publishable=false；新修订题尚缺题目评审和答案提案，明确列出缺项。输出保存为原运行下 publication-readiness-audit.json。代码审查确认与现有发布守卫一致。

`tp53-20261115-033811` 从原 after.json 继续8动作，保持实验推理=true、输出8192，使用新代码。原进程已正常终止，未重启仍在执行的任务。继续运行的效果仍待观察。

另一个新实体完整运行 `apoe-20261115-033415` 使用正式默认模型配置、目标2题、最多24动作，验证多实体质量。

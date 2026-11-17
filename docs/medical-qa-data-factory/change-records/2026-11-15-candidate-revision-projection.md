# 候选版本关系进入主agent待办

## 实际问题
在保存的HFE真实状态中，fcd6、038b、2b16、39d4等是同一问题的修订链，4003为另一个分支。它们都留在PROPOSED列表。主agent把更早2b16误称为当前39d4的新版本，重新评审旧题。

## 实施
依据已有question_design_notes中的revision_of/new_hypothesis_ids，投影明确后继关系。只有已注册、存在的候选才成为后继。旧版本标明superseded_by，历史全部保留，不改状态，不删原文。

旧版不再进入review/publish的工具目标枚举；其publication_readiness有superseded_by_revision阻断。执行层review/verify/publish也检查版本，避免历史待执行动作绕过新枚举。read、close、显式revise不受此检查影响，允许保留历史与从旧版发起新分支。主待办中的publication/verification_readiness只显示未被替代版本。

## 验证
实际HFE快照保存于revision-projection-20261115/actual-state-check.json：5个历史候选投影出3个有后继的旧版、2个当前分支，旧版的review/verify/publish共9个执行请求正确阻断。初版执行检查误放在revise分支，被审阅和真实状态断言共同发现，现已移到公共候选分支并复核批准。

真实主agent回放选择4003（仍有效的另一分支），不再选择已被替代的2b16。但其解释依然混淆了39d4与4003，不能宣称模型理解完全正确或已会重新选题。完整请求响应保留。

此改动不替模型判断科学质量，也不证明剩余分支合格。

## HFE当前完整运行终态
hfe-20261115-053045已退出，20步，1题PUBLISHED，另3个候选。发表题为健康对照中HFE常见变异携带者与野生型的QSM/R2*是否显著不同，仍主要是单项研究统计结论，不计完整质量通过。原题的最终验证还把无统计显著直接推为没有影响，来源为PMID42282212的p18，不能据此声称科学结论已可靠验证。

# 本轮保留的问题样例

以下是从真实模型运行中逐项对照保存的原文后保留的样例。不是所有 PUBLISHED 结果的自动汇总，也不代表整轮合格率；未支持的外推和论文指标摘录已排除。答案使用运行中保存的文本，不人工另造理想答案。

## 1. Ivacaftor针对哪类CFTR突变缺陷发挥药理作用，其对应的分子机制是什么？

**保存的答案**

Ivacaftor targets CFTR mutations that produce a gating defect (e.g., the G551D mutation), which is characterized by normal membrane localization but failure to open. Its molecular mechanism is to act as a CFTR potentiator that promotes channel opening.

**来源标识**：pubmed:24004658

**运行目录**：`cftr-20261113-192433`

**检查范围**：Model review of saved originals; isolated experiment publication, not clinical gold-standard adjudication.

[运行终态](E:/Cursor/assumption/projects/medical-qa-data-factory/data/experiments/question-first-lab/cftr-20261113-192433/after.json) · [请求与响应](E:/Cursor/assumption/projects/medical-qa-data-factory/data/experiments/question-first-lab/cftr-20261113-192433/prompt-and-output.md)

## 2. 双等位基因BRCA1功能丧失变异导致的临床表现特征是什么，其表型与范科尼贫血（Fanconi Anemia）有何关联？

**保存的答案**

双等位基因BRCA1功能丧失变异导致的临床表现特征包括：小头畸形（microcephaly）、身材矮小（short stature）、神经发育迟缓（neurodevelopmental delay）、特殊面容（dysmorphic features）以及癌症易感性（cancer predisposition）。这些特征共同构成了范科尼贫血样表型（Fanconi-anemia-like phenotype）。

**来源标识**：pubmed:29133208

**运行目录**：`brca1-20261113-192529`

**检查范围**：Model review of saved originals; isolated experiment publication, not clinical gold-standard adjudication.

[运行终态](E:/Cursor/assumption/projects/medical-qa-data-factory/data/experiments/question-first-lab/brca1-20261113-192529/after.json) · [请求与响应](E:/Cursor/assumption/projects/medical-qa-data-factory/data/experiments/question-first-lab/brca1-20261113-192529/prompt-and-output.md)

## 3. 为何Evinacumab能在LDL受体缺陷（如纯合子家族性高胆固醇血症）患者中降低LDL-C，而传统降脂疗法通常依赖LDLR功能？

**保存的答案**

Evinacumab是一种重组人单克隆抗体，能够结合并抑制血管生成素样蛋白3（ANGPTL3）。ANGPTL3通常抑制脂蛋白脂肪酶（LPL）和内皮脂酶（EL）的活性，从而阻碍富含甘油三酯的脂蛋白（如VLDL）的代谢。抑制ANGPTL3可恢复LPL和EL的活性，促进VLDL的加工和清除，并在LDL形成之前就减少其前体的积累。这一机制绕过了对LDL受体（LDLR）的依赖，因此能在LDLR功能缺陷（如纯合子家族性高胆固醇血症患者）中降低LDL-C。

**来源标识**：pubmed:38845393

**运行目录**：`ldlr-20261113-193512`

**检查范围**：Model review of saved originals; isolated experiment publication, not clinical gold-standard adjudication.

[运行终态](E:/Cursor/assumption/projects/medical-qa-data-factory/data/experiments/question-first-lab/ldlr-20261113-193512/after.json) · [请求与响应](E:/Cursor/assumption/projects/medical-qa-data-factory/data/experiments/question-first-lab/ldlr-20261113-193512/prompt-and-output.md)

## 4. G551D和F508del两种CFTR突变在蛋白质定位和缺陷机制上有何不同？

**保存的答案**

G551D突变导致CFTR蛋白氯离子通道的门控缺陷（gating defect），即蛋白能够正常定位并转运至细胞膜表面，但在受到细胞信号刺激时无法正常打开；而F508del突变主要导致蛋白折叠和加工缺陷（processing defect），引起内质网相关的降解（ERAD），使大部分突变蛋白无法到达细胞膜，仅少量可抵达细胞膜的蛋白还存在稳定性差和门控异常。

**来源标识**：pubmed:26648081, pubmed:24004658

**运行目录**：`cftr-20261113-201454`

**检查范围**：Saved final draft checked against two original excerpts; isolated runtime publication, not clinical gold-standard adjudication.

[运行终态](E:/Cursor/assumption/projects/medical-qa-data-factory/data/experiments/question-first-lab/cftr-20261113-201454/after.json) · [请求与响应](E:/Cursor/assumption/projects/medical-qa-data-factory/data/experiments/question-first-lab/cftr-20261113-201454/prompt-and-output.md)

## 5. APOE ε4如何通过重编程小胶质细胞的脂质代谢来加剧阿尔茨海默病的病理，特别是涉及哪些关键信号通路和细胞功能缺陷？

**保存的答案**

APOE ε4通过重编程小胶质细胞的脂质代谢加剧阿尔茨海默病病理，主要机制包括：1) 激活LilrB3介导的I型干扰素信号通路以诱发神经炎症；2) 通过PU.1/NF-κB驱动的转录重编程和ER应激-SREBP2激活诱导脂质代谢失衡；3) 导致脂质小滴积累、胆固醇超载及溶酶体功能受损；4) 最终损害小胶质细胞的吞噬能力，影响淀粉样蛋白β、磷酸化tau和病理突触的清除，进而放大神经毒性。

**来源标识**：pubmed:40835432

**运行目录**：`apoe-20261113-204322`

**检查范围**：Final draft checked against saved review abstract. Source supports the stated pathway and phagocytosis chain; not independent clinical consensus or full-text verification. Other published questions in this run are not automatically accepted.

[运行终态](E:/Cursor/assumption/projects/medical-qa-data-factory/data/experiments/question-first-lab/apoe-20261113-204322/after.json) · [请求与响应](E:/Cursor/assumption/projects/medical-qa-data-factory/data/experiments/question-first-lab/apoe-20261113-204322/prompt-and-output.md)

## 尚未计入的结果

- APOE 小鼠结果被泛化为一般血管疾病机制的旧题：虽曾发布，未保留。
- 新版将其修成保留小鼠与饮食条件的问题：范围修复成功，但措辞偏研究式，暂未选入。
- TNF 复审反复改变判断的运行：不能仅凭最终发布计作修题成功。
- CFTR 后续两题已完成发布，完整记录在 cftr-20261113-212218；其中部分内容与上述样例重叠，未重复增加精选数量。

# 原文支持核对与问题发现边界

## 目标与本次结论

先生成独立成立的领域问题，再验证答案。不能把论文结果摘录、PUBLISHED 状态或一次微型实验成功当作目标达成。本次仍未证明稳定的多实体产出。

## 实际失败

- `cftr-20261115-081407`：模型把 “first modulator approved” 写成“临床首选”，原终审仍接受。另有三联疗法和整个 HEMTs 集合覆盖范围混淆，不能按已发布数量计为质量成功。
- `g6pd-20261115-083411`：24 个外层动作结束，7 个候选，未发布。题目集中于一篇六种变异体的动力学研究，出现“在提供的重组表达数据中”，仍偏离领域查询目标。正文实际是 PMID 40943386 的材料，不能把最初尝试的 PMID 37415281 记成已读全文。
- 同一 G6PD 任务的前提评审把“机制有何异同”误解为“原文已经直接做了比较”，拒掉可以继续补证的自然问题；同时会把模拟解释、表达产量等当作更强的机制证据。误拒和误放同时存在。

## 已实现的原文支持核对

`claim_entailment.py` 用独立请求逐项判断主张能否从实际引用原文推出，不把前一评审的解释当作原文。每批最多四项，共享去重的 anchor，复用 `generate_with_evidence` 分页读取大记录；不复制每个主张的整份来源，不静默截断。

在最终评审初步接受后调用；返回 `not_entailed` 时生成带 target、具体主张和原文 ID 的修订问题，交回现有修订路径。`unresolved` 表示未能读完或判定，要求继续读取，不直接宣布事实错误。仅盲答有问题时保留既有一次修订流程，并再核对修订后的主张。审计记录、用量及调用输入输出保留；它不是独立检索。

涉及文件：`generation/claim_entailment.py`、`generation/answer_verification_subagent.py`、`generation/agent_loop.py`、`llm/contracts.py`、`llm/prompts.py`。代码审查指出并修复了原文重复导致上下文过大的问题，以及未决题干同时要求“暂不改”和“修正”的矛盾。

## 真实实验，不是成功率报告

实验目录均在 `data/experiments/question-first-lab/`，保存请求、公开模型输出及结果。

1. `cftr-relation-review-20261115`：给原终审追加关系类型指导，仍漏掉“首个获批→首选”，未采用该 skill 原型。
2. `relation-entailment-isolated-20261115`：一个实际错误和三个人工对照的微型核对全部符合预期；不能代表完整链路。
3. `cftr-claim-entailment-20261115`：对真实七条主张和三段原文核对，抓到候选及盲答的“首选”，其余五条接受。部分范围错误仍可能漏检。
4. `cftr-20261115-090401`：从验证前断点重跑主链路，旧问题因读者版本变化重新评审，误认“如”举例为穷尽二分法，触发不必要改题。初版独立核对在新主张组合中再次漏掉“首选”，只修了盲答，说明不能宣称独立核对已经可靠。
5. `cftr-entailment-full-claim-20261115`：补充“必须支持整条主张及限定词”和通用关系区别示例后，对上述实际五条主张重放，抓到“首选”和未获原文支持的药物代际标签。但仍漏掉集合覆盖混淆，不能作为全部答案正确的证明。
6. `g6pd-discovery-reader-first-20261115`：同一组已读原文，缩短发现 skill 后提出氧化应激及酶缺乏保护机制问题，选题比单篇动力学摘录更自然；现有前提评审仍误拒比较问题。随后将这份 680 字原型接入发现 skill，旧文件保存在 `skill-snapshots-20261115-0910/`；完整链路效果仍待检验。
7. `g6pd-premise-unknown-20261115` 与 `premise-unknown-transfer-20261115`：重写前提 prompt，区分待问内容与断言，减少比较误拒；但 CFTR 对照中仍放过“显著改善→接近正常”，没有采用这个整段替换方案。
8. `premise-comparison-minimal-20261115`：仅在旧前提 prompt 末尾澄清比较疑问与优效断言的区别。真实重放中正常比较通过，CFTR 的 near-normal 错误前提仍被指出。采用这一小增量，前提 prompt/call 同步到 v4，旧审查缓存 hash 自动失效，没有改 schema。

新整链路复验：`g6pd-20261115-091047` 从已读材料、未发现问题的断点开始；`cftr-20261115-091200` 从对应 CFTR 出题前断点开始。它们使用新代码与 skill，不重新获取已经保存的原文。需要记录最终状态及逐题质量，不能把这种断点复验说成新实体从零检索。

### 整链路暴露的新问题与修正

- G6PD 重放到 16 步仍未发布；后半程已转向粒细胞功能与 CGD 关系，并在反馈后补回“严重”限定、把确定的 phenocopy 改成可能的 phenocopy。原题目在答案修订时得到保留，但仍待完成验证发布。
- CFTR 重放到 12 步仍未发布；从生殖调控的宽问题转向精子功能。原文关于 Sertoli cells 与 microRNA 的句法归属有歧义，模型先给一个归属、核对又断言另一个归属。后续候选评审正确要求补清歧义；不能把核对器给出的替代解释当作已验证答案。
- G6PD 的数值核对误报“原文没有数值”：实际引用仅 6000 字符，数据约在第 2964 字符，默认分页首屏 1000 字符，模型没有正确继续读。改为在既有 32k 字符总预算内完整显示去重的非结构化选段，大集合/结构化记录仍分页。真实 `g6pd-entailment-visible-source-20261115` 重放后，2677 输入 token 即看到了数值并消除了错误缺证据判断。这里只证明内容可见性修复，不表示该动力学问题符合目标风格。
- 同一次核对把整个疑问句当作断言，因没有看到答案而要求修题。新增核对现仅处理 proposed_answer / blind_answer，问题前提仍由原有专门 reader 负责。审计只覆盖终审列出的答案主张，不能宣称穷尽覆盖。
- `g6pd-discovery-scope-first-20261115`：尝试追加“先摘原文条件再出题”指令，模型仍未逐字摘录、部分机会仍丢失严重程度并把额外指南需求标为已就绪。没有采用该追加方案。

上述已结束任务的续跑分别为 `g6pd-20261115-092221` 与 `cftr-20261115-092236`，各最多 8 个外层动作，使用新的有限全文展示和答案核对职责划分。不能与前一进程混称同一代码版本。

## 后续判断标准

要同时保留可以查证的自然疑问、修正真正错误的题干前提，并让补证和答案修改保留原始信息需求。改善一个评审样例不等于改善完整出题流程。继续以实际实体链路检查，不把越来越多的检查调用本身当作进展。

## 继续观测：原文覆盖与完整链路（2026-11-15 11:10）

上一轮为实际进展：修改了证据组装和上下文压缩，并进行了真实模型回放。本轮确认三项任务均已正常退出，无需重启旧进程。

- `cftr-20261115-094024`：原先 137218-token 的控制器请求已经能继续执行 8 个动作，未再触发原来的溢出。它进一步读取全文并修订题目，但没有发布；最终仍存在将敲除表型归因于特定蛋白相互作用的证据缺口。证明了该断点可继续，不证明所有超限场景均解决。
- `g6pd-20261115-094151`：完成 6 个动作，无发布。需要继续核对 review/verify 的反馈，不能把模型声称已获批准当成实际状态。
- `brca1-parent-section-discovery-20261115`：同一真实 discovery 输入，增加父小节原文至 22644 字符。模型保留一个 R1699C 结构机制问题；第二个问题标记 NEEDS_EVIDENCE；拒绝将低置信度 AlphaFold 扩展界面预测作为确定事实。这是实际观察到的 NEEDS_EVIDENCE 输出，尚未证明后续补证闭环有效。

旧 BRCA1 发布结果不是成功：p58 的酵母双杂交发现被当成无条件的蛋白结合结论，漏读 p60 的哺乳动物验证不复现以及 p61 的部分结合保留。补齐父小节是为了保留发现后面的验证和限制，不是扩大模型摘要。

实现调整：所有显式选段先占共享 32000 字符预算，再用剩余空间扩展相同命名小节；无法纳入的相关段落 ID 随来源传递。交互式分页不自动扩展。修订复用旧 anchors 时也继承来源覆盖元数据，但不继承旧题干的 ACCEPT。

复核：共享预算和显式来源的元数据路径获代码复核；随后补了复用旧 anchors 的覆盖继承，语法检查通过，等待最终复核。

完整生产链回放已启动：`brca1-20261115-110944`，起点为旧 BRCA1 `step-02-before.json`，14 个外层动作，隔离存储，保留真实提示词和公开响应。任务尚未完成，不能提前报告质量改善。

## 题干不能借用隐含实验条件（2026-11-15 后续）

`brca1-20261115-110944` 的完整回放再次暴露错误：discovery 和 premise reader 都看见 F1668 的选择性结合破坏未在哺乳动物 IP 中复现，但 reader 理由把题干解释成只问 Y2H 的现象，仍 ACCEPT。实际问句没有这个条件。来源补齐有效，但不足以避免模型替题干补限定。

已做真实对照：`brca1-premise-visible-scope-20261115` 使用此次机会原文与失败题，额外包含两个开放问题作为对照。追加指令明确不能把无条件问句解释为仅指成功实验，且单系统材料不自动要求填写全部实验细节。结果：失败的 F1668 无条件机制题 REVISE；ABRAXAS1/CtIP 结合方式比较 ACCEPT；是否因实验系统而异 ACCEPT。仍有局限：模型给出的修订理由会倾向限定具体实验，不能因此视为最终题目风格已达标。

生产采用 `question-reader-v6`；schema 和调用次数不变，模板 hash 自动使旧 acceptance 失效。代码复核通过，语法检查通过。运行中的 BRCA1 进程仍是启动时的 v5，不伪称其后续结果验证了 v6。

跨实体回放 `cftr-premise-scope-transfer-20261115`：沿用此前保存的 CFTR 原始材料和两道题，新版仍把“显著改善=接近正常”的前提判为 REVISE，接受 F508del/G551D 缺陷及 corrector/potentiator 作用机制比较。只证明这些具体样本未退步，尚无稳定性结论。

G6PD 续跑无发布的细节：当前 severe phenocopy 问题进入 verify_answer，但输出 INSUFFICIENT；另有一次模型拼错 hypothesis_id 导致 revise_candidate 未执行。不能将所有未发布都归因于问题评审，也不能通过自动模糊匹配 ID 掩盖错误操作。

## 候选评审擅自扩大答题任务（2026-11-15 11:20）

BRCA1 完整回放在候选 `hypothesis.aad5e3b014024e8ea1265708` 处出现另一个明确错误：问题只问 F1668 的影响及 Y2H/IP 结果是否一致，候选评审却认为题目隐含要求解释为何不一致，判 NEEDS_EVIDENCE，主 agent 随即增加研究动作。原文已直接报告未复现，足以回答事实对比；不应把额外原因解释变成原问句义务。题目整体是否值得入库仍是另一个判断，不能捏造证据缺口来代替。

修改 `objective-candidate-review/SKILL.md`：先按实际问句定义所需证据；是否、差异不自动等于为什么；答案自行增加的无据延伸应修答案。复核建议同时澄清 REWRITE 可用于仅答案缺陷，已加入，保留原题干与公开范围。

真实失败输入回放 `brca1-review-literal-need-20261115`：KEEP，不再要求额外机制。手工反例 `brca1-review-literal-controls-20261115`：同一原文，改为明确问为何并编造翻译后修饰补偿，返回 NEEDS_EVIDENCE，准确指出原文只有不一致观察，没有该因果解释。两个结果不能证明评审整体稳定，但说明此次改动不是直接放宽所有题目。

新实体任务 `hbb-20261115-111842` 已从头启动，count=2、最多18个外层动作，实际模型，隔离存储。第一题为 SCD 与 beta-thalassemia 的分子机制和血液学表现比较。仍在运行，不能把候选当发布或质量达标。

当前运行句柄：BRCA1 `36073`（14动作任务，第13步已完成）；HBB `13667`（第4步已完成）。均经进程句柄确认仍运行，没有因静默重新启动。

更新：BRCA1 句柄36073已退出，14动作完整结束，5个候选仍均PROPOSED，无发布。已从该运行 after.json 启动8动作续跑以实际使用 v6 reader 和当前候选评审skill；这不是重启仍存活的任务。HBB 13667仍运行。

## 首个本轮合格发布样本：HBB（2026-11-15）

运行 `hbb-20261115-111842` 第7步实际发布 `hypothesis.9671c31e2acb0cda5f3b4b25`。

公开题：Sickle cell disease (SCD) and beta-thalassemia are both caused by mutations in the HBB gene, but how do their underlying molecular mechanisms and resulting hematological phenotypes differ?

人工对照原文结论：可作为目标数据集的一部分。题干明确两种疾病和比较维度，无论文身份、样本统计或私有背景依赖。两来源分别为 PMID 29542687 和 34738740；原文直接支持 SCD 的异常血红蛋白聚合、镰变、溶血及血管阻塞等，与 beta-thalassemia 的链合成减少/缺失、无效造血、贫血的核心比较。未将表型列表声明为排他的完整集合。措辞仍可更精确（严格说聚合的是含异常亚基的血红蛋白分子），不把这一样本当成全系统稳定性的证明。

证据范围：使用两篇综述摘要，足以支撑本题核心关系。该样本不证明复杂全文整合能力，不为了增加检索动作强制下载全文。blind answer 和最终review都通过，逐条蕴涵审查通过；人工判断来自读取两个原文锚点，而非仅凭PUBLISHED状态。

过程：实体身份及关联检索 → 三份材料中发现比较信息需求 → 问题独立评审 → 整理答案 → 答案验证 → 候选评审 → 发布。题干独立评审在答案形成之前；候选评审是后续另一次检查。第2个HbF机会仍待补证，任务尚未结束。

BRCA1续跑仍在真实执行历史压缩，已见275787 tokens的组装上下文被分批摘要处理，尚未返回第1个外层动作；这是有活跃调用的等待，不另起重复任务。另启动 TP53 从头任务，目标2题、18动作，检验跨实体表现。

HBB 第10步新增候选 `hypothesis.dbc8ed64abe1aa761017e882`：询问 HBBS→HBBG 编辑比例与表型挽救的剂量效应关系，答案目前把单项临床前材料中的20%阈值及68–80%比例合并。这一候选不能算成功：需核对小鼠/人HSPC模型、基因编辑比例与蛋白比例是否混用，以及问句是否暗示了来源未建立的普遍剂量效应关系。等待实际后续验证，暂不按其reader ACCEPT认定质量合格。该问题不应靠机械禁止所有剂量问题解决。

## 形式可回答不等于选题合格：form v5

HBB 后续两个修订版本把治疗比例改为“人类化小鼠达到多少比例”和“临床前阈值及哪种模型确证”。旧 form v4 明确承认是特定研究结果，却以“可独立检索”为由 ACCEPT。后续 premise v6 正确指出68%和79%来自不同模型，阻止其中一题继续，但这未解决选题本身的问题。

`hbb-form-domain-task-20261115` 真实失败题对照：两道实验阈值摘录 RESELECT；HBB 疾病机制比较、正常体细胞染色体数量 ACCEPT。采用 question-form-v5，强调可回答仅是必要条件；判定知识需求而非数字或专业性。模板和调用版本同步，旧 acceptance 缓存随hash失效，代码复核及语法检查通过。

`form-v5-transfer-20261115` 额外边界：BRCA1 R1699C对伙伴结合的影响 ACCEPT；CFTR两变异机制与药物机制比较 ACCEPT；缺失上图的问题 REVISE。只代表这些具体对照。形式reader看不到来源，仍可能误判新颖领域知识与某次实验结果摘录，不能认为问题已彻底解决。

运行中的 HBB/BRCA1/TP53 使用启动时加载的form v4，后续新进程才应用form v5；不能把旧进程结果归功于新版本。下一阶段应验证新版在整链路中的换方向行为，并改进问题发现本身，避免只增加后端拒绝。

## 问题发现源头对照与新版整链路行为

`hbb-discovery-domain-first-20261115`：同原文、原focus，替换发现skill的选题段落，强调先对象关系后问题。首次输出仍有实验比例题，随后form v5拦截；第二轮产生安全性比较和Makassar治疗原理方向。不能归因为生成skill已解决问题，未采用这段替换。

`hbb-discovery-source-only-20261115`：保留原skill和原文，移除controller focus及research_notebook。得到安全性比较及胎儿-成人血红蛋白转换调节因子问题，后者正确标缺具体因子证据。没有实验数字题，但仍存在泛泛“recently”及临床优势倾向。单次对照不足以直接删除生产focus；保留实验结果等待更多证据。

HBB新流程 `hbb-20261115-113315` 前4动作：review旧候选后，转去discover安全性关系，再design，而非继续改写编辑比例。这是form v5接入后实际换方向的证据，不等于新题已合格。

BRCA1 `brca1-20261115-112427` 已完成8动作，出现 RELEASE_REVIEWED `hypothesis.9eecc9d359740abeef547c7f`：R1699C如何影响ABRAXAS1/BRIP1/CtIP结合及结构机制。已人工读取p63原文，其提供氢键差异、潜在电荷排斥的结构解释，机制带推测性，不能宣传为独立因果实验已证实。答案保留潜在电荷排斥，区分Y2H/IP观察；比旧F1668无条件问题更符合目标。尚未发布，继续6动作发布与剩余方向；不是重启活进程。TP53仍运行。

## 隔离回放摘要继承修复

实验脚本 `data/experiments/question-first-lab/run_live.py` 此前没有复制 `objective_runtime/exploration_memory`，每次隔离续跑都重新生成摘要。已修复为：仅最新提交断点继承该目录；早期断点不继承。复核发现仅依靠covered子集不足以防未来评审信息进入早期回放，因此加上最新提交限制，二次复核通过，语法检查通过。源任务须已停止；当前执行遵守该条件。生产摘要逻辑未改。

仍活跃：HBB 60846 / `hbb-20261115-113315`，BRCA1 30747 / `brca1-20261115-113841`，TP53 75188 / `tp53-20261115-112722`。已确认近期有真实模型响应和研究动作，不因长等待重启。

TP53 第3步发现另一个待验证偏差：form v4 对“ATM/ATR与Chk2在TP53 Ser20磷酸化上的作用有何不同”凭生物知识判REVISE，虽然该角色应只判断公开任务，事实前提由下一阶段依据原文检查。该问句可以包含间接/上游作用，不能自动理解为所有激酶都直接催化。暂记录，尚未为此继续叠加生产规则。另一个药物磷酸化比较机会已保留，后续需检查细胞系范围及候选激酶的不确定性。

## 发布结果与状态措辞对照（后续）

- `brca1-20261115-113841` 6动作已正常结束，`hypothesis.9eecc9d359740abeef547c7f` 已PUBLISHED；前面人工核对过p63及结合证据。单篇正文的结构机制题可作为数据集一部分，但此长链修订效率仍不合格，不能称稳定。
- `hbb-20261115-113315` 10动作结束，仅保留此前1道发布题；中途安全性检索后又修成编辑20%阈值题，未新增合格发布。之前“已换方向”的局部观察不代表循环永久解决，最终结果修正这一判断。
- `hbb-discovery-evidence-order-20261115`：仅将schema字段改为先来源观察/信息需求、后问句，未改生产。输出没有阈值题，但生成“fetal-to-adult hemoglobin switch failure”这一错误前提；原文说正常转换伴随疾病显现，不是转换失败导致疾病。且缺具体分子信息仍ready_for_design。故不采用，结构化顺序本身不足以解决内容正确性。

TP53具体无效修订：step6之前候选已KEEP且verification_ready，主模型却因accepted_answer_proposal_missing认为还需revise_candidate去“propose answer”。实际已有候选答案，缺的是verify_answer接受记录。`tp53-verification-status-wording-20261115`以原模型请求payload修改状态说明，真实回放改选verify_answer；其解释仍想补查，不能当作完全正确的规划能力证明。

生产 `publication_readiness` 已将blocker改为answer_verification_acceptance_missing，明确只有verify_answer可形成验证接受，design/revise生成草稿；同时要求看readiness及已记录缺口。未改门禁和路由。rg未发现旧名称其他消费者；语法检查、代码复核通过。

此刻仅TP53原任务75188仍运行。其他上述实验均已正常结束，不再视为活任务。下一轮应以冻结版本做新的完整运行，并检查旧候选反复进入修订的行为，不能继续仅靠小样本评审回放声称稳定。

## 形式分类单一化：未通过真实交接，已撤回

`form-single-task-classification-20261115` 初步对照把Form模型输出缩为task_kind，避免task_kind与verdict冲突；TP53问题domain、错误SCD/CFTR前提domain、实验摘录source、缺图incomplete，表面符合职责。

曾短暂接入FormTaskReview及form-v6，保留兼容FormReview审查记录。代码复核认为接口兼容，但实际两阶段 `form-v6-premise-handoff-20261115` 显示：对错误SCD/CFTR题，模型仍凭医学记忆分类为source_specific_reading，于是RESELECT早退，未将该题交给premise。虽然没有放行错误题，但职责分离假设未被证实，不能声称修复。

已撤回本轮FormTask类及prompt v6，恢复FormReview、form-v5。其他既有改动（premise-v6、候选review skill、状态说明等）保留，语法检查通过。运行中的EGFR与TP53进程原本已加载form-v5，未被该短暂试验改变。

简短跨运行观测表保存为 `data/experiments/question-first-lab/quality-observations-20261115-1154.json`，明确区分共享断点血缘和独立样本，不相加累计发布量作为成功率。旧TP5318动作零发布，已从step06之前以新状态说明续跑（17030，tp53-20261115-115313）；EGFR新任务62658仍运行。

## 12:13：新EGFR完整运行与TP53外部核验

- `tp53-20261115-115313` 已结束12动作，发布55a79eab药物磷酸化比较，但不计合格。来源PMID11326311只覆盖具体实验条件；外部原始研究 PMID11244509 (https://pubmed.ncbi.nlm.nih.gov/11244509/; DOI10.1038/sj.onc.1204060) 报告微管抑制剂诱导的Ser15磷酸化具有细胞依赖性。原题通称人癌细胞系、答案称紫杉醇仅诱导Ser20，遗漏条件且可能与其他研究不符。发布状态不能替代独立科学核查。
- `egfr-20261115-115153` 已结束11动作、零发布。最初MET旁路机制问题在答案核对后变为Vabametkib联合Lazertinib实验结果问题；主模型最后选择stop，解释却说下一步要publish。后者是另一个尚未解决的动作/解释不一致问题。
- 明确工程问题：`_audit_final_claims` 原来把所有not_entailed映射revise。窄审计只判断所引材料是否充分，不能据此证明主张错误，却把补证问题推向改题。

### 实验及最终小改动

曾尝试新增next_action/evidence_need模型字段（claim-entailment-v2）。真实原EGFR输入 `claim-remedy-20261115-121023` 仍revise；明确区别后 `121056` 反而entailed。未证明分类可靠，已完全撤回v2 schema/prompt/validator。

最终仅保留宿主交接修复：窄审计未entailed统一返回retrieve，带审计具体缺口，并要求按原问题构造查询、不把候选答案当已知条件、不改成论文阅读理解。原验证器本来已明确判断revise/abandon的结果仍早退保留。没有新增模型调用、没有放宽发布要求。原v1模型再跑 `121207` 也entailed，说明模型判定本身存在波动；尚不能声称此次真实补证分支走通。

语法检查、代码复核通过。probe_claim_remedy是窄审计回放（保存的是此前审核后的claim_checks，但输入只取statement/anchor_ids），不是整链路验证；已补充保存updated_checks与每次usage供后续执行。

新运行：EGFR `egfr-20261115-121306` 从step08验证前断点12动作（session8695）；CFTR `cftr-20261115-121322` 全新count2、18动作（session4588）。截至此记录均在运行；其最终结果仍需逐题检查。没有改生产数据库或重启后端。

## 12:20：长历史导致状态误读的对照

EGFR旧末次请求 user payload 348498字符，105481输入tokens；latest_observations单项253134字符。模型输出action=stop，却解释要publish。续跑121306中又出现自称验证通过/已发布但实际不成立的解释，未因补证交接小改就稳定。

`probe_early_memory.py` 调用现有prepare_exploration_history，仅实例trigger0.25/target0.20，摘要后恢复配置，比较同一保存请求。`early-memory-20261115-121737`：compact33479tokens、92042payload字符，选择verify_answer且正确说缺验证；original105481tokens，亦选择verify_answer，但误称已发布1题。只是单次对照；使用静态ReactChoice不是生产快照动态schema，不能当作门禁验证。前两次脚本启动失败（配置dataclass误用与比例约束），均在模型调用前失败，已修。

run_live.py新增可选--compact-trigger，仅设置隔离模型实例，同时provenance记参数；默认无改变。低阈值EGFR `egfr-20261115-122008` (session95190) 从与普通阈值121306相同step08前断点跑12动作，待完整比较。生产默认未调整。

`review-gap-20261115-122605` 实验仅替换候选review skill一段：缺直接关系证据应保留信息需求、NEEDS_EVIDENCE，不改问联合干预结果。原真实EGFR review请求回放却给KEEP，理由“暗示”通路，未证明补证分类改善。未采用此skill重写；文件仅在实验请求中覆盖，生产skill保持原样。
普通阈值EGFR121306已完成12动作，1PUBLISHED(a7af...)，问题回到MET旁路信号领域关系，但经历多次实验题改写。与TP53不同，外部综述 PMID34837746、PMID31832192 确实讨论该一般关系；仍需注意并非证明只有两条通路，较广文献还提STAT，不能以单篇药物实验定义穷尽集合。暂记录其方向合格、答案范围需按实际问法审核。

## 12:35：CFTR先提出可回答问题的指引实验

`answerable-discovery-20261115-123131` 用首次CFTR发现请求、当前生产短skill加“优先已解释关系，扩展不取代现有可回答问题”，走真实formulate_questions及两阶段评审。生成独立问句“VX-445除了作为corrector，是否还具有potentiator功能”，比原先枚举罕见变体的问题更直接。但premise错误地将“是否”当确定断言，返回NEEDS_EVIDENCE。因此尚未采用生产skill，不能声称完整链路修好。

`requested-information-20261115-123514` 试验新增待求信息字段，得到ACCEPT，但premises仍把疑问内容当断言，未解决语义问题，不采用。其自定义schema也未保留生产全部约束/一致性validator，故非严格单字段对照；只记录现象。前一发现实验则使用完整生产reader合同。

这些新脚本均经只读复核，没有执行候选动作或修改生产skill。review_gap已加入唯一替换命中检查；其基底是当前skill，不宣称历史代码精确单变量对照。

## 12:41：完整对照终态与盲答修复交接

- EGFR低阈值122008已结束12动作，零发布；3候选包含具体联合用药实验题和MAPK反馈受体题。没有证明低阈值改善完整出题质量，不修改生产阈值。
- CFTR121322已结束18动作，零发布。两个候选涉及VX-445的共增效条件；后者经过KEEP，但最终审核因blind附加VX-770通用名ivacaftor未在当前引文定义而unresolved。此前一次窄审计还对“permissive conformational change”与“change permissive for activity”作出了不可靠的语言差异拒绝。不能把这些视为真实材料不足。
- 代码核实最终verified_answer_values取blind.answer_values，所以不能忽略blind无据附加信息。既有一次盲答修复仅允许revise，未覆盖窄audit的retrieve，而且repair请求原来没带具体audit repair_issues。
- 新增生产小改动（已语法检查和复核）：只有本次窄audit存在、全部问题在blind、候选checks完全支持等原严格条件满足时，retrieve也可进入既有一次blind_repair；请求补充仅blind的specific_repair_issues，要求去掉无据可选别名或解释；仍重跑完整review和audit，不自动接受。其他候选缺口不触发该修复。
- CFTR `cftr-20261115-123946` 从step18前真实续跑6动作，session72784，待结果。
- answerable-discovery实验增加source-run参数，根目录带实体名防并行碰撞。HBB14389 (`answerable-discovery-hbb-20261115-124124`) 与TP5345181 (`answerable-discovery-tp53-20261115-124127`) 用各自首次发现请求复查指引泛化。生产discovery skill尚未改。

## 12:48：回放精度与实验结论

- CFTR123946普通主agent续跑6动作结束，零发布；主agent重新选择改题，没有精确命中原verify动作，不能据此判断盲答修复有效。
- run_live新增--first-action-record回放保存的真实controller decision，严格要求相同state_digest；仅允许verify_answer、拒绝pending decision，仍过生产校验和派发，下游真实模型，后续恢复正常主agent。objective_research改为只在latest-source-commit断点继承，早期断点不复制未来完成缓存。旧实验不能当严格研究重执行对照，后续新实验用此边界。两轮复核、语法检查通过。
- CFTR124454（62275）精确回放原step18验证动作，第一步已RELEASE_REVIEWED。但本次blind未引入ivacaftor别名，independent_answer_repair=False，不能声称真实修复分支已验证。返回“The presence of VX-770 (or a prior conformational change induced by VX-770)”还须人工核对必要性及机制确定程度。其余3动作仍在运行。
- HBB迁移发现实验124124结束，生成β地贫分型差异、碱基编辑安全机制比较；TP53迁移124127结束，生成Chk2-Mdm2分子机制、旧药物磷酸化模式比较及ATM/ATR间接机制问题。后者仍含已知有条件限制的旧比较，且reader将待求机制当预设。故没有声称新指引稳定，也尚未改生产skill。

当前只有CFTR124454/session62275仍活跃；72784/14389/45181/95190/4588/8695/83639/95095均已结束。总体目标仍未完成。下一步应看CFTR真实交付与源范围，而不是按RELEASE_REVIEWED/PUBLISHED计成功；生产新改动只包括窄audit缺证据交回retrieve及严格受限的盲答自修复交接。

## 14:05：恢复后的当前状态与新主线实验

上轮有真实改动及负向证据，属于progress；暂停已解除。62275已确认结束：CFTR124454发布a935，但原文明确VX445 alone potentiated G551D/G1244E、F508单独给药显著降低而非无效，故“VX770必需”不能作为无条件答案成功。检查真实125234模型请求：完整4019字符引文包含两处反例，不是截断或少context；v3加question后仍entailed，属于模型忽略反例。

生产窄audit现v3将原题传给每条答案主张，防止短答案丢掉所问关系；两调用点已审查、语法通过。实际CFTR失败没有被该改动解决，不能作为有效质量提升证据。probe_claim_remedy已改用最后一轮audit的review_before_audit配最终checks，避免修复前后阶段混合；模块旧docstring已修正。

依skill-creator原则准备了隔离concise-skills：较短main与discovery指引，强调读者的领域疑问、先用已解释关系、按缺口扩展，保留宿主前置条件。SkillOverrideModel仅替换main/discovery，真实请求/压缩估算同样替换且幂等键含内容hash，生产技能未改。已复核无门禁绕过；补充零文件命中失败、后续运行保存实际role/hash到skill-overrides.json（已启动两轮此前没有这个文件，但trace含实际prompt/hash）。

全新运行：HBB140530/session77564、EGFR140533/session8073，均count2/actions18，使用隔离精简skill，保存每步。此刻在运行。不要将未审查的发布量当成功；仍须逐题对原文检查自然性、公开范围和答案。


## 14:51 — 实验结果与数据库正文可见性修复

- 简短 main/discovery 的真实运行：HBB140530（18动作1发布）、EGFR140533（17动作2发布），不足以证明稳定质量。EGFR第二题含提示答案的举例及时间性描述；需要独立质量判断。
- concise-review141300：HBB重复深挖问题改善，但EGFR评审转而要求协同效应的定量计算；不采用该候选评审 skill。
- reader-only 消融 HBB142028 发布2题、EGFR142028发布2题，但EGFR放过“受体增殖”错误措辞，HBB外显子降解结论有范围问题。因此不得把省掉候选评审当作已验证改进；默认评审未改。实验 checkpoint 禁止不带模式参数恢复，防止实验 host KEEP 混入默认策略。
- APOE142216：18动作0发布，反复声称 NCBI 只有基因元数据。核对实际请求发现 /summary/record/summary 确有生物学功能描述，却被 EvidenceView.scalar_fields 的字符串 <=200 限制隐藏。模型未进一步读取索引。这是程序呈现与模型阅读选择共同造成的真实失败，并非原文没有证据。
- 修复 verification_evidence_view.py：字符串以现有完整 JSON 总字符预算判断可见性，不再额外排除长于200字符的字符串。不截断正文；数组、分页、深度、访问数量限制不变。代码审查无阻断项。
- 真实生产核验探针 scalar-evidence-20261115-145025（probe_scalar_evidence.py）使用原始答案与原始锚点，经过 audit_claim_pairs 与实际 EvidenceView。APOE两项从未支持变成支持；HBB简短候选答案支持，带未提供分子细节的盲答仍不支持。证据表明修复提高正文可见性，不是全部放行。
- counterevidence142517：要求先引用支持句与限制句，CFTR仍误判必要条件成立；HBB正例通过。不采用该schema。该实验直接提供全文，与生产分页不同；引用未自动检查，不能作为验证后的引用使用。
- 活跃完整流程：apoe-20261115-145109，session73607；从 APOE142216 step05-before 与保存 verify_answer 动作重放，后续真实主模型决策，10动作，默认双层评审，简短 main/discovery override。尚待终态与逐题审计。


## 14:58 — 完整回放与措辞探针

- APOE145109 已终态，断点后6动作2发布。第一道脂蛋白代谢功能题与实际可见 RefSeq summary 对应；第二道“APOE基因发生突变会导致哪种特定的脂质代谢疾病？”仍存在突变范围与必然性措辞疑虑，不能用发布数作合格数。原 APOE142216 同候选核验反复失败，主要证据正文隐藏障碍已在真实链路消除。
- 新实体 BRCA1，root brca1-20261115-145354，session16404，目标3题/24动作，仍运行。第8步已有独立的BRCA1/BRCA2修复角色比较题（PMID22193408概述支持）；最终答案额外写“早期步骤”，该锚点并未直述早期，后续审计需区分问题适合与答案逐句支持。不可预先称本轮完全合格。
- probe_question_wording.py，wording145521 baseline与附加措辞指令，wording145631短prompt：均未可靠识别EGFR的“受体增殖”错指；baseline还把专业机制误判成论文阅读。未采用任何production prompt修改。实验仅处理四条真实保存题干，无候选答案输入，返回index集合完整校验。
- 本轮明确效果来自正文可见性修复；评审措辞指令实验无正面证据，不堆入正式skill。人工观察记录在 quality-observations-20261115-1458.json。


## 15:10 — 评审模式与无问答输入的阅读实验

- wording150115：只做文字编辑，默认非thinking仍未发现受体增殖。wording150217同任务请求enable_thinking=true后，发现该错指，另3题ACCEPT。此设置仅当前模型实例替换，无默认配置改动；结果是服务端响应差异，不能仅凭请求参数证明服务端内部机制。
- wording150354：正式form模板+thinking仍放过错指；附加明确的动作对象检查+thinking才识别。说明不是单独切thinking即可解决。平均输出token显著增加且有重试，尚不采用为全链路默认。
- counterevidence150420：thinking仍接受CFTR必要条件误推，HBB正例通过。说明对范围/例外的判断仍不稳定，未采用此schema或默认thinking。
- probe_source_observations.py 实验：不向阅读模型提供问答，先按条件整理观察，再让核验模型看原文和阅读索引。引用的原文仍由已有答案所引锚点预选，因此不是独立检索或无偏全文选择。
- 第一次150748因未注册实验role在发送前失败；150812实际阅读包含单独VX445对G551D/G1244E的观察，但重写/拼接引文，精确引用校验失败，未进入核验。审查还发现该角色会自动加载生产综合skill，构成实验混杂。
- 已修成显式实验skill，并让模型引用程序提供的sentence_ids；程序从原文取回source_fragments，不让模型复制引文。仍提供完整原文给核验，摘要只作索引。活动session23468，source-observations-20261115-151019。
- BRCA1主流程session16404仍在继续，已获得正文并产生BRCA1-CtIP切除/细胞周期/冗余关系题，是否独立且可回答待最终审计。


## 15:17 — 修复混合批次发现提前返回

- source-observations151019成功整理并保留了单独处理/组合处理的不同结果，但最终审计仍把CFTR必要条件误判为支持。观察归组也把“饱和VX770”作为含单独VX445观察的共同条件，说明结构化说明本身仍可能混淆条件。保留原句引用不足以保证摘要正确。此方案未进入生产。
- BRCA1-145354已结束：24动作，1发布，其他候选仍未完成；确实调用获取正文并读取，问题不能归因为只读摘要。
- 明确编排缺陷：step12发现3题；两题REVISE（范围/确定性需修），第三题ACCEPT但readiness=needs_evidence。formulate_questions只要retained非空便提前return，原本可用的一次修订尝试被跳过；后续围绕第三题反复补证。
- 已修生产question_discovery.py：累计保留已通过及待补证机会；混合批次尚有REVISE且有容量时，使用原2次上限内的剩余一轮处理局部修订；第二轮空返回也保留第一轮结果。最多4题；新问句仍需reader，未绕过任何审核。最终retained_questions/verdicts与输出对齐。py_compile通过，审查无阻断项。
- 活动精确材料发现探针：answerable-discovery-brca1-20261115-151558，session31291，原revision59 payload与冻结简短discovery skill，真实重新生成与审核。
- 活动完整回放：brca1-20261115-151650，session73028，原step12-before，14动作，默认评审，简短main/discovery，所有调用保存。尚待结果，不能预称编排修复已改善最终质量。

- 15:18 精确材料发现探针151558已完成：本次随机生成4个不同问句，reader全部ACCEPT，其中3项ready_for_design、1项needs_evidence。只执行1轮，未触发新混合修订分支，故不能当作该分支的真实成功证明；说明同材料可产出其他独立机制问题。完整回放73028仍运行。


## 15:26 — 混合修订分支实证与研究摘要缺省

- mixed-discovery-replay152037回放原revision59已观察到的首轮discovery/form/premise结果，历史调用单独标recorded_model_result，不计入live usage；不是相同请求重跑，也不是新模型独立发现首轮结果。后续修订和两层reader均为真实新调用。
- audits证实两轮：首轮REVISE/REVISE/ACCEPT；次轮ACCEPT/REVISE/ACCEPT。保留原待补证机会，并增加2个ready_for_design机制机会。说明局部修订分支在观察到的失败场景中有效；尚未验证这些题最终答案，也不代表整体稳定。
- BRCA1-151650完整回放终止于research_model_request_requires_repair。主模型选择继续研究而非原discover动作，研究第14步read_source_details响应缺summary。最后一次真实response的content中动作、参数存在；没有上下文超限证据。
- 生产research_agent.py：中间工具动作summary可省略（rationale仍必需），finish_research仍由运行时与JSON schema要求非空白总结。未复制rationale冒充summary、未降低工具参数/来源校验。原真实响应不作修改即可通过新ResearchTurn。审查无阻断项。
- 从151650最新after.json续跑会继承研究缓存，避免重查；活动root brca1-20261115-152526，session29839，12外层动作，仍使用冻结简短main/discovery，默认thinking未改。


## 15:37 — 完整续跑结果及候选评审模式比较

- BRCA1-152526已完成12动作窗口，原研究摘要缺省故障已越过，研究阶段正常交回并继续出题。但没有新增发布，仍有过度改题循环。
- 新候选aa396问“BRCA1功能缺陷或调节异常如何导致同源重组缺陷（HRD）？”；答案验证接受，候选评审却要求HRD定义/检测指标等题干未要求的补充，并REWRITE为更细机制。实际修订04552、bd5c等问句逐步把原答案塞入题干，再问材料没展开的“如何”，导致循环。
- candidate-review-replay153327：同保存payload、同生产skill的当前配置回放，请求thinking=true，返回目标aa396的KEEP，避免该具体误导性建议。尚不能仅据这一例全面采用。新脚本只读trace，不执行候选动作；已补ID覆盖校验及输出目录随机后缀。
- 活动对照：EGFR原a3dff候选（含“受体增殖”）session79075/root candidate-review-replay153627；HBB原ae031候选session91059/root candidate-review-replay153628。都保持生产评审skill，实例请求thinking=true，默认模型配置不变。待观察是否既减少过度拒绝又发现真实问题。
- 其他已配置provider readiness只读检查：deepseek-official不可用，未发起调用；Qwen可用。未要求用户补密钥，当前工作可继续。
- 简短状态说明已写入2026-11-15-question-first-quality-status.md，待本轮结果更新终态。


## 15:41 — 两实体组合评审实验

- 候选评审thinking对照已结束：EGFR153627返回KEEP但仍漏“受体增殖”；HBB153628返回KEEP，避免原先要求额外物理机制。说明候选评审不适合作为独立文字校对替代。
- 新实验复用现有两个评审角色，不加审核层：form-reader使用此前能发现错指的措辞补充+thinking；candidate-review仅thinking，生产skill不改。其他角色保持原配置。
- 实验包装在skill_override_model.py，run_live.py新增--deliberate-review。默认关闭；仅两个角色分流到第二真实模型实例。candidate_review_mode参与hash区分，禁止与reader-only消融并用；断点续跑必须保持模式，且form skill hash必须与来源manifest一致，避免复用不同评审条件的旧接受结果。
- py_compile通过；审查确认新鲜运行安全，并指出form缓存身份问题，已通过实验入口hash一致性约束修正。不改生产默认模型、prompt或配置。
- 活动新鲜运行：HBB154008/session25918、EGFR154009/session69716，各目标2题、最多20外层动作；使用deliberate-review-skills冻结文件，所有输入输出保存。尚待效果与成本审计。


## 15:54 — 组合评审两实体结果与出题要点实验

- EGFR154009：11外层动作、2发布，分别为人类EGFR染色体位置和别名。位置最终答案仅7p11.2，未保留候选附加坐标；别名题使用EXACT_SET且未限定来源/完整集合，存在完备性口径疑虑。两题均偏基础，不能据此证明复杂题能力改善。
- HBB154008：11外层动作、2发布。非缺失突变如何影响β珠蛋白表达的机制题，正文PMID34766559直接支持转录/RNA加工/翻译三类，属于独立领域机制问题。另一题问β珠蛋白基因簇其他成员，原文支持所列基因；题干未显式说人类，需保留适用范围审查。最终答案及引用已人工对照，不以PUBLISHED替代质量判断。
- 实际HTTP响应usage统计保存在review-mode-cost-20261115-1553.json：新EGFR36模型响应，HBB43；旧EGFR79、HBB68。新评审角色输出token更高，但本轮总调用更少。两轮选题和材料不同，不能当作严格A/B或宣称模式导致固定比例节省。
- answer-outline构造实验：内部写简短答案要点，reader只接收题干与原文，无草稿泄露。BRCA1（answer-outline154543-4787b8）4个机会中两项ready+ACCEPT、一项needs+ACCEPT、一项ready但reader需补证；CFTR（154543-6bc81e）双重作用机制题ACCEPT，另一项具体突变处理效果比较RESELECT。能生成有价值的关系题，但仍存在过度自信和论文结果方向，尚不采用为生产流程。两项实际请求/响应与稿件完整保存；实验skill替换已加入唯一命中检查。
- 活动新鲜多题运行：APOE155233/session96429（4题，28动作），BRCA1-155233/session64267（3题，24动作），同deliberate-review模式和冻结skill，观察更多机制/关系题，默认配置不变。

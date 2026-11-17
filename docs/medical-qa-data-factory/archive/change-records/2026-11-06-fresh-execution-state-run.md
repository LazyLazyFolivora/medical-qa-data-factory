# 执行状态改造后从零运行 EGFR

任务 react-egfr-execution-state-fresh-20261106。目标困难题 1 道，上限 40 步；实际 36 步后模型主动 stop，COMPLETED_WITH_ZERO_QUESTIONS，发布 0。不是预算耗尽。

## 路径

HGNC 身份 → PrimeKG → PubMed 核内 EGFR / Arf GAP 综述 → EGFR 降解与核转运研究 → PMID 36744831 单分子下拉实验 → 设计一题 → 评审 KEEP → 候选补证 → 验证及重试 → 进一步检索条件 → 全文请求参数错误 → 主动停止。

题目：根据单分子下拉测定实验，辐射激活的表皮生长因子受体（EGFR）在核定位过程中，直接结合哪一种核转运蛋白，且不结合哪一种核转运亚基？

候选答案：KPNA / KPNB1。来源摘要直接给出了这两个信息。困难标签理由是同一事实中的正负对比；没有独立难度评估，不能认为真正困难题。

## 已改善的部分

主 agent 查看 verification_readiness，先 request_more_evidence，再进入 verify_answer。独立检索成功找回 1 篇来源。但该来源恰好也是发现阶段来源 PMID 36744831。

## 仍存在的问题

1. 两次验证出现 role_contract_violation：semantic evidence review references hidden anchors: ['anchor.pubmed.36744831']。独立检索重新找到发现来源，与验证阶段排除来源、审查引用的逻辑存在冲突，需要专项检查。不能因此声称原论文不支持答案：其摘要确实直接陈述 KPNA 而非 KPNB1。
2. 随后盲答不确定，复核要求具体研究条件，标为 study_population。是否确实必须提供剂量/细胞系，还是排除已有证据后的过度要求，需要另行判断，不能照单全收。
3. 后续补证 query 带入了候选答案 KPNA、KPNB1，并堆叠 cell line/dose/timepoint，返回 EMPTY。补证仍有答案泄露和检索过度限定问题。
4. 最后尝试针对 PMID 36744831 获取全文，漏传 provider_id，被入口拒绝，根本没有执行。stop 却宣称该论文全文无法获取。之前确实有一次正文失败，但针对的是另一篇 PMID 30362453（fallback JSONDecodeError）。模型仍混淆“未执行”和“不可获取”。
5. 模型 stop 同时承认 STAT1-KPNA 等可用方向，说明仍有提前停止问题；36 < 40，且有剩余材料。

## 计数与产物

36 次动作：读材料 10、发现检索 6、提取事实 5、参数反馈 5、验证 3、评审 2、补证 2、正文读取 1、设计 1、stop 1。另有事实提取 attribution 错误后重试。

完整结果 data/runs/react-egfr-execution-state-fresh-20261106/fresh-run-report.json。此次未再修改代码。

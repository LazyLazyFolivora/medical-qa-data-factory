# 搜索材料身份与题干字面评审

## 真实失败

APOE `apoe-20261115-034910` 研究动作在普通 `retrieve_materials` 中传入 `fetch_full_text=true`。EvidenceSearchAgent 在 strict_selection 模式仍进行旧式正文 enrichment，用正文替换摘要却保留 material.pubmed.PMID，保存时冲突。

改为 strict_selection 一律不内联 enrichment。ReAct 正文仍通过已有 read_source_details / DetailProvider 获取，该路径给正文独立内容 ID。非 strict 路径行为不变。代码审查通过。

`apoe-20261115-040111` 从失败状态续跑仍冲突于另一论文：旧运行已将正文 material.pubmed.31724031 写入磁盘，但尚未提交运行状态。文件明确标记 literature_full_text，证明是旧的半写入残留。未删除或覆盖它。该恢复问题尚未修复。

`apoe-20261115-040230` 从失败前的 `apoe-20261115-033415/after.json` 开始验证新路径，记录时尚在运行。不能把启动成功写成修复已验证。

## 题干质量

TP53 `tp53-20261115-035625` 的新候选把“通过 Hsp90 保护逃避降解”写成“逃避 MDM2 负反馈和 Hsp90 保护作用”。独立题干读者与候选评审均接受了它，评审解释实际是在回答自己理解的合理问题。

question reader prompt 增加直接指令：按实际词句和动词支配关系核对前提，不默默修复作者想表达的问题；判断关系是否反转、强化或归属错误。没有加生物学关键词硬规则或改变输出 schema。模板 hash 自动使旧评审版本失效，代码审查通过。

真实回放 `reader-wording-20261115-040420` 保存 input.json、完整请求响应及 result.json：错误题 REVISE，自然替代题“肿瘤细胞中突变型p53蛋白异常积累的主要机制是什么？”ACCEPT。

限制：REVISE 理由没有准确抓住“逃避保护”反转，还不恰当地质疑 Hsp90 保护是否属于主要机制。因此只是结果方向改善，不能证明可靠的证据推理或质量目标达成。历史摘要误记 resolved、答案机制归属、旧半写入恢复仍待处理。

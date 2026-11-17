# 正文获取工具与自主阅读选择

## 改动

保留独立工具 read_source_details，由 LLM 选择调用，不新增强制全文阶段。

- 工具说明明确：输入已检索的 PubMed 材料 ID 和具体 evidence_need；负责获取并保存正文，不代表读完正文。
- 返回 fulltext.* 观察产物，按论文列出 acquisition_status、reading_status、正文材料 ID、字符数、前20段索引及分页偏移。
- 保留 provider_status、error_category、retrieval_diagnostics，展示渠道失败及截断信息；旧摘要不覆盖。not_acquired 仅代表本次未取得。
- 后续用 read_artifacts 的 source_sections 读具体方法、结果或其他相关段落。索引预览不计为正文阅读。
- objective-react-agent skill 新增“主动选择正文工具”：针对机制证据、实验条件、人群/药物代际、安全性和跨文献比较缺口考虑正文；明确摘要事实可以直接出题。

## 获取能力实测

记录：data/experiments/fulltext-tool-check-20261112，直接调用生产 PubMedMaterialProvider.retrieve_details，真实网络，逐篇保存完整结果。

| PMID | 结果 | 说明 |
|---|---|---|
| 32094664 | SUCCEEDED | BRCA1-BARD1综述，正文87426字符，未截断，无fallback_errors。 |
| 33667488 | FAILED | PubTator及PMC渠道连接重置；下载备选中Unpaywall邮箱、Elsevier key未设置，Europe PMC/Semantic指向的出版商PDF受Cloudflare限制，OSTI无记录。不能判断论文在任何渠道都不可获取。 |

此次未绕过付费或反爬限制，未修改用户凭据。

## 自主行为实测

使用更新后工具说明和skill，生产CLI、当前Qwen131072、free策略、PrimeKG+PubMed，无指定论文或手工插入工具动作：

- data/experiments/brca1-fulltext-choice-20261112：目标1题，发布1题。选择摘要直接出题；没有read_source_details，也没有fetch_full_text=true。
- data/experiments/il6-fulltext-choice-20261112：目标2题，发布1题后停止。仍未取正文。过程中出现重复无发布提案的发布请求，最后模型误称目标达到；原始记录保留，没有为本任务顺带改动其他编排。

结论：工具能力和获取/阅读边界已明确，但这两轮没有证明模型主动读正文的倾向改善。BRCA1已经实测存在可取正文，也不能据此说这轮模型选中的另一篇论文必然可取。两轮均未实际走到新增fulltext.*观察，线上观察接线由离线集成测试覆盖。

## 验证

38项相关测试通过：research_tools、research_dependencies、react_main_agent、reading_ledger、fulltext_download。覆盖正文另存、索引可用、获取不等于阅读、失败原因保留及摘要保留。代码审查未发现阻塞问题。

本轮完成的是工具返回及选择指导；下一步若继续优化，应在固定材料/相同问题缺口下比较模型决策，避免把自由换题后的不调用一概当成工具故障，或用强制调用伪装成自主选择改善。

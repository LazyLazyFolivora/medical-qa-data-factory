# BRCA1 全检索源真实路径观察

## 运行条件

- 新任务：`react-live-brca1-crosssource-20261106`，没有沿用旧候选或断点。
- 实体 BRCA1 / 672，hard 策略，目标 1 题，最多 30 个动作、100 次模型调用。
- 显式开放 primekg、pubmed、hgnc、ncbi_gene、ensembl。
- 使用现有 CLI 和当前模型配置；未修改代码、skills，未人为指定下一步动作。
- 数据目录：`data/runs/react-live-brca1-crosssource-20261106`。实际发送的 prompt、模型回复、工具结果在 `debug_trace/`。
- 结果：15 个动作后模型主动 stop，COMPLETED_WITH_ZERO_QUESTIONS，发布 0 题。不是预算耗尽或网络检索报错。

## 实际路径

| 步骤 | 行为 | 结果 |
|---|---|---|
| 1 | PrimeKG：BRCA1，direction=both | 成功返回 10 条，主要是组织关联 |
| 2 | PrimeKG：BRCA1，outgoing，gene/protein、disease | 成功返回 10 条疾病关联 |
| 3 | 读取图谱材料 | 执行 |
| 4 | 读取超过 8 个材料 | tool_feedback：Read at most 8 artifacts per call |
| 5 | 读取时抄错材料 ID | tool_feedback：Unknown artifact |
| 6 | 再读图谱材料 | 执行 |
| 7 | 原始决策想调用 PubMed 检索 | query 是字符串而非对象；被校验拒绝，没有发出检索 |
| 8 | understand_facts | 对图谱疾病关联提取事实 |
| 9 | 原始决策想调用 PubMed 补证 CEBPA/AML 关联 | 使用不存在的 hypothesis_id；被拒绝，query 仍为字符串 |
| 10 | 原始决策想调用 PrimeKG 补证 | 再次使用不存在的 hypothesis_id；被拒绝 |
| 11 | design_questions | 生成“所有经证实的 AML 亚型”枚举题 |
| 12 | review_candidate | REJECT |
| 13 | design_questions | 改成排除 Miller-Dieker 后列举 AML 亚型 |
| 14 | review_candidate | REJECT |
| 15 | stop | 模型认为当前图谱方向没有可靠的困难题机会，主动结束 |

第 7 步原始模型输出在 `debug_trace/1793949470944646400-84a9ead044384375aff1a2b03c192481-model_result.json`。它明确意识到 disease_protein 关系不足以说明机制，要求到 PubMed 区分关联性质，但参数为：

```json
{"provider_id":"pubmed","query":"BRCA1 gene mutations AML leukemia Miller-Dieker Fanconi anemia clinical features"}
```

正确的工具结构应使用 `query: {"term": "..."}`。当时返回的泛化错误是 `Search requires query and concrete evidence_need`，没有准确指出 query 类型不对。

第 9 步在 `debug_trace/1793949518395865400-0f38f4cc08f04eefa1ab9cec69d47902-model_result.json`。它再次尝试 PubMed，却选择需要已有候选的 request_more_evidence，并自行编造 `hypothesis.brca1.aml.cebpa.specificity`。此时尚未生成任何正式候选。模型也可以使用 discovery 检索来调查这条线索，但没有这样修复。

## 两道实际题目

1. BRCA1 蛋白与哪些具体的急性髓系白血病（AML）亚型存在关联？请列出所有经证实的 AML 亚型名称。
2. 在 BRCA1 蛋白关联的疾病中，除 Miller-Dieker 无脑回综合征外，还有哪些具体的急性髓系白血病（AML）亚型？

第一题把有限图谱结果升级为“所有经证实”，没有穷尽性证据。第二题仍没有明确集合边界，排除非 AML 疾病没有构成必要的多步求解。这两处设计问题可从本次材料和题干直接看出。

评审还以模型自身知识质疑图谱关联的医学可信度。这些意见没有通过本轮外部检索证实，不能直接当作图谱错误的事实结论；评审对“排除某疾病”的部分解释也不严谨。这里只记录其实际拒绝行为，不背书全部理由。

## 定位

1. 不是没开放其他来源：五个源均已开放。
2. 不是模型完全没想到延伸检索：两次原始输出明确选择 PubMed。
3. 实际跨来源检索被工具参数/前置条件阻断；没有 PubMed 请求抵达 provider，因此这次不能判断 PubMed 可用性或召回质量。
4. 模型未修复失败请求，转而用图谱关系硬造困难题；主代理的设计指令明确要求列出所有亚型，放大了材料不完整的问题。
5. 模型把一个图谱方向失败当作结束理由，未尝试其他来源或新的研究方向。停止理由不能证明“确实无方向可查”。

下一项可控对照：只改善工具参数说明与精确报错，保持研究策略不变，观察模型能否自行修复并真实调用 PubMed。该改动尚未实施。本次没有产生完整的跨来源材料集，不应把仅有的图谱材料误标为完整检索链路结果。

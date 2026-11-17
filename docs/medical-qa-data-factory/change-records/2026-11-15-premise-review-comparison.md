# 题目前提逐项对照实验

## 问题
CFTR 完整运行的第二题问 III 类门控突变破坏什么界面与原子作用以 prevent 通道开放，原文 p34 的一般机制表述为 predicted to interfere，并另举特定突变。原评审接受，未充分区分确定程度及实例/类别范围。

## 固定输入
保持 CFTR 原始候选题和已保存 source_passages，隐藏候选答案和出题者解释。Qwen 推理开启，输出预算8192；原 reader system prompt 保留。

## 三次实际结果
- `premise-comparison-20261115-025935`：新增 premise_checks 列表置于 verdict 前，模型返回空列表，两题均 ACCEPT。
- `premise-required-20261115-030125`：改为必填 question_premise/source_quote/scope_comparison 字段。正常第一题 ACCEPT；第二题 REVISE，指出 predicted 与 prevent 强度差异。
- `reader-v4-production-20261115-030428`：将该结构试接入正式 review_questions，再用同样材料重放。两题均 ACCEPT；第二题虽然引用 predicted，比较字段仍判定一致。引用还有省略号拼接，不应当作已校验的逐字引文。

## 决定
撤回本次 question_reader.py 的字段、context 和 v4 版本改动，保留既有 v3。单次改善不可复现，不能声称解决质量问题；也不能以格式完整代替正确判断。全部实验输入、输出和 prompt 元数据保存在上述目录。当前 TP53 完整运行仍使用启动时的旧 reader，并未混入实验结构。

## 外部原始研究交叉核对

为避免仅凭措辞主观判错，检索了原始研究。PMID 26846474（https://pubmed.ncbi.nlm.nih.gov/26846474/）描述 R117H 降低开放概率，并明确其缺陷不源于 NBD 功能异常；即使 NBD 维持二聚化，门控缺陷仍在。PMID 17353351（https://pubmed.ncbi.nlm.nih.gov/17353351/）区分 G551D 的 ATP 响应缺陷与残留的 ATP 非依赖门控。这进一步支持需要区分突变、门控机制和完全阻断的范围；不能只因综述同一段出现这些机制就将其归并为整类通用结论。

这些是本次人工审计补充的外部依据，未注入原 CFTR 运行，也不能宣称原系统已经自动完成这次交叉核对。

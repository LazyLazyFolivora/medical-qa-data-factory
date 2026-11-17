# 第二轮检索内容与结论审查

审查实际 history.json、sources.json、understanding.json；另外使用同一 PubMedClient 做简化查询并抓取漏检原始研究。未改生产代码。

## 结论

已有四篇材料是真实、可读且大体相关的资料，但仅第一轮搜索取得材料，后续四轮为空。材料集合不足以支持最终机制主题；不能据此认定公开研究不存在。

- PMID 37879444（MARIPOSA-2）：直接支持特定人群（EGFR exon19del/L858R、奥希替尼后进展）中含 amivantamab 化疗方案相对化疗的 PFS/ORR 等结果。不能推出其逆转免疫检查点抑制剂耐药、改变Tregs/MDSCs或由Fc效应造成临床获益。
- PMID 35742933：EGFR突变NSCLC与免疫微环境相关综述，可作背景；摘要保留争议，不支持把所有患者的机制视为相同、确定。
- PMID 35935943：EGFR突变NSCLC免疫治疗综述，指出效果差异及罕见/敏感突变亚型区别，不能概括为所有EGFR突变患者天然不响应。
- PMID 40747794：一般NSCLC免疫治疗耐药综述，包含EGFR、STK11、KEAP1及微环境因素。不能把文中一般性的MDSC/Treg现象都归因为EGFR突变。

## 查询问题

原查询 Amivantamab Fc effector function ADCP ADCC tumor microenvironment immune modulation EGFR mutation subtype difference immunotherapy resistance 同时要求太多概念。后续只是同一串概念换词，没有分解问题、没有采用研发代号 JNJ-61186372、没有从机制研究中发现新的关键词。

同一接口对照（limit=10）：
- amivantamab Fc：SUCCESS，包含 32747419、27786612。
- JNJ-61186372 Fc：SUCCESS，包含 32747419。
- amivantamab trogocytosis：SUCCESS，包含 32747419。

因此空结果不能归因于PubMed没有该类机制资料。此对照不是检索召回率的完整评价，但已足以推翻“无可用Fc机制研究”的泛化判断。

## 漏掉的关键研究

https://pubmed.ncbi.nlm.nih.gov/27786612/ ：临床前研究，低岩藻糖化增强ADCC及FcγRIIIa结合；Fc-silent与Fc-competent对照支持Fc作用的重要性。

https://pubmed.ncbi.nlm.nih.gov/32747419/ ：细胞共培养及异种移植模型，研究单核/巨噬细胞和trogocytosis（膜片段啃噬）对受体下调及抗肿瘤效应的作用。与完整吞噬不同，不能强行把它归入ADCP。

上述论文能回答Fc功能及受体下调机制的一部分问题；仍不能直接证明“通过减少Tregs/MDSCs逆转ICI耐药”，后者是另外一项待证明的命题。

## 对综合认识的评价

正确：未把临床疗效自动当成具体机制的证明，没有在缺链处强行出题。
错误：从TKI耐药后治疗试验跳到ICI免疫耐药；把综述多种背景混成EGFR统一因果链；将几轮未检索到扩大为公开文献缺乏证据。

应按现有证据转向可核对主题：为什么受体信号阻断不足以解释该抗体在特定模型中的全部效应，以及Fc/免疫细胞怎样参与受体下调。先阅读原始研究的模型和对照，再评估能否形成综合问题，不先规定其必须逆转某种耐药。

保存对照：data/runs/research-first-egfr-v2-20261106/retrieval-audit-controls.json；missed-primary-studies.json。审查以摘要及可访问正文片段为基础，并非全文和补充材料的完整复核。

# NCBI Gene 查询指导

## 用它解决什么

用于确认基因身份、规范名称、别名、物种、染色体或细胞遗传学位置，以及读取功能概要、寻找进一步研究的线索。先对照当前信息需求：如果需要的是突变对药物响应的影响、实验机制或具体临床结果，应搜索 PubMed；不必为每个任务都先查基因位置。

## 当前工具怎样查

调用 retrieve_materials，provider_id=ncbi_gene。当前适配器支持：

- 已知基因符号：query={"symbol":"EGFR"}。程序实际检索“符号[Gene Name] AND human[orgn]”；该字段包含规范符号、别名和locus tag，不检索只是正文提到该基因的记录。别名仍可能歧义，返回后要核对身份。
- 基因或蛋白全称：query={"entity_name":"epidermal growth factor receptor"}，或使用mention；程序使用[Gene/Protein Name]字段，不使用全字段搜索。能确认符号时优先symbol。
- 已由来源确认 NCBI GeneID：query={"native_id":"1956"}。必须是数字字符串。不要把 Ensembl、UniProt 或其他数据库的编号直接当作 GeneID；只有已确认的跨库映射才可使用。
- 同时提供两者时，数字 native_id 优先；symbol 不会自动充当身份核验条件。仍需检查返回记录。

名称查询目前固定为人类；数字 GeneID 查询不加物种限制。当前没有 taxid、organism、任意 Entrez term、转录本选择等可用查询参数，不要编造这些字段。非人类任务不能直接套用名称查询；可使用已确认的 GeneID，并核对返回物种，或改用合适来源。

底层先 ESearch，再 ESummary，最多返回前 5 个候选；提高 material_limit 不会突破这个候选上限。返回的是摘要记录，不是 NCBI 网站的完整 Gene 页面、全部关联文献或论文正文。

## 拿到候选后先确认身份

用 read_artifacts 读取实际返回材料，结合 uid、organism、name、nomenclaturesymbol、description、otheraliases 等字段核对实体。人类记录应核对 organism 的物种信息，而不只是名字相同。

多个候选时依据规范符号、别名、物种和描述消歧，不把搜索第一条当作正确答案。别名或全称检索仍有歧义时，先取得明确的规范名称或 GeneID，再查询；不能为了保留结果任取一个。若记录标示停用、替换或重定向，核对当前有效记录，不把旧记录和新记录拼成两个基因。

## 按问题读取字段

| 信息需求 | 读取重点与边界 |
|---|---|
| 名称、身份与别名 | name、nomenclaturesymbol、nomenclaturename、nomenclaturestatus、otheraliases、description。区分基因名称、蛋白名称和别名，字段缺失时不靠记忆补齐。 |
| 染色体与细胞遗传学位置 | chromosome 表示染色体；maplocation 是图谱位置，对人类常为细胞遗传学带区。染色体、带区和碱基坐标是不同精度，按题意使用。 |
| 精确基因组坐标 | 读取 genomicinfo 等实际提供的定位信息，保留参考序列及版本，并核对组装版本和坐标约定。多条定位或 locationhist 不能不加区分地当成当前唯一坐标；缺少必要版本信息时另查，不能猜。 |
| 功能或关联 | summary 如存在，可支持其中直接陈述且保留条件的概要事实，也可提供功能、通路、疾病等检索线索。ESummary 不保证返回完整功能综述，更不保证每条机制都有原始证据。 |

只读取与当前需求有关的内容。字段为空说明该记录未提供，不能推断“没有功能”“不表达”或“与疾病无关”。编号、排序字段和数据库关联数量不是生物学作用强弱的证据。

## 什么时候继续、换来源或返回

身份和位置等需求已有明确字段支持时，可以返回相应事实及来源，不为凑多来源重复查。需要解释作用机制、比较突变、药物响应、人群差异或实验条件时，把摘要里的实体与关系作为线索，转到 PubMed 搜索并阅读相关材料；链接或文献编号不等于已经读过论文。

空结果时检查名称、别名、GeneID 和物种限制；返回多个候选时先消歧。网络或接口错误与“没有记录”分开报告。任务要求超出摘要记录能力时说明具体缺口，选择合适来源，不重复同一查询期待更完整正文。

## 维护依据

本指南针对当前 NcbiGeneMaterialProvider 的 ESearch + ESummary 实现，能力变更时同步更新。
- NCBI Gene 官方帮助：https://www.ncbi.nlm.nih.gov/books/NBK3841/
- Gene 摘要字段说明：https://www.ncbi.nlm.nih.gov/books/NBK3841/table/EntrezGene.T.the_name_attributes_of_gene/

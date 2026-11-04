---
name: objective-workflow-planner
description: 分层调度中的材料探索与候选定向补证，明确局部循环的目标和退出条件。
---

# 分层检索规划

独立取证只按公开题干提问。用实体标识和待核实的关系构造查询，查询条件必须能从题面或规范实体名解释。基因定位查基因组坐标记录，名称与家族查命名机构，组织表达优先找与检测层级匹配的表达资料，实验观察再按人群、条件和关系检索文献。PrimeKG 的 mention 只填实体名称。发布复核的 release_evidence_gap 要求新来源直接把目标实体和所问属性联系起来。

宿主管理阶段依赖。exploration 选择 retrieve_materials 或 stop；verification_gap 选择 retrieve_materials、finish_search 或 search_exhausted。题数是目标，不强迫凑题。没有有价值的新方向时可以少出题或零产出。

exploration：根据实体、已有事实、候选淘汰理由和检索结果选择一个新方向。功能、表达、疾病关联等是平级方向。优先能形成明确查询的事实，不必查全实体。已失败的题目不应成为反复搜索的理由。相同查询、换措辞但无新信息的查询不是进展。没有合理方向就 stop。宿主在新材料返回后自动理解事实，再尝试设计一批候选。

verification_gap：只为给定 hypothesis 的 evidence_gap 定向补证，不改题、不扩大答案范围、不探索其他方向。初次需求是寻找独立来源核实答案、竞争答案和反证；候选评审 KEEP 仅说明设计可继续，不表示答案已验证，不能用其替代独立补证。后续需求以答案验证阶段提供的具体缺口为准。

finish_search 表示结束证据收集、将已收集证据交给答案验证，不表示候选正确。search_exhausted 表示没有可行的新检索方向，也不表示答案错误。有尚未验证的证据时，两种动作都先交给答案验证；没有证据或验证缺口仍未解决时，宿主以明确的证据不足原因关闭为 INDETERMINATE。你不能用“证据充分”的自由文字直接通过或拒绝候选。局部阶段不使用 stop。

candidate_outcomes 是每个候选的当前状态及后续验证、关闭结果，优先于历史 candidate_reviews。KEEP 不等于 ANSWER_VERIFIED，更不等于 PUBLISHED。判断是否停止探索时必须结合 published_question_count；实际为零时明确说明零产出与后续探索价值，不能说已有两道得到支持的题目。拒绝或关闭的原因可用于选择新方向，不能把早期保留结论当作现有成果。

使用 available_providers 中的 provider_id。query 使用该数据源的字段：pubmed={"term":"focused query"}，primekg={"mention":"entity", "direction":"both"}，hgnc={"symbol":"symbol"}，ncbi_gene/ensembl={"symbol":"symbol"}。保持检索开放性，不用已知答案作为唯一检索词，不回避反证。evidence_need 写清本轮要找什么；rationale 写清为什么值得继续或为什么应退出。

每个探索方向和候选都有宿主预算。不要通过重新包装查询规避预算；不得把题数或难度不足当作无限补查的理由。

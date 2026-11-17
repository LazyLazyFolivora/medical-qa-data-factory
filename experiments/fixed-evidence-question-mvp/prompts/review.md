核对候选题与原文。先逐字摘录candidate.question里的物种、疾病阶段、干预范围；没有的填null。禁止从答案、来源、笔记补进这些摘录。然后再看原文对应条件是否一致。
返回简短JSON：{"question_scope":{"species_quote":"题干原文或null","disease_stage_quote":"题干原文或null","intervention_quote":"题干原文或null"},"verdict":"PASS或REVISE或NEEDS_EVIDENCE","issues":[{"location":"question或answer或answer_evidence","text":"有问题的原词句","evidence_relation":"unsupported或contradicted或missing_scope或citation_gap","source_sentence_ids":["S1"],"minimal_fix":"具体最小修正"}],"reason":"简短理由"}。
评审原则：答案应回答题目，并由原文支持。题干必须公开决定适用范围的物种、疾病阶段和干预。不要把同一上游干预导致的同时变化升级为某中介独立因果。原文未充分支持不等于证实相反。特别核对疾病阶段是否真的写在question中；只有疾病名称不等于交代了转化阶段。药理抑制不必意味着分子被完全清除。
无需追求困难题，不以专业术语、单篇摘要或简单答案判低质。不要求解决题目没有问到的所有机制。只有具体且有依据的缺陷才REVISE，改写保持原问题；所需依据不在原文则NEEDS_EVIDENCE。反馈和模型ready均不是证据。原文引用使用句子编号，不编造。句子编号用于索引，不需要只靠一条引文包含所有背景；可综合给定原文。
判断自包含时，答案和原文中的条件不能替公开题干补足。若给定结果只在特定物种或病程下报告，而question缺该适用条件，不得以“答案补充了”“来源说明了”为理由判PASS；明确指出应补的最小条件。逐一核对题目和答案新增的名称、译名是否与原词对应，不确定译名时要求保留原英文词，不自行推荐一个未经核对的译名。

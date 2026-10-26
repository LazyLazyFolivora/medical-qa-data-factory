# Layperson Search Query

Write one question as a Chinese-speaking non-expert would type it into a search
box or ask a knowledgeable assistant.

The supplied question blueprint and frozen evidence are the only source of
medical facts. Preserve the seed entity, relation direction, required evidence,
answer entity type, and all explicit constraints. Do not change the logical
question.

Prefer short, direct wording. Use natural Chinese instead of graph or database
terminology. Do not mention PrimeKG, head, tail, action type, edge, evidence ID,
or internal field names. Do not state or strongly paraphrase the answer or a
required intermediate entity. Do not add symptoms, diagnoses, age, sex, test
results, medication history, or other facts not supplied in the payload.

Do not describe node types or say 暴露因素, 关联记录, 关系类型, or 哪种实体.
Ask about the user's actual subject and target directly, such as
"阿司匹林和哪种疾病有关？" rather than "阿司匹林这种暴露因素与哪种疾病有关？".

The output must remain answerable with exactly the supplied evidence and must
not introduce causal, therapeutic, or diagnostic meaning that the relation does
not assert.


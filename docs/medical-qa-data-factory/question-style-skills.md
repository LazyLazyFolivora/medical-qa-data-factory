# Question Style Skills

问题风格 Skill 是本项目自己的版本化资产，与 Codex 用户级 Skill 目录相互独立。
它们约束 Writer 如何把结构化证据蓝图表达成真实用户可能会输入的问题，但不能改变
答案、关系、证据或逻辑约束。

## 目录结构

```text
skills/question-styles/<skill-id>/
  SKILL.md
  skill.json
  examples.jsonl
  hard-negatives.jsonl
```

- `SKILL.md`：实际发送给模型的风格 Prompt；
- `skill.json`：Skill ID、版本、适用题型、style profiles、长度与禁用词；
- `examples.jsonl`：正例，运行时最多取前三条放入模型指令；
- `hard-negatives.jsonl`：反例，运行时最多取前两条放入模型指令。

当前版本：

- `layperson-search-v1`
- `patient-caregiver-v1`
- `medical-learner-v1`

## 生成流程

```text
QuestionBlueprint
→ 选择 Skill
→ 每蓝图生成最多 3 个 style profile
→ Writer 合约校验 + 样本级拒绝
→ Style Judge（不接收答案）
→ 确定性泄漏检查
→ Answer Verifier（完整冻结证据池，答案盲测）
→ 持久化候选问题
```

同一蓝图的多个表述共享 `logical_question_id`，便于后续按组审核和选择主问题。

## 强制规则

Writer 不得改变：

- 种子实体；
- 关系链、方向和必需证据；
- 候选答案实体；
- 答案类型；
- 题目约束。

风格 Skill 不得：

- 引入未提供的病史、年龄、性别、症状或检查结果；
- 把关联升级为因果、治疗、诊断或机制；
- 写出答案、答案别名或必要中间实体；
- 出现 PrimeKG、数据库字段、head/tail、evidence ID 等技术词；
- 把医学问答改写成要求诊断、处方或治疗建议的问题。

Style Judge 不接收候选答案；Answer Verifier 接收完整的有界冻结证据池，用于识别
过宽问题和替代答案。LLM 验证之外仍有确定性检查。

## 新增或升级 Skill

1. 新建 `<new-skill-id>` 目录并填写四个文件。
2. `skill.json.skill_id` 必须与目录名一致。
3. 修改 Prompt、正例或反例时必须提升 `version`。
4. 运行 Skill 与 Web 测试。
5. 用 fake model 验证流程，再用真实 structured model 做一次混合风格 smoke。

运行元数据会记录角色 Prompt hash 以及每个 Skill 的 Prompt/样例 hash。

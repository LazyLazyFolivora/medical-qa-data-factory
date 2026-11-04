# 客观出题 Skills 使用与代码迁移说明

更新日期：2026-11-02。状态：已接入 skill 加载与部分输入交接，完整状态机迁移尚未完成。

## 分层循环更新（2026-11-03）

Web/CLI 已切换到分层调度，依赖、局部循环、退出条件及初始预算见 [hierarchical-generation-workflow.md](hierarchical-generation-workflow.md)。新增 objective-workflow-planner 只规划探索或候选定向补证；候选判断由 objective-candidate-review 执行。验证盲解接收独立引文但隔离候选答案。验证补证、发布题干修改已接入有界回环。以下旧迁移记录描述历史状态，以本节和分层流程文档为准。

## 本轮迁移记录

第三批已完成：验证阶段不再把 retrieval 返回的全部 anchor 自动视为支持证据。反证审查现在必须显式给出直接支持引用、矛盾引用、竞争答案，并声明所选文本是否直接蕴含候选答案；只有验证尝试中的、且不属于发现阶段的直接支持引用才能进入 VERIFIED resolution。隐藏引用会触发角色契约错误，主题相关但不直接蕴含答案的材料返回 INDETERMINATE。当前“不同于发现 anchor”仍不等于已证明来源独立；转载、同一上游记录和跨 provider 复制的识别仍需补充材料级来源指纹。

verification retrieval 新增 `verification_focus`（support、competing_answers、contradictions 或 comprehensive），实际 artifact 保存该 focus，竞争答案和反证查询覆盖由成功的实际检索记录计算，不再无条件写死。为兼容迁移前已有 artifact，缺失 focus 的历史 verification 记录按 comprehensive 解释；新调用均会显式写入。

题面写作增加最多三次的局部修复循环：检测到来源相对措辞、meta-evidence 措辞或冻结字段漂移时，把原草稿和确定性错误反馈给同一写作角色，只允许修复措辞，不能改变契约或答案。三次仍失败才交由宿主记录失败并设计替代题。答案验证的 INDETERMINATE 现在保持为 hypothesis INDETERMINATE，不再误记为 REJECTED。

第三批验证：完整测试集 1215 项通过、3 项跳过、13 项按配置排除；新增“检索到但不直接蕴含答案不能验证”和“非法题面可在冻结契约内修复”的回归案例。尚未完成材料级来源独立性判定、发布审核失败后的结构化修复路由、真实模型/Planner 联调和跨实体稳定性评测。

第二批已完成：事实提取与问题设计允许空结果并携带 insufficiency_reason；设计明确返回空时不执行强制 portfolio 补题。主循环保存缺口诊断，主 Agent 摘要展示失败、审核及空结果原因。GenerationGoal 新增可选 language 和 planner_environment，设计规格传至写作与答案审核，解释也进入答案审核；公开题面审核的隔离保持不变。环境信息必须由调用方真实提供，当前未增加 CLI/Web 环境配置入口或自动可达性核验。

第二批验证：skill 加载、角色、主循环、审核、写作、主 Agent、工作流与 Web 相关测试共 103 项通过，包含空事实、空设计和审核上下文隔离的新回归案例。仍未实施版本化重写／重验、真实语义支持评估和未达数量目标的主动失败退出。

`llm/objective_skills.py` 已按角色装配六份新 skill 及所需参考，`StructuredGenerationRequest` 在未显式指定 skill 时自动加载；已有显式 skill 不覆盖。发布审核继续使用自己的 loader。设计按目标难度选择参考，装载内容与阶段共同计算版本并加入自动装载请求的幂等键。

已补充检索的具体 evidence_need、事实理解的锚点引文映射与矛盾引用合法性检查，以及答案 compare／counterexample 阶段的完整问题上下文。盲解输入保持隔离。主 Agent 系统提示已移除零题 STOP 的冲突表述。

验证：使用 qa_env 运行 skill 加载、题面生成、主 Agent、材料检索、搜索 Agent、角色约束、发布审核和主循环测试，81 项通过。尚未运行真实模型与 Planner 联调。

下文的“未接入”表格和差距描述保留为迁移前基线；当前状态以本节为准。仍需实现发布审核失败后的结构化修复路由、改变语义后的重新验证、Planner 环境实际可达性核验、跨实体评测及运行失败后的产品级恢复入口。不能将当前 Prompt 接入视为完整迁移完成。

部署注意：当前 loader 从源码项目根目录的 skills 文件夹读取，需随部署提供该目录；尚未实现独立 wheel 内资源打包。缺文件时明确失败。

本文面向后续代码迁移，描述目前七份 skill 如何装载、分派和交接。范围为 `ObjectiveQuestionAgentLoop` 及其角色调用，不替代项目总体架构，也不涉及 Planner 训练标签和数据导出。

## 1. 共同目标和当前状态

生成的问题必须同时满足：

1. 适合 query-planner，通过明确的事实检索、关系连接、条件筛选或证据整合求解。
2. 客观有据，题干能独立定义主体、范围和作答要求，不依赖隐藏出题材料、候选集或数据库覆盖。

题干定义任务，检索提供答案依据。不能为了凑数量、提高难度或迁就工具限制，删掉条件、制造关系或把局部唯一当作客观唯一。

当前七份 skill 的文本已完成初稿和一致性复核。只有发布审核 skill 已被运行时加载；其余六份仍需接入。格式及链接检查不证明模型遵从性、生成稳定性或 Planner 实际可求解性。

特别注意：发布审核规范已经增加 Planner 环境与难度检查，但现有运行时没有完整传递这些输入。严格执行时可能返回不确定并阻止发布，不能把新增规范等同于代码已经支持。

## 2. Skill 与代码入口映射

下列路径相对于本项目根目录，链接可直接访问。

| Skill | 用途 | 当前代码入口／模型 role | 接入状态 |
|---|---|---|---|
| [objective-main-agent](../../skills/objective-main-agent/SKILL.md) | 下一步动作、检索方向、失败回退 | [main_agent.py](../../src/medical_qa_data_factory/generation/main_agent.py) / `objective_main_agent` | 未接入 |
| [objective-material-retrieval](../../skills/objective-material-retrieval/SKILL.md) | 实体驱动探索、筛选、补查 | [evidence_search_agent.py](../../src/medical_qa_data_factory/generation/evidence_search_agent.py)，[material_retrieval_subagent.py](../../src/medical_qa_data_factory/generation/material_retrieval_subagent.py) / `material_retrieval` | 未接入 |
| [objective-fact-understanding](../../skills/objective-fact-understanding/SKILL.md) | 保留条件和证据的事实提取 | [fact_exploration.py](../../src/medical_qa_data_factory/generation/fact_exploration.py) / `fact_understanding` | 未接入 |
| [objective-question-design](../../skills/objective-question-design/SKILL.md) | 分级设计查询任务 | [question_design.py](../../src/medical_qa_data_factory/generation/question_design.py) / `objective_question_design` | 未接入 |
| [objective-answer-verification](../../skills/objective-answer-verification/SKILL.md) | 独立求解、支持、竞争答案与冲突 | [answer_verification_subagent.py](../../src/medical_qa_data_factory/generation/answer_verification_subagent.py) / `objective_answer_verification` | 未接入 |
| [objective-question-realization](../../skills/objective-question-realization/SKILL.md) | 冻结语义转成自包含题面 | [objective_question_realization.py](../../src/medical_qa_data_factory/generation/objective_question_realization.py) / `objective_question_realization` | 未接入 |
| [objective-question-validator](../../skills/objective-question-validator/SKILL.md) | 对原题进行两阶段发布审核 | [release_review_subagent.py](../../src/medical_qa_data_factory/generation/release_review_subagent.py) / `objective_release_review` | 已加载主文件 |

Skill ID 不一定等于模型 role，例如 validator 对应 release_review。不要为方便统一名字而破坏已有角色配置或响应 schema。

## 3. 加载方式

### 3.1 已有调用能力

[StructuredGenerationRequest](../../src/medical_qa_data_factory/llm/contracts.py) 已包含：

- `skill_id`
- `skill_version`
- `skill_instruction`

这三个字段必须同时有值或同时不设置。当前 [openai_compatible.py](../../src/medical_qa_data_factory/llm/providers/openai_compatible.py) 把 `skill_instruction` 追加到角色系统提示；动态 payload 和响应 JSON schema 放在用户消息中。

仅在仓库创建 SKILL.md，不会让模型自动看到文件。普通结构化模型调用也不会因 Markdown 链接自动读取参考文件，必须由宿主加载内容。

### 3.2 建议的装配流程（待实现）

1. 根据 role 和当前子阶段，从固定映射中选择 skill。路径由代码允许列表决定，不允许来源材料或模型返回值指定任意本地文件。
2. 读取 SKILL.md，按明确路由加载所需的教学参考；使用 UTF-8 并保留可复现的文件顺序和内容边界。
3. 将装配后的规范作为 `skill_instruction`，填写对应 ID 和版本。
4. 将本次实体、材料、设计或契约作为 payload，保持材料是数据而非指令的边界。
5. 提供与当前阶段一致的响应 schema。不要让 skill 中的完整示例覆盖阶段 schema。
6. 记录装载文件、内容摘要、阶段、角色模板版本和模型配置，确保后续能够还原调用。

不能只修改 `system_prompt_version` 字符串期待选择另一份模板：当前 provider 按 role 调用 `template_for()` 取得模板正文。模板正文、版本标识和 skill 内容需要一起对齐。

### 3.3 参考文件装配

| 角色 | 应随阶段提供的参考 | 主要供开发者阅读的参考 |
|---|---|---|
| 主 Agent | 主文件中的路由与行为要求 | `current-runtime.md`、`stage-handoffs.md` |
| 检索 | `entity-directions.md`、`search-strategies.md` 中任务相关内容 | 主文件中的现有实现差距 |
| 事实理解 | `extraction-examples.md` | `current-contract.md` |
| 问题设计 | 对应 `easy.md`、`medium.md` 或 `hard.md` | 跨阶段交接说明 |
| 答案验证 | `verification-cases.md`，结合当前子阶段限制 | `current-contract.md` |
| 题面写作 | `writing-examples.md` | `current-contract.md` |
| 发布审核 | 主文件内的规则和校准例子，结合当前子阶段限制 | `runtime-integration.md` |

主 Agent 需要方向规划规则，不需要每轮加载所有角色的示例全文。一个设计调用若同时承担多个难度，需要对应方法全部可见，或拆成按难度明确的调用；不能只传标签。

当前 SKILL.md 同时含运行规范和迁移注释。接入前应明确区分模型用指令与开发者说明，统一调整文件路由和装配策略；不要临时按不稳定标题截取文本，或让模型执行“修改 schema”等工程说明。

### 3.4 版本与缓存

版本摘要应覆盖实际装载的主文件、参考文件、装配策略和阶段选择；修改难度参考也应改变对应调用版本。现有发布审核摘要只覆盖其实际读取的主文件，不能误称已覆盖所有参考。

迁移时核对 operation key、idempotency key 和模型缓存是否包含有效规范版本，避免新 skill 继续复用旧响应。进行中的运行宜固定规范版本；恢复时必须识别版本变化并采用明确策略，不能混用前后审核语义。

## 4. 交接内容和隔离

| 阶段 | 必要输入 | 交付给下一步 |
|---|---|---|
| 调度 | 目标、合法动作、预算、产物及具体失败原因 | 明确的动作、对象和本次需求 |
| 检索 | 实体身份、方向或缺口、指定来源、查询与限制 | 原始材料、锚点映射、来源、真实搜索记录及缺口 |
| 事实理解 | 原文、实体信息、锚点引文与位置 | 保留条件、方向、强度和引用的候选事实 |
| 设计 | 候选事实、语言、题型、目标难度、可用环境信息 | 问题意图、拟公开范围、候选答案、数量语义和检索设计 |
| 验证 | 分阶段隔离输入，随后完整候选语义和验证证据 | 验证状态、依据、必要公开条件及可冻结语义 |
| 写作 | 冻结契约及继续传递的语言、题型、难度要求 | 实际题干、冻结答案、受支持的解释和回读 |
| 审核 | 分阶段提供题干、契约、答案、证据、环境和已要求的难度 | 对原题的判定、具体缺陷和修复方向 |

三条隔离边界必须单独验证：

1. 答案首次盲解不能接收候选答案、发现阶段论证和答案导向提示。
2. 发布审核首次公开审查只看实际题干，不看答案、契约和隐藏证据。该阶段 PASS 不等于整体发布通过。
3. 最终给 Planner 的公开输入不能混入标准答案、隐藏证据或标准解题路径。

语言、题型、目标难度属于持续交接的任务规格。模型建议的难度是暂定标签，不自动成为用户要求。事实条件则必须经过验证，并最终在题干需要时公开。

完整交接说明见 [stage-handoffs.md](../../skills/objective-main-agent/references/stage-handoffs.md)。

## 5. 不能只替换 Prompt 的现有缺口

| 环节 | 当前差距 | 迁移要求 |
|---|---|---|
| 主 Agent | 部分失败摘要为空；未达数量不能 STOP；旧提示允许零题停止 | 提供具体诊断，统一停止语义，增加合理失败退出，不靠无意义动作耗预算 |
| 检索 | evidence_need 只是 purpose；空选择或筛选失败可触发兜底保留材料 | 传递具体缺口，区分无合格材料、模型失败和真实成功 |
| 事实理解 | 至少一条 claim；锚点只有 ID 列表而无完整对应内容 | 支持零事实正常结果，提供引文及范围，核对支持与矛盾引用 |
| 设计 | 无充分的材料不足分支；设计规格没有完整流向后续 | 支持无法设计及补查需求，显式保存并交接规格 |
| 答案验证 | 验证材料默认算支持；查询计数写死；独立性主要按 anchor ID 区分 | 基于实际内容和记录核验支持、独立性、竞争答案及闭合依据 |
| 验证子阶段 | compare 和反证阶段缺少完整问题上下文 | 保持盲解隔离，后续提供完整语义及相应证据；不把模型记忆一致当验证 |
| 冻结与写作 | 通用禁止项、scope 整段充当条件；无冲突返回和局部重写 | 细化必要条件、传递规格，建立失败和版本化重写路径 |
| 发布审核 | 缺少 Planner 环境和难度输入，解释未传入 | 补齐输入与门禁；没有输入不能声称审查完成 |
| 全链路 | 状态、错误和 skill 装载版本不完全匹配新规范 | 明确负结果、恢复、摘要、版本和缓存策略 |

主 Agent 和各子 Agent 不能通过生成未支持字段或编造新动作解决这些差距。`sufficient`、`VERIFIED`、阶段 `PASS`、最终发布通过含义不同，不能串联成自动通过。

## 6. 工程职责与 Skill 职责

Skill 负责：方向选择、出题原则、事实解读边界、语义判断方法、难度方法和失败诊断。

程序负责：权限与合法动作、输入隔离、引用及身份、预算与执行记录、版本、持久化、缓存、状态转换和最终发布。

语义审核不能只靠关键词；字段一致性也不能证明题面语义一致。反过来，模型判断不能覆盖引用不存在、状态非法、输入版本错误等程序失败。

验证和审核当前没有直接检索工具。规范中的“查竞争答案”“查反证”需落实为审查真实检索记录或向主 Agent 发出补查需求，不能授予隐式工具权限。

## 7. 建议迁移顺序

1. 固定现有基线，记录角色调用、schema 和版本，保留可回放的材料；不要将更新规范后的审核结果与旧运行直接混为一谈。
2. 先明确负结果、任务规格和诊断承载，再实现统一且按角色隔离的 skill 装载，核对模板与局部 instruction 的冲突。
3. 用固定材料串起事实理解、设计、验证、写作与审核，检验能否稳定生成合格单题，暂不以扩大检索量和批量数量作为成功标准。
4. 接入实体探索及具体补查需求，保证搜到的材料能回应设计和验证缺口。
5. 接入主 Agent 的错误摘要、重写、重验及失败退出，验证它能利用反馈，而不是重复同一动作。
6. 在明确 Planner 环境中校准三个难度并试运行，再扩大实体范围和题量。

每一步应保持不确定和拒绝结果可追溯。修复措辞不改变契约可重新写作审核；改变问题范围、答案或关系强度需要重新验证。

## 8. 迁移验收清单

### 静态和调用检查

- 七份 skill 格式和引用有效，缺少必要文件时明确失败，不能静默使用过期规范。
- 每个调用加载正确角色和子阶段，实际内容、参考和版本可追溯。
- schema 与阶段响应一致；公开审查不会被要求输出完整答案审核包。
- 修改参考文件能影响相应版本；重试与恢复不错误复用旧缓存。
- 候选答案、隐藏证据和标准路径不穿越盲解、公开审核或 Planner 输入边界。

### 行为案例

- 直接属性材料能生成合格简单题；没有组合证据时不硬凑难题。
- 材料缺条件会补查或暂缓；不存在合格材料时不强行填充。
- 关联不升级成因果，实体或物种不匹配被识别。
- 例子型答案不因存在其他正确例子而误拒；单答案真实多解不能通过。
- 完整集合缺乏闭合依据、转载伪装独立来源或反证检查缺失时不能通过。
- 最终题面漏条件、改变答案范围或提示关键答案能触发对应修复。
- 有效短路径被接受并用于难度校准，不强制额外工具调用。
- Planner 环境未知与事实错误分开报告；公开阶段不因缺少隐藏答案而误拒。
- 相同失败后有针对性调整；预算耗尽、provider 错误和无有效路径都有真实终止记录。

### 稳定性检查

对同一批固定材料重复生成三档题目，分别记录客观正确性、自包含性、Planner 适配、难度符合、修复成功率和主要失败原因。测试规模与通过阈值在试运行前明确，不在本文凭空承诺。

通过格式校验或一次生成成功不足以宣布迁移完成。需保留人工核对及实际模型调用证据，并列出未验证能力。

## 9. 后续维护

规则修改时同步检查设计、验证、写作与审核是否采用同样语义。新增要求要同时说明需要哪些输入、由谁检查、失败回到哪里，以及宿主如何承载结果。

角色行为规范放在对应 skill，模式细节放参考，程序执行事实放接入说明。不要在系统 Prompt、payload instruction 和 skill 三处维护互相矛盾的完整规则副本；系统提示保留职责边界，动态 payload 提供任务，skill 承载可复用方法。

本文件是迁移入口。更细的实现缺口以各 skill 的 `current-contract.md`、`current-runtime.md` 和 `runtime-integration.md` 为线索，并在实际修改代码前重新核对源码。

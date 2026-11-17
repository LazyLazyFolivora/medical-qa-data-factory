# 原文直接出题与已有来源验证（P1/P2）

## 已实现

保留现有ObjectiveQuestionAgentLoop和ReactMainAgent，不新建编排系统。

1. design_questions增加`source_sections={材料ID:[段落ID]}`输入模式。短完整材料可用null；与claim_ids/research_id模式互斥。程序回取原文、创建带原始偏移的锚点，交给原有设计模型。不需要先调用understand_facts，不伪造candidate_claim。
2. QuestionHypothesis增加evidence_basis，缺省claims兼容旧任务；source_passages模式允许空candidate_claim_ids，但仍需真实discovery_anchor_ids。设计模型产生的引用必须来自本次原文。候选评审同时读取这些原文锚点。
3. verify_answer增加existing_sources模式。原文模式候选默认使用它；旧候选可显式指定。原有retrieved模式保留。已有来源模式回读原始blob核对锚点，独立读者仍不见候选答案。
4. 证据计划/覆盖显式记录existing_sources，真实检索attempt列表为空，不伪造检索成功。语义评审可返回具体缺口。审计包含evidence_mode，resolution使用existing-source-review-v1策略标识及实际文档身份，不能称为独立来源交叉验证。
5. 原文设计可以合并多页选段，仍受16000字符总量限制。阅读工具每次4段的分页限制不再误用成出题总段数限制。不会截断原文或替模型改选段。
6. 主agent、设计与验证skill同步说明新旧两种模式。发布仍需验证提案、候选质量KEEP和显式publish；没有自动发布捷径。

相关文件：generation下的source_question_inputs.py、question_design.py、react_main_agent.py、agent_loop.py、react_execution_state.py、react_tools.py、answer_verification_subagent.py、objective_question_contracts.py、subagent_contracts.py。

## 使用方式

主agent可选择：

```json
{"action":"design_questions","parameters":{"source_sections":{"实际材料ID":["实际段落ID"]},"design_instruction":"按原文形成有意义的比较题"}}
```

随后对返回候选执行review_candidate和verify_answer。新原文候选默认已有来源验证；旧候选可指定：

```json
{"action":"verify_answer","parameters":{"hypothesis_id":"实际候选ID","evidence_mode":"existing_sources"}}
```

若需新增来源，主agent仍可request_more_evidence，并以evidence_mode=retrieved核对。两种验证范围有区别，不能仅因同一研究重新检索一次就称为独立支持。

## 工程验证

项目解释器：D:/Anaconda/envs/qa_env/python.exe。13个相关测试文件共157项通过，包含新路径端到端、旧路径、暂停续跑、显式发布、合同、持久化、网页/CLI及skill加载测试。

新增3项测试覆盖：无事实提取/无新检索进入原文验证与发布、无效材料不进入模型、7段原文跨阅读分页组合且不截断。新路径盲答测试断言只有公开题目和原文，没有候选答案；验证计划与attempt列表没有虚构检索。

Python只读代码审查完成，未报告剩余具体P1/P2问题。测试和审查不证明科学答案正确。

## 真实运行与限制

任务`data/runs/direct-source-live-20261109-2`使用真实模型与正式主循环，检索适配器替换为固定PMC5240640全文。没有人工选段、预置事实或参考答案；明确告诉agent固定语料没有新增独立来源。实验使用测试适配器的存储标识document.pubmed，实际论文身份记在experiment.json，不能当作线上PubMed检索成功。

第一次初始化实验（后缀-1）因测试适配器原文末尾空白经合同规范化后与hash不匹配而中断；保留失败轨迹。第二次仅在装载固定语料时去除首尾空白，不改论文内部文本。

首轮12动作：agent出现材料ID与阅读参数错误，最终选择7段原文直接设计，但初版工具继承阅读4段上限，拒绝了总计15621字符的合法选段。修复分页/设计总量混淆后，原任务续跑8动作。

续跑实际调用：design_questions → review_candidate → 4次发布工具前置条件反馈 → verify_answer → verify_answer。没有understand_facts，也没有新增补证检索。生成2个候选，发布0题，预算结束。一题保留PROPOSED并返回ANSWER_SCOPE_UNCONFIRMED（两个答案分项与EXACTLY_ONE不一致）；另一题得到模型验证提案，状态RELEASE_REVIEWED，尚未发布。

质量复核发现严重限制：第二题的盲答与复核补出了“ADCC/ADCP导致受体内化”的确定机制，原文保留替代解释，不能据此认为答案正确。本次不将任何候选计为人工确认合格题。第一题的多分项答案与旧cardinality设计、以及发布误调用，属于后续诊断/修订接口和主agent进展表达需要处理的问题。

因此本轮证明：正式agent能使用原文直接出题，并从已有来源完成盲答和证据评审；不证明步骤总数下降，也不证明产出质量改善。原始请求、模型返回、工具错误和运行结果均在该任务目录保留。未接入自动修订（P3）或研究进展视图（P4），未修改前端、清库或提交git。

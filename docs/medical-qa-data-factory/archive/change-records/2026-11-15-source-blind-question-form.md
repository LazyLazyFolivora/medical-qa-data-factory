# 公开题干评审与证据前提评审分离

## 问题与原始证据
G6PD 真实运行把“其他 tested 补充剂”作为题干比较对象发布。固定材料回放再次 ACCEPT，理由称范围由摘要给出。去掉材料后，同一模型指出读者不知道 tested 指哪些对象。这是后台上下文替公开题干补缺的问题。

## 本轮实施
- 新 objective_question_form_reader 角色只接收题目索引、公开题干及评审上下文说明，不接收 goal、实体提示、材料、答案、笔记。
- 通过表述评审的题目再交给原 objective_question_reader 核对原文与题干前提。原评审保留，未降低标准。
- form 的 REVISE 不会被后续覆盖；索引和顺序保持；两次调用的 usage 累加；错误仍向上传播。
- 审阅版本改为 question-first-v5，并纳入两个模板 hash，避免沿用旧接受记录。
- 两阶段审阅视图分开保存，供发现环节重写与调试。

通过的题目增加一次模型请求。表述不合格的题目不进行第二次证据评审。没有关键词规则判断科学质量，也未强制题目变简单。

## 实际验证及局限
实验根目录：data/experiments/question-first-lab/reader-separation-20261115。

1. 前提抽取+独立断言核对的试验失败：抽取把专业术语当成缺失背景、把疑问当断言；核对仍漏掉口服前提。未采用。
2. 编辑式单次KEEP/EDIT/NEEDS_EVIDENCE试验失败：仍放过隐藏集合，混淆前提错误与答案缺失。未采用。
3. 无材料表述评审9样本保留正常题，识别隐藏集合；没有识别APOE试验数据伪装。不能作为单一质量门槛。
4. 接入后的真实混合批次使用非连续索引2/5/8，正确合并：隐藏集合REVISE、正常关联ACCEPT、安全预设在第二环REVISE。HBB比较与BRCA结构域通过。实际model_request核查证实form payload只含questions和review_context。
5. APOE试验结果题仍被两环接受；HBB表达量问句的评审理由仍暗自补充未给出的知识。模型质量问题未解决。
6. 生产formulate_questions重放：第一轮拒绝tested隐含集合，反馈送回发现模型。第二轮去掉tested却改成常见补充剂的模糊集合，并产生综述立场题。最终零机会。拒绝了原坏题，但没有生成好题，不能称出题成功；第二环还错误认为“差异”必然追问机制。

代码审阅批准。未运行仿真质量测试，以上均为真实模型请求；没有变更默认模型或后台进程。

## 完整任务
启动隔离新实体HFE，目标2题，最多20外层动作：hfe-20261115-051049。需继续审阅终态实际题目与原文，不能用PUBLISHED数量替代质量结论。

## 待继续追查的既有指令矛盾
- generation/question_strategies.py 的 hard.retrieval_goal 仍有“a bounded study comparison is acceptable”；skills/objective-question-design/references/hard.md 也允许一篇研究中模型/干预/终点比较。这些表述需要区分“单篇论文可作为证据”和“以论文结果本身出题”，否则与主skill默认排除论文阅读理解的要求冲突。本轮尚未修改，避免混入正在运行的HFE。
- 默认 gene allowed_fact_types 只有entity_identity/gene_location/classification/observation_association，free策略仍继承easy difficulty_targets。需追踪这些字段是否给模型形成不必要的选题限制；不能仅凭字段名字认定是根因。
- HFE首个研究任务已读到正文，持续收集C282Y/H63D的研究内外显率与风险指标。摘要笔记有把高指标风险倍数写成指标本身倍数的迹象，后续必须对照原文。尚未生成候选，不把检索完成当作质量通过。

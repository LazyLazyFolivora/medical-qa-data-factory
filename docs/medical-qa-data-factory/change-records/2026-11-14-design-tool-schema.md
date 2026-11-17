# 修订工具混用参数：真实失败与 schema 修正

## 观察

LDLR `ldlr-20261114-023501` 连续 8 个动作均未推进，重复混用 research_id 与 source_sections。模型 rationale 明说只传 source_sections，实际参数仍含两者。原 schema 使用 extra=allow 的三个联合分支，因此混用参数符合解码结构，到执行前才被业务校验拒绝。

## 对照与修改

`design-exclusive-schema-20261114-023802` 在原 schema 加 not 条件，重放真实失败输入。服务虽接受请求，仍输出两种参数。这个实验已撤回，不宣称服务所有 JSON Schema 约束都有效。

`design-closed-schema-20261114-023942` 改为封闭设计参数结构，重放相同输入。返回 source_sections + revision_of，没有 research_id，形成合法模式。此次只验证生成调用，未执行所选工具。

正式变更在 `react_call_contracts.py`：DesignParameters 使用 extra=forbid，显式声明共享 design_instruction、revision_of、workflow_version、evidence_sufficiency_reason；SourceDesign 只额外声明 source_sections，ResearchDesign 声明 research_id，ClaimDesign 声明 claim_ids 并保留兼容的可选 research_id。三者继承同样的正式解析规则，实际运行的原文/候选存在性校验保留。其他工具的参数策略不变。

## 后续验证

`ldlr-20261114-024047` 从上述连续失败任务的 after.json 隔离恢复，验证合法调用是否真正执行及修订题质量。

另启动未用于本轮提示词微调的 `tp53-20261114-023625`，目标三题，最多先运行二十个动作，保存所有输入输出。其进程启动早于本次 schema 修改，不将它作为新 schema 的验证。它用于观察已有 question-first、独立评审及评审复用链路的跨实体质量。

当前不能把参数合法性修复等同于合格题目稳定生成。所有请求、响应、失败和隔离检查点保留；没有提交 git、清库或写生产发布列表。

Python 静态审查未发现此次封闭参数结构的兼容性问题：实际消费字段齐全，claim_ids+research_id 保留，host 的 phase/rationale/material_aliases 等元数据在 ReactChoice 校验后才注入，不会被新的 extra=forbid 误拒。全局 pylint 环境缺依赖，不能作为代码验证结果；本轮依据为源码审查和真实模型重放。

# 工厂公开问题交给 query-planner

独立盲求解消费现有 `TaskReleaseExporter` 的 `public/planner_questions.jsonl`，不消费出题探索轨迹。没有增加第二套问题格式或出题流程。

## 生产者

本轮下游已适配 `generate validated --profile primekg` 的公开问题与 PrimeKG 查询环境；该生成路径使用本地只读快照，不调用付费模型。例如：

```bash
medical-qa-data-factory generate validated \
  --profile primekg --kg-config config/knowledge_graphs.json \
  --entity-id <公开种子ID> --entity-type <公开种子类型> \
  --output data/releases/<新的版本目录>
```

只有通过现有验证门槛的任务进入发布。发布数为零时保持零，不能填题来完成联调。

`generate ctd-direct` 及 PrimeKG/CTD/PubMed 融合仍使用同一个发布格式，但不等于本轮下游已经支持这些来源。其预算可能含 `ctd_access`、`pubmed_access`、`primekg_access`；当前 query-planner 消费者只接受 `relation_access`、`tool_calls`、`tokens`。这些发布产物应明确拒绝为暂不支持，待补对应执行器和成本映射后联调；不能丢弃未知预算维度或借用 PrimeKG 费用口径宣布通过。

公开目录仅包含 `planner_questions.jsonl` 和文件 hash 清单 `dataset_manifest.json`。每行使用 `task-construction-v2`：`qid`、`question`、`qtype`、`budget`、`max_steps`、`sufficiency_threshold`、`note`、`environment_ref`。求解器只获得这些公开字段及独立可查询的来源环境。

答案、完成条件、目标证据、验证报告、出题轨迹以及生成种子/episode/provenance 元数据留在 `hidden/`。两侧 manifest 使用相同 `release_id`；公共 manifest 不列出隐藏文件或探索线索。数据目录隔离不等于操作系统访问隔离，调用方应只给策略进程公开目录，评测进程在求解结束后才读取隐藏目录。

## 消费者与证据

query-planner 的 `src/integration/public_questions.py` 负责公开契约校验及 `Question` 映射；`scripts/run_public_questions.py` 使用自己的求解循环生成新 `transitions`。不允许从 `hidden/evaluation_targets.jsonl` 抽取桥接实体、关系链或答案填入解析器。

旧 `training export` 仍输出 `qa_items.jsonl`、`planner_training_samples.jsonl`。其 manifest 明确 `artifact_role=generation_exploration`、`blind_solver_training_compatible=false`。这些是出题方法研究材料，不是 query-planner `train_bc.py` 所需的独立求解轨迹；不能仅改字段名或将隐藏答案变成策略输入来伪造兼容。

联调应保留：公开发布清单、冻结查询环境的标识、新求解轨迹、结束后的独立评测，以及实际失败/未完成结果。查询失败、预算不足、查询未命中分别记录；未命中不等于医学上的阴性结果。

## 本轮离线验收范围

使用已有 `tests/test_generation_world_evaluation.py` 的四边合成 SQLite 测试环境，通过真实 `ConstrainedTaskGenerationService` 和 `TaskReleaseExporter` 生成公开问题，再由下游独立运行。该环境包含非答案分支，属于接口测试资料，不是真实医药问题或真实效果评价，也不使用付费模型。

实际运行产物放在未跟踪的 `data/releases/` 和下游运行目录，避免把答案及测试数据库提交进源码。重新运行选择新发布目录，已有目录不能覆盖。

## WeKnora 的固定版本边界

普通检索和分块回读继续可用。当前服务契约没有可验证的固定版本选择，配置 `snapshot_id` 不能让 provider 宣称 pinned；正式证据执行器明确拒绝据此生成 gold。详见 [WeKnora 接入契约](medical-qa-data-factory/weknora-rag-integration.md)。

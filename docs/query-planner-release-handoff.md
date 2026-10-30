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

## 多方向 KDD 路由评测（2026-10-30）

新增 `evaluation/kdd_routes.py` 的公开任务与回执环境适配，原 `task-construction-v2`、
PrimeKG/ToyKG runner 和生成轨迹导出保持原样。KDD 公开题目使用
`kdd-route-task-v1`，方向为 `pipeline / research / business / public_health`；
固定问题、来源环境 ID/版本、时间截点、预算、split group 与真实/合成属性。
`schemas/v1/kdd-route-{task,target,environment}.schema.json` 是对应严格 schema。

`write_release(tasks, targets, environments, destination)` 只导出调用方提供的题目和审核，
不制造答案。公开目录有 `kdd_route_tasks.jsonl` 和 `environment_manifests.jsonl`，
隐藏目录有 `kdd_route_targets.jsonl`，两侧 manifest 共享 release ID，公开 manifest 不列隐藏文件。
相同 split group 跨训练/开发/测试分区会拒绝；实际评测还须由协调方冻结时间、对象与来源划分。

### 宿主实际调用

`ObservationReceiptEnvironment` 是已实现的可调用适配器，`start` 和 `execute` 两个回调
由 Radar 的现有执行器提供。工厂不读取来源密钥，不运行额外 Agent，不接管权限与账本。

```python
from medical_qa_data_factory.evaluation.kdd_routes import (
    ObservationReceiptEnvironment, run_solver,
)

# public_task 为 RouteTask 或严格公共 JSON，manifest 为真实冻结环境声明。
# host_start(public_task) 返回 query-planner.external.v2 请求。
# host_execute(action) 先持久化/执行/采用真实回执，再返回下述 envelope。
environment = ObservationReceiptEnvironment(
    manifest, start=host_start, execute=host_execute,
)
run = run_solver(public_task, environment, radar_policy.decide)
```

`host_execute` 返回 `{receipt, observation}`，两项均为 JSON 对象。
完整可执行的公共任务、初始请求、所选动作和执行后对象见
[`kdd-host-callback.example.json`](../schemas/v1/kdd-host-callback.example.json)。
这是合成契约样例；真实回调沿用相同形状，填入实际来源、能力、费用和固定产物回执。
query-planner 侧完整 v2 请求另见其 `contracts/radar-policy-v2.json` 中 `example`。
`manifest.source_versions` 必须保留真实来源固定版本；版本或题目、方向、时间截点及原始预算
不一致会拒绝。回执 action ID/kind/arguments 必须与选择一致，宿主采用最新回执后才允许下一步。
未知结果不重发；未知实际用量保持 `null`，不补零。`run_solver` 不吞掉宿主异常再伪造成功回执。

独立求解器通过可执行插件入口运行，环境模块只须实现
`create_environment(public_task) -> ObservationReceiptEnvironment`：

```bash
python -m medical_qa_data_factory eval routes solve   --tasks RELEASE/public/kdd_route_tasks.jsonl   --policy-module /ABS/query-planner/radar_policy.py   --environment-module /ABS/radar_route_environment.py   --out fresh-runs.jsonl

python -m medical_qa_data_factory eval routes evaluate   --runs fresh-runs.jsonl --targets RELEASE/hidden/kdd_route_targets.jsonl   --out post-solve-assessment.jsonl
```

`solve` 没有隐藏目标参数，只把公共问题与宿主观测交给策略。各动作留下当前候选空间和观测
hash、真实回执、策略内容 hash、方法版本及训练状态，输出
`trajectory_origin=independent_solver_rollout`。`generation_exploration` 不能进入评测。
`SyntheticReceiptWorld` 可以按实际选择走不同测试分支，且强制保持 `synthetic_fixture`；
它不是 Fabric/WeKnora/专业 HTTP 服务已接通的证明。

### 独立目标和比较

真实任务没有独立审核时用 `review_status=unreviewed`，评测输出覆盖和正确停止为 `null`、
状态 `unreviewed_no_gold_score`，仍记录动作、失败/未知回执和实际用量。有独立审核时必须
给出 reviewer、review_basis_refs 和信息目标的可接受证据/固定产物集合。
合成预期用 `fixture_expected`；不能转为真实任务 gold。检查覆盖、科学有效性、业务价值分别处理。

同空间对照使用 query-planner 的 `coverage_cost_baseline_v1`；它是单独命名的新对照，
不代表 Radar 原 baseline。`eval routes solve --selector coverage_cost_baseline_v1 ...`
先生成另一条独立轨迹，再用 `eval routes compare --left A --right B --targets H --out C`
比较。公共题目 hash、环境版本/支持动作和初始观测必须一致；动作空间改变则拒绝直接比较。
不同真实/合成属性的结果不合并成一个效果数字。

### 本轮验证记录

新增 11 个测试通过，覆盖四方向独立分支、公共/隐藏隔离、未知回执、环境版本和可比性。
实际 query-planner 的 `information_gain_v1` 与同空间对照均经过完整 CLI 独立运行；
测试故意设置不同信息贡献的分支，用于验证策略选择和回执更新，不用于估计真实业务收益。
产物在 zktest 的
`/home/zk_test/zk_projects/.bioradar-kdd-integration/data-factory/data/runs/kdd-routing-contract-20261030/`：
`cli-information-gain-runs.jsonl`、`cli-coverage-cost-runs.jsonl`、`cli-comparisons.jsonl`，
另有公开/隐藏 release 和可复跑的 `synthetic_environment.py`。以上真实运行使用合成输入，
不会作为真实业务或训练成绩。

全套检查为 1160 通过、5 失败、2 跳过、13 默认排除。5 项已在修改前提交 `84bc9aa`
逐项复现：旧测试仍要求公开 manifest 暴露 writer provenance；缺未跟踪的历史 questions
资料；3 项缺 PowerShell。此次没有为得到全绿而把失败改为跳过。

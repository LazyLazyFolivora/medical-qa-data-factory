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

## 直接评测 Radar 已保存的真实 v2 轨迹

`eval routes import-radar` 只读 Radar 的 `AnalysisView` JSON。它不发送新动作、不调用
solver、不把保存的轨迹改名为新的独立求解；产物明确使用
`trajectory_origin=persisted_radar_host_trace` 和 `executed_new_actions=0`。
原始题目、决策、回执保持原样，输出记录本次读取的文件字节 hash、视图 hash 和独立原生
checker 的内容 hash。源码入口是 `evaluation/radar_routes.py:evaluate_radar_view`。

```bash
python -m medical_qa_data_factory eval routes import-radar \
  --views /ABS/radar-analysis-actual-id.json \
  --provenance real_task \
  --trace-validator /ABS/BioRadar/scripts/query_policy_assertions.py \
  --out /ABS/factory-real-route-assessment.jsonl
```

Radar 导出最小原生字段为：`id / investigation_id / is_demo / input_hash / question`；
完整 `query_policy`（方向、固定时间、方法、checks/decisions/budget，以及
artifacts/artifact_results/execution）；实际 `queries / evidence / calls / budget`，和存在时的
`result.query_policy`。可直接导出已脱敏 AnalysisView，不需要构造工厂 before/after。
真实来源版本来自保存的 source artifact 和 evidence snapshot；不读取运行凭据文件。

原生 checker 独立核对逐步回执、查询与固定产物、专业调用和总账。工厂再次核对可见观察
检查的支持引用和实际模型用量；若保存了 `decision.policy_request`，还核对当时合法候选及
完整已采用回执。只有真实选择当时保存的 request 才能使用，禁止事后补造候选状态。
当前只有观察摘要的旧记录会保留 `saved_requests.status=not_recorded/partial`，不能评估
决策重放和未选动作收益。计算 run、生成探索轨迹与 queued 的零决策 analysis 均不算
可评价路由，返回 `not_evaluable`。仍在执行的记录按 partial 输入检查，不假装已经停止。

报告分开给出：宿主声明的 required checks 覆盖、动作种类/数量/回执状态、动作与准备
调用数、模型真实/未知用量、实际停止原因和未知回执数。宿主检查覆盖不是独立 gold。
没有审核目标时 `independent_outcome` 为 `unreviewed_no_gold_score`，独立目标覆盖与
停止正确性均为 `null`；可用 `--targets` 在这个求解后的命令中提供既有独立审核目标，
绝不传给原生 checker 或策略。目标环境绑定为 `bioradar-analysis:<analysis id>` 与实际
`input_hash`。完整性失败会压住覆盖/结果评分，保留失败原因。

新增导入与原有路由的 27 项定向测试通过；另已实际加载 Radar 的独立原生 checker 验证
原生 v2 形状兼容。测试资料仍是合成契约资料；真实轨迹的结果另存到本次整栈验收目录。

### 首批真实路由固定快照（2026-10-30）

已用上面的 `import-radar --provenance real_task` 命令实际核验两条 Radar 调查：

| 方向 / analysis ID | 已保存动作与回执 | 宿主必要观察检查 | 实际费用 | 路由停止 |
| --- | --- | --- | --- | --- |
| research / `e74dc3c1-b334-43b2-b07f-885876b5506e` | 3 次资料搜索、1 次数据准备、1 次分析；5 条回执 | 4/8 | 动作工具 5 + 准备工具 10；模型 3 次、104672 token | `insufficient / no_eligible_action` |
| public_health / `7af5f453-71ca-47cc-980c-6c2814e76233` | 2 次资料搜索、1 次数据准备、1 次分析；4 条回执 | 4/8 | 动作工具 7 + 准备工具 8；模型 5 次、179379 token | `insufficient / no_eligible_action` |

费用与原生回执及总账一致。公卫的一项取证工具 token 用量仍未知，`action_tokens=null`，
不以模型总量替代。两条都有完整的实际 `policy_request`，逐步历史与合法候选核验通过；
用实际内容 hash 对应的 query-planner 重放，科研 6 步、公卫 5 步的选择均一致。
同候选快照上的 `coverage_cost_baseline_v1` 在公卫有 3 处选择差异，科研没有差异；
替代动作没有执行，因此收益差异与反事实回执仍为 `null`，不算新的独立 solver 运行。

上述固定快照中，科研 analysis 为 `blocked / method_input_invalid`，公卫为 `needs_review`；
路由检查通过不等于报告或科学/业务结论通过。两条独立 gold 目标均未审核，独立覆盖与
停止正确性保持 `null`。原始失败也保留：公卫 Fabric 搜索的 `history_source_invalid` 和
WeKnora 的 `weknora_candidates_not_bound`。管线、商业的 mining 运行不能替代尚未导出的路由轨迹。

可复核原件与报告位于 zktest 的
`/tmp/bioradar-kdd-20261030/independent-evaluation/`：
`factory-inputs/` 按实际读取字节 hash 保存原生 view；
`factory-routing-first-two-fixed-20261030.jsonl` 保留最初两条评测；
`factory-routing-public-health-final-20261030.jsonl` 记录公卫模型回执收齐后的评测；
`factory-routing-same-candidate-audit-first-two.json` 保存精确版本的决策重放和对照差异；
`factory-routing-real-progress.json` 列出各方向实际覆盖范围。旧快照和旧报告不会被新回执覆盖。


## 分层实验汇总与历史回放（2026-10-31）

`radar-route-import-1.1` 同时只读导入原生 v1 和 v2，仍调用同一个独立 native strict checker。
v1 保存原 schema/策略版本和能力差异，不伪装新 v2；没有请求就不能重放、没有回执就不能补账。
新增 `document_read/source_fetch` 可由已有 ObservationReceiptEnvironment 消费真实回执；
工厂不执行外部工具。导入保留未知 coverage、未读材料、动作配额计数、模型 stage 及准备/路由/报告专业调用分项。

`provenance=historical_replay` 表示真实公开历史材料的隔离回放，必须 `AnalysisView.is_demo=false`；
测试环境隔离通过独立 DB/KB 和 environment/mode 表示。A/B/C 的人工消息保持 synthetic_fixture/is_demo=true。
历史回放环境必须声明实际 source_versions，不能用合成分支世界产生历史结果。
该标记本身不证明过去可见性；公开时间、原文版本、存档证明与日期精度另行独立审核。

新增 `eval routes summarize-experiments`，输入封存实验协议、已有 import-radar JSONL，
以及可选的独立事实审查 JSONL。它只在求解之后读取审查；solve 的接口与隐藏目标隔离保持原样。
协议、事实审查正式 schema 和无答案示例位于 `schemas/v1/kdd-route-experiments*` 与
`schemas/v1/kdd-route-fact-review.schema.json`。

```bash
python -m medical_qa_data_factory eval routes import-radar \
  --views /ABS/frozen-analysis.json --provenance historical_replay \
  --trace-validator /ABS/BioRadar/scripts/query_policy_assertions.py \
  --out /ABS/fresh-historical-import.jsonl

python -m medical_qa_data_factory eval routes summarize-experiments \
  --protocol /ABS/sealed-experiments.json --imports /ABS/fresh-imports.jsonl \
  --reviews /ABS/post-run-independent-reviews.jsonl --out /ABS/fresh-experiment-summary.json
```

每个 arm 绑定真实 analysis ID、策略版本，并声明输入/来源范围/模型/提示/预算/host 版本和动作空间。
这些配置 hash 是封存协议的声明，工厂逐项展示差异、核对实际策略版本，不把声明等同于已验证的环境一致性。
新 v2 能力 vs 冻结旧 baseline 是能力/配置对照，不能把结果差异归因为纯策略优越。
原 `compare` 仍只允许同冻结任务与相同动作空间的独立 solver 对照。

结果分开给出：receipt/schema 完整性、原生报告的实际 BioCheck status、绑定精确 view hash 的
独立事实判断。无独立审查、未完成、缺必需判据或历史可见证明时，acceptance_passed 为 null。
检查覆盖和方法运行完成不会转成独立事实正确。全项接受同时需要终止路由完整、原生 PASS、
独立必需事实判据全过；历史任务还要时点证明已审和各判断终点已评。
公卫 D 的聚集风险/传播风险为独立终点；记录审查后的观测日期与精度，不拿官方通报日替代算法目标。
假传言和往季对照是单独 experiment row，使用相同 experiment_group 并标 control_role，
不能只保留命中目标的正例。输出未知用量保持 null，并显示已知 token 下界和缺评估的任务数。

本轮新增测试仅证明协议、回执和评价边界，不包含新的真实业务成绩或训练收益。


2026-10-31 定向验证：factory 路由/原生导入/实验分层 39 项通过；planner v2 21 项与原 v1 16 项通过。
另通过实际 `radar_policy.decide_kdd` + `run_solver` 在四方向各运行两种同空间 selector，
每条走 document_read → source_fetch → complete，并消费各自合成环境的完整回执；
8 份独立契约运行位于 `data/runs/kdd-report-contract-20261031/independent-solver-contract-runs.jsonl`。
该目录被 Git 忽略，属于 synthetic_fixture 接口回归，0 HTTP/模型调用，不是服务已联通或业务效果证据；
独立事实目标保持 unreviewed，未拿合成答案计算成绩。冻结旧 baseline 的能力对照仍须真实分别执行。


`acceptance_passed` 只表示报告完整性、BioCheck 和冻结事实判据的组合，不表示 D 的两个风险假设
都得到支持。每个 arm 的 `business_endpoint_result_summary` 单列各判断终点
supported / unsupported / not_evaluable / unreviewed；总字段 `acceptance_scope` 明确该边界。
正确保留“某终点未获支持”的报告仍可能通过事实审查，假传言对照尤其如此；
禁止把总 acceptance 计数当作 D 两终点命中率或预警有效性分数。

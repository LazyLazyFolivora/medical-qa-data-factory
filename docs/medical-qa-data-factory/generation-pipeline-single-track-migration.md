# Web 生成链路单轨收敛设计

> 状态：迁移设计  
> 代码基线：2026-10-31  
> 范围：Vue 生成工作台及其 JSON API  
> 目标：Web 端只保留一条活动生成链路，旧链路只作为只读历史归档

## 1. 结论

当前问题不是单纯的前端组件太多，而是后端仍同时暴露两套写入口、两套存储、
两套状态机和两套详情协议，前端又把它们投影到了同一个工作台里。

Vue 现在实际只通过 `POST /api/objective/generate` 创建新任务，但后端仍然保留：

- `POST /api/generate`：旧固定工作流入口；
- `POST /api/objective/generate`：新的 objective 主 Agent 入口；
- `GET /api/generation/{task_id}`：旧链路进度；
- `GET /api/objective/{run_id}/summary` 等：新链路进度；
- `tasks/`、`episodes/`、`bundles/`：旧链路数据；
- `objective_runs/`、`objective_releases/`：新链路数据。

`GET /api/generation-workspace` 又把两类数据合并到同一个 `tasks` 数组。结果是：
用户看到一个“任务列表”，实际却包含两种运行模型；前端必须根据是否带
`engine=objective` 猜测应该加载哪套组件、轮询哪个接口、如何解释状态。

建议明确收敛为：

```text
活动生成链路：Objective 主 Agent
历史旧任务：独立只读归档
```

不要把旧任务“转换成” objective 任务，也不要把旧状态强行映射成新状态。两套链路
的数据语义、发布门禁和可恢复能力不同，伪装成同一种任务会继续制造审计歧义。

## 2. 当前实际架构

### 2.1 两条链路的对照

| 维度 | 旧固定工作流 | 新 objective 链路 |
| --- | --- | --- |
| Web 写入口 | `POST /api/generate` | `POST /api/objective/generate` |
| 核心服务 | `EntityGenerationService` | `ObjectiveQuestionAgentLoop` |
| 编排方式 | 固定 LangGraph 节点，内层 ReAct | 主 Agent observe-decide-dispatch-commit |
| 主存储 | `tasks/`、`episodes/`、`bundles/` | `objective_runs/<run_id>/` |
| 发布存储 | 数据集题目与 bundle | `objective_releases/<run_id>/` |
| 活动状态 | `TaskStatus` 与 `EpisodeState` | `RunState` 与 `HypothesisState` |
| 进度接口 | `/api/generation/{task_id}` | `/api/objective/{run_id}/summary` |
| 结果接口 | `/api/generation-workspace` 中的旧字段 | `/api/objective/{run_id}/workbench` |
| 前端运行组件 | `TaskRun.vue` | `ObjectiveRun.vue` |
| 前端轮询 | `pollProgress()` | `pollObjectiveSummary()` |

旧链路定义见
[domain/tasks.py](../../src/medical_qa_data_factory/domain/tasks.py#L25)、
[workflow.py](../../src/medical_qa_data_factory/workflow.py#L17)、
[generation/service.py](../../src/medical_qa_data_factory/generation/service.py#L223) 和
[generation/repository.py](../../src/medical_qa_data_factory/generation/repository.py#L97)。

新链路定义见
[objective_question_contracts.py](../../src/medical_qa_data_factory/generation/objective_question_contracts.py#L127)、
[objective_question_runtime.py](../../src/medical_qa_data_factory/generation/objective_question_runtime.py#L92)、
[agent_loop.py](../../src/medical_qa_data_factory/generation/agent_loop.py#L103) 和
[objective_question_release.py](../../src/medical_qa_data_factory/generation/objective_question_release.py)。

### 2.2 两套写入口仍然同时可用

路由注册位置见
[web/app.py](../../src/medical_qa_data_factory/web/app.py#L2492)：

```text
POST /api/generate             -> start_generation
POST /api/objective/generate   -> start_objective_generation
```

旧入口仍会创建任务、启动旧图并写入旧存储，见
[web/app.py](../../src/medical_qa_data_factory/web/app.py#L1850)。
新入口会创建 objective runtime 并启动主 Agent，见
[web/app.py](../../src/medical_qa_data_factory/web/app.py#L1981)。

两个入口共用同一个进程内 `generation_lock`，见
[web/app.py](../../src/medical_qa_data_factory/web/app.py#L1454)、
[web/app.py](../../src/medical_qa_data_factory/web/app.py#L1943) 和
[web/app.py](../../src/medical_qa_data_factory/web/app.py#L2166)。因此两套任务既
不能并行运行，又没有统一的任务服务，形成“互相排斥但彼此不认识”的状态。

### 2.3 同一个 ID 命名空间

两个入口都使用同一类 ID 校验：

```text
^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$
```

旧入口默认生成 `web-generation-*`，新入口默认生成 `objective-web-*`，
当前 Vue 又会主动传入 `web-<uuid>`。但前缀没有任何强制含义，两个入口都接受
任意符合正则的 ID。

所以不能根据 `web-`、`objective-web-` 或 ID 形状判断链路。实际实例
`web-15f12fff-bb38-4fad-9008-09403edde43e` 使用 `web-` 前缀，却属于旧链路，
就是直接反例。

### 2.4 工作区接口混合两类任务

`generation_workspace()` 从 `objective_runs/` 扫描 objective 任务，再从
`build_entity_workspaces()` 取得旧任务，最后合并：

```python
"tasks": [
    *objective_tasks,
    *legacy_tasks,
]
```

相关实现见
[web/app.py](../../src/medical_qa_data_factory/web/app.py#L1532) 和
[web/app.py](../../src/medical_qa_data_factory/web/app.py#L1750)。

这个合并还有两个不对称之处：

1. objective 任务来自全部 `objective_runs/` 目录，不按当前实体过滤；
2. 旧任务只来自当前所选实体的 `EntityWorkspace.tasks`。

因此同一个下拉框里，objective 项和旧任务项的可见范围、排序依据和筛选条件都
不一致。没有 `task` 查询参数时，接口还可能优先选中当前实体的旧任务，而 objective
任务列表仍在旁边展示。

### 2.5 同一个响应里混有不同的数据结构

两类任务都使用 `task_id`，但公共字段并不一致：

- objective 任务明确返回 `engine: "objective"`；
- 旧任务行和旧 `selected_task` 没有 `engine`；
- objective 题目的 `answer` 是数组；
- 旧题目的 `answer` 是字符串；
- objective 状态还有 `run_state`、`commit_sequence`、`seconds_since_progress`；
- 旧状态依赖 `TaskStatus`、`EpisodeState` 和 `operations/planner_steps`。

前端于是把“没有 `engine`”解释成旧任务，把 `engine === "objective"` 解释成新任务。
这不是显式契约，而是隐式推断。

### 2.6 前端同时维护两套分支

`App.vue` 当前同时导入：

```text
ObjectiveRun.vue
TaskRun.vue
ObjectiveStatus.vue
```

见 [App.vue](../../frontend/src/App.vue#L11)。

关键分叉包括：

- `objectiveTask` 根据 `selectedTask.engine === "objective"` 判断；
- `selectTask()` 先请求混合工作区，再决定是否加载 objective workbench；
- 模板根据 engine 在 `ObjectiveRun` 与 `TaskRun` 之间切换；
- `pollProgress()` 和 `pollObjectiveSummary()` 同时保留；
- `mergeTaskSummary()` 强制把摘要标成 `engine: "objective"`。

对应位置见
[App.vue](../../frontend/src/App.vue#L48)、
[App.vue](../../frontend/src/App.vue#L123)、
[App.vue](../../frontend/src/App.vue#L241)、
[App.vue](../../frontend/src/App.vue#L259)、
[App.vue](../../frontend/src/App.vue#L296) 和
[App.vue](../../frontend/src/App.vue#L611)。

### 2.7 不存在的 objective 运行被伪装成 pending

这是当前“没有反馈”问题中的一个确定性缺陷。

`pollObjectiveSummary()` 先请求：

```text
GET /api/objective/{run_id}/summary
```

如果 summary 返回 404 或 405，`fetchObjectiveStatus()` 会回退到：

```text
GET /api/objective/{run_id}
```

前端把第二个响应的来源标记为 `legacy`，见
[App.vue](../../frontend/src/App.vue#L147)。但 `/api/objective/{run_id}` 并不是
旧链路接口；它是 objective 详细轨迹接口。更严重的是，当运行目录不存在时，它会
返回 HTTP 200：

```json
{
  "engine": "objective",
  "status": "pending",
  "status_label": "准备启动主 Agent",
  "run_state": "CREATED",
  "published_count": 0
}
```

实现见 [web/app.py](../../src/medical_qa_data_factory/web/app.py#L2208)。

结果是一个从未成功创建的任务也会被前端持续显示为“准备启动”，而不是“创建失败”
或“不存在”。这会让轮询、错误提示和用户判断同时失真。

### 2.8 同步 POST 与前置轮询形成隐含竞态

当前 Vue 在发送 `POST /api/objective/generate` 前就开始轮询，因为运行目录要等
POST 开始执行后才出现。`startGeneration()` 的顺序见
[App.vue](../../frontend/src/App.vue#L399)。

与此同时，POST 长连接会一直持有到整次生成结束。虽然生成线程在线程池里执行，
轮询仍可访问，但“创建请求”和“执行请求”没有分离：

- 创建成功没有明确的提交点；
- 创建失败与执行失败都表现为同一个 POST 错误；
- 前端只能靠“暂时查不到”猜测任务是否正在创建；
- 进程/客户端超时后，前端无法仅凭 HTTP 响应判断运行是否已经创建。

### 2.9 现有文档已经出现双轨描述

[generation-frontend-vue.md](./generation-frontend-vue.md) 仍把
`TaskRun.vue` 和 `/api/generation/{task_id}` 写成当前主流程，并在同一文档中又列出
objective 接口。当前代码实际是“Vue 新写入 objective，但仍兼容旧读取和旧组件”，
文档没有把这条边界说清。

这也是需要单轨迁移的原因之一：否则后续修改会继续落在错误的协议或组件上。

## 3. 问题分级

### P0：两类任务共用身份和工作区

同一个 `web-*` 外观、同一个 `tasks` 数组、同一个 `selected_task` 字段，却没有一个
后端强制的 `engine`。调用方必须读取存储来源或猜测字段，任务身份不唯一。

### P0：创建失败可能被显示成永久 pending

summary 404 回退到详情接口后得到 200 伪状态。没有运行文件也不再是明确的错误。

### P0：前端仍有两套活动运行协议

旧轮询、新轮询、旧组件、新组件同时存在于活动页面。任何一次状态字段或错误处理
调整都容易只改到其中一套。

### P1：两个写入口继续产生新历史包袱

即使 Vue 不用 `/api/generate`，外部调用、测试或脚本仍可继续写入旧链路，使“停止
双轨”无法真正完成。

### P1：任务可见范围不一致

objective 下拉项跨实体，旧任务下拉项跟随当前实体。用户无法从列表本身知道筛选
规则。

### P1：公共题目字段不统一

`answer` 的字符串/数组差异会迫使每个消费端自行兼容，且容易把数组直接渲染成
逗号拼接文本。

### P1：旧状态投影不完整

旧 `TaskStatus` 包含 `needs_input` 和 `cancelled`，但工作区状态标签没有覆盖这两个
值。旧 `partial` 在状态机中理论上可再次进入 running，但 Web 没有恢复接口；用户
无法判断“历史部分结果”是否还能继续。

### P2：全局锁把两套实现耦合成一个并发域

移除或替换任一链路时都必须同时考虑锁的生命周期。目标链路应拥有自己的活动运行
协调器，旧归档读取不应参与写锁。

## 4. 实例证据

以旧任务
`web-15f12fff-bb38-4fad-9008-09403edde43e` 为例：

| 检查项 | 实际结果 |
| --- | --- |
| 旧任务目录 | 存在 |
| objective run 目录 | 不存在 |
| release 目录 | 不存在 |
| 实体 | KIF15 |
| 目标题数 | 3 |
| 最终任务状态 | `partial` |
| episode 最终状态 | `partial` |
| 停止原因 | `no_candidate_proposed` |
| 已生成题目 | 0 |
| summary 接口 | 404 |
| objective 详情回退 | 可能返回 200 pending 占位 |
| 旧进度接口 | 可读取旧 episode 轨迹 |

数据位置：

```text
data/runs/objective-web/tasks/web-15f12fff-bb38-4fad-9008-09403edde43e/
data/runs/objective-web/episodes/episode.db3bafe377cb479d3a13a7e7/
```

这个实例说明：同名工作台下可能只有一个目录存在，而前端的两级回退会把“明确不
存在”和“正在创建”混为一谈。

审查时该工作区还有 31 个 `objective_runs/` 和 3 个旧 `tasks/` 目录。这个数量只作为
当前基线快照，不作为迁移后的固定数据量。

## 5. 目标架构

### 5.1 单一活动链路

Web 工作台的活动生成只保留 objective：

```text
POST /api/objective/generate
  -> 创建并持久化 objective run
  -> 启动 objective 主 Agent
  -> 所有状态写入 append-only runtime commits
  -> 通过 objective 投影接口读取状态、轨迹和发布结果
```

旧固定工作流仍可暂时保留给 CLI 或历史脚本，但它不再是 Web 工作台的活动生成
选项，也不能通过 Web 创建新任务。

### 5.2 应用层分责

建议把当前 `web/app.py` 中的混合投影拆成三个明确边界：

```text
Vue
  |
  +-- WorkbenchBootstrapService
  |     实体、模型状态、风格列表，不返回任务混列表
  |
  +-- ObjectiveRunService
  |     创建、summary、workbench、trace、evidence
  |
  +-- LegacyRunArchive
        只读旧 tasks/episodes/bundles
```

三条边界不能共享同一个任务选择器，也不能让旧归档数据进入活动 objective 列表。

### 5.3 存储边界

```text
活动链路
objective_runs/<run_id>/
  objective_runtime/commits/
  materials/
  artifacts/

发布结果
objective_releases/<run_id>/
  public/questions.jsonl
  audit/

旧历史，只读
tasks/<task_id>/
episodes/<episode_id>/
bundles/<bundle_id>/
questions/<question_id>/
```

不得删除旧目录，也不得为了“统一外观”把旧记录复制或改写成 objective runtime。
如果后续确实要统一数据仓库，应单独做语义迁移和重新验证。

### 5.4 目标 API

| 接口 | 责任 |
| --- | --- |
| `GET /api/workbench/bootstrap` | 实体选择、模型状态、风格列表；不混入任务历史 |
| `GET /api/objective/runs?entity_id=...` | 只列 objective 运行 |
| `POST /api/objective/generate` | 创建并启动 objective 运行 |
| `GET /api/objective/{run_id}/summary` | 轻量实时状态 |
| `GET /api/objective/{run_id}/workbench` | 发布题、候选题、拒绝原因和统计 |
| `GET /api/objective/{run_id}/trace` | 主 Agent 决策和产物时间线 |
| `GET /api/questions/{question_id}/evidence` | 单题证据卷宗 |
| `GET /api/legacy/tasks?entity_id=...` | 只读旧任务列表 |
| `GET /api/legacy/tasks/{task_id}` | 只读旧任务详情 |

`/api/objective/{run_id}` 可以保留为 `/trace` 的兼容别名，但不能再承担“运行不存在
时返回 pending”的语义。不存在就是 404。

### 5.5 启动契约

目标契约建议为：

```http
POST /api/objective/generate
-> 202 Accepted
-> Location: /api/objective/{run_id}/summary

{
  "run_id": "web-...",
  "engine": "objective",
  "run_state": "CREATED",
  "created_at": "...",
  "summary_url": "/api/objective/web-.../summary"
}
```

端点必须先在持久存储中创建 run，再返回 202；随后由本地后台执行器启动主 Agent。
前端只有拿到 202 后才开始轮询，避免把“尚未创建”当成“正在创建”。

考虑到当前生成器是同步实现，第一阶段可以暂时保留同步执行，但必须把“启动失败”
和“运行后失败”分清。202/后台执行是最终接口方向；在完成持久化任务执行器前，不应
继续用“查不到就显示 pending”的方式掩盖竞态。

### 5.6 统一运行摘要

每个任务投影至少包含：

```json
{
  "run_id": "web-...",
  "engine": "objective",
  "run_state": "CANDIDATES_PROCESSING",
  "status": "running",
  "is_terminal": false,
  "can_resume": false,
  "current_action": {
    "action": "verify_answer",
    "action_label": "独立验证答案"
  },
  "created_at": "...",
  "updated_at": "...",
  "last_commit_at": "...",
  "target_question_count": 3,
  "published_count": 1,
  "candidate_count": 4,
  "rejected_count": 2,
  "stop_reason": null
}
```

约束：

- `run_state` 是持久化状态，是审计依据；
- `status` 是给 UI 的派生显示状态，不能反过来用于恢复或发布判断；
- `stalled` 只能是派生的观察结果，不能伪装成新的持久化 `run_state`；
- `engine` 必须由后端显式返回，不能靠 ID 前缀推断；
- 旧归档使用 `engine: "legacy"` 和历史原状态，不能映射成 objective 状态。

## 6. 状态语义

### 6.1 旧状态

旧 `TaskStatus`：

```text
pending
running
needs_input
completed
partial
failed
cancelled
```

这些状态属于旧固定工作流。它们不应进入当前 objective 的 `RunState` 枚举。

### 6.2 新状态

objective 的持久化状态：

```text
CREATED
MATERIALS_COLLECTED
CLAIMS_PROPOSED
HYPOTHESES_PROPOSED
CANDIDATES_PROCESSING
COMPLETED
COMPLETED_WITH_ZERO_QUESTIONS
PROVIDER_FAILED
RECOVERY_REQUIRED
VERSION_INCOMPATIBLE
CANCELLED
```

注意：

- `stalled` 是 UI 根据“最后 commit 时间超过阈值”派生的显示状态；
- `RECOVERY_REQUIRED` 是当前 runtime 的终态，不能原地继续；
- `COMPLETED_WITH_ZERO_QUESTIONS` 是正常终态，不是失败；
- `COMPLETED` 表示至少发布一道题，不等于达到目标题数上限。

### 6.3 旧任务在界面中的表达

| 旧状态 | 归档显示 | 行为 |
| --- | --- | --- |
| `pending` | 旧版准备态 | 只读 |
| `running` | 旧版运行中断 | 只读 |
| `needs_input` | 旧版待人工输入 | 不提供继续按钮 |
| `completed` | 旧版已完成 | 查看历史结果 |
| `partial` | 旧版部分结果 | 查看停止原因，不承诺可恢复 |
| `failed` | 旧版失败 | 查看历史错误 |
| `cancelled` | 旧版已取消 | 只读 |

只有明确验证过旧 episode checkpoint 可恢复时，才能显示“继续旧任务”。当前 Web
没有这个恢复协议，因此第一阶段统一按不可恢复处理。

## 7. 前端收敛方案

### 7.1 活动工作台

活动工作台只使用 objective：

```text
App.vue
  -> WorkbenchBootstrap
  -> ObjectiveRuns
  -> ObjectiveSummaryPoll
  -> ObjectiveRun
  -> ObjectiveEvidence
```

删除活动路径中的 `TaskRun`、`pollProgress`、旧 `selected_task` 分支和
`engine` 条件渲染。旧任务进入独立的“历史归档”入口。

### 7.2 状态来源只有一个

每次运行只允许一个轮询循环：

```text
POST 返回 202 和 run_id
  -> 每 900ms 请求 summary
  -> terminal 后停止轮询
  -> terminal 后按需请求 workbench 和 trace
  -> 用户查看题目时再请求 evidence
```

禁止在前端用“summary 失败就请求另一个任务接口”推断运行是否创建成功。

### 7.3 历史归档单独展示

历史归档是独立的只读视图：

- 可以按实体筛选旧任务；
- 明确标注“旧版固定工作流”；
- 展示原 `TaskStatus`、停止原因、episode、bundle 和错误；
- 不提供 objective 的候选、验证、发布流程标签；
- 不进入 objective 的任务下拉框；
- 不与活动运行比较发布进度。

### 7.4 URL

建议明确区分：

```text
/?run=web-...              活动 objective 运行
/?legacy_task=web-...      旧版历史任务
```

在兼容期可以继续接受 `?task=`，但必须根据后端返回的 `engine` 决定跳转，不能根据
ID 猜测。两个存储都命中同一个 ID 时，应返回冲突错误，而不是任选其一。

## 8. 迁移步骤

### 阶段 0：冻结和盘点

1. 统计旧任务、episode、bundle 和 objective run 数量。
2. 确认当前是否有旧任务仍在运行。
3. 保存现有工作区样例和接口响应，作为迁移前后对比基线。
4. 明确“旧 CLI 是否保留”是独立决策，不把删除旧核心代码混入本次 Web 收敛。

验收：

- 有旧任务清单和逐条状态；
- 知道哪些旧任务仍是 `running`；
- 数据目录没有删除或覆盖。

### 阶段 A：隔离但不删除

后端：

- 给旧任务投影显式增加 `engine: "legacy"`；
- objective 列表独立返回，不塞进旧任务数组；
- 修复 `/api/objective/{run_id}` 不存在时返回 404；
- 明确 `stalled` 为派生字段；
- 保留旧查询接口只读。

前端：

- 活动工作台只显示 objective；
- 旧任务移到独立归档入口；
- 删除 summary 404 到 objective 详情的隐式回退；
- 保留现有 `TaskRun` 仅用于归档展示；
- 所有任务行都显式显示引擎。

验收：

- 活动列表不出现 legacy；
- 旧归档不出现 objective；
- summary 404 不再显示为 pending；
- 选择一个 objective 运行只调用 objective 接口。

### 阶段 B：停止旧 Web 写入

后端：

- `POST /api/generate` 停止创建新任务；
- 可选返回 `410 Gone` 和明确错误码 `legacy_generation_retired`；
- 旧进度查询和旧详情继续保留一段时间；
- objective 自己管理运行锁，不再与旧写入共享活动锁。

前端：

- 删除旧活动提交逻辑；
- 删除活动页 `pollProgress()`；
- 删除活动页 `TaskRun` 分支；
- 归档页面仍可打开旧任务详情。

验收：

- 任何 Web 入口都只能调用 objective 创建接口；
- 外部调用旧写入口得到明确退役错误；
- 旧数据仍可读取，且没有被迁移或改写。

### 阶段 C：代码退役

在确认旧任务流量为零、历史数据已备份后：

- 删除混合 `generation_workspace()` 的任务合并逻辑；
- 删除旧活动状态映射和旧活动测试；
- 保留独立的 LegacyArchive reader；
- 若仍要保留 CLI 旧管线，移动到独立模块和独立文档；
- 不得删除 `tasks/`、`episodes/`、`bundles/` 历史数据。

验收：

- Web 代码中只有一个活动 `RunState` 和一个状态轮询入口；
- 旧数据仍能从归档页面读取；
- objective 运行不依赖旧 `GenerationTask` 对象。

## 9. 测试与验收

### 后端契约

- `POST /api/objective/generate` 成功后，运行目录在返回前已经存在；
- 运行不存在时 summary 和 trace 都返回 404；
- summary 不再返回 HTTP 200 的伪 pending；
- objective 运行列表不包含 legacy；
- legacy 归档可用且只读；
- 同一 ID 同时出现在两个存储时返回 409；
- `engine`、`run_state`、`status`、`is_terminal` 在每类响应中都明确存在；
- `RECOVERY_REQUIRED`、`COMPLETED_WITH_ZERO_QUESTIONS` 和 `stalled` 的显示判定
  分别有确定性测试。

### 前端行为

- 开始生成后只启动一个轮询器；
- POST 未成功时显示创建失败，不显示无限 pending；
- 运行结束后停止轮询；
- 选择历史 objective 运行不触发旧进度接口；
- 打开 legacy 任务不触发 objective summary/workbench；
- URL 刷新后按 `run_id` 和引擎恢复正确页面；
- 运行中切换任务仍受明确限制；
- 旧归档没有“继续”或“发布”动作。

### 数据安全

- 迁移前后旧目录文件数和哈希清单一致；
- 不对旧任务做原地改写；
- objective release 只从 objective runtime 读取；
- 日志和 UI 中不把旧 `partial` 误写成 objective 的 `RECOVERY_REQUIRED`。

## 10. 不在本次收敛范围内的问题

这份文档解决的是“两套链路如何从 Web 和 API 边界上收敛”，不自动修复 objective
出题质量、检索质量、模型重试、事实理解和发布门禁本身。objective 链路内部仍有
独立问题，例如：

- 模型预算尚未完整接入主循环；
- 某些子 Agent 失败仍可能升级为整次运行失败；
- 验证结果和候选题状态存在语义差异；
- 题面拒绝原因仍需要更完整的审计留痕；
- 难度和风格的确定性闭环仍需加强。

这些问题应继续依据
[objective-generation-pipeline-audit.md](./objective-generation-pipeline-audit.md)
单独修复，不能通过混合旧链路来掩盖。

## 11. 风险与待决策

### 风险

1. 旧 `TaskStatus.PARTIAL` 在状态机上允许重新进入 running，但 Web 没有恢复协议。
   迁移时不能把它宣传为可继续。
2. 当前代码和运行数据都可能有同名 ID。只按 URL 中的 ID 查询会产生歧义。
3. 修改后端代码不会自动影响已经启动、仍在内存中的旧进程；验收时必须先确认
   运行中的服务版本。
4. 现有测试和文档依赖 `/api/generate` 与混合 `tasks` 字段；停止旧写入口前要
   更新测试和所有调用方。
5. 旧管线可能仍被 CLI 或脚本使用。Web 退役不等于核心代码可以立即删除。
6. 当前 objective POST 是同步长请求。改成 202 需要明确后台执行器、进程重启和
   `RECOVERY_REQUIRED` 的关系。

### 需要确认的产品决策

1. 旧任务是否需要在 Vue 中提供完整归档，还是只提供下载/目录说明。
2. `/api/generate` 退役后立即返回 `410`，还是通过配置开关灰度关闭。
3. Web 是否保留旧 CLI 管线作为独立能力；如果保留，应使用独立命令、目录和状态，
   不能再回到 Web 混合列表。
4. objective 生成暂时保持同步 POST，还是本次一并改成持久化 202 后台任务。
5. 题目统一字段是否采用 `answer_values`（数组）作为唯一机器字段，避免 `answer`
   在不同链路中表示不同形状。

## 12. 建议的最终验收口径

当以下条件全部满足，才算完成单轨迁移：

```text
一个活动写入口        POST /api/objective/generate
一个活动状态来源      objective RunState + committed summary
一个活动轮询接口      /api/objective/{run_id}/summary
一个活动结果组件      ObjectiveRun
一个证据读取入口      /api/questions/{question_id}/evidence
旧数据独立只读        /api/legacy/tasks/*
```

用户在工作台看到的每个任务都必须先回答：

```text
它属于哪个引擎？
它就是数据库中的哪一条 run/task？
它的权威状态是什么？
它是否已结束？
它是否可恢复？
它为什么停止？
```

如果这些问题仍需要前端通过 ID 前缀、字段缺失或“查询不到就猜测 pending”来回答，
就不能认为两套链路已经收敛。

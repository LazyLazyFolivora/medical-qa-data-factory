# 生成题目前端（Vue）设计与操作说明

## 1. 范围

本文描述“生成题目”页面的 Vue 前端实现。范围只覆盖：

- 选择 PrimeKG 种子实体；
- 配置题数、难度、提问风格和生成引擎；
- 启动一次生成任务；
- 查看当前任务的实时进度；
- 查看当前任务产出的题目；
- 查看同一实体的历史任务；
- 查看 Agent 编排、工具查询和失败位置。

题目库、审核、数据集导出等页面暂继续使用原有 Jinja 页面。生成题目页不再使用
`generation.html` 中的服务端结果拼装逻辑，而是由 Vue 调用 JSON API 渲染。

## 2. 用户目标与操作路径

正常用户只需要理解两个阶段：

1. **创建一次任务**：选择一个实体，设置生成范围，然后开始生成。
2. **检查这次任务**：查看进度、结果或失败原因；需要排障时再展开技术轨迹。

页面按以下顺序呈现：

```text
选择实体
   ↓
配置题数 / 难度 / 风格 / 引擎
   ↓
开始生成
   ↓
实时进度与当前步骤
   ↓
当前任务结果
   ↓
按需展开 Agent 与工具执行轨迹
```

页面不会把“新建配置”和“某次历史任务的题目”混在同一结果区域。

## 3. 单一任务身份

生成页的唯一结果身份是 `task_id`。

- 新建任务在浏览器侧生成 `run_id`，并作为后端 `task_id` 使用。
- 任务完成后页面重新拉取 `/api/generation-workspace?task=<task_id>`。
- URL 更新为 `/?entity=<entity>&task=<task_id>`。
- 刷新页面时，后端优先使用 URL 中的 `task`，不会重新猜测用户想看哪次任务。
- 同一实体的历史任务通过下拉框显式切换。
- 题目、停止原因、执行步骤和 Agent 决策都只来自当前 `task_id`。

这条约束修复了此前“最新执行轨迹”和“实体所有历史题目”被拼在一起的问题。

## 4. 页面结构

### 4.1 页头

页头解释产品用途，并用三步指示器表示完整路径：

1. 选择实体；
2. 运行生成；
3. 审查结果。

这里不展示历史统计，避免生成动作被管理指标淹没。

### 4.2 实体选择

实体选择由 `EntityPicker.vue` 实现：

- 展示当前已选实体的名称、类型和 canonical ID；
- 支持按 PrimeKG 名称或 ID 搜索；
- 原生下拉框始终展示数据库返回的前 10 条实体；
- 输入搜索词后，数据库结果会实时替换下拉候选；
- 从下拉框选择实体后立即切换工作区；
- 搜索中的 AbortController 会取消过时请求；
- 生成运行期间禁止切换实体。

生成入口有三处：

1. 顶部主导航的“生成”；
2. 种子实体列表每一行的“用此实体生成题目”；
3. 实体详情页右上角的“用此实体生成题目”。

入口 URL 通过 `?entity=<canonical_id>` 预选实体，Vue 打开页面后自动加载该实体和
最近一次任务。

### 4.3 生成配置

生成配置由 `GenerationComposer.vue` 实现：

- 目标题数：`1-20`，明确标注只是上限；
- 难度：可多选；
- 提问风格：来自后端 Question Style Skills；
- 生成引擎：Agent 或规则基线；
- 底部固定一个主操作：“开始生成”。

模型未就绪时，Agent 选项禁用并显示具体不可用原因。

### 4.4 当前任务

任务区域由 `TaskRun.vue` 实现。标题始终显示：

- 当前任务状态；
- 完整 `task_id`；
- 已生成题数 / 上限；
- 停止原因；
- 历史任务切换；
- 完整任务记录入口。

结果区域按状态渲染：

- `pending` / `running`：等待或生成中的占位状态；
- `completed`：题目卡片；
- `partial`：提示未达到目标，并显示首个失败步骤；
- `failed`：直接显示失败节点、错误代码和原始错误。

每道题包含题面、答案、解释、难度、证据来源、提问风格和验证等级。

### 4.5 技术轨迹

技术轨迹由 `ExecutionTrace.vue` 实现，并放在可展开区域中：

- 正常运行默认收起；
- `partial` 或 `failed` 默认展开；
- “系统执行”展示流水线节点、耗时、工具调用和返回数量；
- “Agent 编排”展示每轮动作、规划意图、参数、策略结果和证据增量；
- 工具查询参数和原始错误按需展开。

这样普通用户先看到结果；排障用户再进入技术细节。

## 5. 前端状态机

```text
loading
  ├─ API 失败 → fatal-error
  └─ API 成功 → ready
                 ├─ 选择或搜索实体 → ready（切换 selected entity）
                 ├─ 切换历史任务 → ready（切换 selected task）
                 ├─ 开始生成 → running
                 │              ├─ 任务完成 → completed
                 │              ├─ 部分完成 → partial
                 │              └─ 失败 → failed
                 └─ 修改配置 → ready
```

`running` 期间：

- 实体与配置控件禁用；
- 每 `800ms` 调用 `/api/generation/<task_id>`；
- 实时更新当前步骤、操作列表和 Agent 决策；
- 后端生成请求结束后重新读取任务工作区，避免只依赖 POST 返回值。

## 6. JSON API

### 6.1 `GET /api/generation-workspace`

查询参数：

| 参数 | 含义 |
| --- | --- |
| `entity` | 选择指定实体 |
| `task` | 选择指定任务；优先级高于 `entity` |

主要响应：

```json
{
  "entities": [],
  "selected_entity_id": "primekg:drug:1191",
  "tasks": [],
  "selected_task": {
    "task_id": "web-...",
    "status": "partial",
    "status_label": "部分完成",
    "target_question_count": 3,
    "question_count": 0,
    "stop_reason": "model_error",
    "detail_url": "/tasks/web-...",
    "operations": [],
    "planner_steps": []
  },
  "questions": [],
  "model": {
    "ready": true,
    "detail": "模型已配置",
    "default_mode": "structured"
  },
  "question_styles": [],
  "entity_error": null
}
```

身份规则：

- 存在 `task` 时，从该任务的首个种子实体确定工作区；
- 不存在 `task` 时，选择该实体最新的任务；
- 请求不存在的 `task` 返回 `404`；
- `questions` 只包含当前 `selected_task` 的题目。

### 6.2 `GET /api/entities?q=<query>&limit=10`

用于 PrimeKG 实体搜索。Vue 端会把 `canonical_id` 归一化为内部 `value`。

### 6.3 `POST /api/generate`

创建并同步执行生成任务。Vue 在请求进行期间并行轮询：

```text
POST /api/generate
while pending/running:
  GET /api/generation/<task_id>
terminal:
  GET /api/generation-workspace?task=<task_id>
```

## 7. 组件边界

```text
frontend/src/App.vue
├── components/EntityPicker.vue
├── components/GenerationComposer.vue
└── components/TaskRun.vue
    └── components/ExecutionTrace.vue
```

- `App.vue`：API 加载、任务身份、URL 同步、生成提交与轮询。
- `EntityPicker.vue`：PrimeKG 搜索与实体切换。
- `GenerationComposer.vue`：生成参数与提交。
- `TaskRun.vue`：任务摘要、状态、结果与历史任务。
- `ExecutionTrace.vue`：技术流水线和 Agent 决策。

组件之间通过 props 和事件通信，不直接修改父级状态。

## 8. 视觉方向

生成页采用“研究工作台”而不是管理后台仪表盘：

- 单列主流程，避免配置、结果和技术轨迹同时争夺注意力；
- 低饱和灰白底、深青色操作强调；
- 编号步骤明确先后关系；
- 技术信息使用等宽字体和可展开容器；
- 失败使用局部红色诊断块，不做全页错误弹窗；
- 桌面和窄屏共用同一阅读顺序。

没有加入装饰性插画或复杂动效。该页面的核心价值是操作顺序、任务身份和可诊断性。

## 9. 构建与运行

首次安装：

```powershell
cd frontend
npm install
```

生产构建：

```powershell
cd frontend
npm run build
```

构建结果写入：

```text
src/medical_qa_data_factory/web/static/generation-app/
├── app.js
├── app.css
└── index.html
```

Python 服务通过现有 StaticFiles 托管这些文件：

```powershell
python -m medical_qa_data_factory web serve `
  --workspace data/runs/local-demo `
  --host 127.0.0.1 `
  --port 8765
```

前端开发模式：

```powershell
cd frontend
npm run dev
```

开发服务器默认端口为 `5173`。它只负责 Vue 页面，API 请求仍需由目标 Python
服务提供。Vite 已将 `/api`、`/tasks`、`/groups` 和 `/static` 代理到
`http://127.0.0.1:8765`；联调前先以该端口启动 Python 服务。

## 10. 验证

后端页面与 API 回归：

```powershell
$env:PYTHONPATH = "src"
python -m pytest tests/test_web.py -q
```

完整回归：

```powershell
$env:PYTHONPATH = "src"
python -m pytest -q
```

已覆盖：

- Vue 页面入口和构建资源；
- `.js` / `.css` 静态资源 MIME；
- 实体下拉默认展示前 10 条；
- 实体实时搜索及选择事件；
- “开始生成”提交的实体与配置参数；
- 最新任务选择；
- 指定历史任务选择；
- 未知任务 `404`；
- 同一实体多任务时题目不串任务；
- 执行轨迹和工具调用数据。

前端组件测试与生产构建：

```powershell
cd frontend
npm test
npm run build
```

## 11. 当前边界与后续

当前仅迁移生成题目页面，其他后台页面仍是原有 Jinja 页面。

后续可继续演进：

- 将 `POST /api/generate` 改为后台任务队列，前端只接收任务 ID；
- 迁移任务详情和审核页面到 Vue；
- 增加 SSE/WebSocket，替代轮询；
- 增加前端组件测试和浏览器端 E2E；
- 对 Agent 参数提供结构化查看器，替代原始 JSON。

# 运行手册

> 面向在本机复跑数据生产与验收的人。命令均为 Windows PowerShell。

## 环境

唯一受支持的环境是 Conda 环境 `qa_env`：

```powershell
conda activate qa_env
$env:PYTHONPATH='E:\Cursor\assumption\projects\medical-qa-data-factory\src'
```

`generate` 在落任何产物前会预检直接依赖（pydantic、langgraph、langgraph-checkpoint-sqlite）。
缺失或过旧会以 `environment_error`（退出码 3）失败，不写入任何文件。

## 数据位置

| 用途 | 路径 | 变量 |
|---|---|---|
| PrimeKG 数据库 | `E:\Cursor\assumption\datasets\PrimeKG\primekg.sqlite` | `MEDICAL_QA_PRIMEKG_PATH` |
| PrimeKG 快照 manifest | `<数据库名>.manifest.json`（与库同目录） | — |
| PrimeKG 实体搜索目录 | `data/cache/primekg-entities.sqlite` | `entity_catalog_path` |
| WeKnora 服务 | `http://localhost:8080` | 见 `config/literature.json` |
| 输出 | `data/runs/<name>/` | — |

PrimeKG 始终以 `mode=ro` 打开并设置 `PRAGMA query_only = ON`，不会被写入。

## 首次准备：固定 PrimeKG 快照

快照 manifest 记录数据库的 sha256，因此查询时不需要反复哈希 1.63 GiB 的文件。
**只做一次**：

```powershell
& 'D:\Anaconda\envs\qa_env\python.exe' -m medical_qa_data_factory kg snapshot init `
  --database $env:MEDICAL_QA_PRIMEKG_PATH
```

库更新之后需要重算：

```powershell
& 'D:\Anaconda\envs\qa_env\python.exe' -m medical_qa_data_factory kg snapshot refresh `
  --database $env:MEDICAL_QA_PRIMEKG_PATH
```

查询时若 size/mtime 与 manifest 不符，会标记 `snapshot_stale` 并要求显式刷新。

## 知识图谱诊断

```powershell
python -m medical_qa_data_factory kg providers
python -m medical_qa_data_factory kg health --provider primekg
python -m medical_qa_data_factory kg resolve --provider primekg --entity aspirin
python -m medical_qa_data_factory kg neighbors --provider primekg --entity aspirin --direction both --limit 20
```

`kg paths` 对 PrimeKG 返回 `kg_capability_error`：该遍历忽略关系/类型过滤，
也无法返回完整有序路径，因此 provider 的 `path_query` capability 声明为 `false`，
生成流程不会调用它。项目的多跳路径由 `knowledge_graph/frontier.py` 的有界扩展自行组装。

## 生成完整世界验收的问题

当前 task-first 正式入口：

```powershell
python -m medical_qa_data_factory generate validated `
  --output runs/validated-disease-v1 `
  --provider primekg `
  --entity-name "2-methylbutyryl-CoA dehydrogenase deficiency" `
  --entity-id 12392 `
  --entity-type disease `
  --page-size 500
```

该命令只发布通过完整冻结 PrimeKG 世界复算、答案基数、约束增益和题面语义回环的任务。输出目录必须不存在或为空。release 的 `public/` 只含 Planner 可见问题及其公开 manifest；`hidden/` 保存 evaluator 真值、验证报告、证据和生成 trace，二者不得作为同一训练输入分发。第一版只支持审定的 `constrained_entity` relation composition，因此某个实体生成 0 题是正常结果。

### CTD-backed validated profile

CTD formal profile 使用固定的 `CtdTaskRunSpec`，其中包含 chemical、public study
context、disease identity/category mapping、Step 4 fusion、provider failure 和
`ValidationEnvironment`：

```powershell
python -m medical_qa_data_factory generate validated `
  --profile ctd `
  --output data/runs/ctd-formal/release `
  --ctd-index data/cache/ctd-evidence.sqlite `
  --ctd-run-spec data/runs/ctd-formal/run-spec.json
```

该命令只在固定 CTD snapshot 内枚举全部疾病、为每个竞争者保存 outcome，并拒绝未知竞争者、
候选答案不一致、无价值 qualifier、shortcut 或语义漂移。它不会把 provider failure 或未评估
竞争者解释成“无答案”。

发布目录：

```text
public/
  planner_questions.jsonl
  dataset_manifest.json
hidden/
  evaluation_targets.jsonl
  validated_tasks.jsonl
  validation_reports.jsonl
  evidence_catalog.jsonl
  generation_episodes.jsonl
  rejected_candidates.jsonl
  generation_traces.jsonl
  quality_report.json
  dataset_manifest.json
```

当前真实 pilot 的阻塞项见
[CTD 正式接入实施状态](ctd-implementation-status.md)，不能用 fixture release 代替 20-seed
真实验收。

## 旧生成/审核兼容入口

下列 `generate entity` 仍用于历史探索、Web 审核和兼容测试；它尚未迁移到新的 `ValidatedTask` 发布契约，其 `PlannerTrajectory` 记录造题过程，不能作为 Query Planner 的正确动作训练标签：

```powershell
python -m medical_qa_data_factory generate entity `
  --workspace data/runs/aspirin-v1 `
  --task-id aspirin-v1 `
  --provider primekg `
  --entity-name "Aspirin" `
  --entity-id D001241 `
  --entity-type exposure `
  --question-count 3 `
  --model-mode rule-based
```

`--model-mode` 三种：

| 模式 | 说明 |
|---|---|
| `rule-based` | 确定性单跳基线，**不调用模型**，不是 ReAct agent |
| `fake` | 测试用脚本化模型替身，必须由调用方注入；运行正式 ReAct 探索主链 |
| `structured` | 从 provider registry 装配真实模型并运行正式 ReAct 探索；缺配置时 fail fast |

`fake` / `structured` 目前由 LLM Planner 决定逐轮 PrimeKG 邻居查询，后续问题只消费
本次探索落盘并策展后的证据。请求带显式 `GenerationSpec` 时，问题 Writer 与 Verifier
也走独立结构化角色；RAG 按需通过服务端 evidence need 调用，并在独立回读、文本 hash、
偏移和 snapshot 校验通过后才落盘正式文献证据。详细边界见
`docs/medical-qa-data-factory/current-implementation-gaps.md`。

### GenerationSpec

`GenerationSpec` 用 JSON 传入，和 `SafetyBudget` 分开：

```json
{
  "schema_version": "generation-spec-v1",
  "question_count": 2,
  "difficulty_targets": ["easy", "medium"],
  "allowed_answer_types": ["entity"],
  "allowed_sources": ["knowledge_graph", "literature"],
  "reasoning_constraints": {
    "min_hops": 1,
    "max_hops": 2
  },
  "evidence_constraints": {
    "min_total_evidence": 1,
    "min_kg_evidence": 1,
    "min_literature_evidence": 0,
    "literature_policy": "when_needed"
  },
  "language": "zh-CN"
}
```

```powershell
python -m medical_qa_data_factory generate entity `
  --workspace data/runs/aspirin-v2 `
  --task-id aspirin-v2 `
  --provider primekg `
  --entity-name "Aspirin" `
  --entity-id D001241 `
  --entity-type exposure `
  --question-count 2 `
  --model-mode structured `
  --generation-spec config/generation-spec.example.json
```

也可用 `--difficulty easy --difficulty medium`、`--literature-policy when_needed`
和 `--language zh-CN` 直接构造 spec。`--safety-budget <json>` 单独配置工具调用、
墙钟时间、观察边数和模型尝试上限。

返回结果新增：

- `planner_trajectory_id`；
- `qa_item_ids`；
- `target_question_count`；
- `target_difficulties_satisfied`。

工作区新增 `qa_items/`、`planner_steps/`、`planner_trajectories/`、
`planner_training_samples/`、`literature_evidence/` 和
`literature_support_assessments/`。

旧训练数据导出（只用于兼容研究，不是 Query Planner 正式 rollout）：

```powershell
python -m medical_qa_data_factory training export `
  --workspace data/runs/aspirin-v2 `
  --output data/exports/aspirin-v2-training
```

导出目录包含 `qa_items.jsonl`、`planner_training_samples.jsonl` 和
`manifest.json`。split 由 `split-group-hash-v1` 确定性分配，同一
`dataset_split_group_id` 不会跨 split；结构指纹冲突时导出失败。

恢复一个已有 episode（checkpoint 在 `<workspace>/checkpoints/generation.sqlite`，
`thread_id` 恒等于 `episode_id`）：

```powershell
python -m medical_qa_data_factory generate resume `
  --workspace data/runs/aspirin-v1 `
  --episode-id <episode-id> `
  --task-id aspirin-v1 `
  --provider primekg `
  --entity-name "Aspirin" `
  --entity-id D001241 `
  --entity-type exposure
```

退出码：`2` 参数或身份错误，`3` capability/config 错误，`4` provider 暂态错误，`1` 内部错误。

## 模型与文献诊断

```powershell
python -m medical_qa_data_factory model providers
python -m medical_qa_data_factory literature providers
python -m medical_qa_data_factory literature preflight
```

`literature preflight` 报告 provider 能否固定语料快照、能否独立回读分块。
这两项决定该 provider 能否产出正式 gold 文本证据。

配置只保存 API key 所在的环境变量名，不接受内联密钥：

```json
{
  "default_provider": "weknora",
  "providers": {
    "weknora": {
      "driver": "weknora",
      "base_url": "http://localhost:8080",
      "api_key_env": "WEKNORA_API_KEY",
      "knowledge_base_ids": ["kb-00000001"],
      "snapshot_id": null
    }
  }
}
```

## 审核工作台

```powershell
python -m medical_qa_data_factory web serve --workspace data/runs/aspirin-v1 --host 127.0.0.1 --port 8765
```

浏览器打开 `http://127.0.0.1:8765`。按 `Ctrl+C` 停止。

首页始终提供从 PrimeKG 读取的 10 个默认种子实体，工作区中已有的实体仍会保留在下拉框中。
搜索框覆盖 PrimeKG 全表，支持按实体名称和原生 ID 查询，最多返回 10 个结果。首次全库搜索会
从边的 `head`、`tail` 两端派生实体索引到 `data/cache/primekg-entities.sqlite`；该索引不修改
PrimeKG 原始数据库。之后只有快照变化时才会重建。

首页“生成引擎”提供 `Agent（LLM）` 和 `规则基线`。Agent 使用 structured ReAct 探索、
结构化 Writer 和独立 Verifier；规则基线不调用模型。复制
`config/models.example.json` 为 `config/models.json`，填入模型 endpoint 和模型名，并设置
其中 `api_key_env` 指向的环境变量。配置和密钥可用时 Agent 自动成为默认模式；否则选项禁用
并显示缺失原因，服务不会把规则结果伪装成 Agent 输出。

“提问风格”可选择普通用户搜索、患者/家属、医学学习者或三种混合。每个逻辑题生成 3 个
候选变体；Writer 从对应 Skill 的 `SKILL.md` 获取风格约束，Style Judge 在不知道答案的
情况下检查自然度、人设和意图保持，Answer Verifier 再使用完整冻结证据池复算唯一答案。
Skill 定义位于 `skills/question-styles/`，修改 Skill 后对应版本和 hash 会进入运行元数据。

生成提交后，浏览器会创建唯一 `run_id`，并通过
`GET /api/generation/{run_id}` 读取当前任务。执行侧栏会实时显示 pipeline 当前节点、完成
节点、耗时，以及 Agent 每轮的查询动作和新增证据数；生成完成后自动刷新为正式产物页面。

| 页面 | 路径 | 用途 |
|---|---|---|
| 问题生成 | `/` | 默认候选、全库搜索、按种子生成与浏览产物 |
| 运行记录 | `/runs` | 各次运行的状态 |
| 分组审核 | `/groups` | 同结构候选并列比较与处置 |
| 单题审核 | `/review/{question_id}` | 证据侧栏、题面编辑、确认/退回/拒绝 |
| 数据集 | `/dataset` | 导出阻断原因 |

分组审核页的处置命令经 `POST /api/groups/{review_group_key}` 落库，命令先入
ReviewOperation journal，再写记录，中断后可由 reconciliation 结算。

## 导出

```powershell
python -m medical_qa_data_factory export --workspace data/runs/aspirin-v1 --output data/exports/v1 --level human_confirmed
```

三个等级各自独立：

| level | 内容 |
|---|---|
| `development` | 一切，含 mock 与未审核候选；带水印 |
| `machine_validated` | 通过机器校验的问题；允许处置未决，manifest 统计待处置组数 |
| `human_confirmed` | 人工确认且组内处置完成的问题；表达变体随主问题导出 |

等级必须显式给出，不由输出目录或记录状态推断。manifest 记录等级、处置统计、
待处置组数与排除原因。

## 验收

```powershell
python -m medical_qa_data_factory eval run `
  --manifest eval/manifests/baseline-v1.json `
  --workspace data/runs/eval-baseline-v1 `
  --report data/runs/eval-baseline-v1/report.json

python -m medical_qa_data_factory eval react `
  --manifest eval/manifests/baseline-v1.json `
  --report data/runs/eval-baseline-v1/react-report.json
```

`eval run` 跑清单里的种子并产出验收报告。`eval react` 在同一份真实图上运行完整
`observe → decide → gate → act → persist → assess` 状态机，测量观察依赖的多跳可达性；
planner 是脚本化的，不是模型，也不生成正式问题。

报告的结论只有三种：`passed`、`failed`、`not_reproducible`。环境与清单不匹配时
是 `not_reproducible`，不会伪装成通过。

## 测试

```powershell
python -m pytest -q -m "not primekg_integration"
python -m pytest -q -m primekg_integration -o addopts=""
```

真实 PrimeKG 的只读集成测试默认不执行，需要显式指定 marker。

## 故障排查

| 症状 | 原因与处理 |
|---|---|
| `kg_config_error` / 数据库找不到 | 检查 `MEDICAL_QA_PRIMEKG_PATH` 与 `config/knowledge_graphs.json` |
| `snapshot_stale` | 数据库变了，跑 `kg snapshot refresh` |
| `environment_error` | 不在 `qa_env` 里，或依赖版本过旧 |
| `model_config_error` | 没有 `config/models.json`，或 `api_key_env` 指向的变量未设置 |
| `literature_config_error` | 没有 `config/literature.json`，或 provider 字段不全 |
| `kg_capability_error`（kg paths） | 预期行为：PrimeKG 不支持完整有序路径查询 |
| 导出被阻断 | 看 `data/exports/<id>/manifest.json` 的 `excluded` 字段，每条都有原因 |
| 审核页处置后没有生效 | 看 `review_operations` 集合里有无 pending 的 operation；有则跑 reconciliation |

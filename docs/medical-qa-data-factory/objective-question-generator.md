# 客观医学题目生成器

> 状态：可运行 MVP
> 主链：有界主 Agent 决策循环
> 当前 provider：PrimeKG、PubMed、可选 CTD；RAG adapter 暂未启用

## 运行条件

1. 使用 Conda 环境 `qa_env`；
2. 配置 `config/models.json`，并设置其中的 `api_key_env`；
3. PrimeKG 配置通过 `config/knowledge_graphs.json` 或 `MEDICAL_QA_KG_CONFIG` 指定；
4. PubMed 可通过公网访问；设置 `NCBI_API_KEY` 可提高限额。
5. 使用 CTD 时传入 `--provider ctd --ctd-index <index>`，或配置
   `config/evidence_enrichment.json` 的 `ctd_index_path`。

## 启动命令

推荐直接使用脚本：

```powershell
# 默认启动 Vue 工作台
pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/objective.ps1

# 直接运行主 Agent 生成
pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/objective.ps1 `
  -Mode generate `
  -EntityName BRCA1 `
  -EntityId 672 `
  -EntityType gene `
  -QuestionCount 1

# 只校验路径、参数和模型状态，不启动服务或创建目录
pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/objective.ps1 `
  -Mode generate -EntityName BRCA1 -DryRun
```

脚本自动设置 `PYTHONPATH`，检查 Python、模型 API key、provider 和 CTD index；
生成模式默认创建 `data/runs/<run-id>` 与 `data/releases/<run-id>`。

底层命令如下。

```powershell
$env:PYTHONPATH='E:\Cursor\assumption\projects\medical-qa-data-factory\src'

& 'D:\Anaconda\envs\qa_env\python.exe' -m medical_qa_data_factory objective generate `
  --workspace data/runs/brca1-objective `
  --output data/runs/brca1-objective-release `
  --run-id brca1-objective `
  --entity-name BRCA1 `
  --entity-id 672 `
  --entity-type gene `
  --question-count 1 `
  --max-actions 12 `
  --provider primekg `
  --provider pubmed
```

`--workspace` 和 `--output` 必须是新的空目录。需要重新运行时更换 `--run-id`、
`--workspace` 和 `--output`，不能覆盖已有不可变审计记录。

## Vue 工作台

启动工作台：

```powershell
& 'D:\Anaconda\envs\qa_env\python.exe' -m medical_qa_data_factory web serve `
  --workspace data/runs/objective-web `
  --host 127.0.0.1 `
  --port 8765
```

在首页的“生成引擎”中选择“主 Agent 循环”。页面会实时展示：

- 主 Agent 每次 action、目标子 Agent 和预期信息增益；
- 材料、anchor、claim、hypothesis、resolution、contract 和复核报告数量；
- 已通过发布门禁的题目及标准答案；
- `COMPLETED_WITH_ZERO_QUESTIONS` 的原因和完整审计摘要；
- 同一工作区中的主 Agent 运行历史。

后端接口：

```text
POST /api/objective/generate
GET  /api/objective/{run_id}
```

Objective run 存放在 `<workspace>/objective_runs/<run_id>`，发布包存放在
`<workspace>/objective_releases/<run_id>`。

## 主 Agent 循环

主 Agent 每次读取：

- `GenerationGoal`；
- 当前 artifact 摘要和 hypothesis 状态；
- 剩余成本、模型调用和 action 预算；
- 可用 provider；
- 由 artifact 前置条件计算的合法 action。

然后提交一个 `MainAgentDecision`。确定性协调器负责：

- 校验 state digest、action、目标角色和前置条件；
- 发起对应的子 Agent 或 provider；
- 保存 artifact、预算、child slot 和 operation；
- 推进 revision 与 commit sequence；
- 检测循环和越权；
- 执行最终发布门禁。

允许的循环不是固定顺序。主 Agent 可以重复检索、补充证据、放弃候选、重试后续
候选、停止并产出 0 题，或推进单个 hypothesis 到发布。

## 输出

```text
public/
  questions.jsonl
  manifest.json
audit/
  validation_reports.jsonl
  runtime_artifacts.jsonl
  main_agent_decisions.jsonl
  manifest.json
```

只有 `ObjectiveValidationReport.PASS + release_allowed=true` 的题会进入
`public/questions.jsonl`。`COMPLETED_WITH_ZERO_QUESTIONS` 是正常成功状态；
此时 `questions.jsonl` 为空，但 audit 保留完整决策、材料、拒绝原因和 provider 记录。

## 当前边界

- PrimeKG 和 PubMed 只提供材料，不能直接写入 gold；
- RAG adapter 缺失不会阻塞运行，也不会被解释为事实不存在；
- CTD 只有在显式启用并存在本地索引时参与检索；
- 发布复核分两阶段，第一阶段完全看不到答案、contract 和 evidence；
- Writer 只接收冻结契约；
- 模型、schema、prompt、provider 和 validator skill 版本均进入审计记录。

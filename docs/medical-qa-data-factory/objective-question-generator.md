# 客观医学题目生成器

> 状态：可运行 MVP
> 主链：有界主 Agent 决策循环
> 当前 provider：HGNC、NCBI Gene、Ensembl、PrimeKG、PubMed、可选 CTD；RAG adapter 暂未启用

## 运行条件

1. 使用 Conda 环境 `qa_env`；
2. 配置 `config/models.json`，并设置其中的 `api_key_env`；
3. PrimeKG 配置通过 `config/knowledge_graphs.json` 或 `MEDICAL_QA_KG_CONFIG` 指定；
4. PubMed 可通过公网访问；设置 `NCBI_API_KEY` 可提高限额。
5. HGNC、NCBI Gene、Ensembl 用于基因身份、分类和染色体位置等结构化事实。
6. 使用 CTD 时传入 `--provider ctd --ctd-index <index>`，或配置
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

## 事实类型与数据源

主 Agent 同时看到 `allowed_fact_types` 和 `fact_source_routes`，不会把所有事实都送去
PubMed：

| 事实类型 | 主来源 | 独立验证 |
|---|---|---|
| 基因身份、分类、染色体位置 | HGNC | NCBI Gene、Ensembl |
| 蛋白功能与结构域 | UniProt | NCBI Gene、Ensembl |
| 疾病关联 | CTD、Open Targets | PubMed |
| 通路成员关系 | Reactome、KEGG | 第二通路来源 |
| 文献结论、冲突、人群与剂量 | PubMed | 原文或独立研究 |

当前默认启用 `hgnc`、`ncbi_gene`、`ensembl`、`primekg`、`pubmed`。结构化来源失败会保留
`FAILED` 或 `UNKNOWN` 终态，不会被解释成“该事实不存在”。

Web API 会按实体类型和生产源自动选择事实范围，并把
`observation_association` 纳入默认范围，使文献中的机制、疾病关联、实验观察和临床
结论能够进入事实抽取。调用方也可以通过 `allowed_fact_types` 显式覆盖默认范围。
前端可以单独启停材料 provider；带 CTD 运行时需配置
`MEDICAL_QA_CTD_INDEX` 或 `config/evidence_enrichment.json`。

检索 Agent 默认最多 4 轮，并允许主 Agent 为单次任务覆盖材料数、轮数、每轮查询数
和候选预览长度。PubMed 先按摘要筛候选，再只为已选 PMID 获取 PubTator/PMC 全文章节；
全文不可用时保留摘要，不把全文缺失解释为材料失败。

例如 ZRSR2 的 `gene_location` 需求会优先执行：

```text
HGNC: ZRSR2           -> Xp22.2, protein-coding gene, cross IDs
NCBI Gene: 8233       -> chromosome X, map location, description
Ensembl: ENSG...      -> assembly location and biotype
PubMed: ZRSR2 chromosome -> optional literature support
```

问题设计阶段可以看到由 candidate claim 绑定并受长度限制的证据片段，从而保留机制、
人群、实验条件和限定词；它仍不能修改 claim、读取无界材料或自行确认答案。

结构化 provider 使用完整记录优先：HGNC 通过 `fetch/symbol` 或 `fetch/id` 获取详情，
Ensembl 使用 `expand=1` 并补充 xrefs，NCBI Gene 保留 genomic/location history 等字段。
每条结构化材料同时提供字段级 anchor 和整条记录 anchor，避免模型只能引用少量白名单字段。
只要存在尚未做事实抽取的新材料，主 Agent 必须先执行 `understand_facts`，之后才能设计问题；
问题设计通过 claim group 组合同一来源的原子 claim，而不是继续把完整结论拆碎。

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
  --max-actions 100 `
  --provider primekg `
  --provider pubmed
```

`--workspace` 和 `--output` 必须是新的空目录。需要重新运行时更换 `--run-id`、
`--workspace` 和 `--output`，不能覆盖已有不可变审计记录。

## Vue 工作台

启动后端 API：

```powershell
& 'D:\Anaconda\envs\qa_env\python.exe' -m medical_qa_data_factory web serve `
  --workspace data/runs/objective-web `
  --host 127.0.0.1 `
  --port 8765
```

在首页的“生成引擎”中选择“主 Agent 循环”。页面会实时展示：

Vue 工作台需在另一个终端启动：

```powershell
cd frontend
npm run dev
```

浏览器访问 `http://127.0.0.1:5173`。后端不再提供 HTML 页面。

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

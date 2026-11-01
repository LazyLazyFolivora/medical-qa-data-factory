# Medical QA Data Factory

用于自动、手动或混合构造多源医学问答数据集的独立服务。

## 边界

- 本项目拥有数据生成任务、证据包、问题规格、调用配置、客观用量和离线参考动作等数据契约。
- 本项目可以调用 KG 和文献检索工具，但不依赖任何具体查询规划器实现。
- 下游选择器或查询规划器只能通过导出的版本化数据契约消费结果。
- `PolicyCost` 是数据构造和离线策略比较使用的实验量；原始 token、耗时和金额单独记录，不直接等同于策略成本。

## KDD 路由对接

已增加多方向公开任务、版本化观测/回执环境及独立 solver 评测入口：
`python -m medical_qa_data_factory eval routes solve|evaluate|compare`。
方向采用 `pipeline / research / business / public_health`。真实宿主通过
`ObservationReceiptEnvironment` 提供当前合法候选与真实回执，工厂不接管工具执行。
公开任务、隐藏目标、环境版本、生成轨迹及新求解轨迹分别保存；未审核真实任务不产生 gold 分数。
具体函数、请求形状、命令及本轮验证限制见
[query-planner 交接](docs/query-planner-release-handoff.md#多方向-kdd-路由评测2026-10-30)。

## 当前状态

当前已实现：

**数据契约与不可变存储**

- GenerationTask、EvidenceBundle、QuestionSpec 的 Pydantic 契约与 JSON Schema；
- 不可覆盖的本地版本化 JSON 产物仓库，写入顺序显式记录（"最新版本"不再依赖 mtime）；
- 变更记录（ChangeRecord）保留每一次修改的父版本与新版本。

**运行身份与预算账本**

- `RequiredEvidenceMode` 为服务端权威的证据模式，绑定在 run / bundle / question / coverage 上；
- `GenerationPreparationRun` 与 `GenerationRunManifest`：正式 run 必须在成功的 provider
  preflight 之后才可冻结；
- `AttemptRecord` 是预算的唯一事实来源，状态机为 reserved → running → completed / failed /
  unknown；未知结果按预留上限计费，不按零计费；
- `reconcile` 结算孤儿 reservation 与结果未知的 running attempt。

**知识图谱**

- 以只读方式接入本地 PrimeKG：实体解析、邻居查询，统一转换为 `KGEvidence`；
  provider 的 `path_query` capability 仍声明为 `false`（`paths()` 忽略关系/类型过滤，
  无法返回完整有序路径），生成流程不会调用它；
- `knowledge_graph/frontier.py` 的有界 frontier 扩展自行组装 N 跳有序路径，每步带 edge id、
  遍历方向与 query trace，可按步重放；拒绝环、重复边、回到起点与跨 snapshot 缝合；
- 路径持久化遵循 evidence-first / path-second / reload-validation：证据先落盘，路径引用
  证据 id，随后从磁盘重载并重新校验，只有通过才发布。

**文献检索**

- `literature/` 提供 `preflight` / `search` / `read_chunk` 三能力与 provider registry；
- WeKnora adapter 的请求与响应形状取自其源码（`POST /knowledge-search`、
  `GET /chunks/by-id/:id`），见 [接入契约](docs/weknora-rag-integration.md)；
- 检索结果必须经同一 snapshot 上的独立回读、文本 hash 与字符偏移一致，才成为文本证据；
- 逐 hop 构造 `LiteratureEvidenceNeed`，支持性判断独立保存且不覆盖证据原文；
  评估集合必须包含全部适用结果，支持/反对/不确定/仅背景/无关都进入。

**生成流程**

- `rule-based` 使用单跳固定 DAG；`fake` / `structured` 在身份校验后先运行有界 ReAct
  探索；带显式 `GenerationSpec` 的请求使用结构化 Writer 和上下文隔离的 Verifier，
  历史请求继续使用确定性规则组件；
- 有界 ReAct 循环 `observe → decide → gate → act → persist → assess`：模型只能提交 proposal，
  Policy Gate 校验后生成 SealedAction，Executor 只执行 SealedAction，非法提案零工具调用；
- 正式 RAG 动作绑定服务端 evidence need，执行 search → 独立 read-back →
  hash/偏移/snapshot 校验，通过后才落盘 `LiteratureEvidence`；
- 每个 episode 同时保存版本化 `QAItem`、`PlannerTrajectory`、逐轮
  `PlannerStep` 和可确定性派生的 `PlannerTrainingSample`；
- 难度按冻结证据计算；多来源 medium、两跳 KG、约束或答案歧义不会由探索轮数决定；
- `AnswerabilityProbe` 在完整固定 snapshot 上确定性求值，Sufficiency Judge 由模型提议、
  代码定夺；模型无法绕过断链、缺文本支持、未解决冲突、歧义与预算硬门禁；
- 强类型 `LogicalQuery`、RelationCompositionPolicy、leave-one-hop-out 与更短路径 shortcut
  检查、answer-blind 验证器、按 lineage 计的一次改写预算。

**审核与导出**

- 实体—关系分组：group key 由种子实体与完整有序关系链生成，可复算；
  `duplicate_detection` 为 advisory（提示而非裁定），`review_group_integrity` 为阻塞型；
- 组内可解释排序（`ranking.v1`），分数只改展示顺序，不改校验等级与处置状态；
- 六个处置命令（keep_independent / set_variant_of / keep_both / revise / return / reject），
  `set_variant_of` 为复合写入，经 ReviewOperation journal 落库，中断后可 reconciliation；
- 分组审核页面 `/groups` 并列展示问题、答案、KG 边、逐 hop 文本证据与排序因素；
- 三套显式导出等级：development / machine_validated / human_confirmed，各自独立门禁。

**模型**

- OpenAI-compatible structured-output adapter：严格 JSON Schema、一次 schema repair、
  稳定错误分类、逐 physical attempt 计费；配置只保存 `api_key_env`，不接受内联密钥。

**尚未实现或未验证**

- fake-model + pinned fake-RAG 的 KG → RAG → Writer → Verifier → QA/Trajectory
  闭环已由测试验证；真实 WeKnora 实机和真实 structured model smoke 仍未执行；
- Planner 正负样本的正式 JSONL 导出、split 防泄漏、采样平衡和人工质量报告仍未完成；
- 来源消融的执行器已实现并有单元测试，但未在真实文献 provider 上跑通，因此
  `verified_route` 在验收报告中仍为 unknown；
- `structured` 模式会从 registry 装配真实模型 provider；本仓库尚未记录一次获授权的真实模型
  smoke，缺配置时按设计 fail fast；
- 人工处置的保留率与改排率尚无数据（验收运行产出 11 个待处置候选，无人工决策）。

详见 [验收报告 baseline-v1](docs/acceptance-report-v1.md) 的"未覆盖项"。

v0 价格仅用于打通数据流，正式比较实验前必须校准并发布新版本，不能原地修改旧版本。开发假工具的产物始终保持 `draft`，不得作为机器校验或人工确认后的 gold 数据。


- [运行手册](docs/running-guide.md)
- [当前产品需求：医学 QA 与 Query Planner 训练数据生成](docs/entity-question-generation-implementation-requirements.md)
- [验收报告 baseline-v1](docs/acceptance-report-v1.md)
- [当前实现问题与差距](docs/current-implementation-gaps.md)
- [WeKnora RAG 接入契约](docs/weknora-rag-integration.md)
- [PrimeKG 与可扩展知识图谱接入设计](docs/primekg-integration-design.md)
- [问题风格 Skill 规范](docs/question-style-skills.md)
- [证据增强自然问答扩充方案](docs/evidence-enrichment-natural-qa-plan.md)
- [历史：面向实体的自动探索与问题生成技术路线](docs/entity-question-generation-agent-roadmap.md)
- [历史：真实生成 Agent 开发实施文档](plans/real-generation-agent-implementation-plan.md)

## 开发环境与启动

项目直接依赖统一维护在
`requirements.txt` 中，传递依赖的完整固定版本在 `constraints.txt`；开发时不需要安装项目本身，
通过 `PYTHONPATH` 和 `python -m` 直接从 `src` 运行。

### Windows（Conda）

首次创建环境并安装依赖：

```bat
conda create --name qa_env python=3.12 pip -y
conda env config vars set -n qa_env PYTHONNOUSERSITE=1
conda activate qa_env
cd /d E:\Cursor\assumption\projects\medical-qa-data-factory
set PYTHONPATH=%CD%\src
python -m pip install -r requirements.txt -c constraints.txt
```

`PYTHONNOUSERSITE=1` 用于避免用户目录中的全局 Python 包污染 Conda 环境。
如果设置该变量时环境已处于激活状态，请先执行一次
`conda deactivate`，再重新执行 `conda activate qa_env`。

以后每次打开新终端，只需执行：

```bat
conda activate qa_env
cd /d E:\Cursor\assumption\projects\medical-qa-data-factory
set PYTHONPATH=%CD%\src
```

### Linux（venv + bash）

项目要求 Python 3.10 以上，推荐 Python 3.12。在项目根目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt -c constraints.txt
export PYTHONPATH="$PWD/src"
```

以后每次进入新 shell：

```bash
cd /path/to/medical-qa-data-factory
source .venv/bin/activate
export PYTHONPATH="$PWD/src"
```

如果使用 Conda for Linux，也可以继续使用 `qa_env`；激活环境后设置
`export PYTHONPATH="$PWD/src"` 即可。

生成开发演示数据：

```bat
python -m medical_qa_data_factory demo run --output data/runs/local-demo --task-id aspirin-local-demo --seed aspirin
```

启动问题生成工作台：

```bat
python -m medical_qa_data_factory web serve --workspace data/runs/local-demo --host 127.0.0.1 --port 8765
```

浏览器打开 `http://127.0.0.1:8765`。保持终端运行，按 `Ctrl+C` 停止服务。
首页是问题生成主工作台；生成记录、问题库、数据集和审核功能位于顶部导航。
首页会从 PrimeKG 边表前部提取并去重展示 10 个种子实体，即使 workspace 为空也可以直接选择；
搜索框覆盖 PrimeKG 全表实体，按名称或原生 ID 返回最多 10 个候选。选择实体、生成数量和难度后，
点击“开始生成”即可直接运行 `rule-based + PrimeKG` 生成任务；生成完成后页面会自动刷新。
生成期间页面每 750 ms 从不可变运行记录读取进度，实时显示当前节点、已完成节点、Planner
轮次、动作和新增证据数，无需等待整次任务结束后才刷新执行轨迹。
全局搜索使用 `data/cache/primekg-entities.sqlite` 派生实体目录，首次搜索会构建一次，
后续查询直接读取索引；原始 PrimeKG 文件始终保持只读。

数据仓库采用不可变版本；如果需要重新运行演示，请更换 `--output` 和
`--task-id`，例如使用 `local-demo-2` 和 `aspirin-local-demo-2`。

`pyproject.toml` 同时声明直接依赖（`dependencies`）与打包元数据、测试配置和命令行入口，
便于 editable install；第三方依赖的具体版本仍以 `requirements.txt` 与 `constraints.txt` 为准。

Web 首页提供两种生成引擎：

- `Agent（LLM）`：`verify_seed` 后进入 ReAct 探索，Planner、Writer 和 Verifier
  使用配置的结构化模型；
- `规则基线`：不调用模型的确定性单跳路径，用于对照和故障排查。

从 `config/models.example.json` 创建 `config/models.json`，填写 OpenAI-compatible endpoint、
模型名和密钥所在的环境变量，再重启 Web 服务。配置和密钥均可用时，首页默认选择 Agent；
否则 Agent 选项会禁用并显示缺失原因，不会静默回退到规则模式。

Agent 模式还提供三种可版本化的问题风格 Skill：

- `layperson-search-v1`：普通用户搜索式；
- `patient-caregiver-v1`：患者或家属自然提问；
- `medical-learner-v1`：医学学习者提问。

Skill 定义位于 `skills/question-styles/`，包含实际发送给模型的 `SKILL.md`、版本清单、正例和
hard negatives。Web 每个逻辑题生成 3 个风格候选，分别经过 Writer 契约校验、独立风格审查、
答案盲测和确定性泄漏检查；不合格变体保存拒绝原因，不阻断其他候选。题目会记录 Skill ID、
版本、风格 profile 和风格分数。

Structured Web 生成会先经过 evidence enrichment：优先补 CTD 原始记录，再读取 PubMed
摘要，只有证据支持的意图和唯一实体答案才会进入问题规划。配置位于
`config/evidence_enrichment.json`。证据冲突、映射歧义或无法在现有实体答案契约下唯一
回答时，该候选会被拒绝。

## 测试

```bat
python -m pytest tests -q
```

真实 PrimeKG 的只读集成测试默认不执行，需要显式指定 marker：

```bat
python -m pytest tests -m primekg_integration -q -o addopts=""
```

当前基线：默认 806 项通过（13 项真实 PrimeKG deselected），真实 PrimeKG 只读 13 项通过。

本地 PrimeKG 路径为 `E:\Cursor\assumption\datasets\PrimeKG\primekg.sqlite`，可用
`MEDICAL_QA_PRIMEKG_PATH` 覆盖。

推荐且默认的 Conda 环境是 `qa_env`。执行 `generate` 前会做一次环境预检：缺失或版本过旧的
直接依赖（`pydantic`、`langgraph`、`langgraph-checkpoint-sqlite`）会以 `environment_error`
（退出码 3）失败，并且不会写入任何产物；非 `qa_env` 的 Conda 环境只会作为提示报告，
不会阻断运行。

候选问题在 verifier 判为 `needs_revision` 时最多改写一次；第二次仍需要改写的候选会被
hard reject，不会自动进入机器校验或人工队列。Step 0 之前已放行的样本不会被自动重判，
处置方式见 `UNFINISHED_CHECKLIST.md` 的「受影响产物的迁移说明」。

## 知识图谱诊断

证据增强诊断：

```bat
python -m medical_qa_data_factory evidence ctd build --source data/external/ctd/CTD_chemicals_diseases.csv.gz --source-release "CTD YYYY-MM" --output data/cache/ctd-evidence.sqlite
python -m medical_qa_data_factory evidence ctd lookup --index data/cache/ctd-evidence.sqlite --chemical-id D001241 --disease "prostate cancer"
python -m medical_qa_data_factory evidence ctd enumerate --index data/cache/ctd-evidence.sqlite --chemical-id D001241
python -m medical_qa_data_factory evidence ctd audit --index data/cache/ctd-evidence.sqlite --primekg-database ../../datasets/PrimeKG/primekg.sqlite
python -m medical_qa_data_factory evidence pubmed search --query "\"aspirin\" AND \"prostate cancer\"" --limit 5
python -m medical_qa_data_factory evidence pubmed fetch --pmid 34626297 --limit 1
```

`ctd enumerate` 不设置结果上限，并按 CTD disease identity 合并别名；同名但
disease ID 不同的记录不会被合并。`ctd audit` 为每条 PrimeKG CTD
`exposure_disease` 边输出 `mapped_exact`、`mapped_crosswalk`、`ambiguous` 或
`unmapped`。如有经审核的 ID crosswalk，可通过 `--crosswalk` 传入
`ctd-disease-crosswalk-v1` JSON；名称匹配与 crosswalk 冲突时审计会关闭式失败为
`ambiguous`。

```json
{
  "schema_version": "ctd-disease-crosswalk-v1",
  "entries": [
    {
      "primekg_disease_id": "5070",
      "ctd_disease_ids": ["MESH:D009369"]
    }
  ]
}
```

```bat
python -m medical_qa_data_factory kg providers
python -m medical_qa_data_factory kg health --provider primekg
python -m medical_qa_data_factory kg resolve --provider primekg --entity aspirin
python -m medical_qa_data_factory kg neighbors --provider primekg --entity aspirin --direction both --limit 20
python -m medical_qa_data_factory kg paths --provider primekg --start aspirin --end headache --max-hops 2
```

## 模型诊断

`structured` 模式的 provider 从 `config/models.json` 装配，可用
`MEDICAL_QA_MODEL_CONFIG` 覆盖路径。配置只保存 API key 所在的环境变量名，
不接受内联密钥：

```json
{
  "default_provider": "local",
  "providers": {
    "local": {
      "driver": "openai_compatible",
      "base_url": "http://127.0.0.1:8000/v1/chat/completions",
      "model_id": "my-model",
      "api_key_env": "MY_MODEL_API_KEY"
    }
  }
}
```

```bat
python -m medical_qa_data_factory model providers
```

该命令只输出环境变量名和是否存在，不输出密钥本身。

## 文献检索诊断

真实文献检索通过 WeKnora 接入，接口契约见
[WeKnora RAG 接入契约](docs/weknora-rag-integration.md)。provider 从
`config/literature.json` 装配，可用 `MEDICAL_QA_LITERATURE_CONFIG` 覆盖路径。
配置只保存 API key 所在的环境变量名：

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

```bat
python -m medical_qa_data_factory literature providers
python -m medical_qa_data_factory literature preflight
```

`preflight` 报告 provider 能否固定语料快照、能否独立回读分块。这两项决定该
provider 能否产出正式 gold 文本证据。

`kg paths` 对 PrimeKG 返回稳定错误码 `kg_capability_error`：该遍历忽略关系/类型过滤，
也无法返回完整有序路径，因此 provider 的 `path_query` capability 声明为 `false`，
生成流程不会调用它。

数据库快照 manifest 保存在数据库同目录的 `<database-name>.manifest.json`。普通查询只读取
manifest，绝不隐式扫描 1.63 GiB 的数据库文件；size/mtime 变化会标记 `snapshot_stale` 并
要求显式刷新：

```bat
python -m medical_qa_data_factory kg snapshot init --database "%MEDICAL_QA_PRIMEKG_PATH%"
python -m medical_qa_data_factory kg snapshot refresh --database "%MEDICAL_QA_PRIMEKG_PATH%"
```

## 实体驱动问题生成

上游必须显式给出实体身份（provider / native id / entity type / name）。项目只做严格身份
验证，不做名称模糊匹配或实体消歧；身份不匹配时 episode 直接 `failed`，stop reason 为
`invalid_upstream_identity`。

```bat
python -m medical_qa_data_factory generate entity ^
  --workspace data/runs/aspirin-v1 ^
  --task-id aspirin-v1 ^
  --provider primekg ^
  --entity-name "Aspirin" ^
  --entity-id D001241 ^
  --entity-type exposure ^
  --question-count 5 ^
  --model-mode rule-based
```

真实 PrimeKG 只读 smoke：

```bat
python -m medical_qa_data_factory generate entity ^
  --workspace data/runs/aspirin-primekg-smoke ^
  --task-id aspirin-primekg-smoke ^
  --provider primekg ^
  --entity-name "Aspirin" ^
  --entity-id D001241 ^
  --entity-type exposure ^
  --question-count 3 ^
  --model-mode rule-based
```

恢复一个已有 episode（checkpoint 固定在 `<workspace>/checkpoints/generation.sqlite`，
`thread_id` 恒等于 `episode_id`）：

```bat
python -m medical_qa_data_factory generate resume ^
  --workspace data/runs/aspirin-v1 ^
  --episode-id <episode-id> ^
  --task-id aspirin-v1 ^
  --provider primekg ^
  --entity-name "Aspirin" ^
  --entity-id D001241 ^
  --entity-type exposure
```

命令输出 JSON summary（task / episode / trajectory / bundle / QAItem / question IDs）。
失败时 stderr 输出
`{"error":{"code":"...","message":"...","retryable":false,"episode_id":null}}`。
退出码：`2` 参数或身份错误，`3` capability/config 错误，`4` provider 暂态错误，`1` 内部错误。

生成的候选答案在人工确认前不得称为 gold，也不得进入正式导出。工作台把该字段显示为
「候选答案（Gold short answer）」，底层 `QuestionSpec.gold_answer` 保持向后兼容。

`--model-mode` 支持三种模式，且它们的装配彼此分离，不再互相冒充：

- `rule-based`：确定性单跳回归基线，**不调用任何模型，也不是 ReAct Agent**；
- `fake`：测试用脚本化模型替身，必须由调用方注入，CLI 不会自动装配；
- `structured`：真实模型模式，从 provider registry 装配 adapter；缺少配置时 fail fast，
  不会静默退回 `rule-based` 后仍声称运行了结构化模型。

正式 `generate entity` 有两条明确路径：`rule-based` 保留固定 DAG；`fake` / `structured`
在 `verify_seed` 后进入 `observe → decide → gate → act → persist → assess` ReAct 子图，
随后才执行 `curate_evidence → build_claims → ... → finalize`。第二轮 Planner 会收到第一轮
`ToolObservation`，工具证据先以正式 `KGEvidence` ID 落盘，再被 observation 和证据包引用。
带显式 `GenerationSpec` 时，LLM 同时负责探索决策和结构化 Writer；Verifier 使用独立的
role 与 payload，不读取 Writer 的候选答案或理由。RAG 已按 evidence need 接入正式执行链，
但真实 WeKnora 与真实 structured provider 的实机 smoke 仍需外部服务与授权配置。

```bat
python -m medical_qa_data_factory generate entity ^
  --workspace data/runs/aspirin-v2 ^
  --task-id aspirin-v2 ^
  --provider primekg ^
  --entity-name "Aspirin" ^
  --entity-id D001241 ^
  --entity-type exposure ^
  --question-count 2 ^
  --model-mode structured ^
  --generation-spec config/generation-spec.example.json ^
  --safety-budget config/safety-budget.example.json
```

Provider 配置在 `config/knowledge_graphs.json`，路径相对项目根目录解析。
可用 `MEDICAL_QA_PRIMEKG_PATH` 覆盖本地数据库路径，用 `MEDICAL_QA_KG_CONFIG`
覆盖配置文件路径。数据库始终以 `mode=ro` 加 `PRAGMA query_only = ON` 打开，
查询不会修改原始文件。

## 验收

Planner/QA 训练数据导出：

```bat
python -m medical_qa_data_factory training export ^
  --workspace data/runs/aspirin-v2 ^
  --output data/exports/aspirin-v2-training
```

输出 `qa_items.jsonl`、`planner_training_samples.jsonl` 和 `manifest.json`。
`split-group-hash-v1` 以 `dataset_split_group_id` 为原子做确定性 80/10/10
划分，并在同一结构指纹跨 split 时阻断导出。

当前公开契约快照位于 `schemas/v1/`，可直接用于下游 schema 校验。

验收清单固定了种子、图快照、模型与文献 provider 的快照版本：

```bat
python -m medical_qa_data_factory eval run ^
  --manifest eval/manifests/baseline-v1.json ^
  --workspace data/runs/eval-baseline-v1 ^
  --report data/runs/eval-baseline-v1/report.json

python -m medical_qa_data_factory eval react ^
  --manifest eval/manifests/baseline-v1.json ^
  --report data/runs/eval-baseline-v1/react-report.json
```

`eval run` 跑清单里的种子并产出验收报告；`eval react` 在同一份真实图上通过完整 ReAct
状态机测量观察依赖的多跳可达性（planner 是脚本化的，不是模型，也不生成正式问题）。
报告结论只有 `passed`、`failed`、`not_reproducible`
三种，环境与清单不匹配时不会伪装成通过。

结果见 [验收报告 baseline-v1](docs/acceptance-report-v1.md)。

## 导出 JSON Schema

```bat
python -m medical_qa_data_factory schema export --output schemas/v0
```

## 运行开发演示

```bat
python -m medical_qa_data_factory demo run --output data/runs/demo --task-id aspirin-demo --seed aspirin
```

产物按不可变版本保存：

```text
data/runs/demo/
├── tasks/<task_id>/<record_version>.json
├── bundles/<bundle_id>/<bundle_version>.json
└── questions/<question_id>/<question_version>.json
```

同一个版本不会被覆盖；重复写入会直接报错。真实文献 RAG 和真实生成模型将在后续通过 `ports.py` 中的接口接入。

## 启动可视化工作台

### 快速启动

在项目根目录启动。以下命令适用于 Windows CMD 或 Anaconda Prompt：

```bat
conda activate qa_env
cd /d E:\Cursor\assumption\projects\medical-qa-data-factory
set PYTHONPATH=%CD%\src
python -m medical_qa_data_factory web serve --workspace data/runs/eval-baseline-v1 --host 127.0.0.1 --port 8765
```

PowerShell 使用：

```powershell
conda activate qa_env
Set-Location E:\Cursor\assumption\projects\medical-qa-data-factory
$env:PYTHONPATH = (Resolve-Path .\src).Path
python -m medical_qa_data_factory web serve --workspace data/runs/eval-baseline-v1 --host 127.0.0.1 --port 8765
```

Linux（bash）使用：

```bash
cd /path/to/medical-qa-data-factory
source .venv/bin/activate
export PYTHONPATH="$PWD/src"
python -m medical_qa_data_factory web serve \
  --workspace data/runs/eval-baseline-v1 \
  --host 127.0.0.1 \
  --port 8765
```

浏览器打开：

```text
http://127.0.0.1:8765
```

保持终端运行；按 `Ctrl+C` 停止服务。端口被占用时可以把 `8765` 改成其他本地端口。

默认绑定 `127.0.0.1`，只允许本机访问。如果部署到受控的 Linux 服务器并需要通过反向代理
访问，可改为 `--host 0.0.0.0`；不要直接把开发服务暴露到公网，生产环境应由反向代理、
访问控制和进程管理器负责 TLS、认证与服务守护。

### 使用新生成的开发数据

如果不使用仓库已有的 `eval-baseline-v1`，可以先生成一份开发数据，再把同一目录作为
`--workspace` 启动：

```bat
python -m medical_qa_data_factory demo run --output data/runs/local-demo --task-id aspirin-local-demo --seed aspirin
python -m medical_qa_data_factory web serve --workspace data/runs/local-demo --host 127.0.0.1 --port 8765
```

数据仓库不可原地覆盖；重复运行 demo 时请更换 `--output` 和 `--task-id`。

Linux 上的 PrimeKG 路径通过环境变量配置，不需要修改源码：

```bash
export MEDICAL_QA_PRIMEKG_PATH=/srv/medical-qa/data/primekg.sqlite
```

运行服务的用户需要对 workspace 有写权限；PrimeKG SQLite 文件只需要读权限。建议把
workspace 与只读知识库放在不同目录，并使用绝对路径配置外部数据库。

### 页面入口

当前界面提供：

- `/`：问题生成主工作台，选择种子实体并查看生成的问题、答案、证据摘要和运行进度；
- `/runs`：生成记录、任务状态与错误；
- `/entities`：按种子实体浏览问题和证据产物；
- `/dataset`：数据集导出与门禁；
- `/groups`：分组审核与候选处置；
- 任务流水线、错误和不可变产物版本；
- EvidenceBundle / QuestionSpec 跳转；
- KG → Claim ← Literature 三栏审核联动；
- 人工审核写入：确认、退回、拒绝，以及 claim 判定；
- mock 环境和 draft 禁止作为 gold 的持续提示。

目前首页的“开始生成”用于展示 Web 参数入口，尚未提交真实生成任务。正式生成仍通过
`medical-qa-data-factory generate entity` 或相应的 `python -m medical_qa_data_factory generate entity`
命令执行；产物写入当前 workspace 后，刷新页面即可看到。

审核动作不会原地覆盖记录，而是写入新的不可变版本，并在 `changes` 中记录审核人、动作、说明和父版本。

审核写入接口：

```text
POST /api/review/questions/{question_id}
  {"action": "confirm|return|reject", "summary": "...",
   "canonical_question": "...", "changed_fields": ["canonical_question"],
   "gold_short_answer": "...", "taxonomy": {"hop_complexity": 2},
   "verified_route": "hybrid", "dataset_split": "train"}

POST /api/review/bundles/{claim_id}
  {"status": "supported|conflicted|insufficient|rejected", "summary": "..."}
```

`summary` 必填，会进入版本变更记录。已拒绝的问题不能直接确认，只能退回。`changed_fields` 必须如实反映真正改动过的字段：值没变的字段不会被记为修改，也不会白白让依赖它的校验失效。

## 自动校验与状态流

校验分两层。`validation.py` 定义字段与校验类型的依赖关系：编辑某个字段会让读取它的校验重置为 `not_run`，避免界面上出现"人工确认通过"但旧校验仍挂着失败的矛盾状态。`validators.py` 里的 11 个校验器真正执行这些检查，并把校验器版本、结果和失败原因写进 `ValidationCheck`。

阻塞型校验（`BLOCKING_FOR_MACHINE_VALIDATION`）共 9 项，覆盖引用完整性、claim 证据支持、答案一致性、证据充分性、答案泄漏、重复检测、taxonomy、路由合理性和来源可引用性。自然度与变体语义等价为提示型，由人决定。

状态流是单向的：`draft` 通过全部阻塞校验后变为 `machine_validated`，人工确认后才变为 `human_confirmed`。自动流程永远不会写入 `human_confirmed`。任何一项必需校验没有记录，视为 `not_run` 而非通过。

```bat
# 运行全部校验并写入新的不可变版本
python -m medical_qa_data_factory validate run --workspace data/runs/demo

# 只查看每条问题的校验状态，不写任何文件
python -m medical_qa_data_factory validate status --workspace data/runs/demo
```

## 导出门禁

`question_leakage` 和 `duplicate_detection` 属于阻塞型自动校验，只能由校验器运行结果决定，人工点击无法将其标记为通过；未运行或未通过时确认会被拒绝。

导出必须先过门禁：

```bat
# 正式导出，有阻塞项时返回非零退出码
python -m medical_qa_data_factory export --workspace data/runs/demo --output exports

# 开发快照，允许包含 mock 或未确认数据，manifest 带水印
python -m medical_qa_data_factory export --workspace data/runs/demo --output exports --development
```

正式导出只写合格子集，失败样本不会阻塞其他样本：每条不合格记录的原因都会记进 manifest 的 `excluded` 字段，同时 `per_question_checks` 保留每条问题各项校验的状态与校验器版本。

工作区的阻塞条件：存在未确认或已拒绝的问题、阻塞校验缺失或失败、缺少可引用来源、任何 mock 工具产物。工作台的"数据集"页展示同一份阻塞清单，正式导出按钮在有阻塞项时不可用。


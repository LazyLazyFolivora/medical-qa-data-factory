# Medical QA Data Factory

李明洋维护数据生产与离线评价：从固定知识环境生成问题、证据和候选答案，区分公开题目与隐藏评价目标；为雷达导出路由与故障诊断。线上每步路由在 query-planner，执行与总预算在 BioRadar。

## 安装、启动与检查

Python 3.10+（开发使用 3.12）：

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt -c constraints.txt
export PYTHONPATH="$PWD/src"
python -m pytest tests -q
python -m medical_qa_data_factory web serve --workspace data/runs/my-workspace --host 127.0.0.1 --port 8765
```

页面提供问题、证据和人工审核；正式生成使用 CLI。PrimeKG 测试另运行 `python -m pytest tests -m primekg_integration -q -o addopts=""`，需真实只读快照。雷达四项集成检查见 [BioRadar README](../BioRadar/README.md)，本仓库没有对应 Makefile。

## 数据与真实问题

设置 `MEDICAL_QA_PRIMEKG_PATH`，或在 [config/knowledge_graphs.json](config/knowledge_graphs.json) 配置实际路径。图谱以只读方式打开；首次 `python -m medical_qa_data_factory kg snapshot init --database "$MEDICAL_QA_PRIMEKG_PATH"` 固定快照，资料更新才显式 refresh。`kg providers`、`kg health --provider primekg`、`kg resolve --provider primekg --entity aspirin` 可核对来源与身份。

从真实固定图环境生成受约束的问题，例如：

```sh
python -m medical_qa_data_factory generate validated --profile primekg   --entity-name '2-methylbutyryl-CoA dehydrogenase deficiency'   --entity-id 12392 --entity-type disease --output data/releases/my-first
```

ID 必须对应当前快照。只有完整世界复算、答案基数、约束增益和题面检查通过的任务才发布，结果为零就保留零。该入口目前不是任意实体、任意题型的通用生成器。CTD 使用 `evidence ctd build/lookup/enumerate/audit` 与显式身份映射；PubMed 使用 `evidence pubmed search/fetch`。CTD/PubMed 融合的真实多种子评价仍未完成。

模型与文献来源分别由 [config/models.example.json](config/models.example.json)和 `config/literature.json`（私有运行配置，按所用 provider 创建） 装配，配置只保存密钥环境变量名。`model providers`、`literature providers`、`literature preflight` 查看能力。普通 WeKnora RAG 缺少可验证固定快照时，不能用于正式 gold；雷达的 KDD 固定版本接口是另一条有显式原文绑定的路径。

旧 `generate entity` 保留 rule-based/fake/structured 三种显式模式；前者是零模型规则基线。结构化模式执行 observe→decide→gate→act→persist→assess，保存实际证据及回执；`generate resume` 恢复原 episode。`validate run/status` 查看或写新验证版本，`export` 只有审核通过才能正式导出，`--development` 保留开发标记。已有输出目录不可原地覆盖。

## 公开题目交给路由器

正式发布的 `public/` 只含问题及其公开 manifest；答案、要求、验证记录与出题轨迹在 `hidden/`。query-planner 独立读 `public/planner_questions.jsonl` 重新求解，评测阶段才读取隐藏目标。旧 `training export` 是出题探索数据，不是独立求解策略的训练轨迹。

当前 query-planner 只支持映射后的 relation_access/tool_calls/tokens；CTD/PubMed 等独立计费维度仍会明确拒绝，不能丢弃维度后声称兼容。路由 KDD 任务在 [schemas/v1](schemas/v1/)：pipeline/research/business/public_health，共用固定环境、时间和 split group。

`ObservationReceiptEnvironment` 只接入宿主 start/execute 回调，不读取模型/来源密钥，不另起线上执行器。数据加工、计算、专业评价和图查询的回执来自雷达实际执行。

## 读取已保存的雷达诊断

```sh
python -m medical_qa_data_factory eval routes export-radar-diagnostics   --views saved-view.json --scenario C --phase smoke --out diagnostics.jsonl
```

命令不调用模型、不导入来源、不重跑调查。只投影保存的错误码、阶段、引用身份、来源版本、路由及用量；缺字段保持缺失，未知用量保持 null，并给已知下界。引用原文、隐藏答案、完整模型提示和原始输出不随诊断复制。完整语义原因只以宿主实际保存的内容为准。

## 本轮实际验证与未完成

依据 `/home/zk_test/zk_projects/.branch-archive-20261101/runs.jsonl`，诊断测试 32 项通过；系统 32 项业务分析共 233 次新模型请求，已知 token 下界 5,863,856，1 次用量未知。A/B/C PASS 0/3，四个 baseline 出稿 0/4，D 两个判断终点未证明；成功导出诊断不能把原生失败改成通过。

下一步：先修雷达和专业方法的实际失败，再做独立效果评价；补齐公开 CTD/PubMed 环境和费用映射；扩展真实题型与种子，而不是复用隐藏目标生成看似正确的求解轨迹。现有正式题型覆盖与真实融合数据量仍不足。

主线仅 main，已推送。原 41 个 Markdown 中只保留本 README 及程序实际读取的 4 份 SKILL.md：三个 question-styles 提示和 objective-question-validator；读取点分别为 `generation/question_skills.py`、`generation/release_review_subagent.py`。它们是运行输入，不是额外说明文件。旧文档和分支可从同目录 medical-qa-data-factory.bundle 读取，运行数据留在受控归档。

# Medical QA Data Factory 当前实现与差距

> 文档类型：实现现状记录，不是需求替代品  
> 更新日期：2026-10-26  
> 当前需求依据：`development-architecture.md`、`acceptance-criteria.md`、`generated-question-types-and-language-criteria.md`

## 1. 当前结论

原独立 `mvp/` 已删除，任务生成正在原项目的 `generation / knowledge_graph / domain` 主干中重建。当前已经形成一个可在完整 PrimeKG 快照上运行的第一条生产纵切：

```text
真实实体
  -> 图谱候选发现
  -> TaskCandidate
  -> LogicalQuery / InformationRequirements
  -> 完整冻结世界复算
  -> 任务价值与基数验证
  -> ValidatedTask
  -> 受控题面表达与语义回环
  -> 公开问题 / 隐藏评估目标分离发布
```

它已经能发布一类真实、答案可复算、约束确有判别作用的问题；但尚未达到三份规范文档定义的完整产品范围。当前不能把“第一条纵切跑通”写成“平台全部开发完成”。

## 2. 已完成的重建内容

### 2.1 删除并隔离错误 MVP

- 删除 `src/medical_qa_data_factory/mvp/`、`tests/mvp/` 和对应演示产物；
- 删除 `mvp run` CLI；
- 删除旧 MVP schema 导出；
- 删除前快照位于 `E:\Cursor\assumption\backups\medical-qa-data-factory-removed-mvp-20261026.zip`。

### 2.2 任务优先的核心契约

已实现并测试：

- `ValidationEnvironment` / `ProviderSnapshotRef`；
- `TaskCandidate`；
- `TaskSpec`；
- `ValidationReport`；
- `ValidatedTask`；
- `InformationRequirement`；
- `AcceptableEvidenceSet`；
- `AnswerContract`；
- `PlannerQuestionInput`；
- `EvaluationTarget`。

Candidate 不能直接交给 Writer；正式发布对象会重新校验 validation report 的内容身份、任务绑定和题面语义绑定。

### 2.3 完整 PrimeKG 世界复算

`PrimeKGWorldEvaluator` 已支持：

- forward / reverse；
- relation 与 entity type 约束；
- 完整分页；
- 多 bridge 合并；
- answer constraint 交集；
- 去重与 cardinality；
- snapshot / provider mismatch 拒绝；
- truncation、unsupported filter、temporal scope 和 negation 的 fail-closed 行为。

候选发现使用的两条边不再负责证明答案唯一；`TaskValidator` 会在固定 PrimeKG 快照中重新执行完整 query。

### 2.4 第一种正式题型

当前正式支持：

```text
带约束的唯一实体选择（constrained_entity）
```

已审定的真实 PrimeKG relation composition：

```text
disease --disease_protein--> protein
pathway --pathway_protein--> protein
```

题面形式：

```text
与{疾病}相关且参与{通路}的蛋白是哪一种？
```

发布条件包括：

1. 完整世界交集答案严格为一个；
2. Candidate 提议答案与完整世界答案一致；
3. 去掉通路约束后答案集合必须扩大；
4. 如果去掉约束答案仍唯一，则以 `shortcut_detected` 拒绝；
5. 题面必须精确 round-trip 回原始 relation composition 和方向；
6. 题面不得泄漏答案 ID 或别名。

测试用 `targets + associated_with` profile 仍保留用于 provider-neutral 回归，不作为 PrimeKG 的业务 relation 声明。

### 2.5 发布边界

正式 release 物理分离为两棵目录：

- `public/planner_questions.jsonl`：Planner 可见问题；
- `public/dataset_manifest.json`：只列公开文件，不引用隐藏 artifact；
- `hidden/evaluation_targets.jsonl`：隐藏答案和 requirements；
- `hidden/validated_tasks.jsonl`；
- `hidden/validation_reports.jsonl`；
- `hidden/evidence_catalog.jsonl`；
- `hidden/generation_traces.jsonl`；
- `hidden/rejected_candidates.jsonl`；
- `hidden/quality_report.json`；
- `hidden/dataset_manifest.json`。

两个 manifest 分别只为各自边界内的文件记录 SHA-256。训练侧只分发 `public/`；`hidden/` 仅供 evaluator 和审计。生成检索轨迹使用 graph query trace，不冒充 Query Planner rollout 或正确动作标签。

### 2.6 正式 CLI

新增：

```powershell
python -m medical_qa_data_factory generate validated ...
```

该入口完成精确实体解析、冻结环境构造、任务生成、完整世界验证和不可变 release 导出。无法解析的实体、非空输出目录和 provider 错误会在写正式产物前失败。

## 3. 真实 PrimeKG 验证结果

当前固定快照：

- snapshot ID：`bd22ae20ed3037e2d4bb518904406143`；
- SHA-256：`7a0c995be7232bd3ae22b56545ec8ead518ba38a699a6c2416544f48fa7fb0bb`。

两个对照运行：

1. `saccharopinuria`：完整世界本来只有一个关联蛋白，通路约束无额外判别作用；候选以 `shortcut_detected` 拒绝，发布 0 题；
2. `2-methylbutyryl-CoA dehydrogenase deficiency`：完整世界有两个相关蛋白，三个通路条件分别形成严格单答案交集；发布 3 题。

对应产物：

- `runs/rejected-saccharopinuria-shortcut-20261026/`；
- `runs/validated-2methylbutyryl-20261026/`。

## 4. 尚未完成的产品范围

### 4.1 题型覆盖不足

尚未实现：

- 单关系实体定位；
- 隐藏中间实体的两跳问题；
- 显式中间线索的两跳问题；
- source-bounded 文献问题。

因此当前不是通用“从任意实体生成多种问题”的完整平台。

### 4.2 Candidate Source 仍窄

当前只有关系组合 allow-list，没有完成：

- 从 evidence exploration 自动发现可用 relation composition；
- 自由出题 Agent 提出结构化任务意图；
- 针对 EvidenceGap 的补搜循环；
- 搜索饱和、预算耗尽和 provider failure 的统一停止语义。

### 4.3 文献尚未进入正式任务认证

旧链路存在 WeKnora / PubMed 能力，但新的 `ValidatedTask` 纵切尚未把文献用于：

- relation wording 支持；
- qualifier / polarity / claim 强度验证；
- 固定文献集合内的 source-bounded task；
- 文献 evidence catalog 与 hash/span provenance。

文献不能单独证明开放世界唯一性，这一限制仍保持。

### 4.4 题面验证仍需扩展

当前 round-trip verifier 是针对已审定模板的确定性解析器。尚未完成：

- qualifier 保留；
- 隐藏 bridge 泄漏检测；
- 独立 blind answer verifier；
- 自然度模型/人工评分；
- 跨实体近重复和模板占比门禁；
- 中英文实体名规范化。

### 4.5 旧 Generation Service 尚未迁移完

`generate entity`、旧 `PlannerTrajectory / PlannerStep` 和旧训练导出仍为兼容路径。它们记录的是造题过程，不能作为 Query Planner 的正确动作标签，也不是当前正式 task release。

迁移完成前：

- 正式题目数据使用 `generate validated`；
- `generate entity` 仅视为历史探索/审核工作流；
- 旧 `planner_training_samples` 不得与 Query Planner 正式 rollout 混用。

### 4.6 数据集级验收尚未完成

仍需：

- 至少 20 个跨实体类型真实 seed 的 pilot；
- 高度数、多解、无解、分页、失败恢复对抗集；
- 题型分布、答案分布、模板占比和近重复报告；
- 人工盲审；
- 与 query-planner 的公开/隐藏双 loader 联调；
- Query Planner 真实执行产生的 heterogeneous action rollout。

## 5. 当前测试事实

2026-10-26 当前默认回归：

```text
851 passed, 13 deselected
```

`primekg_integration` marker 默认不随单元测试运行，不能把 deselected 写成 passed。

## 6. 下一实施顺序

1. 将 task-first 纵切纳入原 `Generation Service` 的 episode / EvidenceGap / stop reason；
2. 实现隐藏 bridge 两跳题，并继续使用完整分支合并验收；
3. 接入文献 claim 支持与固定 provenance；
4. 实现 blind verifier、近重复与数据集级 value gate；
5. 迁移旧 `PlannerTrajectory` 命名和 loader，明确历史 schema；
6. 完成 20-seed 真实 pilot 和 query-planner 双视图联调。

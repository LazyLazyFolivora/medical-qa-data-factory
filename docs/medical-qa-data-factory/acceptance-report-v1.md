# 验收报告 baseline-v1

> 验收清单：`eval/manifests/baseline-v1.json`
> manifest hash：`5f27b8d6d517754b6f5a5e033164a3972b53696c1481e182084ea7dfac26fe4d`
> 运行日期：2026-10-20
> 环境：`D:\Anaconda\envs\qa_env\python.exe`（Python 3.12.15）

## 固定项

| 项目 | 值 |
|---|---|
| PrimeKG 快照 sha256 | `7a0c995be7232bd3ae22b56545ec8ead518ba38a699a6c2416544f48fa7fb0bb` |
| PrimeKG provider snapshot id | `bd22ae20ed3037e2d4bb518904406143` |
| PrimeKG 数据库大小 | 1,754,337,280 字节（只读） |
| policy version | `entity-generation-v1` |
| validators | `validators.v2` |
| review group | `review-group-v1` |
| ranking | `ranking.v1` |
| ablation | `ablation-v1` |
| run identity | `run-identity-v1` |
| 模型 | `rule-based`（无模型调用） |
| 文献 provider | **未固定快照**，不进入正式验收 |

## 种子

| seed | native id | 类型 | 目标题数 | max hops |
|---|---|---|---:|---:|
| ibuprofen | DB01050 | drug | 3 | 3 |
| warfarin | DB00682 | drug | 3 | 3 |
| metformin | DB00331 | drug | 3 | 3 |
| aspirin | D001241 | exposure | 2 | 2 |

## 结果

**结论：passed**（环境匹配，四个种子全部完成）

| 指标 | 值 |
|---|---|
| 产出问题 | 11 |
| 审核分组 | 7（大小 2,2,2,2,1,1,1） |
| 已处置 / 待处置 | 0 / 11（保留率 0.00） |
| verified route 分布 | unknown × 11 |
| 正式文献可用 | false |
| policy cost | 0.0（rule-based 不计费） |

### 逐种子

| seed | 状态 | stop reason | 问题数 | 观测 hop | coverage gap |
|---|---|---|---:|---|---|
| ibuprofen | completed | target_reached | 3 | 1 | — |
| warfarin | completed | target_reached | 3 | 1 | — |
| metformin | completed | target_reached | 3 | 1 | — |
| aspirin | completed | target_reached | 2 | 1 | — |

rule-based 流水线是单跳基线，因此观测 hop 全为 1。

### 多跳可达性（`eval react`）

在**同一份真实 PrimeKG**上运行完整的
`observe → decide → gate → act → persist → assess` 状态机。planner 是脚本化的，
每轮动作依赖上一轮 observation，**不是模型**；本节只回答"真实图上多跳是否可达"，
不回答"模型能否找到多跳"，也不生成正式问题。

| seed | 状态 | 产出路径的 hop 数 | 最大 hop | 发现实体数 | 工具调用 |
|---|---|---|---:|---:|---:|
| ibuprofen | completed | 2, 3 | 3 | 117 | 3 |
| warfarin | completed | 2, 3 | 3 | 126 | 3 |
| metformin | completed | 2, 3 | 3 | 115 | 3 |
| aspirin | completed | 2 | 2 | 28 | 2 |

三个 `max_hops=3` 的种子都到达 2-hop 与 3-hop。验收清单为 aspirin 明确设置了
`max_hops=2`，所以它只报告到 2-hop；这是清单预算，不是探索失败或图度数结论。

## 未覆盖项

以下能力在本次验收中**没有**被验证，报告不作任何声明：

| 项 | 原因 |
|---|---|
| 文献证据与逐 hop 文本 coverage | 本地 WeKnora 服务未运行，provider 未能固定语料快照，也不满足 read_chunk 回读要求，按计划不进入正式验收 |
| `hybrid_per_hop` 问题的正式 gold | 依赖上一行 |
| 来源消融的三路 episode 与 verified route | 消融执行器已实现并有单元测试，但未在真实文献 provider 上跑通，因此 verified route 仍为 unknown |
| structured 模式的真实模型运行 | 未配置真实模型 provider；`structured` 模式在缺配置时按设计 fail fast |
| 独立答案复算 | 需要多跳 query 与真实文献，尚未在端到端流程中接��� |
| 人工处置的保留率与改排率 | 本次运行产出 11 个待处置候选，无人工决策 |

## 本次运行修掉的问题

- `eval react` 过去导入了 ReAct graph 却直接调用 frontier builder，能力标签与实际执行不符。
  现已改为真实执行完整 ReAct 状态机，并让第二轮以后动作引用上一轮 observation。
- ReAct 的已知实体、observation、动作哈希、策略与停滞计数过去保存在 builder 闭包中，
  重建 graph 后无法从 checkpoint 恢复。现已全部以可序列化 graph state 为事实来源。
- 空工具结果过去也会把 `has_evidence` 置真，从而允许无证据 finish。现仅在产生新 evidence
  或 path 时置真。
- 边预算刚好耗尽时，整轮运行作废并丢弃已采集的证据。改为：有证据时继续后续阶段，
  只有在零证据时才终止。
- 报告的 question_count 只统计种子产出，与实际工作区中的问题数可能不一致。
  改为取两者的较大值。
- 报告的 route 分布最初借用 expected_route。改为使用 verified_route——未经过消融的
  问题就是 unknown，报告不替它借一个。

## 复现

```powershell
$env:PYTHONPATH='E:\Cursor\assumption\projects\medical-qa-data-factory\src'
$env:MEDICAL_QA_PRIMEKG_PATH='E:\Cursor\assumption\datasets\PrimeKG\primekg.sqlite'

# 快照（只做一次，耗时约 2 分钟）
& 'D:\Anaconda\envs\qa_env\python.exe' -m medical_qa_data_factory kg snapshot init `
  --database $env:MEDICAL_QA_PRIMEKG_PATH

# 单跳基线验收
& 'D:\Anaconda\envs\qa_env\python.exe' -m medical_qa_data_factory eval run `
  --manifest eval/manifests/baseline-v1.json `
  --workspace data/runs/eval-baseline-v1 `
  --report data/runs/eval-baseline-v1/report.json

# 多跳可达性
& 'D:\Anaconda\envs\qa_env\python.exe' -m medical_qa_data_factory eval react `
  --manifest eval/manifests/baseline-v1.json `
  --report data/runs/eval-baseline-v1/react-report.json
```

## 测试基线

| 套件 | 结果 |
|---|---|
| `pytest -q -m "not primekg_integration"` | 771 passed, 13 deselected |
| `pytest -q -m primekg_integration -o addopts=""` | 13 passed |

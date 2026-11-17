# Qwen 推理模式对照

## 配置核查

此前真实请求均带 `chat_template_kwargs.enable_thinking=false`，输出默认预留 4096 tokens。默认 structured_output_mode 为 json_object，主 agent 单独使用 json_schema。不能把主 agent 的结构化约束推定为所有角色都有同样约束。

## 隔离重放

`data/experiments/question-first-lab/thinking-discovery-20261114-025606` 保持正式出题与读者 prompt、材料及原始 focus 不变，只在进程内开启推理并将输出预留增至 8192。正式配置未改。

- TP53：读者拒绝了样本肿瘤谱和变异比例比较，保留待独立验证的基因型/表型关联方向，并标为 needs_evidence。没有产生已证实可发布题。两次请求分别耗时约 41.7、34.8 秒，共约 9797 输出 tokens，明显超过原先非推理请求成本。
- CFTR：形成 F508del/G551D 缺陷差异、F508del 膜表面残留缺陷问题；读者拒绝缺少具体证据的修饰基因机制题。仍需检查问题是否暗示各突变仅用某一类药物，以及残留缺陷能否支持所问临床疗效外推。出题出现一次 schema 修复请求；总共三个请求，约 9185 输出 tokens。

这是单次对照的改善迹象，不证明推理模式稳定，也不能把 REVISE 数量增加当质量提升。推理内容不当作证据；判断依据仍是公开题目、原文和工具实际状态。

## 完整流程验证

随后通过进程内模型工厂覆盖运行独立 CFTR 任务，目标两题、先跑十二动作。真实生产链路仍使用正式 tools/skills/持久化；模型配置差异只存在于该实验进程，实际请求记录 enable_thinking=true 和输出预算。未更换模型、修改默认配置或重启后端。

必须检查其实际发布题、来源及修订行为，才能决定是否把推理配置接入正式流程。若仅更慢或仍产生同类错误，不应默认采用。

运行观察（`cftr-20261114-030238`）：首个 PubMed 请求因 ConnectionResetError 10054 失败，研究 agent 换查询后成功取得十篇材料，没有把网络失败当研究结束。当前进程仍在处理首个研究子任务，尚无完整主步骤终态或题目。已有五次 research agent 调用约 125 秒、三次 reading_summary 调用约 61 秒。全角色开启推理会让资料摘要也增加延迟，后续应分别评估质量判断与机械摘要是否需要同样配置；当前未据此修改正式配置。

## 完整流程终态复查（2026-11-15）

CFTR 实验已结束，11 个外层动作，两道题 PUBLISHED。发布状态不是人工质量结论。

1. “What molecular checkpoints and protein interactions determine the retention and degradation of misfolded CFTR variants in the endoplasmic reticulum?” 题目脱离论文成立，原文 p21–22 支持主要回答。答案把“at least four”写为四项，并把 F508del 的伴侣陷阱描述推广至全部错误折叠变体，仍应保留例子与一般机制的区别。
2. “Which structural interfaces and atomic interactions are disrupted by Class III gating mutations to prevent CFTR channel opening?” 原文 p34 使用 predicted、impacts，并另述 R117H gating defect；题目采用 prevent，答案把个别例子放入整个 III 类，证据强度和分类范围均需复核。不能算完全合格。

这轮确实从试验数字摘录转向领域机制问题，但仍暴露概括时丢失限定条件的问题。先前评审只确认原文出现相关实体和机制，未充分检查题干的量词、分类边界和因果强度。暂不将全角色推理配置设为默认。

后续隔离运行：`tp53-20261115-025244`，实体 TP53，目标两题，最多16外层动作，保持进程内 enable_thinking=true / output_reserve_tokens=8192。正式配置不变；保留每一步的输入输出。

# 正文读取超过预算时分页

BRCA1 `brca1-20261115-041007` 的补证 agent 成功获取三篇全文，随后反复选择总量超过 16000 字符的段落；旧工具整次拒绝。收到提示后模型甚至选择更多段落，浪费研究步骤。

修改 read_artifacts 的原文读取：返回当前预算内的完整段落前缀，用 remaining_paragraph_ids / next_read 明确列出未读部分。聚合仍限制 16000 字符，单材料每页最多四段，分隔符计入预算；所有输入段落 ID 先验证。未返回部分不会进入 returned_paragraph_ids 或阅读覆盖，不把索引当证据。

material_passages 新增可选 paginate，默认关闭。设计锚点、证据交接等调用保留严格选段行为，避免静默遗漏设计证据。只有阅读动作采用分页。

用该实跑已经保存的三篇全文做实际数据回放，结果保存在 `data/experiments/question-first-lab/reading-pagination-20261115/actual-source-replay.json`：合计返回 15362 字符，逐段偏移和原始字节对应，未读段落明确列出。代码审查通过，并按建议把续读输入写为 input_artifact_ids 和 parameters.paragraph_ids，避免模型误用输出字段。

当前仍在运行的 BRCA1 进程未加载此 Python 修改；尚未证明它在真实模型下会正确续页。需要该进程终止后从检查点运行新代码验证，不重启或打断活跃运行。原文下载成功也不等于最终答案充分。

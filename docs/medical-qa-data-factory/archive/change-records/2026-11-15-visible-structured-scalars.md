# 结构化证据的短字段直接可见

HBB `hbb-20261115-043107` 的位置题最终验证只收到 search/summary 两个 read_ref，未见 maplocation。盲答没有展开原文就答了 11p15.4，复核也不展开便报告证据缺失，主 agent 后续反复读取同一材料。字段实际在保存原文中，不是下载丢失。

EvidenceView 增加有界 visible_scalar_fields：仅递归字典，按 JSON Pointer 保留原路径和精确值；不展开数组，长字符串仍按引用读取。最多遍历 128 项、深度 8，序列化短字段预览最多 4000 字符，并计入现有初始可见预算。标签明确非完整覆盖。无问题/答案关键词筛选，不把来源摘要当新证据。

代码审查通过。原有预算是近似视图预算，预览会占用部分初始空间，后面的材料可能需要更多展开；外显子比较等逻辑未改。

真实失败输入回放 `hbb-scalar-replay-20261115`：恢复相同候选的原始锚点，调用正式 generate_with_evidence 和 FinalQuestionReview。初始视图直接含 11p15.4，模型 accept 并引用原始 anchor.design.6b797f376420507aabbbcd09。input/result 和完整请求响应均保存。不是通过修改答案或编造证据获得通过。

随后从已终止的 HBB 检查点启动自主续跑，检查主 agent 是否能从重复读取恢复到验证/发布。G6PD 原固定版本运行继续，不中途重启。单次回放通过不等于整个生成目标完成。

续跑 `hbb-20261115-044404` 已终止为 COMPLETED，仅两步：verify_answer、publish_question。HBB 位置题依据原记录 maplocation 完成发布；加上先前 Hb A 组成题，共两道不同简单知识题。比较题仍未完成，不能据此声称多路整合题能力已验证。

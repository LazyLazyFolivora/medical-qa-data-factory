# Qwen 与少检索题规则更新

默认模型确认使用 qwen-self-hosted / Qwen3.6-FP8，窗口 131072（65536×2）。DeepSeek配置保留。配置只约束客户端预算，不能修改服务端模型的实际容量；已经运行且缓存配置的进程需要重启后读取。

修改 research-first-pilot skill：从强求综合改成允许单一观察和多路整合；摘要支持必要对象、条件和结果即可提出相应范围的题，不因为单篇可答而拒绝。其他机制缺口不得阻止局部可答问题。

同步修改 research_first_pilot.py 的设计指令，删除“简单查询直接答全；可以返回无候选”的歧义，并取消“答案必须组织相互关联事实”的要求。正式 objective-question-design 与 objective-candidate-review skill 同步加入检索覆盖原则。旧接口难度字段及其数量统计未在本次迁移，不能宣称已移除整套难度路由。

验证：实际加载默认配置输出Qwen3.6-FP8 / 131072；test_research_first_pilot.py 与 test_llm_config.py 共22项通过。本次没有重跑完整生成链路，测试通过不代表出题质量已经改善。

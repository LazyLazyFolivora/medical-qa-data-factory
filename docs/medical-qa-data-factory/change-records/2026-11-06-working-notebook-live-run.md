# EGFR 工作笔记真实运行

任务：react-egfr-notebook-full-20261106；目标困难题 1 道；累计 30 个动作；产出 0；最终 RECOVERY_REQUIRED / max_agent_actions。两道候选均由主 agent 显式关闭。

## 实际路径

1. 初始 PrimeKG 参数错误，修正后取回组织定位信息，提取事实。
2. 使用不存在的关系名称筛选互作，返回空；转 PubMed，但受限环境 WinError 10013 阻断网络。笔记错误概括成 timeout。
3. 去掉关系筛选后得到 EGFR 与 ABCA1、ABL1、ABL2 等 PPI 线索。
4. 新增笔记机制发生 schema 错误：模型照抄宿主 unresolved_source_ids，严格模型拒绝额外字段。修复为忽略模型回传的两个宿主字段并重新计算，测试确认不能绕过来源检查。代码评审通过。
5. 从断点在允许联网环境继续。EGFR ABL1 kinase interaction signaling 返回 8 篇文章，agent 发现没有直接互作证据，转向其他机制材料。
6. 第一题把 Vitexin/胃癌前病变和 Aflatoxin B1/神经毒性拼成比较题。评审 REJECT：缺乏自然比较边界。主 agent 关闭后继续查。
7. 检索 EGFR 突变结构机制，读取材料并生成第二题：外显子 19 缺失 Δ746ELREA750 与外显子 20 插入 V769insASV、D770insNPG 在 αC 螺旋稳定及活性构象方面的差异。评审 KEEP。
8. 未为候选执行 request_more_evidence，就连续尝试 verify_answer。前两次 provider_result_ids 不属于候选，之后缺少成功独立证据。主 agent 将前置条件缺失误判为验证系统故障，反复尝试并最终关闭候选。实际没有进入盲答验证，也没有难度评估。

## 计数

30/30 个决策含工作笔记。动作：检索 6、读材料 6、理解事实 3、设计 2、评审 2、关闭 2、工具反馈 9。未采用并行 searches、深入正文 read_source_details 或 synthesize_evidence。

## 质量判断

第一题拒绝合理。第二题比较对象更集中，但不能认为合格困难题：证据主要为分子动力学模拟，题干却说“在非小细胞肺癌患者中”，容易把计算研究结论写成患者层面事实；术语密度并不证明 planner 难度。尚未独立验证。

笔记的可观察性有效，但研究收益没有对照证据。笔记中仍有把当前拟执行动作写成既成进展、把访问权限错误概括为超时、把验证前置条件误认为工具故障的情况。错误总结会被后续决策沿用。

## 下一步建议

优先让工作笔记区分宿主给出的实际执行状态与模型解释；工具反馈应清楚说明缺失前置条件和可补救动作，LLM 仍决定是否补证。不用“失败几次”代替“缺口是否可补齐”的判断。再处理设计/评审中计算研究与患者结论的范围偏移。此次未修改这些出题或路由策略。

完整机器可读结果：data/runs/react-egfr-notebook-full-20261106/notebook-run-report.json。原始调用与来源材料：同目录 debug_trace 和 objective_runtime/commits。

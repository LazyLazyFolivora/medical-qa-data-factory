# DeepSeek TP53 完整试跑：2026-11-12

实体 TP53，无指定论文或预置材料，12轮动作预算。复用 research_first_pilot，独立配置 deepseek-official / deepseek-flash / 1000000 context；全局默认未改变。输出在 data/experiments/tp53-deepseek-live-20261112-run2，包含 graph-leads、每步输入输出、检索返回、history、debug_trace 和 result.json。

首次 runpy 启动触发 Pydantic Literal forward-reference 错误，模型请求尚未执行。改为正常 import main 后完整运行结束，保留第一次失败目录 tp53-deepseek-live-20261112。

真实路径：图谱→自主选择DNA损伤/p53/p21检查点方向→PubMed搜索→尝试全文获取→转向NBS1相关细胞研究→多次相近检索→研究复核→设计返回candidate:null。没有进入候选评审，因为未生成候选。

结果：没有合格题目，也没有发布。检索到材料不等于读到正文；本轮全文获取未成功，判断依赖摘要。下载失败包括开放副本未找到，以及Unpaywall邮件/Elsevier密钥未配置，不应统称所有网络不可用。

主要发现：

1. DeepSeek能描述不同细胞体系和证据边界，未直接把两项实验写成因果解释。但仍围绕同一缺口反复改写查询。
2. 研究阶段最终ready_for_question=true；设计仍拒绝，并明确称单篇摘要内的观察可回答，但属于“简单捷径”。这与用户允许少检索题的目标冲突。
3. 门槛修改不能保证模型实际遵循。现有设计指令同时存在“允许单一观察”和“答案需要组织相互关联事实”等要求，skill仍多处强调综合。应统一这些语义，而不是据此次失败断言DeepSeek不能出题或上下文不足。

此结果是原试验链路的完整执行，不含之前独立 locked_need_probe 的拆分写题步骤。下一项应先修正设计阶段对单来源可答问题的误拒，再在相同材料上验证，之后再重跑实体起步流程。所有总结仍需证据核对，模型声称某终点与另一终点不等价不应未经原文检查直接成为事实标签。

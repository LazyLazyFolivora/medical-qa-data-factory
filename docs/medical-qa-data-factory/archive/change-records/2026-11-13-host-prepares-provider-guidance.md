# 由程序自动准备检索来源指导

## 修正职责

上一版把 load_provider_guide 暴露成模型动作，多了一轮不必要的模型决策。主 agent 已经选择 PubMed，研究 agent 却还要申请加载指导，也可能跳过该动作。

现在研究委托进入 ResearchSession 后，程序先读取 preferred_provider，自动准备来源指导和参数结构，再调用模型。此准备不占研究动作数，不伪造检索结果。PubMed 专门指导位于 skills/objective-research-agent/references/pubmed.md，PrimeKG 延用关系选择指导和实际关系清单。

模型工具改为 select_provider：只在首次没有指定来源、需要换来源或换 PrimeKG 实体时选择来源和研究需求。程序在收到来源选择后准备对应指导，下一轮才生成查询。已加载来源仍支持批量检索。旧 load_provider_guide 待执行动作可以恢复为来源选择；旧查询如果生成于首次准备指导之前，会保留在 superseded_pending_before_guidance 并重新生成，不直接执行。

若模型跳过来源选择、给尚未准备的来源生成了查询，程序接受来源选择并准备指导，但不执行该查询，下一轮要求根据指导重新生成参数。来源准备失败记录实际错误，允许选择其他来源或重试。

## 真实观察

实验：data/experiments/egfr-auto-provider-guidance-20261113-154804。

复用 egfr-single-step-guidance-20261113-153020 第一步真实主模型委托，preferred_provider=pubmed；新建空研究状态。生产 research_evidence 先自动加载 PubMed 指导，之后真实调用 Qwen。第一轮请求已经包含指导全文和 query schema。研究 history=[]，说明未消耗一个加载工具动作。

模型直接返回 retrieve_materials，参数为 PubMed 的 term 和 material_limit=10。为保留单步调试边界，模型响应后暂停，未实际搜索、未发布题目。全部实际请求、响应和自动加载事件在实验目录 prompt-and-output.md。

原单步任务及其错误记录保留未改写。该实验仅证明指导加载顺序正确，不证明查询质量或出题成功；委托中的预设示例和模型仍较多的查询条件需要后续观察。

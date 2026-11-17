# 后端启动与 Qwen 实跑修订

2026-11-12 未发现运行中的Python后端，启动项目web serve，workspace=data/runs/objective-web，端口8765，进程271904；health返回ok。日志在data/backend-qwen-20261112.*.log。默认Qwen3.6-FP8，131072上下文。

实体TP53，无指定论文，运行research_first_pilot至结束，目录data/experiments/tp53-qwen-skillfix-20261112。此为CLI试验链路，不是前端发布链路。检索得到LFS等材料，正文下载失败后存在反复获取同一篇的问题。最终生成单队列发病年龄比较题，模型评审promising，但题干范围泛化，人工审查未接受。

更新research-first-pilot、objective-question-design、objective-candidate-review三个skill：单队列结果必须在公开题干限定适用范围，不能仅在答案或limitations里限定；围绕有意义的关联提问，不机械罗列数值；关联不升级为因果。

对实际检索的PMID26014290摘要做三次明确反馈修订，所有请求与输出保存scope-repair、scope-repair-2、scope-repair-3。最后一次明确法国TP53胚系变异携带者队列、首次肿瘤发病终点，只比较显性负效应错义组与另两类组，不主张后两组彼此差异显著。最终答案忠实于摘要，作为单来源有边界关联题可以保留。未发布。

局限：此轮不是零人工干预成功，也未达到跨实体稳定或多路题覆盖。复核曾误放行泛化题，正文不可得时重复下载仍待处理。不能据本轮将模型评审全部视为可靠。最终题面略冗长但客观范围成立；证据是摘要，不宣称已读全文。后续需要独立新实体验证修订skill的自动效果。

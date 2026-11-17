# 研究先行出题实验

分支：experiment/research-before-question（从 main 创建；原有未提交修改保留，未清库、未提交 Git）。本次新增独立实验入口，未改变现有主 agent 或前端流程。Git 分支本身不隔离未提交文件，故实验实现仅放新文件中，不改动生产入口。

## 假设与最小流程

不是看到一条事实就挖空出题，而是先搜集、理解同一主题的相关事实，形成有条件、有出处的综合认识，再反向概括为问题。

图谱实体及多类关系样本 → LLM 自选主题、search/details/read/finish → 持续更新 findings/conditions/limitations、综合结论、联系与差异、缺口 → ready=true 时由另一次模型调用概括题目 → 独立草稿批评。

模型负责主题、查询、阅读和是否足够；宿主只提供调用、保存和轮次上限。没有固定生成配额，也不自动发布。批评是模型建议，不是独立检索验证或医学事实核定。

## 代码与运行

入口：src/medical_qa_data_factory/generation/research_first_pilot.py。
测试：tests/test_research_first_pilot.py（3 项通过：材料不足不出题、研究先于设计及审查、分页原文可进入设计）。代码审查发现并修复了引文材料被新材料挤出上下文、正文无法读取后半段的问题。来源预览前 4500 字，read 可分页；保留每来源最近 3 个阅读片段；最多显示 16 个来源，被引用来源优先。该有界上下文仍可能限制大规模综合，不代表阅读完整全文。

PowerShell：
```powershell
$env:PYTHONPATH='src'
D:/Anaconda/envs/qa_env/python.exe -X utf8 -m medical_qa_data_factory.generation.research_first_pilot --entity EGFR --output data/runs/research-first-egfr-new --max-rounds 6
```
输出目录必须不存在；每次新目录。需要模型服务和 PubMed 网络可达。max-rounds 是 search/details/read 动作共享上限；达到上限时再总结一次。

输出：graph-leads.json、research-*-input/output.json、retrieval-*.json、sources.json、read-passages.json、understanding.json、design/critique-input/output.json（若进入）、result.json、debug_trace。原始材料和每次模型输入输出均可核对。

## 真实实验

第一轮 data/runs/research-first-egfr-20261106：一次宽泛检索后模型认为足够，围绕 EGFR 生物学、患者筛选、治疗与耐药生成百科式多子题；独立批评建议 revise。仍然是摘要拼接，不是自然的必要综合。批评本身也有问题：单来源支持并不自动意味着缺乏支持。不能照单全收模型评价。

于是要求收窄具体机制或条件差异，不把综述概述视为研究完成，也不将旧文疗效结论写成当前治疗建议。

第二轮 data/runs/research-first-egfr-v2-20261106：自选 EGFR 突变 NSCLC 免疫耐药及 Amivantamab 免疫效应主题；用完 6 次搜索后 ready=false，无候选。模型识别到临床疗效结果不能直接支持所设想的 Fc/TME 机制，未强行把它们连成事实。

同时暴露：查询过长，后续聚焦未获证据的机制；没有主动使用正文/分页；最终把几次空检索误写成公开文献库缺少直接证据。这是无依据的覆盖推断，只能说本次检索未获取，不能据此声称研究不存在。总结中的具体医学陈述也仍需审核。

结论：研究→综合→出题流程已经跑通，但尚未产出合格困难题。下步应提高针对性搜索和研究主题调整能力，并用固定优质材料区分“检索不足”与“综合能力不足”，不把增加资料数量当作完成研究。

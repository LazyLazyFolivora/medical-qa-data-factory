# 实体出题方法最小实验

后续的图谱与文献三阶段实验、实际输出及问题分析见
[图谱探索出题实验](graph-question-pilot.md)。本文件保留单份资料实验的运行方法。

目的：观察从资料发现信息需求、再组织答案和自然题干的方法是否有效。
实验不进入生产主循环、独立验证或发布。当前仅支持人类基因符号，获取
一条 reviewed UniProt 记录；它是探索阶段的最小替身，不代表完整检索能力。

入口：`src/medical_qa_data_factory/question_method_pilot.py`。
方法 prompt 暂存于该模块的 METHOD，先通过实际输出打磨，再迁入正式 skill。
使用专用 question_method_pilot role，避免生产候选事实和补题规则干扰。

## 运行

在项目根目录执行 PowerShell：

```powershell
$env:PYTHONPATH='src'
$env:PYTHONIOENCODING='utf-8'
D:\Anaconda\envs\qa_env\python.exe -m medical_qa_data_factory.question_method_pilot --entity PNMA1 --output data/experiments/pnma1-new --generate
```

默认读取项目模型配置；可用 `--config` 和 `--provider` 指定。
省略 `--generate` 只取资料并保存首轮请求。输出目录必须是新目录。
复用原资料比较 prompt 时：

```powershell
D:\Anaconda\envs\qa_env\python.exe -m medical_qa_data_factory.question_method_pilot --snapshot data/experiments/pnma1-method-03/source.json --output data/experiments/pnma1-next --generate
```

两次模型调用依次产生信息需求、问题和答案。模型被要求最多提出五个方向，
最多出三题，可少出或不出；目前数量由 prompt 指导。原始资料、两轮实际请求、
模型响应及 usage 均保存，review.md 用于人工讨论。

## 第一轮真实结果（2026-11-02）

结果：`data/experiments/pnma1-method-03/review.md`。
UniProt Q8ND90 材料产生五个信息需求和三道草稿：组织表达、定位比较、抗体疾病关联。
此前两次获取因网络权限和远端 HTTP 500 失败，01/02 目录没有有效实验结果。

初步观察：

- 问题目标比泛问“何种组织特异性”明确，也没有通过挖去 PNMA1 来制造识别题。
- 资料仅说核仁定位以及肿瘤细胞中的细胞质定位；模型擅自把前者限定为正常细胞。
  下一轮应教模型从原文识别比较的两侧是否真的成立，而非靠“比较”一词制造任务。
- 定位对比在同一条注释就能读到，模型仍评为中等。需要更具体的短路径示范。
- 模型把互作实验次数解释成“关联性强”，超出直接字段含义。
- 机会阶段有家族功能等缺口，最终 follow_up 却为空；应明确保留值得深入的方向。
- 当前“意义”经常只是“原文有这个字段”，没有解释答题者能弄清楚什么。

人工审阅关注：问题是否自然、作答目标是否明确、答案是否保持材料条件、
提问是否有实际知识用途。第一轮不是正确性通过，也不是稳定性结论。
下一轮使用相同 source.json 改教学 prompt，逐题对照，不先增加自动审核层。

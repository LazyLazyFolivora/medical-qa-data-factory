# 实验资料

本目录保存独立方法实验，不作为当前生产实现的说明。

- [固定证据出题](fixed-evidence-question-mvp/README.md)：提示词、来源句子与审核样例。
- [检索需求覆盖](retrieval-coverage-mvp/README.md)：试验脚本、提示与结果分析。

运行输出仍写入被 Git 忽略的 `data/experiments/`。检索实验依赖已有实验材料与模型配置，不能视为开箱即用的离线测试。

已有 `src/medical_qa_data_factory/*_pilot.py` 模块仍有测试、导入或独立运行入口，本次没有删除或改变它们的模块路径。

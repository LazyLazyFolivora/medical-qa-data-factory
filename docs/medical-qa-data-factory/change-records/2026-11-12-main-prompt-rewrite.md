# 主prompt重写

基线：data/experiments/skills-baseline-before-rewrite-20261112。仅重写主agent指导，不改其他skill、工具实现或状态机。

## 新职责

主agent区分任务目标与当前研究目标。依据实际观察、已有支持和具体缺口选择动作；处理候选反馈；一题完成或方向失败后判断其他方向；按宿主计数、预算与剩余方向停止。

主skill保留简短的情况—行动依据表、候选处理与发布边界、工作笔记和执行事实分离。删除重复的参数教程、正文阅读细节、历史修复案例及旧的出题入口描述；参数契约由现有工具说明提供，问题发现/出题/评审仍使用各自skill。

free策略在主指令中明确为自由选择问题，不将difficulty_targets用作只准简单题或强制困难题的限制；未修改CLI默认字段或宿主计数逻辑。

## 实际加载位置同步

- skills/objective-react-agent/SKILL.md：主要职责与决策指导。
- llm/prompts.py：objective_react_agent角色的系统引导语。
- generation/react_main_agent.py：user payload中的working_notebook_contract、research_method、instruction，删去重复研究流程，统一到现有working_notebook。

响应schema、工具集合、上下文处理、研究问题发现伴随skill加载及发布约束未更改。主skill内容摘要仍自动进入版本和幂等键。

## 本轮检查范围

检查修改的Python文件语法并进行代码审查；按当前讨论范围未运行测试、未调用真实模型，尚不声称出题效果改善。后续可直接运行完整真实流程观察。

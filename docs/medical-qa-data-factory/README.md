# medical-qa-data-factory 文档索引

本目录只描述 `medical-qa-data-factory` 项目。项目的交付边界是：从一个生物医药实体出发，通过版本固定、可追溯的知识图谱、文献和结构化数据库证据构造并验证 `ValidatedTask`，再分别发布 Planner 可见问题与隐藏评估目标。

`query-planner` 如何求解问题、记录 rollout 和生成策略训练标签，不属于本目录。跨项目设计见：

- [跨项目总体架构](../query-planner-training-data-architecture.md)
- [跨项目总体验收标准](../query-planner-training-data-acceptance-criteria.md)

## 当前实施依据

开发时按以下优先级阅读：

1. [项目开发架构](development-architecture.md)：本项目要开发什么、核心契约、跨项目边界和实施顺序；
2. [项目验收标准](acceptance-criteria.md)：单题、数据集和 Query Planner 联调何时可以认为通过；
3. [生成问题的题型与语言规范](generated-question-types-and-language-criteria.md)：什么样的问题可以进入数据集；
4. [PrimeKG 集成设计](primekg-integration-design.md)：数据源接入与图查询基础；
5. [运行指南](running-guide.md)：现有命令和运行方式。

当前重建工作：

- [原架构重建实施计划](original-architecture-reconstruction-plan.md)：删除独立 MVP，在原始主干上实现真实 Validation World、任务验证和发布链路。

发生冲突时，开发架构定义产品边界和数据契约，项目验收标准定义通过条件，题型与语言规范只定义问题内容和措辞。三者均优先于历史路线和旧验收报告。

## 补充设计

- [实现需求](entity-question-generation-implementation-requirements.md)
- [当前实现差距](current-implementation-gaps.md)
- [自然问答与证据增强计划](evidence-enrichment-natural-qa-plan.md)
- [问题风格 Skills](question-style-skills.md)
- [Weknora RAG 集成](weknora-rag-integration.md)

## 历史材料

- [早期 Agent 路线图](entity-question-generation-agent-roadmap.md)
- [旧版验收报告](acceptance-report-v1.md)

历史材料用于追溯决策，不作为当前功能已经验收的证明。

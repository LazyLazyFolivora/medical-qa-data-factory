# 当前链路依赖与死代码候选清单（2026-11-13）

本次只审计并生成清单，没有删除代码，没有运行模型或测试。扫描183个Python模块，以及scripts/tests中的静态导入；另检查CLI注册、文档中的模块运行命令和动态导入关键字。

## 当前实际执行链路

- CLI：`__main__.py → cli.py → generation/objective_cli.py::run_objective_generation_command`。
- 后端新任务/恢复：`web/app.py → ObjectiveQuestionAgentLoop`，主决策均为`ReactMainAgent`。
- 调度：`generation/agent_loop.py`、`react_main_agent.py`、`react_call_contracts.py`、`react_tools.py`。
- 研究：`research_agent.py`，内部复用检索、正文获取、读取工具；问题发现：`question_discovery.py`。
- 来源：`material_retrieval_subagent.py`、`evidence_search_agent.py`、`objective_provider_adapters.py`、`structured_biomedical_providers.py`、`research_tools.py`，以及knowledge_graph、evidence_enrichment中的来源适配器。
- 事实/整合：事实理解子agent（见agent_loop调用）、`research_tools.py::synthesize_evidence`；事实提取是可选路径。
- 出题：`question_design.py`、`source_question_inputs.py`、`question_strategies.py`。
- 评审：`react_tools.py`发起模型调用，`candidate_review.py`提供响应类型等复用内容。
- 验证/难度：`answer_verification_subagent.py`、`difficulty_assessment.py`；ReAct验证通过形成发布提案，`react_tools.py`检查KEEP与提案后发布。
- 持久化/导出：`objective_question_runtime.py`、`objective_question_persistence.py`、`objective_question_contracts.py`、`objective_question_release.py`。
- 上下文/观测：`exploration_memory.py`、`react_observations.py`、`react_execution_state.py`、`material_aliases.py`、`llm/run_trace.py`及模型服务层。
- Skill：`llm/objective_skills.py`按角色读取；主控、研究、问题发现、证据整合、事实理解、出题、候选评审、最终验证、难度评估均有对应skill。skill是字符串路径加载，不能按Python导入数判死。

## 高置信运行死代码候选

这里的“运行死代码”限定为仓库当前生产代码和脚本中没有静态调用入口，不代表外部用户绝不手动import。保留测试不等于有生产用途。下面文件均没有发现自身的__main__运行入口。

| 文件 | 证据 | 建议 |
|---|---|---|
| `web/group_view.py` | 源码、脚本和测试没有导入；CandidateView、GroupView及本文件build_candidate_view没有外部引用 | 可优先移除；generation/grouping.py另有同名build_groups，不能误删 |
| `generation/ablation_runner.py` | 仅测试导入 | 历史消融实现，可归档/删除并处理对应测试 |
| `generation/path_persistence.py` | 仅测试导入 | 旧路径保存模块，确认不恢复旧流程后可移除 |
| `generation/requirement_matching.py` | 仅测试导入 | 旧需求匹配模块 |
| `generation/revision.py` | 仅测试导入 | 旧路径修订模块，不是当前design_questions修订入口 |
| `generation/shortcuts.py` | 仅测试导入 | 旧捷径分析模块 |
| `generation/subagent_dispatch.py` | 仅测试导入 | 旧分发器，当前使用agent_loop/react_tools |
| `generation/sufficiency.py` | 仅测试导入 | 旧充分性判断模块 |
| `knowledge_graph/frontier.py` | 仅测试导入 | 旧图谱多跳构造；running-guide仍称其在用，文档已与调用关系不符 |

这9个文件是本次明确列出的候选下限，不是全部可删代码的精确统计。删除后还应重新扫描它们的独占依赖。

## 混合文件中的旧实现

- `main_agent.py::StructuredMainAgent`：src/scripts中没有实例化入口，仅测试引用。整个文件不能删，当前仍导入MainAgentAction、_ROLE_BY_ACTION、_unprocessed_fact_material_ids和决策校验。
- `workflow_scheduler.py::HierarchicalMainAgent`：生产入口不用，测试仍用。整个文件不能删，当前仍引用target_question_count、is_hierarchical及常量。
- `agent_loop.py`仍保留旧realize_question、review_release以及层级流程兼容分支。它们不是当前ReAct工具集合里的动作，但同文件的公共执行与持久化功能正在使用；需按函数拆分后才能删除旧分支。
- `objective_question_realization.py`、`release_review_subagent.py`等仍被共享模块导入；不能仅凭最近一次trace没有调用就将整文件判死。

## 当前主链路之外，但仍有入口

- `generation/service.py → generation/graph.py → generation/react_graph.py`：旧服务/生成与评估入口仍引用；web/app.py也导入EntityGenerationService。因此LangGraph相关模块不是无人引用的文件。
- `evaluation/react_run.py`明确调用build_react_graph；评估入口仍存在。
- `evidence_preparation_pilot.py`没有Python静态导入者，但有__main__入口，文档给出了python -m命令。因此它是可独立运行的实验，不是已证实死代码。
- `question_pipeline_pilot.py`等pilot与旧CLI generate/validated、demo入口属于历史或辅助功能，优先归档，不和主链路混为一谈。
- `__init__.py`及`__main__.py`具有Python包/启动语义，不能按零导入计数删除。

## 全量模块清单的解释

从objective_cli与web/app两个模块追踪显式静态导入，覆盖132个模块。该数是模块级依赖范围，包含条件导入和其他路由，不能解读为132个模块都在每次生成时执行。入口cli本身、包初始化和动态资源另行保留；未统计外部依赖包的内部模块。

JSON文件逐项给出直接依赖、导入者和行号；以下表列出全部183个模块。未落在这两个入口的导入范围，只表示需要检查其他入口，不能直接判死。此审计没有执行路径覆盖率，也没有证明混合文件内部每个函数是否可达。

| 模块路径（相对项目根） | 两入口静态导入范围 | 源码/脚本导入处数 | 测试导入处数 |
|---|---|---:|---:|
| `src/medical_qa_data_factory/__init__.py` | 范围外/特殊入口 | 0 | 1 |
| `src/medical_qa_data_factory/__main__.py` | 范围外/特殊入口 | 0 | 0 |
| `src/medical_qa_data_factory/cli.py` | 范围外/特殊入口 | 1 | 9 |
| `src/medical_qa_data_factory/costing/__init__.py` | 范围外/特殊入口 | 0 | 1 |
| `src/medical_qa_data_factory/costing/config.py` | 范围外/特殊入口 | 1 | 0 |
| `src/medical_qa_data_factory/costing/ledger.py` | 范围外/特殊入口 | 1 | 0 |
| `src/medical_qa_data_factory/costing/model.py` | 范围外/特殊入口 | 1 | 0 |
| `src/medical_qa_data_factory/costing/models.py` | 范围外/特殊入口 | 4 | 0 |
| `src/medical_qa_data_factory/demo_adapters.py` | 范围外/特殊入口 | 1 | 3 |
| `src/medical_qa_data_factory/domain/__init__.py` | 在范围内 | 38 | 26 |
| `src/medical_qa_data_factory/domain/common.py` | 在范围内 | 114 | 27 |
| `src/medical_qa_data_factory/domain/evidence.py` | 在范围内 | 2 | 0 |
| `src/medical_qa_data_factory/domain/questions.py` | 在范围内 | 13 | 16 |
| `src/medical_qa_data_factory/domain/tasks.py` | 在范围内 | 1 | 0 |
| `src/medical_qa_data_factory/environment.py` | 范围外/特殊入口 | 1 | 1 |
| `src/medical_qa_data_factory/evaluation/__init__.py` | 范围外/特殊入口 | 0 | 1 |
| `src/medical_qa_data_factory/evaluation/cli.py` | 范围外/特殊入口 | 1 | 0 |
| `src/medical_qa_data_factory/evaluation/manifest.py` | 范围外/特殊入口 | 4 | 2 |
| `src/medical_qa_data_factory/evaluation/react_run.py` | 范围外/特殊入口 | 1 | 1 |
| `src/medical_qa_data_factory/evaluation/runner.py` | 范围外/特殊入口 | 2 | 3 |
| `src/medical_qa_data_factory/evidence_enrichment/__init__.py` | 范围外/特殊入口 | 0 | 0 |
| `src/medical_qa_data_factory/evidence_enrichment/contracts.py` | 在范围内 | 10 | 2 |
| `src/medical_qa_data_factory/evidence_enrichment/ctd.py` | 在范围内 | 16 | 9 |
| `src/medical_qa_data_factory/evidence_enrichment/models.py` | 在范围内 | 16 | 10 |
| `src/medical_qa_data_factory/evidence_enrichment/primekg_ctd_identity.py` | 范围外/特殊入口 | 5 | 2 |
| `src/medical_qa_data_factory/evidence_enrichment/pubmed.py` | 在范围内 | 9 | 6 |
| `src/medical_qa_data_factory/evidence_enrichment/pubmed_evidence.py` | 在范围内 | 6 | 3 |
| `src/medical_qa_data_factory/evidence_enrichment/service.py` | 在范围内 | 2 | 2 |
| `src/medical_qa_data_factory/evidence_preparation_pilot.py` | 范围外/特殊入口 | 0 | 0 |
| `src/medical_qa_data_factory/export.py` | 在范围内 | 2 | 2 |
| `src/medical_qa_data_factory/fact_pair_pilot.py` | 范围外/特殊入口 | 1 | 1 |
| `src/medical_qa_data_factory/focused_question_pilot.py` | 范围外/特殊入口 | 2 | 2 |
| `src/medical_qa_data_factory/generation/__init__.py` | 范围外/特殊入口 | 0 | 6 |
| `src/medical_qa_data_factory/generation/ablation.py` | 范围外/特殊入口 | 1 | 3 |
| `src/medical_qa_data_factory/generation/ablation_runner.py` | 范围外/特殊入口 | 0 | 1 |
| `src/medical_qa_data_factory/generation/actions.py` | 在范围内 | 9 | 5 |
| `src/medical_qa_data_factory/generation/agent_loop.py` | 在范围内 | 12 | 7 |
| `src/medical_qa_data_factory/generation/answer_verification_subagent.py` | 在范围内 | 2 | 3 |
| `src/medical_qa_data_factory/generation/attempt_ledger.py` | 范围外/特殊入口 | 1 | 3 |
| `src/medical_qa_data_factory/generation/blind_verification.py` | 范围外/特殊入口 | 1 | 2 |
| `src/medical_qa_data_factory/generation/budgets.py` | 在范围内 | 1 | 0 |
| `src/medical_qa_data_factory/generation/candidate_assessment.py` | 在范围内 | 1 | 1 |
| `src/medical_qa_data_factory/generation/candidate_review.py` | 在范围内 | 2 | 1 |
| `src/medical_qa_data_factory/generation/claims.py` | 在范围内 | 2 | 1 |
| `src/medical_qa_data_factory/generation/composition.py` | 范围外/特殊入口 | 1 | 2 |
| `src/medical_qa_data_factory/generation/contracts.py` | 在范围内 | 32 | 28 |
| `src/medical_qa_data_factory/generation/ctd_direct_evidence.py` | 范围外/特殊入口 | 1 | 1 |
| `src/medical_qa_data_factory/generation/ctd_execution.py` | 在范围内 | 1 | 1 |
| `src/medical_qa_data_factory/generation/ctd_world_evaluation.py` | 在范围内 | 6 | 3 |
| `src/medical_qa_data_factory/generation/curation.py` | 在范围内 | 3 | 2 |
| `src/medical_qa_data_factory/generation/difficulty_assessment.py` | 在范围内 | 2 | 2 |
| `src/medical_qa_data_factory/generation/entity_planner.py` | 在范围内 | 1 | 1 |
| `src/medical_qa_data_factory/generation/evidence_fusion.py` | 在范围内 | 7 | 4 |
| `src/medical_qa_data_factory/generation/evidence_needs.py` | 在范围内 | 1 | 1 |
| `src/medical_qa_data_factory/generation/evidence_provider.py` | 在范围内 | 12 | 11 |
| `src/medical_qa_data_factory/generation/evidence_search_agent.py` | 在范围内 | 3 | 1 |
| `src/medical_qa_data_factory/generation/executor.py` | 在范围内 | 6 | 3 |
| `src/medical_qa_data_factory/generation/exploration.py` | 在范围内 | 6 | 5 |
| `src/medical_qa_data_factory/generation/exploration_memory.py` | 在范围内 | 2 | 2 |
| `src/medical_qa_data_factory/generation/fact_exploration.py` | 在范围内 | 2 | 4 |
| `src/medical_qa_data_factory/generation/fact_source_policy.py` | 在范围内 | 3 | 1 |
| `src/medical_qa_data_factory/generation/fulltext_download.py` | 在范围内 | 1 | 1 |
| `src/medical_qa_data_factory/generation/graph.py` | 在范围内 | 3 | 4 |
| `src/medical_qa_data_factory/generation/grouping.py` | 在范围内 | 5 | 2 |
| `src/medical_qa_data_factory/generation/literature_execution.py` | 在范围内 | 1 | 0 |
| `src/medical_qa_data_factory/generation/logical_query.py` | 在范围内 | 12 | 11 |
| `src/medical_qa_data_factory/generation/main_agent.py` | 在范围内 | 4 | 3 |
| `src/medical_qa_data_factory/generation/material_aliases.py` | 在范围内 | 2 | 2 |
| `src/medical_qa_data_factory/generation/material_retrieval_subagent.py` | 在范围内 | 1 | 1 |
| `src/medical_qa_data_factory/generation/objective_cli.py` | 在范围内 | 1 | 0 |
| `src/medical_qa_data_factory/generation/objective_provider_adapters.py` | 在范围内 | 3 | 1 |
| `src/medical_qa_data_factory/generation/objective_question_contracts.py` | 在范围内 | 32 | 21 |
| `src/medical_qa_data_factory/generation/objective_question_persistence.py` | 在范围内 | 9 | 9 |
| `src/medical_qa_data_factory/generation/objective_question_realization.py` | 在范围内 | 2 | 2 |
| `src/medical_qa_data_factory/generation/objective_question_release.py` | 在范围内 | 2 | 1 |
| `src/medical_qa_data_factory/generation/objective_question_runtime.py` | 在范围内 | 10 | 9 |
| `src/medical_qa_data_factory/generation/path_persistence.py` | 范围外/特殊入口 | 0 | 2 |
| `src/medical_qa_data_factory/generation/planner.py` | 在范围内 | 6 | 4 |
| `src/medical_qa_data_factory/generation/planning.py` | 在范围内 | 2 | 2 |
| `src/medical_qa_data_factory/generation/primekg_ctd_pubmed.py` | 范围外/特殊入口 | 3 | 2 |
| `src/medical_qa_data_factory/generation/primekg_ctd_pubmed_task.py` | 范围外/特殊入口 | 2 | 1 |
| `src/medical_qa_data_factory/generation/probe.py` | 在范围内 | 2 | 2 |
| `src/medical_qa_data_factory/generation/prompt_writing.py` | 范围外/特殊入口 | 2 | 1 |
| `src/medical_qa_data_factory/generation/question_design.py` | 在范围内 | 2 | 3 |
| `src/medical_qa_data_factory/generation/question_discovery.py` | 在范围内 | 1 | 0 |
| `src/medical_qa_data_factory/generation/question_skills.py` | 在范围内 | 4 | 2 |
| `src/medical_qa_data_factory/generation/question_strategies.py` | 在范围内 | 5 | 1 |
| `src/medical_qa_data_factory/generation/ranking.py` | 范围外/特殊入口 | 1 | 1 |
| `src/medical_qa_data_factory/generation/react_call_contracts.py` | 在范围内 | 2 | 0 |
| `src/medical_qa_data_factory/generation/react_execution_state.py` | 在范围内 | 1 | 2 |
| `src/medical_qa_data_factory/generation/react_graph.py` | 在范围内 | 2 | 1 |
| `src/medical_qa_data_factory/generation/react_main_agent.py` | 在范围内 | 9 | 5 |
| `src/medical_qa_data_factory/generation/react_observations.py` | 在范围内 | 3 | 4 |
| `src/medical_qa_data_factory/generation/react_tools.py` | 在范围内 | 3 | 0 |
| `src/medical_qa_data_factory/generation/release_review_subagent.py` | 在范围内 | 2 | 2 |
| `src/medical_qa_data_factory/generation/repository.py` | 在范围内 | 8 | 9 |
| `src/medical_qa_data_factory/generation/requirement_matching.py` | 范围外/特殊入口 | 0 | 2 |
| `src/medical_qa_data_factory/generation/research_agent.py` | 在范围内 | 1 | 0 |
| `src/medical_qa_data_factory/generation/research_documents.py` | 在范围内 | 6 | 7 |
| `src/medical_qa_data_factory/generation/research_first_pilot.py` | 范围外/特殊入口 | 1 | 1 |
| `src/medical_qa_data_factory/generation/research_tools.py` | 在范围内 | 11 | 0 |
| `src/medical_qa_data_factory/generation/review_operations.py` | 在范围内 | 5 | 3 |
| `src/medical_qa_data_factory/generation/review_service.py` | 在范围内 | 1 | 1 |
| `src/medical_qa_data_factory/generation/revision.py` | 范围外/特殊入口 | 0 | 1 |
| `src/medical_qa_data_factory/generation/routing.py` | 在范围内 | 1 | 2 |
| `src/medical_qa_data_factory/generation/run_identity.py` | 在范围内 | 7 | 4 |
| `src/medical_qa_data_factory/generation/semantics.py` | 在范围内 | 9 | 2 |
| `src/medical_qa_data_factory/generation/service.py` | 在范围内 | 3 | 12 |
| `src/medical_qa_data_factory/generation/shortcuts.py` | 范围外/特殊入口 | 0 | 1 |
| `src/medical_qa_data_factory/generation/skills/__init__.py` | 在范围内 | 3 | 3 |
| `src/medical_qa_data_factory/generation/skills/contracts.py` | 在范围内 | 2 | 0 |
| `src/medical_qa_data_factory/generation/skills/registry.py` | 在范围内 | 1 | 0 |
| `src/medical_qa_data_factory/generation/source_question_inputs.py` | 在范围内 | 3 | 2 |
| `src/medical_qa_data_factory/generation/state.py` | 在范围内 | 3 | 2 |
| `src/medical_qa_data_factory/generation/structured_biomedical_providers.py` | 在范围内 | 2 | 2 |
| `src/medical_qa_data_factory/generation/style_evaluation.py` | 在范围内 | 1 | 1 |
| `src/medical_qa_data_factory/generation/subagent_contracts.py` | 在范围内 | 18 | 11 |
| `src/medical_qa_data_factory/generation/subagent_dispatch.py` | 范围外/特殊入口 | 0 | 1 |
| `src/medical_qa_data_factory/generation/sufficiency.py` | 范围外/特殊入口 | 0 | 1 |
| `src/medical_qa_data_factory/generation/task_candidates.py` | 在范围内 | 4 | 4 |
| `src/medical_qa_data_factory/generation/task_compiler.py` | 在范围内 | 4 | 6 |
| `src/medical_qa_data_factory/generation/task_contracts.py` | 在范围内 | 17 | 17 |
| `src/medical_qa_data_factory/generation/task_realization.py` | 在范围内 | 4 | 3 |
| `src/medical_qa_data_factory/generation/task_release.py` | 在范围内 | 5 | 5 |
| `src/medical_qa_data_factory/generation/task_service.py` | 在范围内 | 6 | 5 |
| `src/medical_qa_data_factory/generation/task_validation.py` | 在范围内 | 5 | 6 |
| `src/medical_qa_data_factory/generation/training.py` | 在范围内 | 7 | 2 |
| `src/medical_qa_data_factory/generation/training_export.py` | 范围外/特殊入口 | 1 | 1 |
| `src/medical_qa_data_factory/generation/training_repository.py` | 在范围内 | 4 | 2 |
| `src/medical_qa_data_factory/generation/verification.py` | 在范围内 | 2 | 1 |
| `src/medical_qa_data_factory/generation/verification_evidence_view.py` | 在范围内 | 4 | 1 |
| `src/medical_qa_data_factory/generation/workflow_scheduler.py` | 在范围内 | 4 | 5 |
| `src/medical_qa_data_factory/generation/world_evaluation.py` | 在范围内 | 2 | 2 |
| `src/medical_qa_data_factory/generation/writing.py` | 在范围内 | 2 | 2 |
| `src/medical_qa_data_factory/graph_question_pilot.py` | 范围外/特殊入口 | 4 | 2 |
| `src/medical_qa_data_factory/knowledge_graph/__init__.py` | 在范围内 | 11 | 13 |
| `src/medical_qa_data_factory/knowledge_graph/collector.py` | 在范围内 | 1 | 0 |
| `src/medical_qa_data_factory/knowledge_graph/contracts.py` | 在范围内 | 20 | 7 |
| `src/medical_qa_data_factory/knowledge_graph/entity_catalog.py` | 在范围内 | 1 | 0 |
| `src/medical_qa_data_factory/knowledge_graph/errors.py` | 在范围内 | 12 | 4 |
| `src/medical_qa_data_factory/knowledge_graph/frontier.py` | 范围外/特殊入口 | 0 | 2 |
| `src/medical_qa_data_factory/knowledge_graph/path_builder.py` | 在范围内 | 1 | 2 |
| `src/medical_qa_data_factory/knowledge_graph/ports.py` | 在范围内 | 9 | 1 |
| `src/medical_qa_data_factory/knowledge_graph/providers/__init__.py` | 范围外/特殊入口 | 0 | 0 |
| `src/medical_qa_data_factory/knowledge_graph/providers/primekg.py` | 在范围内 | 6 | 17 |
| `src/medical_qa_data_factory/knowledge_graph/registry.py` | 在范围内 | 4 | 3 |
| `src/medical_qa_data_factory/literature/__init__.py` | 在范围内 | 1 | 0 |
| `src/medical_qa_data_factory/literature/config.py` | 在范围内 | 2 | 0 |
| `src/medical_qa_data_factory/literature/contracts.py` | 在范围内 | 5 | 3 |
| `src/medical_qa_data_factory/literature/errors.py` | 在范围内 | 3 | 1 |
| `src/medical_qa_data_factory/literature/providers/weknora.py` | 在范围内 | 2 | 2 |
| `src/medical_qa_data_factory/literature/registry.py` | 在范围内 | 3 | 1 |
| `src/medical_qa_data_factory/llm/__init__.py` | 范围外/特殊入口 | 0 | 9 |
| `src/medical_qa_data_factory/llm/attempted_model.py` | 范围外/特殊入口 | 1 | 1 |
| `src/medical_qa_data_factory/llm/config.py` | 在范围内 | 4 | 3 |
| `src/medical_qa_data_factory/llm/context.py` | 在范围内 | 1 | 1 |
| `src/medical_qa_data_factory/llm/contracts.py` | 在范围内 | 33 | 17 |
| `src/medical_qa_data_factory/llm/errors.py` | 在范围内 | 17 | 11 |
| `src/medical_qa_data_factory/llm/fake.py` | 范围外/特殊入口 | 1 | 10 |
| `src/medical_qa_data_factory/llm/objective_skills.py` | 在范围内 | 1 | 2 |
| `src/medical_qa_data_factory/llm/ports.py` | 在范围内 | 2 | 0 |
| `src/medical_qa_data_factory/llm/prompts.py` | 在范围内 | 3 | 1 |
| `src/medical_qa_data_factory/llm/providers/openai_compatible.py` | 在范围内 | 1 | 6 |
| `src/medical_qa_data_factory/llm/registry.py` | 在范围内 | 19 | 2 |
| `src/medical_qa_data_factory/llm/run_trace.py` | 在范围内 | 7 | 0 |
| `src/medical_qa_data_factory/machine_validation.py` | 在范围内 | 5 | 5 |
| `src/medical_qa_data_factory/pipeline.py` | 范围外/特殊入口 | 1 | 3 |
| `src/medical_qa_data_factory/ports.py` | 在范围内 | 3 | 0 |
| `src/medical_qa_data_factory/question_method_pilot.py` | 范围外/特殊入口 | 5 | 0 |
| `src/medical_qa_data_factory/question_pipeline_pilot.py` | 范围外/特殊入口 | 0 | 1 |
| `src/medical_qa_data_factory/repository.py` | 在范围内 | 10 | 19 |
| `src/medical_qa_data_factory/review.py` | 在范围内 | 1 | 5 |
| `src/medical_qa_data_factory/schema_export.py` | 范围外/特殊入口 | 1 | 1 |
| `src/medical_qa_data_factory/validation.py` | 在范围内 | 3 | 1 |
| `src/medical_qa_data_factory/validation_applicability.py` | 在范围内 | 2 | 2 |
| `src/medical_qa_data_factory/validators.py` | 在范围内 | 2 | 3 |
| `src/medical_qa_data_factory/web/__init__.py` | 范围外/特殊入口 | 1 | 3 |
| `src/medical_qa_data_factory/web/app.py` | 在范围内 | 2 | 4 |
| `src/medical_qa_data_factory/web/group_view.py` | 范围外/特殊入口 | 0 | 0 |
| `src/medical_qa_data_factory/web/objective_projection.py` | 在范围内 | 1 | 1 |
| `src/medical_qa_data_factory/web/production.py` | 在范围内 | 1 | 1 |
| `src/medical_qa_data_factory/web/view_models.py` | 在范围内 | 1 | 0 |
| `src/medical_qa_data_factory/workflow.py` | 在范围内 | 2 | 1 |

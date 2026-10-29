"""Starlette application for the local evidence workbench."""
from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import re
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from ..domain import ClaimStatus, EndpointKind, EvidenceBundle, EntityRef
from ..domain.common import new_id
from ..domain import DatasetSplit
from ..domain import Route as DomainRoute
from ..knowledge_graph import (
    EntityLookup,
    EntitySearchQuery,
    GraphEntity,
    KnowledgeGraphError,
    KnowledgeGraphProvider,
    provider_from_config,
)
from ..repository import LocalArtifactRepository, RecordNotFoundError
from .view_models import (
    build_entity_workspaces,
    build_generation_activity,
    build_question_library_rows,
)
from ..review import InvalidReviewAction, review_claim, review_question
from ..export import ExportBlockedError, build_export_plan, write_dataset_export
from ..generation.repository import GenerationRepository
from ..generation.contracts import (
    DifficultyTarget,
    EntityGenerationRequest,
    EvidenceConstraints,
    EvidenceSource,
    GenerationSpec,
    LiteraturePolicy,
    SafetyBudget,
    UpstreamEntitySeed,
)
from ..generation.service import EntityGenerationService, GenerationError
from ..generation.agent_loop import ObjectiveQuestionAgentLoop
from ..generation.main_agent import StructuredMainAgent
from ..generation.objective_provider_adapters import (
    PrimeKgMaterialProvider,
    PubMedMaterialProvider,
)
from ..generation.objective_question_contracts import GenerationGoal, RunManifest
from ..generation.objective_question_persistence import (
    ObjectiveQuestionPersistence,
)
from ..generation.objective_question_release import export_objective_release
from ..generation.objective_question_runtime import (
    ObjectiveRuntimeRepository,
    RuntimeBudget,
    RunState as RuntimeRunState,
)
from ..generation.review_operations import ReviewCommand, ReviewCommandError
from ..generation.review_service import ReviewService
from ..generation.question_skills import (
    DEFAULT_QUESTION_STYLE_ID,
    available_question_skills,
    load_question_skills,
)
from ..generation.training_repository import TrainingRepository
from ..llm.errors import ModelError
from ..llm.registry import ProviderRegistry
from .group_view import build_groups

mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")


WEB_ROOT = Path(__file__).parent
templates = Jinja2Templates(directory=WEB_ROOT / "templates")
WEB_TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
AGENT_ACTION_LABELS = {
    "query_neighbors": "查询图谱邻居",
    "expand_entity": "扩展实体",
    "query_paths": "查询证据路径",
    "query_ctd": "查询 CTD 直接证据",
    "retrieve_literature": "检索文献",
    "finish": "结束探索",
}
SKILL_LABELS = {
    "ctd-direct-evidence-lookup": "Skill：CTD 直接证据检索",
    "pubmed-evidence-search": "Skill：PubMed 文献证据检索",
    "primekg-entity-expansion": "Skill：PrimeKG 实体扩展",
    "primekg-neighbor-exploration": "Skill：PrimeKG 邻域探索",
    "primekg-path-exploration": "Skill：PrimeKG 路径探索",
}

OBJECTIVE_ACTION_LABELS = {
    "retrieve_materials": "收集材料",
    "request_more_evidence": "补充证据",
    "understand_facts": "理解并提取事实",
    "design_questions": "设计候选题",
    "verify_answer": "独立验证答案",
    "realize_question": "生成最终题面",
    "review_release": "发布复核",
    "stop": "结束运行",
}

OBJECTIVE_STATE_LABELS = {
    "CREATED": "创建运行",
    "MATERIALS_COLLECTED": "材料已收集",
    "CLAIMS_PROPOSED": "候选事实已提取",
    "HYPOTHESES_PROPOSED": "候选题已提出",
    "CANDIDATES_PROCESSING": "候选处理中",
    "COMPLETED": "已完成",
    "COMPLETED_WITH_ZERO_QUESTIONS": "完成，0 题通过",
    "PROVIDER_FAILED": "数据源失败",
    "RECOVERY_REQUIRED": "需要恢复",
    "VERSION_INCOMPATIBLE": "运行版本不兼容",
    "CANCELLED": "已取消",
}


def _objective_run_root(workspace: str | Path, run_id: str) -> Path:
    return Path(workspace) / "objective_runs" / run_id


def _objective_release_root(workspace: str | Path, run_id: str) -> Path:
    return Path(workspace) / "objective_releases" / run_id


def _objective_run_payload(
    workspace: str | Path,
    run_id: str,
) -> dict[str, object]:
    run_root = _objective_run_root(workspace, run_id)
    runtime = ObjectiveRuntimeRepository(run_root, run_id=run_id)
    snapshot = runtime.load()
    release_root = _objective_release_root(workspace, run_id)
    public_questions_path = release_root / "public" / "questions.jsonl"
    questions = []
    if public_questions_path.is_file():
        questions = [
            json.loads(line)
            for line in public_questions_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    reports = [
        artifact.payload
        for artifact in snapshot.artifacts.values()
        if artifact.artifact_type == "objective_validation_report"
    ]
    steps = []
    for sequence, decision in enumerate(snapshot.decisions.values(), start=1):
        steps.append(
            {
                "sequence": sequence,
                "decision_id": decision.decision_id,
                "action": decision.action,
                "action_label": OBJECTIVE_ACTION_LABELS.get(
                    decision.action, decision.action
                ),
                "target_subagent_role": decision.target_subagent_role,
                "state_label": OBJECTIVE_STATE_LABELS.get(
                    snapshot.run_state.value, snapshot.run_state.value
                ),
                "information_gain_label": (
                    f"{round(decision.expected_information_gain * 100)}%"
                ),
                "parameters": decision.action_parameters,
                "stop_reason": decision.stop_reason,
            }
        )
    artifact_counts: dict[str, int] = {}
    for artifact in snapshot.artifacts.values():
        artifact_counts[artifact.artifact_type] = (
            artifact_counts.get(artifact.artifact_type, 0) + 1
        )
    terminal = snapshot.run_state in {
        RuntimeRunState.COMPLETED,
        RuntimeRunState.COMPLETED_WITH_ZERO_QUESTIONS,
        RuntimeRunState.PROVIDER_FAILED,
        RuntimeRunState.RECOVERY_REQUIRED,
        RuntimeRunState.VERSION_INCOMPATIBLE,
        RuntimeRunState.CANCELLED,
    }
    status = (
        "failed"
        if snapshot.run_state
        in {
            RuntimeRunState.PROVIDER_FAILED,
            RuntimeRunState.RECOVERY_REQUIRED,
            RuntimeRunState.VERSION_INCOMPATIBLE,
            RuntimeRunState.CANCELLED,
        }
        else "completed"
        if terminal
        else "running"
    )
    return {
        "task_id": run_id,
        "engine": "objective",
        "status": status,
        "status_label": OBJECTIVE_STATE_LABELS.get(
            snapshot.run_state.value, snapshot.run_state.value
        ),
        "run_state": snapshot.run_state.value,
        "state_revision": snapshot.state_revision,
        "commit_sequence": snapshot.commit_sequence,
        "stop_reason": snapshot.stop_reason,
        "target_question_count": (
            snapshot.goal.question_count_upper_bound
            if snapshot.goal is not None
            else None
        ),
        "published_count": len(questions),
        "main_agent_steps": steps,
        "planner_steps": [],
        "operations": [],
        "artifact_counts": dict(sorted(artifact_counts.items())),
        "hypotheses": {
            key: value.value for key, value in snapshot.hypotheses.items()
        },
        "validation_reports": sorted(
            reports, key=lambda item: str(item.get("report_id", ""))
        ),
        "questions": questions,
        "detail_url": (
            f"/?task={run_id}"
        ),
    }


def _objective_artifact_counts(
    artifacts: list[dict[str, object]],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for artifact in artifacts:
        artifact_type = str(artifact.get("artifact_type", "unknown"))
        counts[artifact_type] = counts.get(artifact_type, 0) + 1
    return counts


def _run_objective_web_generation(
    *,
    workspace: str | Path,
    run_id: str,
    entity_canonical_id: str,
    entity_name: str,
    entity_type: str,
    question_count: int,
    max_actions: int,
    providers: list[str],
    model: object,
    objective_providers: dict[str, object] | None = None,
) -> dict[str, object]:
    run_root = _objective_run_root(workspace, run_id)
    release_root = _objective_release_root(workspace, run_id)
    if run_root.exists() or release_root.exists():
        raise FileExistsError(f"objective run already exists: {run_id}")
    provider_map = dict(objective_providers or {})
    kg_provider = None
    if "primekg" in providers and "primekg" not in provider_map:
        kg_provider = provider_from_config("primekg")
        provider_map["primekg"] = PrimeKgMaterialProvider(kg_provider)
    if "pubmed" in providers and "pubmed" not in provider_map:
        provider_map["pubmed"] = PubMedMaterialProvider()
    selected = {name: provider_map[name] for name in providers if name in provider_map}
    if not selected:
        raise ValueError("no usable objective provider configured")
    manifest_seed = json.dumps(
        {
            "model": getattr(model, "model_config_id", "structured-model"),
            "providers": sorted(selected),
        },
        sort_keys=True,
    )
    bundle_digest = hashlib.sha256(
        manifest_seed.encode("utf-8")
    ).hexdigest()
    manifest = RunManifest(
        manifest_id=f"manifest.{run_id}",
        run_id=run_id,
        orchestrator_version="objective-agent-loop-v1",
        main_agent_version="objective-main-agent-v1",
        subagent_role_versions={"all": "v1"},
        model_versions={
            "structured": getattr(
                model,
                "model_config_id",
                "structured-model",
            )
        },
        schema_versions={"objective_question_contracts": "objective-question-v1"},
        validation_policy_version="objective-policy-v1",
        data_snapshot_or_retrieval_time_policy="pinned-per-run",
        runtime_bundle_id="objective-runtime-v1",
        runtime_bundle_digest=bundle_digest,
    )
    goal = GenerationGoal(
        run_id=run_id,
        target_entity=entity_name,
        target_entity_id=entity_canonical_id.rsplit(":", 1)[-1],
        target_entity_type=entity_type or None,
        question_count_upper_bound=question_count,
        difficulty_targets=["easy"],
        style_targets=["concise"],
        allowed_fact_types=["entity_identity", "classification", "gene_location"],
        run_budget={"max_actions": max_actions},
        run_manifest_id=manifest.manifest_id,
        runtime_bundle_id=manifest.runtime_bundle_id,
    )
    try:
        runtime = ObjectiveRuntimeRepository.create(
            run_root,
            manifest=manifest,
            goal=goal,
            budget=RuntimeBudget(
                max_cost=10.0,
                max_model_calls=max(20, max_actions * 3),
            ),
        )
        result = ObjectiveQuestionAgentLoop(
            runtime=runtime,
            persistence=ObjectiveQuestionPersistence(run_root),
            goal=goal,
            main_agent=StructuredMainAgent(
                model,
                provider_ids=sorted(selected),
            ),
            model=model,
            providers=selected,
            max_actions=max_actions,
        ).run()
        export_objective_release(result, release_root)
    finally:
        if kg_provider is not None:
            kg_provider.close()
    return _objective_run_payload(workspace, run_id)


def _seed_ref(entity: GraphEntity) -> EntityRef:
    return entity.ref(source=entity.provider_id)


def _merge_seed_entities(
    graph_entities: list[GraphEntity],
    entity_records: list,
) -> list[EntityRef]:
    """Keep graph candidates first, then retain selectable workspace history."""
    choices: dict[str, EntityRef] = {}
    for entity in graph_entities:
        ref = _seed_ref(entity)
        choices[ref.canonical_id or ref.entity_ref_id] = ref
    for record in entity_records:
        key = record.entity.canonical_id or record.entity.entity_ref_id
        choices.setdefault(key, record.entity)
    return list(choices.values())


def _structured_model_status() -> dict[str, object]:
    """Secret-free readiness check for the Agent path."""
    try:
        registry = ProviderRegistry.from_config()
        provider = registry.provider_config()
    except ModelError as exc:
        return {
            "ready": False,
            "provider_id": None,
            "model_id": None,
            "detail": exc.message,
        }
    if not provider.has_api_key():
        return {
            "ready": False,
            "provider_id": provider.provider_id,
            "model_id": provider.model_id,
            "detail": f"缺少环境变量 {provider.api_key_env}",
        }
    return {
        "ready": True,
        "provider_id": provider.provider_id,
        "model_id": provider.model_id,
        "detail": f"{provider.provider_id} / {provider.model_id}",
    }


def _upstream_seed(entity: EntityRef) -> UpstreamEntitySeed:
    """Recover the provider-native identity persisted on a linked entity."""
    provider_id = entity.namespace
    canonical_id = entity.canonical_id
    if not provider_id or not canonical_id or not entity.entity_type:
        raise ValueError("所选实体缺少 provider、canonical_id 或 entity_type")

    parts = canonical_id.split(":", 2)
    if len(parts) == 3 and parts[0] == provider_id:
        native_id = parts[2]
    elif len(parts) >= 2 and parts[0] == provider_id:
        native_id = canonical_id.split(":", 1)[1]
    else:
        native_id = canonical_id
    if not native_id:
        raise ValueError("无法从所选实体还原知识图谱原生 ID")

    return UpstreamEntitySeed(
        provider_id=provider_id,
        native_id=native_id,
        entity_type=entity.entity_type,
        canonical_name=entity.canonical_name or entity.raw_mention,
        raw_mention=entity.raw_mention,
    )


def _run_workbench_generation(
    workspace: Path,
    entity: EntityRef,
    *,
    question_count: int,
    difficulty_targets: list[DifficultyTarget],
    language: str,
    model_mode: str,
    task_id: str,
    question_style_ids: list[str],
    structured_model: object | None = None,
    evidence_enricher: object | None = None,
) -> dict:
    """Run the same generation service used by the CLI and return UI data."""
    seed = _upstream_seed(entity)
    spec = GenerationSpec(
        question_count=question_count,
        difficulty_targets=difficulty_targets,
        allowed_sources=(
            [
                EvidenceSource.KNOWLEDGE_GRAPH,
                EvidenceSource.LITERATURE,
            ]
            if model_mode == "structured"
            else [EvidenceSource.KNOWLEDGE_GRAPH]
        ),
        evidence_constraints=EvidenceConstraints(
            literature_policy=(
                LiteraturePolicy.WHEN_NEEDED
                if model_mode == "structured"
                else LiteraturePolicy.FORBIDDEN
            )
        ),
        language=language,
        question_style_ids=question_style_ids,
        variants_per_blueprint=3,
        evidence_enrichment=True,
    )
    generation_request = EntityGenerationRequest(
        task_id=task_id,
        workspace=str(workspace),
        seed=seed,
        question_count=question_count,
        generation_spec=spec,
        safety_budget=SafetyBudget(
            max_tool_calls=20,
            max_wall_time_seconds=300,
            max_edges_observed=500,
            max_model_attempts=20,
        ),
    )
    result = EntityGenerationService(
        workspace=workspace,
        provider_id=seed.provider_id,
        model_mode=model_mode,
        model=structured_model,
        evidence_enricher=evidence_enricher,
    ).run(generation_request)
    return {
        "task_id": result.task_id,
        "episode_id": result.episode_id,
        "status": result.status,
        "question_ids": result.question_ids,
        "max_questions": result.target_question_count,
        # Compatibility for clients created before max_questions became the
        # public upper-bound name.
        "target_question_count": result.target_question_count,
        "model_mode": model_mode,
    }


def _bundle_for_claim(
    repository: LocalArtifactRepository, claim_id: str
) -> EvidenceBundle | None:
    for bundle in repository.list_latest_bundles():
        if any(claim.claim_id == claim_id for claim in bundle.claims):
            return bundle
    return None


def _workspace_is_demo(repository: LocalArtifactRepository) -> bool:
    return any(
        snapshot.endpoint_kind is EndpointKind.MOCK
        for bundle in repository.list_latest_bundles()
        for snapshot in bundle.tool_snapshots
    )


def _generation_progress(
    repository: LocalArtifactRepository, task_id: str
) -> dict[str, object]:
    """Read a running generation directly from its append-only audit records."""
    try:
        task = repository.load_latest_task(task_id)
    except RecordNotFoundError:
        return {
            "task_id": task_id,
            "status": "pending",
            "status_label": "准备生成",
            "current_step": None,
            "operations": [],
            "planner_steps": [],
            "question_count": 0,
            "max_questions": None,
            "target_question_count": None,
        }
    generation_repository = GenerationRepository(repository.root)
    episodes = generation_repository.list_episodes()
    matches = [item for item in episodes if item.task_id == task_id]
    episode = max(matches, key=lambda item: item.started_at) if matches else None
    activity = build_generation_activity(str(repository.root), task)
    planner_steps = []
    if episode is not None:
        observations_by_id = {
            item.observation_id: item
            for item in generation_repository.list_observations_by_episode(
                episode.episode_id
            )
        }
        planner_steps = [
            {
                "turn": step.turn,
                "phase": 0,
                "node": "agent_explore",
                "action_type": step.selected_action.action_type,
                "skill_id": step.selected_action.skill_id,
                "action_label": (
                    SKILL_LABELS.get(
                        step.selected_action.skill_id,
                        f"Skill：{step.selected_action.skill_id}",
                    )
                    if step.selected_action.skill_id
                    else AGENT_ACTION_LABELS.get(
                        step.selected_action.action_type,
                        step.selected_action.action_type.replace("_", " "),
                    )
                ),
                "parameters": step.selected_action.parameters,
                "parameters_json": json.dumps(
                    step.selected_action.parameters,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
                "subject_entity_id": step.selected_action.parameters.get(
                    "subject_entity_id"
                ),
                "expected_information": step.selected_action.expected_information,
                "evidence_gap": step.selected_action.evidence_gap,
                "evidence_gain": step.outcome.evidence_gain,
                "status": step.execution.status,
                "status_label": {
                    "success": "查询成功",
                    "empty": "查询无结果",
                    "error": "执行失败",
                    "rejected": "策略拒绝",
                    "finish": "结束探索",
                }.get(step.execution.status, step.execution.status),
                "policy_result": step.execution.policy_result,
                "rejection_code": step.execution.rejection_code,
                "observation": (
                    {
                        "tool_name": observations_by_id[
                            step.execution.observation_id
                        ].tool_name,
                        "operation_key": observations_by_id[
                            step.execution.observation_id
                        ].operation_key,
                        "status": observations_by_id[
                            step.execution.observation_id
                        ].status,
                        "rows_examined": observations_by_id[
                            step.execution.observation_id
                        ].rows_examined,
                        "new_evidence_ids": observations_by_id[
                            step.execution.observation_id
                        ].new_evidence_ids,
                        "error_code": observations_by_id[
                            step.execution.observation_id
                        ].error_code,
                        "message": observations_by_id[
                            step.execution.observation_id
                        ].message,
                        "truncated": observations_by_id[
                            step.execution.observation_id
                        ].truncated,
                    }
                    if step.execution.observation_id
                    in observations_by_id
                    else None
                ),
                "error_code": step.execution.rejection_code,
                "error_message": step.execution.rejection_message or "",
            }
            for step in TrainingRepository(str(repository.root)).list_planner_steps()
            if step.episode_id == episode.episode_id
        ]
        for record in generation_repository.list_agent_decisions_by_episode(
            episode.episode_id
        ):
            decision = record.decision
            if decision.decision == "continue_search":
                continue
            planner_steps.append(
                {
                    "turn": record.turn,
                    "phase": 1,
                    "node": "agent_decision",
                    "action_type": decision.decision,
                    "action_label": {
                        "propose_candidate": "提出候选任务",
                        "abandon_direction": "放弃当前方向",
                        "stop": "请求停止探索",
                    }.get(decision.decision, decision.decision),
                    "parameters": {
                        "next_action": decision.next_action,
                        "selected_skill_id": decision.selected_skill_id,
                        "skill_arguments": decision.skill_arguments,
                    },
                    "parameters_json": json.dumps(
                        {
                            "next_action": decision.next_action,
                            "selected_skill_id": decision.selected_skill_id,
                            "skill_arguments": decision.skill_arguments,
                        },
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    ),
                    "skill_id": decision.selected_skill_id,
                    "subject_entity_id": None,
                    "expected_information": "; ".join(
                        decision.expected_information
                    ),
                    "evidence_gap": "; ".join(decision.unresolved_gap_ids),
                    "evidence_gain": 0,
                    "status": decision.decision,
                    "status_label": "模型决策",
                    "policy_result": "accepted",
                    "rejection_code": None,
                    "observation": None,
                    "error_code": None,
                    "error_message": "",
                }
            )
        for record in generation_repository.list_validation_feedback_by_episode(
            episode.episode_id
        ):
            feedback = record.feedback
            planner_steps.append(
                {
                    "turn": record.turn,
                    "phase": 2,
                    "node": "candidate_validation",
                    "action_type": "candidate_validation",
                    "action_label": {
                        "validated": "候选验证通过",
                        "recoverable_gap": "候选需要补证",
                        "terminal_rejection": "候选终结拒绝",
                    }[feedback.status],
                    "parameters": {
                        "candidate_id": feedback.candidate_id,
                        "evidence_gaps": [
                            item.model_dump(mode="json")
                            for item in feedback.evidence_gaps
                        ],
                        "reason_codes": feedback.reason_codes,
                    },
                    "parameters_json": json.dumps(
                        {
                            "candidate_id": feedback.candidate_id,
                            "evidence_gaps": [
                                item.model_dump(mode="json")
                                for item in feedback.evidence_gaps
                            ],
                            "reason_codes": feedback.reason_codes,
                        },
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    ),
                    "skill_id": None,
                    "subject_entity_id": feedback.candidate_id,
                    "expected_information": "; ".join(
                        [item.description for item in feedback.evidence_gaps]
                        or feedback.reason_codes
                        or ["完整世界验证通过"]
                    ),
                    "evidence_gap": "; ".join(
                        item.description for item in feedback.evidence_gaps
                    ),
                    "evidence_gain": 0,
                    "status": feedback.status,
                    "status_label": feedback.status,
                    "policy_result": (
                        "accepted"
                        if feedback.status == "validated"
                        else "rejected"
                    ),
                    "rejection_code": (
                        feedback.reason_codes[0]
                        if feedback.reason_codes
                        else None
                    ),
                    "observation": None,
                    "error_code": (
                        feedback.reason_codes[0]
                        if feedback.reason_codes
                        else None
                    ),
                    "error_message": feedback.message or "",
                }
            )
        planner_steps.sort(
            key=lambda item: (int(item["turn"]), int(item.get("phase", 0)))
        )
    operations = []
    current_step = None
    if activity is not None:
        operations = [
            {
                "sequence": item.sequence,
                "node": item.node,
                "label": item.label,
                "description": item.description,
                "status": item.status,
                "status_label": item.status_label,
                "elapsed_label": item.elapsed_label,
                "records_returned": item.records_returned,
                "tool_call_count": item.tool_call_count,
                "error_code": item.error_code,
                "error_message": item.error_message,
                "retryable": item.retryable,
                "tool_calls": [
                    {
                        "tool_name": call.tool_name,
                        "operation_key": call.operation_key,
                        "status": call.status,
                        "status_label": call.status_label,
                        "records_returned": call.records_returned,
                        "elapsed_label": call.elapsed_label,
                        "error_code": call.error_code,
                        "message": call.message,
                    }
                    for call in item.tool_calls
                ],
            }
            for item in activity.operations
        ]
        current_step = next(
            (item for item in reversed(operations) if item["status"] == "started"),
            operations[-1] if operations else None,
        )
    logical_question_ids: set[str] = set()
    if episode is not None:
        for question_id in episode.question_ids:
            try:
                logical_question_ids.add(
                    repository.load_latest_question(
                        question_id
                    ).logical_question_id
                )
            except RecordNotFoundError:
                continue
    question_count = len(logical_question_ids)
    status = (
        episode.state.value if episode is not None else task.status.value
    )
    return {
        "task_id": task_id,
        "status": status,
        "status_label": {
            "pending": "准备生成",
            "running": "运行中",
            "completed": "已完成",
            "partial": "部分完成",
            "failed": "失败",
        }.get(status, status),
        "current_step": current_step,
        "operations": operations,
        "planner_steps": planner_steps,
        "question_count": question_count,
        "max_questions": task.target_question_count,
        "target_question_count": task.target_question_count,
    }


def _base_context(request: Request, repository: LocalArtifactRepository) -> dict:
    return {
        "request": request,
        "environment": "DEMO" if _workspace_is_demo(repository) else "LOCAL",
        "workspace": str(repository.root),
    }


def _parse_route(value: object) -> DomainRoute | None:
    if value in (None, ""):
        return None
    try:
        return DomainRoute(str(value))
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"unknown route: {value}"
        ) from exc


def _parse_split(value: object) -> DatasetSplit | None:
    if value in (None, ""):
        return None
    try:
        return DatasetSplit(str(value))
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"unknown dataset split: {value}"
        ) from exc


def _review_context(
    request: Request, repository: LocalArtifactRepository, question_id: str
) -> dict:
    try:
        question = repository.load_latest_question(question_id)
        bundle = repository.load_latest_bundle(question.bundle_ids[0])
    except (RecordNotFoundError, IndexError) as exc:
        raise HTTPException(status_code=404) from exc
    context = _base_context(request, repository)
    context.update(
        question=question,
        bundle=bundle,
        seed_entity=bundle.seed_entities[0],
        question_versions=repository.list_versions("questions", question_id),
        active_nav="review",
    )
    return context


def create_app(
    workspace: str | Path,
    *,
    entity_provider: KnowledgeGraphProvider | None = None,
    structured_model: object | None = None,
    evidence_enricher: object | None = None,
    objective_providers: dict[str, object] | None = None,
) -> Starlette:
    repository = LocalArtifactRepository(workspace)
    generation_lock = asyncio.Lock()
    entity_search_lock = asyncio.Lock()
    model_status = (
        {
            "ready": True,
            "provider_id": getattr(
                structured_model, "model_config_id", "injected-model"
            ),
            "model_id": getattr(structured_model, "model_config_id", "injected-model"),
            "detail": "已注入测试模型",
        }
        if structured_model is not None
        else _structured_model_status()
    )
    default_model_mode = "structured" if model_status["ready"] else "rule-based"
    provider_instance = entity_provider
    provider_error: str | None = None

    def get_entity_provider() -> KnowledgeGraphProvider:
        nonlocal provider_instance, provider_error
        if provider_instance is not None:
            return provider_instance
        try:
            provider_instance = provider_from_config()
            provider_error = None
        except KnowledgeGraphError as exc:
            provider_error = exc.message
            raise
        return provider_instance

    def search_graph_entities(
        *, query: str = "", limit: int = 10
    ) -> tuple[list[GraphEntity], bool]:
        nonlocal provider_error
        try:
            result = get_entity_provider().search_entities(
                EntitySearchQuery(query=query, limit=limit)
            )
            provider_error = None
            return result.entities, result.truncated
        except KnowledgeGraphError as exc:
            provider_error = exc.message
            return [], False

    def resolve_graph_entity(canonical_id: str) -> EntityRef:
        provider = get_entity_provider()
        parts = canonical_id.split(":", 2)
        if len(parts) != 3 or parts[0] != provider.provider_id:
            raise ValueError("所选实体不属于当前 PrimeKG provider")
        entity_type, native_id = parts[1], parts[2]
        resolution = provider.resolve_entity(
            EntityLookup(
                mention=native_id,
                native_id=native_id,
                entity_types=[entity_type],
                limit=20,
            )
        )
        matches = [
            item.entity
            for item in resolution.candidates
            if item.entity.canonical_id == canonical_id
        ]
        if len(matches) != 1:
            raise ValueError("无法在 PrimeKG 中唯一确认所选实体")
        return _seed_ref(matches[0])

    def close_entity_provider() -> None:
        if provider_instance is not None:
            provider_instance.close()

    @asynccontextmanager
    async def lifespan(_: Starlette):
        try:
            yield
        finally:
            close_entity_provider()

    async def generation_workspace(request: Request) -> JSONResponse:
        entity_records = build_entity_workspaces(repository)
        graph_entities, _ = await run_in_threadpool(
            search_graph_entities, query="", limit=10
        )
        seed_entities = _merge_seed_entities(graph_entities, entity_records)
        task_id = request.query_params.get("task")
        objective_payload = None
        objective_tasks = []
        objective_runs_root = Path(workspace) / "objective_runs"
        if objective_runs_root.is_dir():
            for run_dir in sorted(
                (item for item in objective_runs_root.iterdir() if item.is_dir()),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            ):
                try:
                    run_payload = _objective_run_payload(
                        workspace, run_dir.name
                    )
                except Exception:
                    continue
                objective_tasks.append(
                    {
                        "task_id": run_dir.name,
                        "engine": "objective",
                        "status": run_payload["status"],
                        "status_label": run_payload["status_label"],
                        "target_question_count": run_payload[
                            "target_question_count"
                        ],
                        "question_count": run_payload["published_count"],
                    }
                )
                if run_dir.name == task_id:
                    objective_payload = run_payload
        requested_task = None
        if task_id and objective_payload is None:
            try:
                requested_task = repository.load_latest_task(task_id)
            except RecordNotFoundError as exc:
                raise HTTPException(status_code=404) from exc
        selected_id = (
            (requested_task.seed_entities[0].canonical_id or
             requested_task.seed_entities[0].entity_ref_id)
            if requested_task is not None
            else request.query_params.get("entity")
        )
        default_entity = (
            entity_records[0].entity
            if entity_records
            else seed_entities[0] if seed_entities else None
        )
        selected_entity = next(
            (
                entity
                for entity in seed_entities
                if selected_id
                in {entity.entity_ref_id, entity.canonical_id}
            ),
            default_entity,
        )
        selected_record = next(
            (
                record
                for record in entity_records
                if selected_entity is not None
                and (
                    (
                        selected_entity.canonical_id
                        and record.entity.canonical_id
                        == selected_entity.canonical_id
                    )
                    or record.entity.entity_ref_id
                    == selected_entity.entity_ref_id
                )
            ),
            None,
        )
        record_tasks = (
            selected_record.tasks
            if selected_record is not None and selected_record.tasks
            else []
        )
        selected_task = next(
            (task for task in record_tasks if task.task_id == task_id),
            record_tasks[0] if record_tasks else None,
        )
        if objective_payload is not None:
            selected_task = None
        all_question_rows = build_question_library_rows(repository)
        question_rows = [
            row
            for row in all_question_rows
            if selected_task is not None and row.task_id == selected_task.task_id
        ]
        progress = (
            _generation_progress(repository, selected_task.task_id)
            if selected_task is not None
            else {
                "operations": [],
                "planner_steps": [],
                "question_count": 0,
                "max_questions": None,
                "target_question_count": None,
            }
        )
        question_counts: dict[str, int] = {}
        for row in all_question_rows:
            if row.task_id is not None:
                question_counts[row.task_id] = (
                    question_counts.get(row.task_id, 0) + 1
                )

        task_payload = objective_payload
        if selected_task is not None and objective_payload is None:
            task_payload = {
                "task_id": selected_task.task_id,
                "status": (
                    progress.get("status") or selected_task.status.value
                ),
                "status_label": (
                    progress.get("status_label")
                    or selected_task.status.value
                ),
                "current_stage": selected_task.current_stage,
                "target_question_count": selected_task.target_question_count,
                "question_count": question_counts.get(selected_task.task_id, 0),
                "stop_reason": progress.get("stop_reason"),
                "detail_url": str(
                    request.url_for(
                        "task_detail", task_id=selected_task.task_id
                    )
                ),
                "operations": progress.get("operations", []),
                "planner_steps": progress.get("planner_steps", []),
            }

        return JSONResponse(
            {
                "entities": [
                    {
                        "value": (
                            entity.canonical_id or entity.entity_ref_id
                        ),
                        "entity_ref_id": entity.entity_ref_id,
                        "canonical_id": entity.canonical_id,
                        "name": entity.raw_mention,
                        "canonical_name": entity.canonical_name,
                        "entity_type": entity.entity_type,
                    }
                    for entity in seed_entities
                ],
                "selected_entity_id": (
                    selected_entity.canonical_id or selected_entity.entity_ref_id
                    if selected_entity is not None
                    else None
                ),
                "tasks": [
                    *objective_tasks,
                    *[
                    {
                        "task_id": task.task_id,
                        "status": task.status.value,
                        "status_label": {
                            "pending": "准备生成",
                            "running": "运行中",
                            "completed": "已完成",
                            "partial": "部分完成",
                            "failed": "失败",
                        }.get(task.status.value, task.status.value),
                        "target_question_count": task.target_question_count,
                        "question_count": question_counts.get(task.task_id, 0),
                        "updated_at": task.updated_at.isoformat(),
                    }
                    for task in record_tasks
                    ],
                ],
                "selected_task": task_payload,
                "questions": (
                    objective_payload["questions"]
                    if objective_payload is not None
                    else [
                    {
                        "question_id": row.question.question_id,
                        "text": row.question.canonical_question,
                        "answer": row.question.gold_answer.short_answer,
                        "explanation": row.question.gold_answer.explanation,
                        "difficulty_label": row.difficulty_label,
                        "source_label": row.source_label,
                        "style_label": row.style_label,
                        "style_score_label": row.style_score_label,
                        "validation_label": row.validation_label,
                        "review_url": str(
                            request.url_for(
                                "review",
                                question_id=row.question.question_id,
                            )
                        ),
                    }
                    for row in question_rows
                    ]
                ),
                "model": {
                    "ready": model_status["ready"],
                    "detail": model_status["detail"],
                    "default_mode": default_model_mode,
                },
                "question_styles": [
                    {
                        "skill_id": skill.skill_id,
                        "display_name": skill.display_name,
                    }
                    for skill in available_question_skills()
                ],
                "entity_error": provider_error,
            }
        )

    async def generation(request: Request) -> HTMLResponse:
        context = _base_context(request, repository)
        context.update(
            active_nav="generate",
        )
        return templates.TemplateResponse(request, "generation.html", context)

    async def questions(request: Request) -> HTMLResponse:
        rows = build_question_library_rows(repository)
        counts = {
            "all": len(rows),
            "draft": sum(
                row.question.validation_level.value == "draft" for row in rows
            ),
            "machine_validated": sum(
                row.question.validation_level.value == "machine_validated"
                for row in rows
            ),
            "human_confirmed": sum(
                row.question.validation_level.value == "human_confirmed"
                for row in rows
            ),
            "rejected": sum(
                row.question.validation_level.value == "rejected" for row in rows
            ),
        }
        context = _base_context(request, repository)
        context.update(
            question_rows=rows,
            counts=counts,
            active_nav="questions",
        )
        return templates.TemplateResponse(request, "questions.html", context)

    async def entities(request: Request) -> HTMLResponse:
        entity_records = build_entity_workspaces(repository)
        counts = {
            "entities": len(entity_records),
            "questions": sum(len(record.questions) for record in entity_records),
            "review": sum(
                question.validation_level.value == "draft"
                for record in entity_records
                for question in record.questions
            ),
            "issues": sum(record.issue_count for record in entity_records),
        }
        context = _base_context(request, repository)
        context.update(
            entity_records=entity_records,
            counts=counts,
            active_nav="entities",
        )
        return templates.TemplateResponse(request, "entities.html", context)

    async def search_entities_api(request: Request) -> JSONResponse:
        query = request.query_params.get("q", "").strip()
        if len(query) > 200:
            return JSONResponse(
                {"error": "搜索内容不能超过 200 个字符"}, status_code=400
            )
        try:
            limit = int(request.query_params.get("limit", "10"))
            if not 1 <= limit <= 20:
                raise ValueError
        except ValueError:
            return JSONResponse(
                {"error": "返回数量必须在 1 到 20 之间"}, status_code=400
            )
        try:
            async with entity_search_lock:
                result = await run_in_threadpool(
                    get_entity_provider().search_entities,
                    EntitySearchQuery(query=query, limit=limit),
                )
        except KnowledgeGraphError as exc:
            return JSONResponse(
                {"error": exc.message, "code": exc.code},
                status_code=503 if exc.retryable else 422,
            )
        return JSONResponse(
            {
                "provider": result.snapshot.provider_id,
                "query": query,
                "entities": [
                    {
                        "name": entity.name,
                        "native_id": entity.native_id,
                        "entity_type": entity.entity_type,
                        "canonical_id": entity.canonical_id,
                        "entity_ref_id": entity.ref().entity_ref_id,
                    }
                    for entity in result.entities
                ],
                "truncated": result.truncated,
                "elapsed_ms": result.trace.elapsed_ms,
            }
        )

    async def start_generation(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except ValueError:
            return JSONResponse({"error": "请求内容必须是 JSON"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "请求内容必须是 JSON 对象"}, status_code=400)

        task_id = str(payload.get("run_id") or new_id("web-generation")).strip()
        if not WEB_TASK_ID_PATTERN.fullmatch(task_id):
            return JSONResponse({"error": "run_id 格式无效"}, status_code=400)
        entity_ref_id = str(payload.get("entity_ref_id") or "").strip()
        entity_canonical_id = str(
            payload.get("entity_canonical_id") or ""
        ).strip()
        record = next(
            (
                item
                for item in build_entity_workspaces(repository)
                if (
                    entity_ref_id
                    and item.entity.entity_ref_id == entity_ref_id
                )
                or (
                    entity_canonical_id
                    and item.entity.canonical_id == entity_canonical_id
                )
            ),
            None,
        )
        if record is not None:
            selected_entity = record.entity
        elif entity_canonical_id:
            try:
                selected_entity = await run_in_threadpool(
                    resolve_graph_entity, entity_canonical_id
                )
            except KnowledgeGraphError as exc:
                return JSONResponse(
                    {"error": exc.message, "code": exc.code},
                    status_code=503 if exc.retryable else 422,
                )
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=422)
        else:
            return JSONResponse({"error": "找不到所选种子实体"}, status_code=404)

        try:
            question_count = int(
                payload.get(
                    "max_questions",
                    payload.get("question_count", 3),
                )
            )
            if not 1 <= question_count <= 20:
                raise ValueError("最多生成题数必须在 1 到 20 之间")
            raw_difficulties = payload.get(
                "difficulty_targets", ["easy", "medium"]
            )
            if not isinstance(raw_difficulties, list) or not raw_difficulties:
                raise ValueError("至少选择一个难度")
            difficulty_targets = [
                DifficultyTarget(value) for value in raw_difficulties
            ]
            if len(set(difficulty_targets)) != len(difficulty_targets):
                raise ValueError("难度选项不能重复")
            language = str(payload.get("language") or "zh-CN")
            model_mode = str(
                payload.get("model_mode") or default_model_mode
            ).strip()
            if model_mode not in {"rule-based", "structured"}:
                raise ValueError("不支持的 Web 生成模式")
            raw_style_ids = payload.get(
                "question_style_ids", [DEFAULT_QUESTION_STYLE_ID]
            )
            if not isinstance(raw_style_ids, list) or not raw_style_ids:
                raise ValueError("至少选择一个出题风格")
            question_style_ids = [str(item).strip() for item in raw_style_ids]
            if len(question_style_ids) > 3:
                raise ValueError("一次最多选择三个出题风格")
            load_question_skills(question_style_ids)
            if model_mode == "structured" and not model_status["ready"]:
                return JSONResponse(
                    {
                        "error": f"Agent 模式未就绪：{model_status['detail']}",
                        "code": "model_config_error",
                        "retryable": False,
                    },
                    status_code=422,
                )
        except (TypeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        if generation_lock.locked():
            return JSONResponse(
                {"error": "已有生成任务正在运行，请等待它完成"},
                status_code=409,
            )

        try:
            async with generation_lock:
                result = await run_in_threadpool(
                    _run_workbench_generation,
                    repository.root,
                    selected_entity,
                    question_count=question_count,
                    difficulty_targets=difficulty_targets,
                    language=language,
                        model_mode=model_mode,
                        task_id=task_id,
                        question_style_ids=question_style_ids,
                        structured_model=structured_model,
                        evidence_enricher=evidence_enricher,
                )
        except GenerationError as exc:
            status_code = 503 if exc.retryable else 422
            return JSONResponse(
                {
                    "error": exc.message,
                    "code": exc.code,
                    "retryable": exc.retryable,
                },
                status_code=status_code,
            )
        except Exception as exc:
            return JSONResponse(
                {"error": str(exc), "code": "internal_error"},
                status_code=500,
            )
        return JSONResponse(result)

    async def start_objective_generation(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except ValueError:
            return JSONResponse({"error": "请求内容必须是 JSON"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "请求内容必须是 JSON 对象"}, status_code=400)
        run_id = str(payload.get("run_id") or new_id("objective-web")).strip()
        if not WEB_TASK_ID_PATTERN.fullmatch(run_id):
            return JSONResponse({"error": "run_id 格式无效"}, status_code=400)
        canonical_id = str(payload.get("entity_canonical_id") or "").strip()
        parts = canonical_id.split(":", 2)
        if len(parts) != 3 or not all(parts):
            return JSONResponse(
                {
                    "error": (
                        "entity_canonical_id must be a provider:type:id "
                        "canonical_id"
                    )
                },
                status_code=400,
            )
        entity_name = str(payload.get("entity_name") or "").strip()
        if not entity_name:
            return JSONResponse({"error": "缺少实体名称"}, status_code=400)
        entity_type = str(
            payload.get("entity_type") or parts[1]
        ).strip()
        if objective_providers is None:
            try:
                resolved_entity = await run_in_threadpool(
                    resolve_graph_entity, canonical_id
                )
            except KnowledgeGraphError as exc:
                return JSONResponse(
                    {"error": exc.message, "code": exc.code},
                    status_code=503 if exc.retryable else 422,
                )
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=422)
            entity_name = resolved_entity.canonical_name or entity_name
            entity_type = resolved_entity.entity_type or entity_type
        try:
            question_count = int(payload.get("question_count", 1))
            max_actions = int(payload.get("max_actions", 12))
            if not 1 <= question_count <= 20:
                raise ValueError("题数上限必须在 1 到 20 之间")
            if not 2 <= max_actions <= 40:
                raise ValueError("主 Agent 最大动作数必须在 2 到 40 之间")
        except (TypeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        requested_providers = payload.get("providers", ["primekg", "pubmed"])
        if not isinstance(requested_providers, list) or not requested_providers:
            return JSONResponse({"error": "至少选择一个材料 provider"}, status_code=400)
        providers = list(dict.fromkeys(str(item) for item in requested_providers))
        if not set(providers) <= {"primekg", "pubmed"}:
            return JSONResponse(
                {"error": f"不支持的材料 provider：{providers}"},
                status_code=400,
            )
        if not model_status["ready"] and structured_model is None:
            return JSONResponse(
                {
                    "error": f"主 Agent 模型未就绪：{model_status['detail']}",
                    "code": "model_config_error",
                },
                status_code=422,
            )
        if generation_lock.locked():
            return JSONResponse(
                {"error": "已有生成任务正在运行，请等待它完成"},
                status_code=409,
            )
        try:
            model = structured_model
            if model is None:
                from ..llm.registry import build_model_port

                model = build_model_port()
            async with generation_lock:
                result = await run_in_threadpool(
                    _run_objective_web_generation,
                    workspace=repository.root,
                    run_id=run_id,
                    entity_canonical_id=canonical_id,
                    entity_name=entity_name,
                    entity_type=entity_type,
                    question_count=question_count,
                    max_actions=max_actions,
                    providers=providers,
                    model=model,
                    objective_providers=objective_providers,
                )
        except FileExistsError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except Exception as exc:
            return JSONResponse(
                {
                    "error": str(exc),
                    "code": str(
                        getattr(exc, "code", "objective_generation_error")
                    ),
                },
                status_code=500,
            )
        return JSONResponse(result)

    async def objective_progress(request: Request) -> JSONResponse:
        run_id = request.path_params["run_id"]
        if not WEB_TASK_ID_PATTERN.fullmatch(run_id):
            return JSONResponse({"error": "任务 ID 格式无效"}, status_code=400)
        try:
            payload = await run_in_threadpool(
                _objective_run_payload,
                repository.root,
                run_id,
            )
        except FileNotFoundError:
            return JSONResponse(
                {
                    "task_id": run_id,
                    "engine": "objective",
                    "status": "pending",
                    "status_label": "准备启动主 Agent",
                    "run_state": "CREATED",
                    "published_count": 0,
                    "artifact_counts": {},
                    "main_agent_steps": [],
                    "questions": [],
                }
            )
        except Exception as exc:
            return JSONResponse(
                {"error": str(exc), "code": "objective_progress_error"},
                status_code=500,
            )
        return JSONResponse(payload)

    async def generation_progress(request: Request) -> JSONResponse:
        task_id = request.path_params["task_id"]
        if not WEB_TASK_ID_PATTERN.fullmatch(task_id):
            return JSONResponse({"error": "任务 ID 格式无效"}, status_code=400)
        return JSONResponse(
            await run_in_threadpool(_generation_progress, repository, task_id)
        )

    async def entity_detail(request: Request) -> HTMLResponse:
        entity_ref_id = request.path_params["entity_ref_id"]
        record = next(
            (
                item
                for item in build_entity_workspaces(repository)
                if item.entity.entity_ref_id == entity_ref_id
            ),
            None,
        )
        if record is None:
            raise HTTPException(status_code=404)
        context = _base_context(request, repository)
        context.update(record=record, active_nav="entities")
        return templates.TemplateResponse(request, "entity_detail.html", context)

    async def runs(request: Request) -> HTMLResponse:
        tasks = repository.list_latest_tasks()
        question_rows = build_question_library_rows(repository)
        question_counts: dict[str, int] = {}
        for row in question_rows:
            if row.task_id is not None:
                question_counts[row.task_id] = (
                    question_counts.get(row.task_id, 0) + 1
                )
        bundles_by_task: dict[str, int] = {}
        for bundle in repository.list_latest_bundles():
            bundles_by_task[bundle.task_id] = (
                bundles_by_task.get(bundle.task_id, 0) + len(bundle.kg_evidence)
            )
        latest_episode_by_task = {}
        for episode in GenerationRepository(str(repository.root)).list_episodes():
            current = latest_episode_by_task.get(episode.task_id)
            if current is None or episode.started_at > current.started_at:
                latest_episode_by_task[episode.task_id] = episode
        task_rows = [
            {
                "task": task,
                "question_count": question_counts.get(task.task_id, 0),
                "evidence_count": bundles_by_task.get(task.task_id, 0),
                "stop_reason": (
                    latest_episode_by_task[task.task_id].stop_reason.value
                    if task.task_id in latest_episode_by_task
                    and latest_episode_by_task[task.task_id].stop_reason
                    else None
                ),
            }
            for task in tasks
        ]
        counts = {
            "all": len(tasks),
            "running": sum(task.status.value == "running" for task in tasks),
            "attention": sum(
                task.status.value in {"needs_input", "partial", "failed"}
                for task in tasks
            ),
            "completed": sum(task.status.value == "completed" for task in tasks),
        }
        context = _base_context(request, repository)
        context.update(
            tasks=tasks,
            task_rows=task_rows,
            counts=counts,
            active_nav="tasks",
        )
        return templates.TemplateResponse(request, "queue.html", context)

    async def task_detail(request: Request) -> HTMLResponse:
        task_id = request.path_params["task_id"]
        try:
            task = repository.load_latest_task(task_id)
        except RecordNotFoundError as exc:
            raise HTTPException(status_code=404) from exc

        bundles = []
        for artifact_id in task.artifact_ids:
            try:
                bundles.append(repository.load_latest_bundle(artifact_id))
                continue
            except RecordNotFoundError:
                pass
        question_rows = [
            row
            for row in build_question_library_rows(repository)
            if row.task_id == task_id
        ]
        activity = build_generation_activity(str(repository.root), task)
        progress = _generation_progress(repository, task_id)
        context = _base_context(request, repository)
        context.update(
            task=task,
            bundles=bundles,
            question_rows=question_rows,
            planner_steps=progress.get("planner_steps", []),
            evidence_count=sum(
                len(bundle.kg_evidence) + len(bundle.literature_evidence)
                for bundle in bundles
            ),
            stop_reason=activity.stop_reason if activity is not None else None,
            activity=activity,
            version_count=len(repository.list_versions("tasks", task_id)),
            active_nav="tasks",
        )
        return templates.TemplateResponse(request, "task_detail.html", context)

    async def review(request: Request) -> HTMLResponse:
        question_id = request.path_params["question_id"]
        context = _review_context(request, repository, question_id)
        return templates.TemplateResponse(request, "review.html", context)

    async def review_question_action(request: Request) -> JSONResponse:
        question_id = request.path_params["question_id"]
        payload = await request.json()
        action = str(payload.get("action", ""))
        summary = str(payload.get("summary") or "").strip()
        if not summary:
            return JSONResponse({"error": "summary is required"}, status_code=400)
        try:
            question = repository.load_latest_question(question_id)
            updated = review_question(
                question,
                action,
                summary=summary,
                changed_fields=set(payload.get("changed_fields") or []),
                verified_route=_parse_route(payload.get("verified_route")),
                gold_short_answer=payload.get("gold_short_answer"),
                dataset_split=_parse_split(payload.get("dataset_split")),
                canonical_question=payload.get("canonical_question"),
                question_variants=payload.get("question_variants"),
                gold_answer=payload.get("gold_answer"),
                evidence_requirements=payload.get("evidence_requirements"),
                taxonomy=payload.get("taxonomy"),
                query_intent=payload.get("query_intent"),
            )
            repository.save_question(updated)
        except RecordNotFoundError as exc:
            raise HTTPException(status_code=404) from exc
        except (InvalidReviewAction, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(
            {
                "question_id": updated.question_id,
                "question_version": updated.question_version,
                "validation_level": updated.validation_level.value,
                "change_summary": updated.changes[-1].summary,
            }
        )

    async def groups(request: Request) -> HTMLResponse:
        """The grouped review page: candidates that ask the same question."""
        group_records = build_groups(repository)
        counts = {
            "groups": len(group_records),
            "candidates": sum(record.size for record in group_records),
            "pending_groups": sum(1 for record in group_records if record.has_pending),
            "review": sum(
                1
                for record in group_records
                for candidate in record.candidates
                if candidate.disposition.value == "pending"
            ),
            "issues": sum(
                len(candidate.failed_checks)
                for record in group_records
                for candidate in record.candidates
            ),
        }
        context = _base_context(request, repository)
        context.update(
            group_records=group_records,
            counts=counts,
            active_nav="review",
        )
        return templates.TemplateResponse(request, "groups.html", context)

    async def group_action(request: Request) -> JSONResponse:
        """Apply one review command inside a group."""
        group_key = request.path_params["review_group_key"]
        payload = await request.json()
        action = str(payload.get("action", ""))
        subject_id = str(payload.get("question_id", ""))
        target_id = payload.get("target_question_id")
        try:
            command = ReviewCommand(action)
        except ValueError:
            return JSONResponse(
                {"error": f"unknown review action: {action}"}, status_code=400
            )
        try:
            subject = repository.load_latest_question(subject_id)
            target = (
                repository.load_latest_question(str(target_id))
                if target_id
                else None
            )
        except RecordNotFoundError as exc:
            raise HTTPException(status_code=404) from exc
        service = ReviewService(repository=GenerationRepository(workspace))
        try:
            outcome = service.run_command(
                command,
                subject=subject,
                target=target,
                all_questions=repository.list_latest_questions(),
            )
        except ReviewCommandError as exc:
            return JSONResponse(
                {"error": exc.message, "code": exc.code}, status_code=400
            )
        for updated in outcome.questions:
            repository.save_question(updated)
        return JSONResponse(
            {
                "review_group_key": group_key,
                "operation_id": outcome.operation.operation_id,
                "status": outcome.operation.status.value,
                "questions": [
                    {
                        "question_id": item.question_id,
                        "question_version": item.question_version,
                        "review_disposition": item.review_disposition.value,
                        "variant_of_question_id": item.variant_of_question_id,
                    }
                    for item in outcome.questions
                ],
            }
        )

    async def review_claim_action(request: Request) -> JSONResponse:
        claim_id = request.path_params["claim_id"]
        payload = await request.json()
        status = str(payload.get("status", ""))
        summary = str(payload.get("summary") or "").strip()
        if not summary:
            return JSONResponse({"error": "summary is required"}, status_code=400)
        try:
            claim_status = ClaimStatus(status)
        except ValueError:
            return JSONResponse({"error": f"unknown claim status: {status}"}, status_code=400)
        bundle = _bundle_for_claim(repository, claim_id)
        if bundle is None:
            raise HTTPException(status_code=404)
        try:
            updated = review_claim(bundle, claim_id, claim_status, summary=summary)
            repository.save_bundle(updated)
        except InvalidReviewAction as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(
            {
                "bundle_id": updated.bundle_id,
                "bundle_version": updated.bundle_version,
            }
        )

    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "workspace": str(repository.root)})

    async def dataset(request: Request) -> HTMLResponse:
        plan = build_export_plan(repository)
        context = _base_context(request, repository)
        context.update(
            plan=plan,
            exportable_count=len(plan.formal_ready),
            active_nav="dataset",
        )
        return templates.TemplateResponse(request, "dataset.html", context)

    async def export_dataset(request: Request) -> JSONResponse:
        payload = {}
        if request.method == "POST":
            try:
                payload = await request.json()
            except ValueError:
                payload = {}
        development = bool(payload.get("development"))
        try:
            target = write_dataset_export(
                repository, repository.root / "exports", development=development
            )
        except ExportBlockedError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        return JSONResponse({"path": str(target), "development": development})

    async def not_found(request: Request, _: Exception) -> HTMLResponse:
        context = _base_context(request, repository)
        context.update(active_nav="")
        return templates.TemplateResponse(
            request, "404.html", context, status_code=404
        )

    routes = [
        Route("/", generation, name="generation"),
        Route("/questions", questions, name="questions"),
        Route(
            "/api/entities",
            search_entities_api,
            methods=["GET"],
            name="search_entities",
        ),
        Route(
            "/api/generation-workspace",
            generation_workspace,
            methods=["GET"],
            name="generation_workspace",
        ),
        Route(
            "/api/generate",
            start_generation,
            methods=["POST"],
            name="start_generation",
        ),
        Route(
            "/api/objective/generate",
            start_objective_generation,
            methods=["POST"],
            name="start_objective_generation",
        ),
        Route(
            "/api/objective/{run_id}",
            objective_progress,
            methods=["GET"],
            name="objective_progress",
        ),
        Route(
            "/api/generation/{task_id}",
            generation_progress,
            methods=["GET"],
            name="generation_progress",
        ),
        Route("/entities", entities, name="entities"),
        Route(
            "/entities/{entity_ref_id:path}",
            entity_detail,
            name="entity_detail",
        ),
        Route("/tasks", runs, name="runs"),
        Route("/runs", runs, name="legacy_runs"),
        Route("/tasks/{task_id}", task_detail, name="task_detail"),
        Route("/review/{question_id}", review, name="review"),
        Route("/groups", groups, name="groups"),
        Route(
            "/api/groups/{review_group_key}",
            group_action,
            methods=["POST"],
            name="group_action",
        ),
        Route(
            "/api/review/questions/{question_id}",
            review_question_action,
            methods=["POST"],
            name="review_question",
        ),
        Route(
            "/api/review/bundles/{claim_id}",
            review_claim_action,
            methods=["POST"],
            name="review_claim",
        ),
        Route("/health", health, name="health"),
        Route("/dataset", dataset, name="dataset"),
        Route("/api/export", export_dataset, methods=["POST"], name="export_dataset"),
        Mount(
            "/static",
            app=StaticFiles(directory=WEB_ROOT / "static"),
            name="static",
        ),
    ]
    return Starlette(
        routes=routes,
        exception_handlers={404: not_found},
        lifespan=lifespan,
        debug=False,
    )


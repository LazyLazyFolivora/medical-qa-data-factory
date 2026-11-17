"""Starlette application for the local evidence workbench."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from ..domain import ClaimStatus, EvidenceBundle, EntityRef
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
from ..generation.fact_source_policy import (
    FACT_SOURCE_ROUTES,
    default_objective_fact_types,
)
from ..generation.objective_run import (
    DEFAULT_DIFFICULTIES,
    DEFAULT_GENERATION_STRATEGY,
    DEFAULT_MAX_ACTIONS,
    DEFAULT_PROVIDERS,
    DEFAULT_STYLE_TARGETS,
    create_objective_run,
    objective_provider_map as _objective_provider_map,
    run_objective_loop,
)
from ..generation.objective_question_contracts import (
    EvidenceMaterial,
)
from ..generation.objective_question_persistence import (
    ObjectiveQuestionPersistence,
)
from ..generation.objective_question_release import export_objective_release
from ..generation.objective_question_runtime import (
    CheckpointIntegrityError,
    ObjectiveRuntimeRepository,
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
from .objective_projection import (
    build_production_progress,
    OBJECTIVE_STALL_AFTER_SECONDS,
    build_objective_summary,
    build_objective_workbench,
    build_question_evidence_dossier,
    objective_decision_order,
    public_questions_for_run,
)

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

OBJECTIVE_PROVIDER_LABELS = {
    "primekg": "PrimeKG",
    "pubmed": "PubMed",
    "ctd": "CTD",
    "rag": "RAG",
    "hgnc": "HGNC",
    "ncbi_gene": "NCBI Gene",
    "ensembl": "Ensembl",
}

OBJECTIVE_MATERIAL_LABELS = {
    "knowledge_graph_edge": "知识图谱边",
    "literature_abstract": "文献摘要",
    "literature_full_text": "文献全文",
    "hgnc_gene_record": "HGNC 基因记录",
    "ncbi_gene_record": "NCBI Gene 记录",
    "ensembl_gene_record": "Ensembl 基因记录",
    "ctd_chemical_disease_record": "CTD 化学物—疾病记录",
    "text": "文本材料",
}
OBJECTIVE_ARTIFACT_LABELS = {
    "provider_result": "数据源结果",
    "evidence_material": "证据材料",
    "evidence_anchor": "证据定位",
    "candidate_claim": "候选事实",
    "fact_understanding_batch": "事实抽取批次",
    "question_hypothesis": "候选题",
    "answer_resolution": "答案核验",
    "frozen_question_contract": "冻结题目契约",
    "objective_question_draft": "题目草稿",
    "objective_validation_report": "发布复核",
    "subagent_failure": "子 Agent 失败",
}


def _objective_run_root(workspace: str | Path, run_id: str) -> Path:
    return Path(workspace) / "objective_runs" / run_id


def _objective_release_root(workspace: str | Path, run_id: str) -> Path:
    return Path(workspace) / "objective_releases" / run_id


def _objective_run_provider_ids(snapshot: object) -> list[str]:
    names = set(snapshot.manifest.provider_adapter_versions)
    for artifact in snapshot.artifacts.values():
        if artifact.artifact_type != "provider_result":
            continue
        request = artifact.payload.get("request", {})
        provider_id = str(request.get("provider_id") or "").strip()
        if provider_id:
            names.add(provider_id)
    return sorted(names)


def _recovery_error_payload(exc: Exception) -> dict[str, object]:
    if isinstance(exc, ModelError):
        return exc.as_dict()
    return {
        "code": type(exc).__name__,
        "message": str(exc),
    }


def _objective_progress_timing(
    run_root: str | Path,
    *,
    terminal: bool,
    now: datetime | None = None,
) -> tuple[datetime | None, int, bool]:
    commit_dir = Path(run_root) / "objective_runtime" / "commits"
    commits = (
        sorted(
            commit_dir.glob("*.json"),
            key=lambda item: int(item.stem),
        )
        if commit_dir.is_dir()
        else []
    )
    if not commits:
        return None, 0, False
    last_commit_at = datetime.fromtimestamp(
        commits[-1].stat().st_mtime,
        tz=timezone.utc,
    )
    current_time = now or datetime.now(timezone.utc)
    seconds_since_progress = max(
        0,
        int((current_time - last_commit_at).total_seconds()),
    )
    stalled = (
        not terminal
        and seconds_since_progress >= OBJECTIVE_STALL_AFTER_SECONDS
    )
    return last_commit_at, seconds_since_progress, stalled


def _trace_query_label(query: Any) -> str:
    if not isinstance(query, dict):
        return str(query or "")
    return " · ".join(
        f"{key}={value}"
        for key, value in query.items()
    )


def _decision_output_trace(artifacts: list[Any]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    providers = []
    claims = []
    hypotheses = []
    resolutions = []
    drafts = []
    validations = []
    failures = []
    fact_batches = []
    for artifact in artifacts:
        artifact_type = artifact.artifact_type
        counts[artifact_type] = counts.get(artifact_type, 0) + 1
        payload = artifact.payload
        if artifact_type == "provider_result":
            request = payload.get("request", {})
            provider_id = str(request.get("provider_id", ""))
            providers.append(
                {
                    "provider_id": provider_id,
                    "provider_label": OBJECTIVE_PROVIDER_LABELS.get(
                        provider_id,
                        provider_id,
                    ),
                    "purpose": artifact.metadata.get(
                        "purpose",
                        "discovery",
                    ),
                    "terminal_status": payload.get("terminal_status"),
                    "error_category": payload.get("error_category"),
                    "material_count": len(
                        payload.get("material_ids", [])
                    ),
                    "candidate_count": payload.get("usage", {}).get(
                        "candidate_count",
                        len(payload.get("material_ids", [])),
                    ),
                    "query": request.get("query"),
                    "query_label": _trace_query_label(
                        request.get("query")
                    ),
                    "search_trace": payload.get("usage", {}).get(
                        "search_trace",
                        [],
                    ),
                    "detail_trace": payload.get("usage", {}).get(
                        "detail_trace",
                        [],
                    ),
                    "search_rationale": payload.get("usage", {}).get(
                        "search_rationale"
                    ),
                }
            )
        elif artifact_type == "candidate_claim":
            claims.append(
                {
                    "claim_id": payload.get("claim_id"),
                    "subject": payload.get("subject"),
                    "predicate": payload.get("predicate"),
                    "object_or_value": payload.get("object_or_value"),
                    "claim_strength": payload.get("claim_strength"),
                    "polarity": payload.get("polarity"),
                    "supporting_anchor_ids": list(
                        payload.get("supporting_anchor_ids", [])
                    ),
                }
            )
        elif artifact_type == "question_hypothesis":
            hypotheses.append(
                {
                    "hypothesis_id": payload.get("hypothesis_id"),
                    "question_intent": payload.get("question_intent"),
                    "public_scope": payload.get("proposed_public_scope"),
                    "answer_type": payload.get("proposed_answer_type"),
                    "answer_values": list(
                        payload.get("proposed_answer_values", [])
                    ),
                    "candidate_claim_ids": list(
                        payload.get("candidate_claim_ids", [])
                    ),
                }
            )
        elif artifact_type == "answer_resolution":
            resolutions.append(
                {
                    "resolution_id": payload.get("resolution_id"),
                    "hypothesis_id": payload.get("hypothesis_id"),
                    "status": payload.get("status"),
                    "answer_values": list(
                        payload.get("verified_answer_values", [])
                    ),
                    "verification_sources": list(
                        payload.get("verification_sources", [])
                    ),
                    "completeness_basis": payload.get(
                        "completeness_basis"
                    ),
                }
            )
        elif artifact_type == "objective_question_draft":
            drafts.append(
                {
                    "draft_id": payload.get("draft_id"),
                    "question_text": payload.get("question_text"),
                    "answer_values": list(
                        payload.get("answer_values", [])
                    ),
                }
            )
        elif artifact_type == "objective_validation_report":
            validations.append(
                {
                    "report_id": payload.get("report_id"),
                    "question_id": payload.get("question_id"),
                    "verdict": payload.get("verdict"),
                    "release_allowed": payload.get("release_allowed"),
                    "primary_reason": payload.get("primary_reason"),
                    "blocking_findings": list(
                        payload.get("blocking_findings", [])
                    ),
                }
            )
        elif artifact_type == "fact_understanding_batch":
            fact_batches.append(
                {
                    "batch_id": payload.get("batch_id"),
                    "material_count": len(
                        payload.get("material_ids", [])
                    ),
                    "claim_count": len(payload.get("claim_ids", [])),
                }
            )
        elif artifact_type == "subagent_failure":
            failures.append(
                {
                    "failure_id": payload.get("failure_id"),
                    "action": payload.get("action"),
                    "error_code": payload.get("error_code"),
                    "error_message": payload.get("error_message"),
                    "retryable": payload.get("retryable"),
                }
            )
    return {
        "counts": {
            key: counts[key]
            for key in sorted(counts)
        },
        "count_labels": {
            OBJECTIVE_ARTIFACT_LABELS.get(key, key): count
            for key, count in sorted(counts.items())
        },
        "providers": providers,
        "claims": claims,
        "hypotheses": hypotheses,
        "resolutions": resolutions,
        "drafts": drafts,
        "validations": validations,
        "fact_batches": fact_batches,
        "failures": failures,
    }


def _objective_run_payload(
    workspace: str | Path,
    run_id: str,
) -> dict[str, object]:
    run_root = _objective_run_root(workspace, run_id)
    runtime = ObjectiveRuntimeRepository(run_root, run_id=run_id)
    snapshot = runtime.load()
    questions = public_questions_for_run(workspace, run_id, snapshot)
    reports = [
        artifact.payload
        for artifact in snapshot.artifacts.values()
        if artifact.artifact_type == "objective_validation_report"
    ]
    artifacts_by_decision: dict[str, list[Any]] = {}
    for artifact in snapshot.artifacts.values():
        if artifact.parent_decision_id:
            artifacts_by_decision.setdefault(
                artifact.parent_decision_id,
                [],
            ).append(artifact)
    steps = []
    for sequence, decision in enumerate(
        objective_decision_order(workspace, run_id, snapshot),
        start=1,
    ):
        outputs = _decision_output_trace(
            artifacts_by_decision.get(decision.decision_id, [])
        )
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
                "expected_information_gain": (
                    decision.expected_information_gain
                ),
                "input_artifact_ids": list(decision.input_artifact_ids),
                "parameters": decision.action_parameters,
                "stop_reason": decision.stop_reason,
                "outputs": outputs,
            }
        )
    artifact_counts: dict[str, int] = {}
    for artifact in snapshot.artifacts.values():
        artifact_counts[artifact.artifact_type] = (
            artifact_counts.get(artifact.artifact_type, 0) + 1
        )
    anchor_counts: dict[str, int] = {}
    for artifact in snapshot.artifacts.values():
        if artifact.artifact_type != "evidence_anchor":
            continue
        material_id = str(artifact.payload.get("material_id", ""))
        if material_id:
            anchor_counts[material_id] = anchor_counts.get(material_id, 0) + 1
    retrieval_sources = []
    result_purpose_by_request: dict[str, str] = {}
    for artifact in snapshot.artifacts.values():
        if artifact.artifact_type != "provider_result":
            continue
        provider_result = artifact.payload
        request = provider_result.get("request", {})
        request_id = str(request.get("request_id", ""))
        purpose = str(artifact.metadata.get("purpose", "discovery"))
        if request_id:
            result_purpose_by_request[request_id] = purpose
        query = request.get("query", {})
        provider_id = str(request.get("provider_id", ""))
        if isinstance(query, dict) and isinstance(query.get(provider_id), dict):
            query = query[provider_id]
        usage = provider_result.get("usage", {})
        trace = usage.get("search_trace", [])
        if trace:
            for item in trace:
                trace_query = item.get("query", {})
                query_label = (
                    " · ".join(
                        f"{key}={value}"
                        for key, value in trace_query.items()
                    )
                    if isinstance(trace_query, dict)
                    else str(trace_query)
                )
                retrieval_sources.append(
                    {
                        "provider_id": provider_id,
                        "provider_label": OBJECTIVE_PROVIDER_LABELS.get(
                            provider_id, provider_id
                        ),
                        "purpose": purpose,
                        "terminal_status": item.get("terminal_status"),
                        "query_label": query_label,
                        "query": trace_query,
                        "material_count": item.get("material_count", 0),
                        "candidate_count": usage.get(
                            "candidate_count",
                            item.get("material_count", 0),
                        ),
                        "search_rationale": usage.get("search_rationale"),
                        "detail_trace": usage.get("detail_trace", []),
                        "snapshot_id": provider_result.get("snapshot_id"),
                        "error_category": item.get("error_category"),
                        "search_round": item.get("round"),
                    }
                )
        else:
            query_label = (
                " · ".join(
                    f"{key}={value}" for key, value in query.items()
                )
                if isinstance(query, dict)
                else str(query)
            )
            retrieval_sources.append(
                {
                    "provider_id": provider_id,
                    "provider_label": OBJECTIVE_PROVIDER_LABELS.get(
                        provider_id, provider_id
                    ),
                    "purpose": purpose,
                    "terminal_status": provider_result.get(
                        "terminal_status"
                    ),
                    "query_label": query_label,
                    "query": query,
                    "material_count": len(
                        provider_result.get("material_ids", [])
                    ),
                    "candidate_count": usage.get(
                        "candidate_count",
                        len(provider_result.get("material_ids", [])),
                    ),
                    "search_rationale": usage.get("search_rationale"),
                    "detail_trace": usage.get("detail_trace", []),
                    "snapshot_id": provider_result.get("snapshot_id"),
                    "error_category": provider_result.get(
                        "error_category"
                    ),
                }
            )
    persistence = ObjectiveQuestionPersistence(run_root)
    materials = []
    for artifact in snapshot.artifacts.values():
        if artifact.artifact_type != "evidence_material":
            continue
        material = EvidenceMaterial.model_validate(artifact.payload)
        provider_id = material.provider_id
        preview = ""
        try:
            preview = persistence.load_raw_content(material).decode("utf-8")
        except Exception:
            preview = artifact.payload.get("content_preview", "")
        preview = " ".join(str(preview).split())
        if len(preview) > 320:
            preview = preview[:317] + "..."
        purpose = result_purpose_by_request.get(
            material.retrieval_request_id,
            artifact.metadata.get("purpose", "discovery"),
        )
        purposes = list(
            dict.fromkeys(
                [
                    *artifact.metadata.get("purposes", []),
                    str(purpose),
                ]
            )
        )
        materials.append(
            {
                "material_id": material.material_id,
                "provider_id": provider_id,
                "provider_label": OBJECTIVE_PROVIDER_LABELS.get(
                    provider_id, provider_id
                ),
                "source_record_id": material.source_record_id,
                "material_type": material.material_type,
                "material_label": OBJECTIVE_MATERIAL_LABELS.get(
                    material.material_type, material.material_type
                ),
                "purpose": purpose,
                "purposes": purposes,
                "retrieved_at": material.retrieved_at.isoformat(),
                "anchor_count": anchor_counts.get(material.material_id, 0),
                "preview": preview,
            }
        )
    terminal = snapshot.run_state in {
        RuntimeRunState.COMPLETED,
        RuntimeRunState.COMPLETED_WITH_ZERO_QUESTIONS,
        RuntimeRunState.PROVIDER_FAILED,
        RuntimeRunState.RECOVERY_REQUIRED,
        RuntimeRunState.VERSION_INCOMPATIBLE,
        RuntimeRunState.CANCELLED,
    }
    last_commit_at, seconds_since_progress, stalled = (
        _objective_progress_timing(
            run_root,
            terminal=terminal,
        )
    )
    status = (
        "stalled"
        if stalled
        else "failed"
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
    status_label = (
        "长时间无进展"
        if stalled
        else OBJECTIVE_STATE_LABELS.get(
            snapshot.run_state.value,
            snapshot.run_state.value,
        )
    )
    effective_stop_reason = snapshot.stop_reason
    if stalled:
        effective_stop_reason = (
            f"主 Agent 已超过 {OBJECTIVE_STALL_AFTER_SECONDS // 60} 分钟"
            "没有产生新的 runtime commit，任务可能卡在模型调用或后台异常；"
            "当前 run 无法从该检查点自动继续。"
        )
    pending_decision = runtime.pending_decision()
    last_successful_step = (
        len(steps) - 1
        if pending_decision is not None
        else len(steps)
    )
    next_step = (
        len(steps)
        if pending_decision is not None
        else snapshot.resume_after_step
        if snapshot.resume_after_step is not None
        else len(steps) + 1
    )
    return {
        "task_id": run_id,
        "engine": "objective",
        "status": status,
        "status_label": "已暂停" if snapshot.stop_reason == "user_paused" else status_label,
        "pause_requested": runtime.pause_requested,
        "run_state": snapshot.run_state.value,
        "can_resume": (
            snapshot.run_state is RuntimeRunState.RECOVERY_REQUIRED
            or stalled
        ),
        "recovery_state": (
            snapshot.recovery_state.value
            if snapshot.recovery_state is not None
            else None
        ),
        "resume_count": snapshot.resume_count,
        "recovery_events": list(snapshot.recovery_events),
        "resume_events": list(snapshot.resume_events),
        "last_successful_step": last_successful_step,
        "next_step": next_step,
        "pending_decision_id": (
            pending_decision.decision_id
            if pending_decision is not None
            else None
        ),
        "pending_action": (
            pending_decision.action
            if pending_decision is not None
            else None
        ),
        "state_revision": snapshot.state_revision,
        "commit_sequence": snapshot.commit_sequence,
        "stop_reason": effective_stop_reason,
        "last_commit_at": (
            last_commit_at.isoformat()
            if last_commit_at is not None
            else None
        ),
        "seconds_since_progress": seconds_since_progress,
        "stalled": stalled,
        "target_question_count": (
            snapshot.goal.question_count_upper_bound
            if snapshot.goal is not None
            else None
        ),
        "goal": (
            {
                "target_entity": snapshot.goal.target_entity,
                "target_entity_id": snapshot.goal.target_entity_id,
                "target_entity_type": snapshot.goal.target_entity_type,
                "difficulty_targets": list(
                    snapshot.goal.difficulty_targets
                ),
                "style_targets": list(snapshot.goal.style_targets),
            }
            if snapshot.goal is not None
            else None
        ),
        "published_count": len(questions),
        "main_agent_steps": steps,
        "planner_steps": [],
        "operations": [],
        "artifact_counts": dict(sorted(artifact_counts.items())),
        "retrieval_sources": sorted(
            retrieval_sources,
            key=lambda item: (
                str(item["provider_id"]),
                str(item["purpose"]),
                str(item["query_label"]),
            ),
        ),
        "materials": sorted(
            materials,
            key=lambda item: (
                str(item["provider_id"]),
                str(item["source_record_id"]),
            ),
        ),
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


def _objective_published_question_rows(
    workspace: str | Path,
) -> list[dict[str, object]]:
    releases_root = Path(workspace) / "objective_releases"
    runs_root = Path(workspace) / "objective_runs"
    rows: list[dict[str, object]] = []
    run_ids = {item.name for root in (releases_root, runs_root) if root.is_dir()
               for item in root.iterdir() if item.is_dir()}
    for run_id in sorted(run_ids):
        questions_path = releases_root / run_id / "public" / "questions.jsonl"
        snapshot = None
        try:
            snapshot = ObjectiveRuntimeRepository(
                _objective_run_root(workspace, run_id),
                run_id=run_id,
            ).load()
        except Exception:
            pass
        questions = public_questions_for_run(workspace, run_id, snapshot)
        if not questions:
            continue
        contract_by_id = {}
        draft_by_id = {}
        resolution_by_id = {}
        report_by_question_id = {}
        if snapshot is not None:
            for artifact in snapshot.artifacts.values():
                if artifact.artifact_type == "frozen_question_contract":
                    contract_by_id[artifact.artifact_id] = artifact.payload
                elif artifact.artifact_type == "objective_question_draft":
                    draft_by_id[artifact.artifact_id] = artifact.payload
                elif artifact.artifact_type == "answer_resolution":
                    resolution_by_id[artifact.artifact_id] = artifact.payload
                elif artifact.artifact_type == "objective_validation_report":
                    question_id = str(
                        artifact.payload.get("question_id") or ""
                    )
                    if question_id:
                        report_by_question_id[question_id] = artifact.payload
        published_at = datetime.fromtimestamp(
            (questions_path if questions_path.is_file() else runs_root / run_id).stat().st_mtime,
            tz=timezone.utc,
        )
        if snapshot is not None:
            commit_dir = (
                _objective_run_root(workspace, run_id)
                / "objective_runtime"
                / "commits"
            )
            commits = (
                sorted(
                    commit_dir.glob("*.json"),
                    key=lambda item: int(item.stem),
                )
                if commit_dir.is_dir()
                else []
            )
            if commits:
                published_at = datetime.fromtimestamp(
                    commits[-1].stat().st_mtime,
                    tz=timezone.utc,
                )
        for payload in questions:
            question_id = str(payload.get("question_id") or "")
            report = report_by_question_id.get(question_id, {})
            answers = payload.get("answer", [])
            if not isinstance(answers, list):
                answers = [answers]
            sources = []
            draft = draft_by_id.get(question_id)
            contract = (
                contract_by_id.get(str(draft.get("contract_id")))
                if draft is not None
                else None
            )
            resolution = None
            if contract is not None and contract.get("resolution_id") is not None:
                resolution = resolution_by_id.get(
                    str(contract["resolution_id"])
                )
            if resolution is not None:
                sources = [
                    OBJECTIVE_PROVIDER_LABELS.get(source, source)
                    for source in resolution.get("verification_sources", [])
                ]
            suggested_retrieval = payload.get("suggested_retrieval")
            rows.append(
                {
                    "run_id": run_id,
                    "question_id": question_id,
                    "question": str(payload.get("question") or ""),
                    "answers": [str(value) for value in answers],
                    "explanation": str(payload.get("explanation") or ""),
                    **({"suggested_retrieval": suggested_retrieval} if suggested_retrieval else {}),
                    "entity_name": (
                        snapshot.goal.target_entity
                        if snapshot is not None and snapshot.goal is not None
                        else ""
                    ),
                    "entity_type": (
                        snapshot.goal.target_entity_type
                        if snapshot is not None and snapshot.goal is not None
                        else None
                    ),
                    "sources": sources,
                    "verdict": str(report.get("verdict") or "PASS"),
                    "published_at": published_at,
                    "run_url": f"/?task={run_id}",
                }
            )
    return sorted(rows, key=lambda row: row["published_at"], reverse=True)


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
    allowed_fact_types: list[str],
    difficulty_targets: list[str],
    style_targets: list[str],
    model: object,
    model_provider_id: str | None = None,
    objective_providers: dict[str, object] | None = None,
    generation_strategy: str = DEFAULT_GENERATION_STRATEGY,
    publication_slot=None,
) -> dict[str, object]:
    run_root = _objective_run_root(workspace, run_id)
    release_root = _objective_release_root(workspace, run_id)
    if run_root.exists() or release_root.exists():
        raise FileExistsError(f"objective run already exists: {run_id}")
    selected, kg_provider = _objective_provider_map(
        providers,
        objective_providers,
    )
    try:
        runtime = create_objective_run(
            run_root,
            run_id=run_id,
            entity_name=entity_name,
            entity_id=entity_canonical_id.split(":", 2)[-1],
            entity_type=entity_type or None,
            question_count=question_count,
            providers=selected,
            difficulty_targets=difficulty_targets,
            style_targets=style_targets,
            generation_strategy=generation_strategy,
            allowed_fact_types=allowed_fact_types,
            model=model,
            model_provider_id=model_provider_id,
            max_actions=max_actions,
        )
        try:
            result = run_objective_loop(runtime, runtime.load().goal, model, selected, publication_slot=publication_slot)
        except Exception as exc:
            try:
                latest = runtime.load()
                runtime.set_run_state(
                    expected_state_revision=latest.state_revision,
                    target=RuntimeRunState.RECOVERY_REQUIRED,
                    stop_reason=(
                        f"{type(exc).__name__}: {exc}"
                    ),
                    recovery_error=_recovery_error_payload(exc),
                )
            except Exception:
                pass
            raise
        _export_completed_web_run(result, release_root)
    finally:
        if kg_provider is not None:
            kg_provider.close()
    return _objective_run_payload(workspace, run_id)


def _export_completed_web_run(result, release_root: Path) -> None:
    if result.run_state not in {RuntimeRunState.COMPLETED, RuntimeRunState.COMPLETED_WITH_ZERO_QUESTIONS}:
        return
    manifest = release_root / "audit" / "manifest.json"
    if manifest.is_file():
        previous = json.loads(manifest.read_text(encoding="utf-8"))
        if previous.get("run_id") == result.run_id and previous.get("run_state") == "RECOVERY_REQUIRED":
            # Older web runs exported a provisional package when paused.
            # Build the final package first and keep the old files outside the release index.
            from uuid import uuid4
            workspace = release_root.resolve().parent.parent
            if release_root.resolve() != workspace / "objective_releases" / result.run_id:
                raise ValueError("unexpected release path")
            # A private temp directory on Windows gives the moved release an
            # owner-only ACL. Inherit the workspace ACL like a normal export.
            staging = workspace / f"release-repair-{uuid4().hex}"
            staging.mkdir()
            ready = export_objective_release(result, staging / "completed")
            previous_path = staging / "paused"
            release_root.rename(previous_path)
            try:
                ready.rename(release_root)
            except OSError:
                previous_path.rename(release_root)
                raise
            return
    export_objective_release(result, release_root)


def _resume_objective_web_generation(
    *,
    workspace: str | Path,
    run_id: str,
    model: object | None = None,
    model_provider_id: str | None = None,
    objective_providers: dict[str, object] | None = None,
    worker_interrupted: bool = False,
    publication_slot=None,
) -> dict[str, object]:
    run_root = _objective_run_root(workspace, run_id)
    release_root = _objective_release_root(workspace, run_id)
    runtime = ObjectiveRuntimeRepository(run_root, run_id=run_id)
    snapshot = runtime.load()
    recovering = (
        snapshot.run_state is RuntimeRunState.RECOVERY_REQUIRED
    )
    if not recovering:
        _last_commit, age, stalled = _objective_progress_timing(
            run_root,
            terminal=False,
        )
        if not (stalled or worker_interrupted):
            raise ValueError("run is neither recoverable nor stalled")
        if snapshot.run_state in {
            RuntimeRunState.COMPLETED,
            RuntimeRunState.COMPLETED_WITH_ZERO_QUESTIONS,
            RuntimeRunState.PROVIDER_FAILED,
            RuntimeRunState.VERSION_INCOMPATIBLE,
            RuntimeRunState.CANCELLED,
        }:
            raise ValueError("run is terminal")
    goal = snapshot.goal
    if goal is None:
        raise ValueError("objective run has no generation goal")

    provider_ids = _objective_run_provider_ids(snapshot)
    if not provider_ids and objective_providers:
        provider_ids = sorted(objective_providers)
    selected, kg_provider = _objective_provider_map(
        provider_ids,
        objective_providers,
    )
    selected_model = model
    if selected_model is None:
        from ..llm.registry import build_model_port

        requested_provider = (
            model_provider_id
            or snapshot.manifest.model_versions.get("provider_id")
            or None
        )
        selected_model = build_model_port(requested_provider or None)

    if recovering:
        runtime.resume_from_recovery()
    else:
        reason = (
            "web worker interrupted; resume from the saved checkpoint"
            if worker_interrupted
            else f"no runtime commit for {age} seconds; resume from the pending decision"
        )
        runtime.resume_stalled(
            reason=reason,
        )
    try:
        result = run_objective_loop(runtime, goal, selected_model, selected, publication_slot=publication_slot)
    except Exception as exc:
        try:
            latest = runtime.load()
            if latest.run_state is not RuntimeRunState.RECOVERY_REQUIRED:
                runtime.set_run_state(
                    expected_state_revision=latest.state_revision,
                    target=RuntimeRunState.RECOVERY_REQUIRED,
                    stop_reason=f"{type(exc).__name__}: {exc}",
                    recovery_error=_recovery_error_payload(exc),
                )
        except Exception:
            pass
        raise
    finally:
        if kg_provider is not None:
            kg_provider.close()

    _export_completed_web_run(result, release_root)
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
        providers = registry.describe()
        default_provider = registry.config.default_provider
        provider = next(
            item
            for item in providers
            if item["provider_id"] == default_provider
        )
    except ModelError as exc:
        return {
            "ready": False,
            "provider_id": None,
            "model_id": None,
            "default_provider": None,
            "providers": [],
            "detail": exc.message,
        }
    return {
        "ready": provider["ready"],
        "provider_id": provider["provider_id"],
        "model_id": provider["model_id"],
        "default_provider": default_provider,
        "providers": providers,
        "detail": provider["detail"],
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


def create_app(
    workspace: str | Path,
    *,
    entity_provider: KnowledgeGraphProvider | None = None,
    structured_model: object | None = None,
    evidence_enricher: object | None = None,
    objective_providers: dict[str, object] | None = None,
    production_database_url: str | None = None,
) -> Starlette:
    repository = LocalArtifactRepository(workspace)
    generation_lock = asyncio.Lock()
    objective_job: asyncio.Task | None = None
    active_objective_run_id = ""
    entity_search_lock = asyncio.Lock()

    def generation_busy() -> bool:
        return bool(active_objective_run_id) or generation_lock.locked()

    def web_worker_interrupted(run_id: str) -> bool:
        return (
            run_id.startswith(("web-", "objective-web-"))
            and active_objective_run_id != run_id
            and not production_owns_run(run_id)
        )

    async def run_objective_job(run_id, operation, **kwargs):
        nonlocal active_objective_run_id, objective_job

        async def execute():
            nonlocal active_objective_run_id
            try:
                async with generation_lock:
                    return await run_in_threadpool(operation, run_id=run_id, **kwargs)
            finally:
                active_objective_run_id = ""

        active_objective_run_id = run_id
        objective_job = asyncio.create_task(execute())
        # Observe errors even if the client disconnected before the worker finished.
        objective_job.add_done_callback(
            lambda task: task.exception() if not task.cancelled() else None
        )
        return await asyncio.shield(objective_job)
    model_status = (
        {
            "ready": True,
            "provider_id": getattr(
                structured_model, "model_config_id", "injected-model"
            ),
            "model_id": getattr(structured_model, "model_config_id", "injected-model"),
            "default_provider": "injected-model",
            "providers": [
                {
                    "provider_id": "injected-model",
                    "model_id": getattr(
                        structured_model,
                        "model_config_id",
                        "injected-model",
                    ),
                    "driver": "injected",
                    "auth_mode": "none",
                    "structured_output_mode": "injected",
                    "ready": True,
                    "detail": "已注入测试模型",
                }
            ],
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

    from .production import MAX_PRODUCTION_WORKERS, ProductionConfig, ProductionService
    from ..llm.registry import build_model_port
    from ..llm.run_trace import trace_event

    production_source = None
    import threading
    production_model_gate = threading.BoundedSemaphore(MAX_PRODUCTION_WORKERS)

    class ProductionModel:
        def __init__(self, model):
            self.model = model

        def __getattr__(self, name):
            return getattr(self.model, name)

        def generate_structured(self, request, *args, **kwargs):
            queued = time.monotonic()
            with production_model_gate:
                started = time.monotonic()
                try:
                    return self.model.generate_structured(request, *args, **kwargs)
                finally:
                    trace_event("production_model_timing", role=request.role, invocation_id=request.invocation_id,
                                queue_ms=round((started-queued)*1000), execution_ms=round((time.monotonic()-started)*1000))


    def production_page(entity_type, after):
        nonlocal production_source
        if production_source is None:
            production_source = entity_provider or provider_from_config()
        return production_source.production_entities(entity_type, after)

    def production_progress(run_id):
        try:
            return build_production_progress(repository.root, run_id)
        except (FileNotFoundError, CheckpointIntegrityError):
            return None

    def production_pause(run_id):
        root = _objective_run_root(repository.root, run_id)
        try:
            ObjectiveRuntimeRepository(root, run_id=run_id).request_pause()
        except (FileNotFoundError, CheckpointIntegrityError, ValueError):
            # A worker may finish between the coordinator's poll and pause request.
            return

    production_provider_gates = {
        name: threading.BoundedSemaphore(1)
        for name in (*DEFAULT_PROVIDERS, "ctd")
    }
    other_provider_gate = threading.BoundedSemaphore(1)
    production_fulltext_gate = threading.BoundedSemaphore(2)

    class ProductionProvider:
        def __init__(self, provider, gate, detail_gate):
            self.provider = provider
            self.gate = gate
            self.detail_gate = detail_gate

        def __getattr__(self, name):
            value = getattr(self.provider, name)
            if not callable(value):
                return value

            gate = self.detail_gate if name == "retrieve_details" else self.gate

            def limited(*args, **kwargs):
                queued = time.monotonic()
                with gate:
                    started = time.monotonic()
                    try:
                        return value(*args, **kwargs)
                    finally:
                        trace_event("production_provider_timing", provider_id=self.provider.capability.provider_id,
                                    method=name, queue_ms=round((started-queued)*1000),
                                    execution_ms=round((time.monotonic()-started)*1000))
            return limited

    def production_entity_for_run(run_id):
        runtime = ObjectiveRuntimeRepository(_objective_run_root(repository.root, run_id), run_id=run_id)
        try:
            goal = runtime.load().goal
        except (FileNotFoundError, CheckpointIntegrityError):
            return None
        if not goal or not goal.target_entity_id or not goal.target_entity_type:
            return None
        identity = goal.target_entity_id
        entity_type = {"gene": "gene/protein"}.get(goal.target_entity_type, goal.target_entity_type)
        if identity.count(":") < 2:
            if not run_id.startswith(("web-", "objective-web-")):
                return None
            identity = f"primekg:{entity_type}:{identity}"
        return identity, goal.target_entity, entity_type

    def production_execute(entity, config):
        def publication_slot():
            return production.publication_slot(entity, config)

        run_id = entity["run_id"]
        runtime = ObjectiveRuntimeRepository(_objective_run_root(repository.root, run_id), run_id=run_id)
        try:
            snapshot = runtime.load()
        except FileNotFoundError:
            snapshot = None
        if snapshot and snapshot.run_state in {RuntimeRunState.COMPLETED, RuntimeRunState.COMPLETED_WITH_ZERO_QUESTIONS}:
            if not (_objective_release_root(repository.root, run_id) / "audit" / "manifest.json").is_file():
                # A finished checkpoint still supplies published questions and trace to the production dataset.
                logging.getLogger(__name__).warning("Completed production checkpoint lacks release package: %s", run_id)
            return build_objective_summary(repository.root, run_id)
        providers = (
            _objective_run_provider_ids(snapshot) if snapshot else
            list(objective_providers or DEFAULT_PROVIDERS)
        )
        selected_providers, graph_provider = _objective_provider_map(providers, objective_providers)
        managed_providers = {
            name: ProductionProvider(
                provider,
                production_provider_gates.get(name, other_provider_gate),
                production_fulltext_gate if name == "pubmed" else production_provider_gates.get(name, other_provider_gate),
            )
            for name, provider in selected_providers.items()
        }
        try:
            model_provider_id = snapshot.manifest.model_versions.get("provider_id") if snapshot else None
            raw_model = structured_model or build_model_port(model_provider_id)
            if model_provider_id is None:
                model_provider_id = getattr(getattr(raw_model, "config", None), "provider_id", None)
            model = ProductionModel(raw_model)
            if snapshot:
                if snapshot.run_state is not RuntimeRunState.RECOVERY_REQUIRED:
                    runtime.set_run_state(expected_state_revision=snapshot.state_revision,
                        target=RuntimeRunState.RECOVERY_REQUIRED, stop_reason="production_resume")
                _resume_objective_web_generation(workspace=repository.root, run_id=run_id,
                    model=model, objective_providers=managed_providers, publication_slot=publication_slot)
            else:
                _run_objective_web_generation(workspace=repository.root, run_id=run_id,
                    entity_canonical_id=entity["identity"], entity_name=entity["name"],
                    entity_type=entity["entity_type"], question_count=entity["quota"],
                    max_actions=config.max_actions, providers=providers,
                    allowed_fact_types=default_objective_fact_types(entity["entity_type"], available_providers=providers),
                    difficulty_targets=config.difficulty_targets, style_targets=list(DEFAULT_STYLE_TARGETS),
                    generation_strategy=config.generation_strategy,
                    model=model, model_provider_id=model_provider_id, objective_providers=managed_providers,
                    publication_slot=publication_slot)
            return build_objective_summary(repository.root, run_id)
        finally:
            if graph_provider is not None:
                graph_provider.close()

    from .production_db import DATABASE_URL_ENV, production_database_url as configured_production_database_url

    database_url = production_database_url or configured_production_database_url()
    production = ProductionService(
        repository.root, source=production_page, execute=production_execute,
        progress=production_progress, pause=production_pause, entity_for_run=production_entity_for_run,
        race_last_question=True, database_url=database_url,
    ) if database_url else None

    def production_busy():
        return production is not None and production.thread is not None and production.thread.is_alive()

    def production_owns_run(run_id):
        return production is not None and production.owns_run(run_id)

    def production_unavailable():
        return JSONResponse(
            {"error": f"请在项目根目录的 .env 中设置 {DATABASE_URL_ENV} 并执行生产数据库迁移"},
            status_code=503,
        )

    async def production_api(request):
        if production is None:
            return production_unavailable()
        try:
            if request.method == "GET":
                return JSONResponse({"triggers": await run_in_threadpool(production.list_triggers)})
            payload = await request.json()
            config = ProductionConfig.model_validate(payload)
            if generation_busy() or production_busy():
                return JSONResponse({"error": "已有生成任务运行中"}, status_code=409)
            async with generation_lock:
                if structured_model is None:
                    await run_in_threadpool(build_model_port)
                trigger_id = await run_in_threadpool(production.start, config)
            return JSONResponse({"trigger_id": trigger_id}, status_code=202)
        except (ValueError, ModelError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def production_control(request):
        if production is None:
            return production_unavailable()
        try:
            trigger_id = request.path_params["trigger_id"]
            action = request.path_params["action"]
            if action == "resume":
                if generation_busy():
                    raise ValueError("单实体任务正在运行，请先完成或暂停")
                async with generation_lock:
                    await run_in_threadpool(production.control, trigger_id, action)
            else:
                await run_in_threadpool(production.control, trigger_id, action)
            return JSONResponse({"accepted": True}, status_code=202)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    async def production_questions(request):
        if production is None:
            return production_unavailable()
        try:
            page = max(1, int(request.query_params.get("page", "1")))
            result = await run_in_threadpool(production.questions,
                request.query_params.get("trigger_id"), page,
                request.query_params.get("difficulty") or None, request.query_params.get("search", ""))
            return JSONResponse(result)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def production_entities(request):
        if production is None:
            return production_unavailable()
        try:
            page = max(1, int(request.query_params.get("page", "1")))
            return JSONResponse(await run_in_threadpool(
                production.entity_history, search=request.query_params.get("search", ""), page=page,
            ))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def production_dataset(request):
        if production is None:
            return production_unavailable()
        await run_in_threadpool(production.import_releases)
        return StreamingResponse(
            production.dataset_rows(request.query_params.get("trigger_id")),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": 'attachment; filename="objective-dataset.jsonl"'},
        )

    def close_entity_provider() -> None:
        if provider_instance is not None:
            provider_instance.close()

    @asynccontextmanager
    async def lifespan(_: Starlette):
        try:
            if production is not None:
                await run_in_threadpool(production.recover)
        except ValueError as exc:
            logging.getLogger(__name__).warning("Cannot acquire production coordinator: %s", exc)
        try:
            yield
        finally:
            if objective_job is not None and not objective_job.done():
                await asyncio.gather(objective_job, return_exceptions=True)
            if production is not None:
                await run_in_threadpool(production.close)
            if production_source is not None and production_source is not provider_instance:
                production_source.close()
            close_entity_provider()

    async def generation_workspace(request: Request) -> JSONResponse:
        entity_records = await run_in_threadpool(build_entity_workspaces, repository)
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
                    run_payload = (
                        _objective_run_payload(workspace, run_dir.name)
                        if run_dir.name == task_id
                        else build_objective_summary(
                            workspace,
                            run_dir.name,
                        )
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
        objective_entity_id = None
        if objective_payload is not None:
            goal = objective_payload.get("goal") or {}
            target_name = str(goal.get("target_entity") or "").strip()
            target_id = str(goal.get("target_entity_id") or "").strip()
            target_matches = [
                entity
                for entity in seed_entities
                if (
                    target_name
                    and target_name.casefold()
                    in {
                        entity.raw_mention.casefold(),
                        (entity.canonical_name or "").casefold(),
                    }
                )
                or (
                    target_id
                    and (
                        (entity.canonical_id or "").endswith(
                            f":{target_id}"
                        )
                        or entity.entity_ref_id.endswith(
                            f":{target_id}"
                        )
                    )
                )
            ]
            if not target_matches and target_name:
                try:
                    searched_entities, _ = await run_in_threadpool(
                        search_graph_entities,
                        query=target_name,
                        limit=10,
                    )
                except Exception:
                    searched_entities = []
                for entity in searched_entities:
                    if (
                        entity.name.casefold()
                        == target_name.casefold()
                        or (
                            target_id
                            and entity.canonical_id.endswith(
                                f":{target_id}"
                            )
                        )
                    ):
                        seed_entities.append(_seed_ref(entity))
                        target_matches.append(seed_entities[-1])
                        break
            if target_matches:
                entity = target_matches[0]
                objective_entity_id = (
                    entity.canonical_id or entity.entity_ref_id
                )
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
            else (
                objective_entity_id
                or request.query_params.get("entity")
            )
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
                "engine": "legacy",
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
                "detail_url": f"/?task={selected_task.task_id}",
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
                        "engine": "legacy",
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
                        "review_url": None,
                    }
                    for row in question_rows
                    ]
                ),
                "model": {
                    **model_status,
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

    async def workbench_bootstrap(request: Request) -> JSONResponse:
        """Return generation configuration without mixing run histories."""
        entity_records = await run_in_threadpool(build_entity_workspaces, repository)
        graph_entities, _ = await run_in_threadpool(
            search_graph_entities,
            query="",
            limit=10,
        )
        seed_entities = _merge_seed_entities(graph_entities, entity_records)
        requested_entity = request.query_params.get("entity")
        selected_entity = next(
            (
                entity
                for entity in seed_entities
                if requested_entity
                in {entity.entity_ref_id, entity.canonical_id}
            ),
            seed_entities[0] if seed_entities else None,
        )
        return JSONResponse(
            {
                "entities": [
                    {
                        "value": entity.canonical_id or entity.entity_ref_id,
                        "entity_ref_id": entity.entity_ref_id,
                        "canonical_id": entity.canonical_id,
                        "name": entity.raw_mention,
                        "canonical_name": entity.canonical_name,
                        "entity_type": entity.entity_type,
                    }
                    for entity in seed_entities
                ],
                "selected_entity_id": (
                    selected_entity.canonical_id
                    or selected_entity.entity_ref_id
                    if selected_entity is not None
                    else None
                ),
                "model": model_status,
                "objective_defaults": {
                    "difficulty_targets": list(DEFAULT_DIFFICULTIES),
                    "providers": list(DEFAULT_PROVIDERS),
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

    async def objective_runs(request: Request) -> JSONResponse:
        """List objective runs only, optionally scoped to one entity."""
        entity_id = str(request.query_params.get("entity_id") or "").strip()
        requested_native_id = entity_id.split(":", 2)[-1]
        runs = []
        objective_runs_root = Path(workspace) / "objective_runs"
        run_dirs = (
            sorted(
                (item for item in objective_runs_root.iterdir() if item.is_dir()),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
            if objective_runs_root.is_dir()
            else []
        )
        for run_dir in run_dirs:
            try:
                summary = await run_in_threadpool(
                    build_objective_summary,
                    repository.root,
                    run_dir.name,
                    worker_interrupted=web_worker_interrupted(run_dir.name),
                )
            except (FileNotFoundError, ValueError):
                continue
            except (OSError, CheckpointIntegrityError) as exc:
                logging.getLogger(__name__).warning("Cannot read history for %s: %s", run_dir.name, exc)
                continue
            goal = summary.get("goal") or {}
            target_id = str(goal.get("target_entity_id") or "")
            if entity_id and target_id not in {entity_id, requested_native_id}:
                continue
            runs.append(summary)
        return JSONResponse({"runs": runs, "run_count": len(runs)})

    async def legacy_generation_retired(_: Request) -> JSONResponse:
        return JSONResponse(
            {
                "error": "旧版 Web 生成链路已退役，请使用 objective 生成接口",
                "code": "legacy_generation_retired",
            },
            status_code=410,
        )

    def legacy_task_row(task, *, entity=None) -> dict[str, object]:
        progress = _generation_progress(repository, task.task_id)
        status = str(progress.get("status") or task.status.value)
        return {
            "task_id": task.task_id,
            "engine": "legacy",
            "status": status,
            "status_label": {
                "pending": "旧版准备态",
                "running": "旧版运行中断",
                "needs_input": "旧版待人工输入",
                "completed": "旧版已完成",
                "partial": "旧版部分结果",
                "failed": "旧版失败",
                "cancelled": "旧版已取消",
            }.get(status, status),
            "is_terminal": status
            in {"completed", "partial", "failed", "cancelled"},
            "can_resume": False,
            "target_question_count": task.target_question_count,
            "question_count": int(progress.get("question_count") or 0),
            "stop_reason": progress.get("stop_reason"),
            "updated_at": task.updated_at.isoformat(),
            "entity_id": (
                entity.canonical_id or entity.entity_ref_id
                if entity is not None
                else None
            ),
        }

    async def legacy_tasks(request: Request) -> JSONResponse:
        """List old fixed-workflow tasks without projecting objective state."""
        entity_id = str(request.query_params.get("entity_id") or "").strip()
        rows = []
        seen: set[str] = set()
        for record in build_entity_workspaces(repository):
            record_entity_id = (
                record.entity.canonical_id or record.entity.entity_ref_id
            )
            if entity_id and entity_id != record_entity_id:
                continue
            for task in record.tasks:
                if task.task_id in seen:
                    continue
                seen.add(task.task_id)
                rows.append(legacy_task_row(task, entity=record.entity))
        return JSONResponse({"tasks": rows, "task_count": len(rows)})

    async def legacy_task_detail(request: Request) -> JSONResponse:
        task_id = request.path_params["task_id"]
        if not WEB_TASK_ID_PATTERN.fullmatch(task_id):
            return JSONResponse({"error": "任务 ID 格式无效"}, status_code=400)
        try:
            task = repository.load_latest_task(task_id)
        except RecordNotFoundError:
            return JSONResponse(
                {
                    "error": "旧版任务不存在",
                    "code": "legacy_task_not_found",
                    "task_id": task_id,
                },
                status_code=404,
            )
        payload = legacy_task_row(task)
        progress = await run_in_threadpool(
            _generation_progress,
            repository,
            task_id,
        )
        payload.update(
            {
                "operations": progress.get("operations", []),
                "planner_steps": progress.get("planner_steps", []),
            }
        )
        return JSONResponse(payload)

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

        if generation_busy() or production_busy():
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
        try:
            repository.load_latest_task(run_id)
        except RecordNotFoundError:
            pass
        else:
            return JSONResponse(
                {
                    "error": "run_id 已被旧版任务占用",
                    "code": "run_id_conflict",
                    "run_id": run_id,
                },
                status_code=409,
            )
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
            max_actions = int(payload.get("max_actions", DEFAULT_MAX_ACTIONS))
            if not 1 <= question_count <= 20:
                raise ValueError("题数上限必须在 1 到 20 之间")
            if not 2 <= max_actions <= 100:
                raise ValueError("主 Agent 最大动作数必须在 2 到 100 之间")
        except (TypeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        generation_strategy = str(payload.get("generation_strategy", DEFAULT_GENERATION_STRATEGY))
        if generation_strategy not in {"auto", "easy", "medium", "hard", "free"}:
            return JSONResponse({"error": "未知出题策略"}, status_code=400)
        raw_difficulties = payload.get(
            "difficulty_targets",
            list(DEFAULT_DIFFICULTIES),
        )
        if (
            not isinstance(raw_difficulties, list)
            or not raw_difficulties
        ):
            return JSONResponse(
                {"error": "difficulty_targets 必须是非空数组"},
                status_code=400,
            )
        difficulty_targets = [
            str(item).strip() for item in raw_difficulties
        ]
        if (
            any(
                item not in {"easy", "medium", "hard"}
                for item in difficulty_targets
            )
            or len(set(difficulty_targets)) != len(difficulty_targets)
        ):
            return JSONResponse(
                {
                    "error": (
                        "difficulty_targets 只能包含不重复的 "
                        "easy、medium、hard"
                    )
                },
                status_code=400,
            )
        raw_style_ids = payload.get("question_style_ids")
        if raw_style_ids is not None:
            if (
                not isinstance(raw_style_ids, list)
                or not raw_style_ids
            ):
                return JSONResponse(
                    {"error": "question_style_ids 必须是非空数组"},
                    status_code=400,
                )
            question_style_ids = [
                str(item).strip() for item in raw_style_ids
            ]
            if len(question_style_ids) > 3 or len(
                set(question_style_ids)
            ) != len(question_style_ids):
                return JSONResponse(
                    {
                        "error": (
                            "question_style_ids 必须不重复且最多包含 3 项"
                        )
                    },
                    status_code=400,
                )
            try:
                load_question_skills(question_style_ids)
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            style_targets = question_style_ids
        else:
            raw_style_targets = payload.get("style_targets", list(DEFAULT_STYLE_TARGETS))
            if (
                not isinstance(raw_style_targets, list)
                or not raw_style_targets
            ):
                return JSONResponse(
                    {"error": "style_targets 必须是非空数组"},
                    status_code=400,
                )
            style_targets = [
                str(item).strip() for item in raw_style_targets
            ]
        requested_providers = payload.get(
            "providers",
            list(DEFAULT_PROVIDERS),
        )
        if not isinstance(requested_providers, list) or not requested_providers:
            return JSONResponse({"error": "至少选择一个材料 provider"}, status_code=400)
        providers = list(dict.fromkeys(str(item) for item in requested_providers))
        if not set(providers) <= {
            "primekg",
            "ctd",
            "pubmed",
            "hgnc",
            "ncbi_gene",
            "ensembl",
        }:
            return JSONResponse(
                {"error": f"不支持的材料 provider：{providers}"},
                status_code=400,
            )
        raw_fact_types = payload.get("allowed_fact_types")
        if raw_fact_types is None:
            allowed_fact_types = default_objective_fact_types(
                entity_type,
                available_providers=providers,
            )
        else:
            if (
                not isinstance(raw_fact_types, list)
                or not raw_fact_types
            ):
                return JSONResponse(
                    {"error": "allowed_fact_types 必须是非空数组"},
                    status_code=400,
                )
            allowed_fact_types = [
                str(item).strip() for item in raw_fact_types
            ]
            if (
                any(
                    item not in FACT_SOURCE_ROUTES
                    for item in allowed_fact_types
                )
                or len(set(allowed_fact_types))
                != len(allowed_fact_types)
            ):
                return JSONResponse(
                    {
                        "error": (
                            "allowed_fact_types 包含未知或重复的事实类型"
                        )
                    },
                    status_code=400,
                )
        requested_model_provider = str(
            payload.get("model_provider_id") or ""
        ).strip()
        selected_model_provider = requested_model_provider or str(
            model_status.get("default_provider") or ""
        )
        if structured_model is None:
            try:
                registry = ProviderRegistry.from_config()
                provider_status = next(
                    item
                    for item in registry.describe()
                    if item["provider_id"] == selected_model_provider
                )
            except (ModelError, StopIteration) as exc:
                detail = (
                    exc.message
                    if isinstance(exc, ModelError)
                    else f"未知模型 provider：{selected_model_provider}"
                )
                return JSONResponse(
                    {"error": detail, "code": "model_config_error"},
                    status_code=422,
                )
            if not provider_status["ready"]:
                return JSONResponse(
                    {
                        "error": (
                            f"主 Agent 模型未就绪：{provider_status['detail']}"
                        ),
                        "code": "model_config_error",
                    },
                    status_code=422,
                )
        elif requested_model_provider and requested_model_provider != str(
            model_status["default_provider"]
        ):
            return JSONResponse(
                {
                    "error": "注入模型不支持切换 provider",
                    "code": "model_config_error",
                },
                status_code=400,
            )
        if generation_busy() or production_busy():
            return JSONResponse(
                {"error": "已有生成任务正在运行，请等待它完成"},
                status_code=409,
            )
        try:
            model = structured_model
            if model is None:
                from ..llm.registry import build_model_port

                model = build_model_port(selected_model_provider or None)
            result = await run_objective_job(
                run_id,
                _run_objective_web_generation,
                workspace=repository.root,
                entity_canonical_id=canonical_id,
                entity_name=entity_name,
                entity_type=entity_type,
                question_count=question_count,
                max_actions=max_actions,
                providers=providers,
                allowed_fact_types=allowed_fact_types,
                difficulty_targets=difficulty_targets,
                generation_strategy=generation_strategy,
                style_targets=style_targets,
                model=model,
                model_provider_id=selected_model_provider,
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

    async def pause_objective_generation(request: Request) -> JSONResponse:
        run_id = request.path_params["run_id"]
        if not WEB_TASK_ID_PATTERN.fullmatch(run_id):
            return JSONResponse({"error": "任务 ID 格式无效"}, status_code=400)
        runtime = ObjectiveRuntimeRepository(_objective_run_root(repository.root, run_id), run_id=run_id)
        try:
            await run_in_threadpool(runtime.request_pause)
        except FileNotFoundError:
            return JSONResponse({"error": "任务尚未创建或不存在，请稍后重试"}, status_code=404)
        except ValueError:
            return JSONResponse({"error": "任务已停止，无需暂停"}, status_code=409)
        return JSONResponse({"run_id": run_id, "pause_requested": True}, status_code=202)

    async def resume_objective_generation(
        request: Request,
    ) -> JSONResponse:
        run_id = request.path_params["run_id"]
        if production_owns_run(run_id):
            return JSONResponse({"error": "请在自动出题页面恢复所属触发任务，避免重复执行实体"}, status_code=409)
        if not WEB_TASK_ID_PATTERN.fullmatch(run_id):
            return JSONResponse(
                {"error": "任务 ID 格式无效"},
                status_code=400,
            )
        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            return JSONResponse(
                {"error": "请求内容必须是 JSON 对象"},
                status_code=400,
            )
        model_provider_id = str(
            payload.get("model_provider_id") or ""
        ).strip()
        try:
            snapshot = await run_in_threadpool(
                ObjectiveRuntimeRepository(
                    _objective_run_root(repository.root, run_id),
                    run_id=run_id,
                ).load
            )
        except FileNotFoundError:
            return JSONResponse(
                {
                    "error": "生成任务不存在",
                    "code": "run_not_found",
                    "run_id": run_id,
                },
                status_code=404,
            )
        recoverable = (
            snapshot.run_state is RuntimeRunState.RECOVERY_REQUIRED
        )
        interrupted = web_worker_interrupted(run_id)
        if not recoverable:
            _last_commit, _age, stalled = await run_in_threadpool(
                _objective_progress_timing,
                _objective_run_root(repository.root, run_id),
                terminal=False,
            )
            recoverable = stalled or interrupted
        if not recoverable:
            return JSONResponse(
                {
                    "error": "任务当前不在可恢复或停滞状态",
                    "code": "run_not_resumable",
                    "run_id": run_id,
                    "run_state": snapshot.run_state.value,
                },
                status_code=409,
            )
        if generation_busy() or production_busy():
            return JSONResponse(
                {"error": "已有生成任务正在运行，请等待它完成"},
                status_code=409,
            )
        try:
            result = await run_objective_job(
                run_id,
                _resume_objective_web_generation,
                workspace=repository.root,
                model=structured_model,
                model_provider_id=model_provider_id or None,
                objective_providers=objective_providers,
                worker_interrupted=interrupted,
            )
        except FileNotFoundError:
            return JSONResponse(
                {
                    "error": "生成任务不存在",
                    "code": "run_not_found",
                    "run_id": run_id,
                },
                status_code=404,
            )
        except ValueError as exc:
            return JSONResponse(
                {
                    "error": str(exc),
                    "code": "run_not_resumable",
                    "run_id": run_id,
                },
                status_code=409,
            )
        except Exception as exc:
            return JSONResponse(
                {
                    "error": str(exc),
                    "code": str(
                        getattr(exc, "code", "objective_resume_error")
                    ),
                    "run_id": run_id,
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
                    "error": "生成任务不存在",
                    "code": "run_not_found",
                    "run_id": run_id,
                },
                status_code=404,
            )
        except Exception as exc:
            return JSONResponse(
                {"error": str(exc), "code": "objective_progress_error"},
                status_code=500,
            )
        return JSONResponse(payload)

    async def objective_summary(request: Request) -> JSONResponse:
        run_id = request.path_params["run_id"]
        if not WEB_TASK_ID_PATTERN.fullmatch(run_id):
            return JSONResponse({"error": "任务 ID 格式无效"}, status_code=400)
        try:
            payload = await run_in_threadpool(
                build_objective_summary,
                repository.root,
                run_id,
                worker_interrupted=web_worker_interrupted(run_id),
            )
        except FileNotFoundError:
            return JSONResponse(
                {"error": "生成任务不存在", "code": "run_not_found"},
                status_code=404,
            )
        except Exception as exc:
            return JSONResponse(
                {"error": str(exc), "code": "objective_summary_error"},
                status_code=500,
            )
        return JSONResponse(payload)

    async def objective_workbench(request: Request) -> JSONResponse:
        run_id = request.path_params["run_id"]
        if not WEB_TASK_ID_PATTERN.fullmatch(run_id):
            return JSONResponse({"error": "任务 ID 格式无效"}, status_code=400)
        try:
            payload = await run_in_threadpool(
                build_objective_workbench,
                repository.root,
                run_id,
                worker_interrupted=web_worker_interrupted(run_id),
            )
        except FileNotFoundError:
            return JSONResponse(
                {"error": "生成任务不存在", "code": "run_not_found"},
                status_code=404,
            )
        except Exception as exc:
            return JSONResponse(
                {"error": str(exc), "code": "objective_workbench_error"},
                status_code=500,
            )
        return JSONResponse(payload)

    async def question_evidence(request: Request) -> JSONResponse:
        question_id = request.path_params["question_id"]
        if not WEB_TASK_ID_PATTERN.fullmatch(question_id):
            return JSONResponse({"error": "题目 ID 格式无效"}, status_code=400)
        try:
            payload = await run_in_threadpool(
                build_question_evidence_dossier,
                repository.root,
                question_id,
            )
        except FileNotFoundError:
            return JSONResponse(
                {"error": "题目不存在", "code": "question_not_found"},
                status_code=404,
            )
        except Exception as exc:
            return JSONResponse(
                {"error": str(exc), "code": "question_evidence_error"},
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

    async def published_questions_api(_: Request) -> JSONResponse:
        rows = _objective_published_question_rows(repository.root)
        questions = [
            {
                **row,
                "published_at": row["published_at"].isoformat(),
            }
            for row in rows
        ]
        return JSONResponse(
            {
                "questions": questions,
                "question_count": len(questions),
                "run_count": len(
                    {row["run_id"] for row in questions}
                ),
            }
        )

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

    async def root(_: Request) -> JSONResponse:
        return JSONResponse(
            {
                "service": "medical-qa-data-factory-api",
                "status": "ok",
                "frontend": "run the Vue app separately",
            }
        )

    async def not_found(_: Request, __: Exception) -> JSONResponse:
        return JSONResponse(
            {"error": "Not found", "code": "not_found"},
            status_code=404,
        )

    routes = [
        Route("/api/objective/{run_id}/pause", pause_objective_generation, methods=["POST"]),
        Route("/", root, name="api_root"),
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
            legacy_generation_retired,
            methods=["POST"],
            name="start_generation",
        ),
        Route(
            "/api/workbench/bootstrap",
            workbench_bootstrap,
            methods=["GET"],
            name="workbench_bootstrap",
        ),
        Route(
            "/api/objective/runs",
            objective_runs,
            methods=["GET"],
            name="objective_runs",
        ),
        Route(
            "/api/objective/generate",
            start_objective_generation,
            methods=["POST"],
            name="start_objective_generation",
        ),
        Route(
            "/api/objective/{run_id}/summary",
            objective_summary,
            methods=["GET"],
            name="objective_summary",
        ),
        Route(
            "/api/objective/{run_id}/resume",
            resume_objective_generation,
            methods=["POST"],
            name="resume_objective_generation",
        ),
        Route(
            "/api/objective/{run_id}/workbench",
            objective_workbench,
            methods=["GET"],
            name="objective_workbench",
        ),
        Route(
            "/api/objective/{run_id}/trace",
            objective_progress,
            methods=["GET"],
            name="objective_trace",
        ),
        Route(
            "/api/questions/{question_id}/evidence",
            question_evidence,
            methods=["GET"],
            name="question_evidence",
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
        Route(
            "/api/legacy/tasks",
            legacy_tasks,
            methods=["GET"],
            name="legacy_tasks",
        ),
        Route(
            "/api/legacy/tasks/{task_id}",
            legacy_task_detail,
            methods=["GET"],
            name="legacy_task_detail",
        ),
        Route(
            "/api/published-questions",
            published_questions_api,
            methods=["GET"],
            name="published_questions_api",
        ),
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
        Route("/api/production", production_api, methods=["GET", "POST"]),
        Route("/api/production/questions", production_questions, methods=["GET"]),
        Route("/api/production/entities", production_entities, methods=["GET"]),
        Route("/api/production/dataset", production_dataset, methods=["GET"]),
        Route("/api/production/{trigger_id}/{action}", production_control, methods=["POST"]),
        Route("/health", health, name="health"),
        Route("/api/export", export_dataset, methods=["POST"], name="export_dataset"),
    ]
    app = Starlette(
        routes=routes,
        exception_handlers={404: not_found},
        lifespan=lifespan,
        debug=False,
    )
    allowed_origins = [
        item.strip()
        for item in os.environ.get(
            "MEDICAL_QA_CORS_ORIGINS",
            "http://127.0.0.1:5173,http://localhost:5173",
        ).split(",")
        if item.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
    )
    return app


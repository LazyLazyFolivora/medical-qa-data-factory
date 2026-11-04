"""Read-only projections for the objective-question research workbench."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..generation.objective_question_contracts import HypothesisState, RunState
from ..generation.objective_question_runtime import ObjectiveRuntimeRepository
from ..generation.workflow_scheduler import target_question_count


OBJECTIVE_STALL_AFTER_SECONDS = 10 * 60

_TERMINAL_STATES = {
    RunState.COMPLETED,
    RunState.COMPLETED_WITH_ZERO_QUESTIONS,
    RunState.PROVIDER_FAILED,
    RunState.RECOVERY_REQUIRED,
    RunState.VERSION_INCOMPATIBLE,
    RunState.CANCELLED,
}
_FAILED_STATES = {
    RunState.PROVIDER_FAILED,
    RunState.RECOVERY_REQUIRED,
    RunState.VERSION_INCOMPATIBLE,
    RunState.CANCELLED,
}

_PROVIDER_LABELS = {
    "primekg": "PrimeKG",
    "pubmed": "PubMed",
    "ctd": "CTD",
    "rag": "RAG",
    "hgnc": "HGNC",
    "ncbi_gene": "NCBI Gene",
    "ensembl": "Ensembl",
}
_MATERIAL_LABELS = {
    "knowledge_graph_edge": "知识图谱边",
    "literature_abstract": "文献摘要",
    "literature_full_text": "文献全文",
    "hgnc_gene_record": "HGNC 基因记录",
    "ncbi_gene_record": "NCBI Gene 记录",
    "ensembl_gene_record": "Ensembl 基因记录",
    "ctd_chemical_disease_record": "CTD 化学物—疾病记录",
    "text": "文本材料",
}
_STATE_LABELS = {
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
_QUESTION_STATUS_LABELS = {
    "published": "已发布",
    "rejected": "已拒绝",
    "indeterminate": "无法确定",
    "candidate": "候选中",
}
_ACTION_LABELS = {
    "retrieve_materials": "收集材料",
    "request_more_evidence": "补充证据",
    "understand_facts": "理解并提取事实",
    "design_questions": "设计候选题",
    "verify_answer": "独立验证答案",
    "realize_question": "生成最终题面",
    "review_release": "发布复核",
    "stop": "结束运行",
    "close_candidate": "结束当前候选",
}


def _run_root(workspace: str | Path, run_id: str) -> Path:
    return Path(workspace) / "objective_runs" / run_id


def _release_root(workspace: str | Path, run_id: str) -> Path:
    return Path(workspace) / "objective_releases" / run_id


def _public_questions(
    workspace: str | Path,
    run_id: str,
) -> list[dict[str, Any]]:
    path = _release_root(workspace, run_id) / "public" / "questions.jsonl"
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            payload = json.loads(line)
            if isinstance(payload, dict):
                rows.append(payload)
    return rows


def _runtime_snapshot(workspace: str | Path, run_id: str):
    return ObjectiveRuntimeRepository(
        _run_root(workspace, run_id),
        run_id=run_id,
    ).load()


def public_questions_for_run(workspace, run_id, snapshot, *, index=None):
    """Include committed publications before final export, keeping exported rows by ID."""
    rows = {row["question_id"]: row for row in _public_questions(workspace, run_id)}
    if snapshot is None or HypothesisState.PUBLISHED not in snapshot.hypotheses.values():
        return list(rows.values())
    if index is None:
        index = _runtime_index(snapshot)
    for qid, report in index["reports"].items():
        if qid in rows or report.get("verdict") != "PASS" or not report.get("release_allowed"):
            continue
        draft = index["drafts"].get(qid)
        if draft is None:
            continue
        contract = index["contracts"].get(draft["contract_id"], {})
        resolution = index["resolutions"].get(contract.get("resolution_id"), {})
        if snapshot.hypotheses.get(resolution.get("hypothesis_id")) is not HypothesisState.PUBLISHED:
            continue
        rows[qid] = {
            "question_id": qid, "question": draft["question_text"],
            "answer": draft["answer_values"], "explanation": draft.get("concise_explanation"),
            "assessed_difficulty": report.get("assessed_difficulty"),
            "schema_version": "objective-public-question-v1",
        }
    return list(rows.values())


def _progress_timing(
    workspace: str | Path,
    run_id: str,
    *,
    terminal: bool,
    now: datetime | None = None,
) -> tuple[datetime | None, int, bool]:
    commit_dir = _run_root(workspace, run_id) / "objective_runtime" / "commits"
    files = (
        sorted(commit_dir.glob("*.json"), key=lambda item: int(item.stem))
        if commit_dir.is_dir()
        else []
    )
    last_commit_at = (
        datetime.fromtimestamp(files[-1].stat().st_mtime, tz=timezone.utc)
        if files
        else None
    )
    if last_commit_at is None:
        return None, 0, False
    current = now or datetime.now(timezone.utc)
    age = max(0, int((current - last_commit_at).total_seconds()))
    return (
        last_commit_at,
        age,
        not terminal and age >= OBJECTIVE_STALL_AFTER_SECONDS,
    )


@lru_cache(maxsize=4096)
def _commit_decision_ids(
    path: str, mtime_ns: int, ctime_ns: int, size: int
) -> tuple[str, ...]:
    """Cache only IDs; file metadata invalidates edits without retaining snapshots.

    Failed reads are not cached, so a transient failure can recover next poll.
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    snapshot = payload.get("snapshot", {}) if isinstance(payload, dict) else {}
    decisions = snapshot.get("decisions", {}) if isinstance(snapshot, dict) else {}
    return tuple(str(key) for key in decisions) if isinstance(decisions, dict) else ()


def objective_decision_order(
    workspace: str | Path,
    run_id: str,
    snapshot,
) -> list[Any]:
    commit_dir = _run_root(workspace, run_id) / "objective_runtime" / "commits"
    files = (
        sorted(commit_dir.glob("*.json"), key=lambda item: int(item.stem))
        if commit_dir.is_dir()
        else []
    )
    seen: set[str] = set()
    ordered_ids: list[str] = []
    for path in files:
        try:
            stat = path.stat()
            decision_ids = _commit_decision_ids(
                str(path.resolve()), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size
            )
        except (OSError, ValueError):
            continue
        for decision_id in decision_ids:
            if decision_id not in seen:
                seen.add(decision_id)
                ordered_ids.append(decision_id)
    if len(ordered_ids) < len(snapshot.decisions):
        ordered_ids.extend(
            decision_id
            for decision_id in snapshot.decisions
            if decision_id not in seen
        )
    return [
        snapshot.decisions[decision_id]
        for decision_id in ordered_ids
        if decision_id in snapshot.decisions
    ]


def _latest_decision(workspace: str | Path, run_id: str, snapshot):
    """Summaries need only the newest decision, not the entire history."""
    if not snapshot.decisions:
        return None
    commit_dir = _run_root(workspace, run_id) / "objective_runtime" / "commits"
    candidates = list(snapshot.decisions)
    for path in sorted(commit_dir.glob("*.json"), key=lambda item: int(item.stem), reverse=True):
        try:
            stat = path.stat()
            ids = _commit_decision_ids(str(path.resolve()), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
        except (OSError, ValueError):
            continue
        previous = set(ids)
        added = [key for key in candidates if key not in previous]
        if added:
            return snapshot.decisions[added[-1]]
        candidates = [key for key in ids if key in snapshot.decisions]
    return snapshot.decisions[candidates[-1]] if candidates else None


def _summary_from_snapshot(
    workspace: str | Path,
    run_id: str,
    snapshot,
    *,
    public_questions: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    questions = (
        public_questions
        if public_questions is not None
        else public_questions_for_run(workspace, run_id, snapshot)
    )
    terminal = snapshot.run_state in _TERMINAL_STATES
    last_commit_at, age, stalled = _progress_timing(
        workspace,
        run_id,
        terminal=terminal,
        now=now,
    )
    status = (
        "stalled"
        if stalled
        else "failed"
        if snapshot.run_state in _FAILED_STATES
        else "completed"
        if terminal
        else "running"
    )
    rejected_count = sum(
        state is HypothesisState.REJECTED
        for state in snapshot.hypotheses.values()
    )
    indeterminate_count = sum(
        state is HypothesisState.INDETERMINATE
        for state in snapshot.hypotheses.values()
    )
    candidate_count = sum(
        state
        not in {
            HypothesisState.PUBLISHED,
            HypothesisState.REJECTED,
            HypothesisState.INDETERMINATE,
        }
        for state in snapshot.hypotheses.values()
    )
    stop_reason = snapshot.stop_reason
    if stalled:
        stop_reason = (
            f"主 Agent 已超过 {OBJECTIVE_STALL_AFTER_SECONDS // 60} 分钟"
            "没有产生新的 runtime commit，任务可能卡在模型调用或后台异常。"
        )
    status_label = (
        "长时间无进展"
        if stalled
        else _STATE_LABELS.get(
            snapshot.run_state.value,
            snapshot.run_state.value,
        )
    )
    decision_count = len(snapshot.decisions)
    last_decision = _latest_decision(workspace, run_id, snapshot)
    latest_slots = (
        [
            slot
            for slot in snapshot.child_slots.values()
            if slot.parent_decision_id == last_decision.decision_id
        ]
        if last_decision is not None
        else []
    )
    pending_decision = (
        last_decision
        if last_decision is not None
        and (
            not latest_slots
            or any(slot.state == "OPEN" for slot in latest_slots)
        )
        else None
    )
    last_successful_step = (
        decision_count - 1
        if pending_decision is not None
        else decision_count
    )
    next_step = (
        decision_count
        if pending_decision is not None
        else snapshot.resume_after_step
        if snapshot.resume_after_step is not None
        else last_successful_step + 1
    )
    current_action = (
        {
            "action": last_decision.action,
            "action_label": _ACTION_LABELS.get(
                last_decision.action,
                last_decision.action,
            ),
            "target_subagent_role": last_decision.target_subagent_role,
            "parameters": last_decision.action_parameters,
            "stop_reason": last_decision.stop_reason,
        }
        if last_decision is not None
        else {
            "action": snapshot.run_state.value,
            "action_label": status_label,
            "target_subagent_role": None,
            "parameters": {},
            "stop_reason": None,
        }
    )
    return {
        "schema_version": "objective-run-summary-v1",
        "run_id": run_id,
        "task_id": run_id,
        "engine": "objective",
        "status": status,
        "status_label": "已暂停" if snapshot.stop_reason == "user_paused" else status_label,
        "run_state": snapshot.run_state.value,
        "is_terminal": terminal,
        "pause_requested": (_run_root(workspace, run_id) / "objective_runtime" / "pause.requested").exists(),
        "can_resume": (
            snapshot.run_state is RunState.RECOVERY_REQUIRED
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
        "stop_reason": stop_reason,
        "last_commit_at": (
            last_commit_at.isoformat() if last_commit_at is not None else None
        ),
        "seconds_since_progress": age,
        "stalled": stalled,
        "target_question_count": (
            snapshot.goal.question_count_upper_bound
            if snapshot.goal is not None
            else None
        ),
        "published_count": len(questions),
        "counted_toward_target": target_question_count(snapshot),
        "candidate_count": candidate_count,
        "rejected_count": rejected_count,
        "indeterminate_count": indeterminate_count,
        "main_agent_step_count": len(snapshot.decisions),
        "current_action": current_action,
        "workflow_phase": last_decision.action_parameters.get("phase") if last_decision else "exploration",
        "active_hypothesis_id": last_decision.action_parameters.get("hypothesis_id") if last_decision else None,
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
    }


def build_objective_summary(
    workspace: str | Path,
    run_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    snapshot = _runtime_snapshot(workspace, run_id)
    return _summary_from_snapshot(
        workspace,
        run_id,
        snapshot,
        now=now,
    )


def _runtime_index(snapshot) -> dict[str, dict[str, Any]]:
    drafts: dict[str, dict[str, Any]] = {}
    contracts: dict[str, dict[str, Any]] = {}
    resolutions: dict[str, dict[str, Any]] = {}
    anchors: dict[str, dict[str, Any]] = {}
    materials: dict[str, dict[str, Any]] = {}
    hypotheses: dict[str, dict[str, Any]] = {}
    reports: dict[str, dict[str, Any]] = {}
    for artifact in snapshot.artifacts.values():
        payload = artifact.payload
        if artifact.artifact_type == "objective_question_draft":
            drafts[str(payload.get("draft_id"))] = payload
        elif artifact.artifact_type == "frozen_question_contract":
            contracts[str(payload.get("contract_id"))] = payload
        elif artifact.artifact_type == "answer_resolution":
            resolutions[str(payload.get("resolution_id"))] = payload
        elif artifact.artifact_type == "evidence_anchor":
            anchors[str(payload.get("anchor_id"))] = payload
        elif artifact.artifact_type == "evidence_material":
            materials[str(payload.get("material_id"))] = payload
        elif artifact.artifact_type == "question_hypothesis":
            hypotheses[str(payload.get("hypothesis_id"))] = payload
        elif artifact.artifact_type == "objective_validation_report":
            reports[str(payload.get("question_id"))] = payload
    return {
        "drafts": drafts,
        "contracts": contracts,
        "resolutions": resolutions,
        "anchors": anchors,
        "materials": materials,
        "hypotheses": hypotheses,
        "reports": reports,
    }


def _question_status(
    report: dict[str, Any] | None,
    hypothesis_state: HypothesisState | None,
) -> str:
    if report is not None:
        if report.get("release_allowed"):
            return "published"
        if report.get("verdict") == "REJECT":
            return "rejected"
        return "indeterminate"
    if hypothesis_state is HypothesisState.PUBLISHED:
        return "published"
    if hypothesis_state is HypothesisState.REJECTED:
        return "rejected"
    if hypothesis_state is HypothesisState.INDETERMINATE:
        return "indeterminate"
    return "candidate"


def _answer_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _question_row(
    question_id: str,
    index: dict[str, dict[str, Any]],
    hypothesis_states: dict[str, HypothesisState],
    *,
    public_question: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    draft = index["drafts"].get(question_id)
    contract = (
        index["contracts"].get(str(draft.get("contract_id")))
        if draft is not None
        else None
    )
    resolution = (
        index["resolutions"].get(str(contract.get("resolution_id")))
        if contract is not None
        else None
    )
    report = index["reports"].get(question_id)
    hypothesis_id = (
        str(resolution.get("hypothesis_id")) if resolution is not None else None
    )
    hypothesis_state = (
        hypothesis_states.get(hypothesis_id) if hypothesis_id else None
    )
    status = _question_status(report, hypothesis_state)
    answers = (
        public_question.get("answer")
        if public_question is not None and "answer" in public_question
        else draft.get("answer_values", [])
        if draft is not None
        else []
    )
    return {
        "question_id": question_id,
        "draft_id": question_id if draft is not None else None,
        "hypothesis_id": hypothesis_id,
        "text": (
            public_question.get("question")
            if public_question is not None
            else draft.get("question_text")
            if draft is not None
            else None
        ),
        "answer": _answer_values(answers),
        "assessed_difficulty": (report or {}).get("assessed_difficulty"),
        "explanation": (
            public_question.get("explanation")
            if public_question is not None
            else draft.get("concise_explanation")
            if draft is not None
            else None
        ),
        "status": status,
        "status_label": _QUESTION_STATUS_LABELS[status],
        "hypothesis_state": (
            hypothesis_state.value if hypothesis_state is not None else None
        ),
        "verdict": report.get("verdict") if report is not None else None,
        "release_allowed": (
            bool(report.get("release_allowed")) if report is not None else False
        ),
        "reason": report.get("primary_reason") if report is not None else None,
        "blocking_findings": (
            list(report.get("blocking_findings", []))
            if report is not None
            else []
        ),
        "evidence_count": (
            len(resolution.get("supporting_anchor_ids", []))
            if resolution is not None
            else 0
        ),
        "public_scope": (
            contract.get("public_scope") if contract is not None else None
        ),
        "answer_cardinality": (
            contract.get("answer_cardinality")
            if contract is not None
            else None
        ),
    }


def _candidate_rows(
    snapshot,
    index: dict[str, dict[str, Any]],
    published_ids: set[str],
) -> list[dict[str, Any]]:
    resolutions_by_hypothesis = {
        str(resolution.get("hypothesis_id")): resolution
        for resolution in index["resolutions"].values()
    }
    contracts_by_resolution = {
        str(contract.get("resolution_id")): contract
        for contract in index["contracts"].values()
    }
    drafts_by_contract = {
        str(draft.get("contract_id")): draft
        for draft in index["drafts"].values()
    }
    rows = []
    for hypothesis_id, state in snapshot.hypotheses.items():
        resolution = resolutions_by_hypothesis.get(hypothesis_id)
        contract = (
            contracts_by_resolution.get(str(resolution.get("resolution_id")))
            if resolution is not None
            else None
        )
        draft = (
            drafts_by_contract.get(str(contract.get("contract_id")))
            if contract is not None
            else None
        )
        question_id = (
            str(draft.get("draft_id")) if draft is not None else None
        )
        if question_id and question_id in published_ids:
            continue
        report = index["reports"].get(question_id) if question_id else None
        status = _question_status(report, state)
        hypothesis = index["hypotheses"].get(hypothesis_id, {})
        row = (
            _question_row(
                question_id,
                index,
                snapshot.hypotheses,
            )
            if question_id
            else None
        )
        if row is None:
            row = {
                "question_id": None,
                "draft_id": None,
                "hypothesis_id": hypothesis_id,
                "text": None,
                "answer": list(
                    hypothesis.get("proposed_answer_values", [])
                ),
                "explanation": None,
                "status": status,
                "status_label": _QUESTION_STATUS_LABELS[status],
                "hypothesis_state": state.value,
                "verdict": (
                    report.get("verdict") if report is not None else None
                ),
                "release_allowed": False,
                "reason": (
                    report.get("primary_reason")
                    if report is not None
                    else None
                ),
                "blocking_findings": [],
                "evidence_count": (
                    len(resolution.get("supporting_anchor_ids", []))
                    if resolution is not None
                    else 0
                ),
                "public_scope": (
                    contract.get("public_scope")
                    if contract is not None
                    else hypothesis.get("proposed_public_scope")
                ),
                "answer_cardinality": (
                    contract.get("answer_cardinality")
                    if contract is not None
                    else hypothesis.get("proposed_cardinality")
                ),
            }
        row["question_intent"] = hypothesis.get("question_intent")
        row["candidate_id"] = question_id or hypothesis_id
        rows.append(row)
    return rows


def _retrieval_sources(snapshot) -> list[dict[str, Any]]:
    rows = []
    for artifact in snapshot.artifacts.values():
        if artifact.artifact_type != "provider_result":
            continue
        payload = artifact.payload
        request = payload.get("request", {})
        query = request.get("query", {})
        provider_id = str(request.get("provider_id", ""))
        if isinstance(query, dict) and isinstance(query.get(provider_id), dict):
            query = query[provider_id]
        usage = payload.get("usage", {})
        trace = usage.get("search_trace", [])
        if trace:
            for item in trace:
                trace_query = item.get("query", {})
                rows.append(
                    {
                        "provider_id": provider_id,
                        "provider_label": _PROVIDER_LABELS.get(
                            provider_id,
                            provider_id,
                        ),
                        "purpose": artifact.metadata.get(
                            "purpose",
                            "discovery",
                        ),
                        "terminal_status": item.get("terminal_status"),
                        "query": trace_query,
                        "query_label": _query_label(trace_query),
                        "search_round": item.get("round"),
                        "material_count": item.get("material_count", 0),
                        "candidate_count": usage.get(
                            "candidate_count",
                            item.get("material_count", 0),
                        ),
                        "search_rationale": usage.get("search_rationale"),
                        "detail_trace": usage.get("detail_trace", []),
                        "error_category": item.get("error_category"),
                    }
                )
        else:
            rows.append(
                {
                    "provider_id": provider_id,
                    "provider_label": _PROVIDER_LABELS.get(
                        provider_id,
                        provider_id,
                    ),
                    "purpose": artifact.metadata.get(
                        "purpose",
                        "discovery",
                    ),
                    "terminal_status": payload.get("terminal_status"),
                    "query": query,
                    "query_label": _query_label(query),
                    "search_round": None,
                    "material_count": len(payload.get("material_ids", [])),
                    "candidate_count": usage.get(
                        "candidate_count",
                        len(payload.get("material_ids", [])),
                    ),
                    "search_rationale": usage.get("search_rationale"),
                    "detail_trace": usage.get("detail_trace", []),
                    "error_category": payload.get("error_category"),
                }
            )
    return rows


def _query_label(query: Any) -> str:
    if not isinstance(query, dict):
        return str(query or "")
    return " · ".join(f"{key}={value}" for key, value in query.items())


def build_objective_workbench(
    workspace: str | Path,
    run_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    snapshot = _runtime_snapshot(workspace, run_id)
    index = _runtime_index(snapshot)
    public_questions = public_questions_for_run(workspace, run_id, snapshot, index=index)
    published = []
    for public_question in public_questions:
        question_id = str(public_question.get("question_id", ""))
        detail = _question_row(
            question_id,
            index,
            snapshot.hypotheses,
            public_question=public_question,
        )
        published.append(
            detail
            or {
                "question_id": question_id,
                "draft_id": question_id,
                "hypothesis_id": None,
                "text": public_question.get("question"),
                "answer": _answer_values(public_question.get("answer")),
                "explanation": public_question.get("explanation"),
                "status": "published",
                "status_label": _QUESTION_STATUS_LABELS["published"],
                "hypothesis_state": None,
                "verdict": "PASS",
                "release_allowed": True,
                "reason": None,
                "blocking_findings": [],
                "evidence_count": 0,
                "public_scope": None,
                "answer_cardinality": None,
            }
        )
    published_ids = {
        str(item["question_id"])
        for item in published
        if item.get("question_id")
    }
    candidates = _candidate_rows(snapshot, index, published_ids)
    summary = _summary_from_snapshot(
        workspace,
        run_id,
        snapshot,
        public_questions=public_questions,
        now=now,
    )
    return {
        **summary,
        "questions": published,
        "published_questions": published,
        "candidate_questions": [
            item for item in candidates if item["status"] == "candidate"
        ],
        "rejected_questions": [
            item for item in candidates if item["status"] == "rejected"
        ],
        "indeterminate_questions": [
            item for item in candidates if item["status"] == "indeterminate"
        ],
        "retrieval_sources": _retrieval_sources(snapshot),
        "artifact_counts": {
            artifact_type: sum(
                artifact.artifact_type == artifact_type
                for artifact in snapshot.artifacts.values()
            )
            for artifact_type in sorted(
                {
                    artifact.artifact_type
                    for artifact in snapshot.artifacts.values()
                }
            )
        },
    }


def _evidence_rows(
    anchor_ids: list[str],
    *,
    role: str,
    index: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = []
    seen = set()
    for anchor_id in anchor_ids:
        anchor = index["anchors"].get(str(anchor_id))
        if anchor is None or anchor_id in seen:
            continue
        seen.add(anchor_id)
        material = index["materials"].get(str(anchor.get("material_id")))
        provider_id = (
            str(material.get("provider_id"))
            if material is not None
            else ""
        )
        material_type = (
            str(material.get("material_type"))
            if material is not None
            else ""
        )
        rows.append(
            {
                "role": role,
                "anchor_id": anchor_id,
                "material_id": anchor.get("material_id"),
                "provider_id": provider_id,
                "provider_label": _PROVIDER_LABELS.get(
                    provider_id, provider_id
                ),
                "source_record_id": (
                    material.get("source_record_id")
                    if material is not None
                    else None
                ),
                "material_type": material_type,
                "material_label": _MATERIAL_LABELS.get(
                    material_type, material_type
                ),
                "document_id": anchor.get("document_id"),
                "section_or_field": anchor.get("section_or_field"),
                "start_offset": anchor.get("start_offset"),
                "end_offset": anchor.get("end_offset"),
                "quoted_text": anchor.get("quoted_text"),
            }
        )
    return rows


def _find_question(
    workspace: str | Path,
    question_id: str,
) -> tuple[str, dict[str, Any] | None, Any]:
    releases_root = Path(workspace) / "objective_releases"
    if releases_root.is_dir():
        for release_dir in sorted(
            (item for item in releases_root.iterdir() if item.is_dir()),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        ):
            for question in _public_questions(workspace, release_dir.name):
                if str(question.get("question_id")) == question_id:
                    try:
                        snapshot = _runtime_snapshot(
                            workspace,
                            release_dir.name,
                        )
                    except Exception:
                        continue
                    return release_dir.name, question, snapshot
    runs_root = Path(workspace) / "objective_runs"
    if runs_root.is_dir():
        for run_dir in sorted(
            (item for item in runs_root.iterdir() if item.is_dir()),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        ):
            try:
                snapshot = _runtime_snapshot(workspace, run_dir.name)
            except Exception:
                continue
            artifact = snapshot.artifacts.get(question_id)
            if (
                artifact is not None
                and artifact.artifact_type == "objective_question_draft"
            ):
                return run_dir.name, None, snapshot
    raise FileNotFoundError(question_id)


def build_question_evidence_dossier(
    workspace: str | Path,
    question_id: str,
) -> dict[str, Any]:
    run_id, public_question, snapshot = _find_question(workspace, question_id)
    index = _runtime_index(snapshot)
    question = _question_row(
        question_id,
        index,
        snapshot.hypotheses,
        public_question=public_question,
    )
    if question is None:
        raise FileNotFoundError(question_id)
    draft = index["drafts"].get(question_id, {})
    contract = index["contracts"].get(str(draft.get("contract_id")), {})
    resolution = index["resolutions"].get(
        str(contract.get("resolution_id")),
        {},
    )
    report = index["reports"].get(question_id)
    evidence = [
        *_evidence_rows(
            list(resolution.get("supporting_anchor_ids", [])),
            role="supporting",
            index=index,
        ),
        *_evidence_rows(
            list(resolution.get("contradicting_anchor_ids", [])),
            role="contradicting",
            index=index,
        ),
    ]
    return {
        "question": question,
        "validation": (
            {
                "report_id": report.get("report_id"),
                "verdict": report.get("verdict"),
                "release_allowed": bool(report.get("release_allowed")),
                "primary_reason": report.get("primary_reason"),
                "secondary_reasons": list(
                    report.get("secondary_reasons", [])
                ),
                "blocking_findings": list(
                    report.get("blocking_findings", [])
                ),
                "public_question_audit_id": report.get(
                    "public_question_audit_id"
                ),
                "answer_audit_id": report.get("answer_audit_id"),
                "validator_version": report.get("validator_version"),
            }
            if report is not None
            else None
        ),
        "evidence": evidence,
        "run_context": {
            "run_id": run_id,
            "target_entity": (
                snapshot.goal.target_entity
                if snapshot.goal is not None
                else None
            ),
            "target_entity_id": (
                snapshot.goal.target_entity_id
                if snapshot.goal is not None
                else None
            ),
            "target_entity_type": (
                snapshot.goal.target_entity_type
                if snapshot.goal is not None
                else None
            ),
            "run_state": snapshot.run_state.value,
            "stop_reason": snapshot.stop_reason,
            "commit_sequence": snapshot.commit_sequence,
        },
        "provenance": {
            "draft_id": question_id,
            "contract_id": draft.get("contract_id"),
            "resolution_id": contract.get("resolution_id"),
            "verification_sources": list(
                resolution.get("verification_sources", [])
            ),
        },
    }

"""Web API for the evidence-first objective agent."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from starlette.testclient import TestClient

from medical_qa_data_factory.generation.main_agent import (
    MainAgentDecision,
    MainAgentDecisionProposal,
)
from medical_qa_data_factory.generation.agent_loop import (
    ObjectiveQuestionAgentLoop,
)
from medical_qa_data_factory.generation.objective_question_contracts import (
    DomainOutcome,
    ExecutionStatus,
    GenerationGoal,
    RunManifest,
    RunState,
    SubagentResult,
    VerificationVerdict,
    canonical_digest,
)
from medical_qa_data_factory.generation.objective_question_runtime import (
    ObjectiveRuntimeRepository,
    RuntimeBudget,
)
from medical_qa_data_factory.web.app import (
    OBJECTIVE_STALL_AFTER_SECONDS,
    _resume_objective_web_generation,
    _objective_progress_timing,
    create_app,
)
from test_objective_agent_loop import (
    FakeMaterialProvider,
    _model as objective_subagent_model,
)


def _app(tmp_path):
    return create_app(
        tmp_path,
        structured_model=object(),
        objective_providers={},
    )


def test_objective_progress_reports_a_stale_nonterminal_run(
    tmp_path,
) -> None:
    run_root = tmp_path / "objective_runs" / "stale-run"
    commit_dir = run_root / "objective_runtime" / "commits"
    commit_dir.mkdir(parents=True)
    latest = commit_dir / "00000001.json"
    latest.write_text("{}", encoding="utf-8")
    now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    old_timestamp = (
        now - timedelta(seconds=OBJECTIVE_STALL_AFTER_SECONDS + 1)
    ).timestamp()
    os.utime(latest, (old_timestamp, old_timestamp))

    last_commit, age, stalled = _objective_progress_timing(
        run_root,
        terminal=False,
        now=now,
    )

    assert last_commit == now - timedelta(
        seconds=OBJECTIVE_STALL_AFTER_SECONDS + 1
    )
    assert age == OBJECTIVE_STALL_AFTER_SECONDS + 1
    assert stalled is True
    assert _objective_progress_timing(
        run_root,
        terminal=True,
        now=now,
    )[2] is False


def test_published_questions_api_lists_only_public_release_rows(
    tmp_path,
) -> None:
    public_root = (
        tmp_path
        / "objective_releases"
        / "web-published-1"
        / "public"
    )
    public_root.mkdir(parents=True)
    (public_root / "questions.jsonl").write_text(
        json.dumps(
            {
                "question_id": "draft.contract.published-1",
                "question": "What is the approved symbol of KIF15?",
                "answer": ["KIF15"],
                "explanation": "KIF15 is the approved symbol.",
                "schema_version": "objective-public-question-v1",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    client = TestClient(_app(tmp_path))

    response = client.get("/api/published-questions")

    assert response.status_code == 200
    payload = response.json()
    assert payload["question_count"] == 1
    assert payload["questions"][0]["question"] == (
        "What is the approved symbol of KIF15?"
    )
    assert payload["questions"][0]["explanation"] == (
        "KIF15 is the approved symbol."
    )
    assert payload["questions"][0]["run_id"] == "web-published-1"


def test_objective_generation_endpoint_returns_run_summary(
    tmp_path, monkeypatch
) -> None:
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return {
            "task_id": kwargs["run_id"],
            "engine": "objective",
            "status": "completed",
            "status_label": "已完成",
            "run_state": "COMPLETED",
            "stop_reason": "objective_questions_complete",
            "target_question_count": 1,
            "published_count": 1,
            "questions": [
                {
                    "question_id": "draft.brca1",
                    "text": "On which human chromosome is BRCA1 located?",
                    "answer": ["chromosome 17"],
                    "explanation": "BRCA1 maps to chromosome 17.",
                }
            ],
        }

    monkeypatch.setattr(
        "medical_qa_data_factory.web.app._run_objective_web_generation",
        fake_run,
    )
    client = TestClient(_app(tmp_path))
    response = client.post(
        "/api/objective/generate",
        json={
            "run_id": "objective-web-1",
            "entity_canonical_id": "primekg:gene:672",
            "entity_name": "BRCA1",
            "entity_type": "gene",
            "question_count": 1,
            "difficulty_targets": ["medium", "hard"],
            "question_style_ids": ["patient-caregiver-v1"],
            "providers": ["primekg", "pubmed"],
        },
    )

    assert response.status_code == 200, response.json()
    payload = response.json()
    assert payload["engine"] == "objective"
    assert payload["published_count"] == 1
    assert captured["entity_canonical_id"] == "primekg:gene:672"
    assert captured["providers"] == ["primekg", "pubmed"]
    assert captured["difficulty_targets"] == ["medium", "hard"]
    assert captured["style_targets"] == ["patient-caregiver-v1"]


def test_web_lists_and_selects_a_named_model_provider(
    tmp_path,
    monkeypatch,
) -> None:
    model_config = tmp_path / "models.json"
    model_config.write_text(
        json.dumps(
            {
                "default_provider": "local-a",
                "providers": {
                    "local-a": {
                        "driver": "openai_compatible",
                        "base_url": "http://127.0.0.1:8001/v1/chat/completions",
                        "model_id": "model-a",
                        "auth_mode": "none",
                        "structured_output_mode": "json_object",
                        "supports_strict_json_schema": False,
                    },
                    "local-b": {
                        "driver": "openai_compatible",
                        "base_url": "http://127.0.0.1:8002/v1/chat/completions",
                        "model_id": "model-b",
                        "auth_mode": "none",
                        "structured_output_mode": "prompt_only",
                        "supports_strict_json_schema": False,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MEDICAL_QA_MODEL_CONFIG", str(model_config))
    selected = {}
    model = object()

    def fake_build_model_port(provider_id=None, **_kwargs):
        selected["provider_id"] = provider_id
        return model

    def fake_run(**kwargs):
        selected["model"] = kwargs["model"]
        return {
            "task_id": kwargs["run_id"],
            "engine": "objective",
            "status": "completed",
            "status_label": "已完成",
            "run_state": "COMPLETED_WITH_ZERO_QUESTIONS",
            "is_terminal": True,
            "target_question_count": 1,
            "published_count": 0,
            "questions": [],
        }

    monkeypatch.setattr(
        "medical_qa_data_factory.llm.registry.build_model_port",
        fake_build_model_port,
    )
    monkeypatch.setattr(
        "medical_qa_data_factory.web.app._run_objective_web_generation",
        fake_run,
    )
    client = TestClient(
        create_app(tmp_path, objective_providers={})
    )

    bootstrap = client.get("/api/workbench/bootstrap").json()
    response = client.post(
        "/api/objective/generate",
        json={
            "run_id": "objective-web-model-b",
            "entity_canonical_id": "primekg:gene:672",
            "entity_name": "BRCA1",
            "entity_type": "gene",
            "question_count": 1,
            "providers": ["pubmed"],
            "model_provider_id": "local-b",
        },
    )

    assert bootstrap["model"]["default_provider"] == "local-a"
    assert [
        item["provider_id"] for item in bootstrap["model"]["providers"]
    ] == ["local-a", "local-b"]
    assert response.status_code == 200, response.json()
    assert selected == {"provider_id": "local-b", "model": model}


def test_objective_progress_endpoint_exposes_agent_and_audit_summary(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "medical_qa_data_factory.web.app._objective_run_payload",
        lambda *args, **kwargs: {
            "task_id": "objective-web-1",
            "engine": "objective",
            "status": "running",
            "status_label": "主 Agent 正在验证",
            "run_state": "CANDIDATES_PROCESSING",
            "target_question_count": 1,
            "published_count": 0,
            "main_agent_steps": [
                {
                    "sequence": 1,
                    "action": "verify_answer",
                    "action_label": "独立验证答案",
                    "target_subagent_role": "objective_answer_verification",
                    "state_label": "候选处理中",
                    "information_gain_label": "80%",
                }
            ],
            "artifact_counts": {"candidate_claim": 3, "evidence_material": 6},
            "hypotheses": {},
            "questions": [],
        },
    )
    client = TestClient(_app(tmp_path))
    response = client.get("/api/objective/objective-web-1")

    assert response.status_code == 200, response.json()
    payload = response.json()
    assert payload["main_agent_steps"][0]["action"] == "verify_answer"
    assert payload["artifact_counts"]["evidence_material"] == 6


def test_missing_objective_run_is_not_reported_as_pending(tmp_path) -> None:
    client = TestClient(_app(tmp_path))

    response = client.get("/api/objective/never-created")

    assert response.status_code == 404
    assert response.json()["code"] == "run_not_found"


def test_workbench_bootstrap_does_not_mix_run_history(tmp_path) -> None:
    client = TestClient(_app(tmp_path))

    response = client.get("/api/workbench/bootstrap")

    assert response.status_code == 200
    payload = response.json()
    assert "tasks" not in payload
    assert "selected_task" not in payload
    assert "questions" not in payload


def test_objective_run_list_contains_only_objective_runs(
    tmp_path,
    monkeypatch,
) -> None:
    (tmp_path / "objective_runs" / "objective-one").mkdir(parents=True)
    (tmp_path / "tasks" / "legacy-one").mkdir(parents=True)
    monkeypatch.setattr(
        "medical_qa_data_factory.web.app.build_objective_summary",
        lambda _workspace, run_id: {
            "task_id": run_id,
            "engine": "objective",
            "status": "completed",
            "status_label": "已完成",
            "run_state": "COMPLETED",
            "is_terminal": True,
            "can_resume": False,
            "target_question_count": 1,
            "published_count": 1,
            "goal": {
                "target_entity_id": "672",
                "target_entity": "BRCA1",
                "target_entity_type": "gene",
            },
        },
    )
    client = TestClient(_app(tmp_path))

    response = client.get(
        "/api/objective/runs?entity_id=primekg:gene:672"
    )

    assert response.status_code == 200
    payload = response.json()
    assert [item["task_id"] for item in payload["runs"]] == [
        "objective-one"
    ]
    assert all(item["engine"] == "objective" for item in payload["runs"])


def _create_recovery_run(
    tmp_path,
    run_id: str,
    *,
    question_count: int = 1,
) -> ObjectiveRuntimeRepository:
    manifest = RunManifest(
        manifest_id=f"manifest.{run_id}",
        run_id=run_id,
        orchestrator_version="objective-agent-loop-v1",
        main_agent_version="objective-main-agent-v1",
        subagent_role_versions={"all": "v1"},
        model_versions={"structured": "local:test", "provider_id": "local"},
        schema_versions={"objective_question_contracts": "objective-question-v1"},
        validation_policy_version="objective-policy-v1",
        data_snapshot_or_retrieval_time_policy="pinned-per-run",
        runtime_bundle_id="objective-runtime-v1",
        runtime_bundle_digest="a" * 64,
        provider_adapter_versions={"pubmed": "v1"},
    )
    goal = GenerationGoal(
        run_id=run_id,
        target_entity="BRCA1",
        target_entity_id="672",
        target_entity_type="gene",
        question_count_upper_bound=question_count,
        difficulty_targets=["easy"],
        style_targets=["concise"],
        allowed_fact_types=["gene_location"],
        run_budget={"max_actions": 4},
        run_manifest_id=manifest.manifest_id,
        runtime_bundle_id=manifest.runtime_bundle_id,
    )
    repo = ObjectiveRuntimeRepository.create(
        tmp_path / "objective_runs" / run_id,
        manifest=manifest,
        goal=goal,
        budget=RuntimeBudget(max_cost=10.0, max_model_calls=20),
    )
    snapshot = repo.load()
    repo.set_run_state(
        expected_state_revision=snapshot.state_revision,
        target=RunState.MATERIALS_COLLECTED,
    )
    snapshot = repo.load()
    repo.set_run_state(
        expected_state_revision=snapshot.state_revision,
        target=RunState.RECOVERY_REQUIRED,
        stop_reason="ModelError: model provider returned HTTP 400",
        recovery_error={
            "code": "model_bad_request",
            "message": "model provider rejected the request (HTTP 400)",
            "provider_response": "{\"error\":\"unsupported schema\"}",
            "attempts": 4,
        },
    )
    return repo


def test_recovery_summary_exposes_resume_context(tmp_path) -> None:
    run_id = "objective-resume-summary"
    _create_recovery_run(tmp_path, run_id)
    client = TestClient(_app(tmp_path))

    response = client.get(f"/api/objective/{run_id}/summary")

    assert response.status_code == 200, response.json()
    payload = response.json()
    assert payload["run_state"] == "RECOVERY_REQUIRED"
    assert payload["can_resume"] is True
    assert payload["last_successful_step"] == 0
    assert payload["next_step"] == 1
    assert payload["recovery_events"][0]["code"] == "model_bad_request"
    assert payload["recovery_events"][0]["attempts"] == 4


def test_resume_endpoint_dispatches_the_same_run(
    tmp_path,
    monkeypatch,
) -> None:
    run_id = "objective-resume-endpoint"
    repo = _create_recovery_run(tmp_path, run_id)
    captured = {}

    def fake_resume(**kwargs):
        captured.update(kwargs)
        repo.resume_from_recovery()
        return {
            "task_id": kwargs["run_id"],
            "engine": "objective",
            "status": "running",
            "status_label": "运行中",
            "run_state": "MATERIALS_COLLECTED",
            "can_resume": False,
        }

    monkeypatch.setattr(
        "medical_qa_data_factory.web.app._resume_objective_web_generation",
        fake_resume,
    )
    client = TestClient(_app(tmp_path))
    response = client.post(f"/api/objective/{run_id}/resume", json={})

    assert response.status_code == 200, response.json()
    assert captured["run_id"] == run_id
    assert repo.load().run_state is RunState.MATERIALS_COLLECTED


def test_resume_endpoint_rejects_a_non_recovery_run(tmp_path) -> None:
    run_id = "objective-not-paused"
    repo = _create_recovery_run(tmp_path, run_id)
    repo.resume_from_recovery()
    client = TestClient(_app(tmp_path))

    response = client.post(f"/api/objective/{run_id}/resume", json={})

    assert response.status_code == 409
    assert response.json()["code"] == "run_not_resumable"


def test_resume_endpoint_reports_a_missing_run(tmp_path) -> None:
    client = TestClient(_app(tmp_path))

    response = client.post("/api/objective/missing/resume", json={})

    assert response.status_code == 404
    assert response.json()["code"] == "run_not_found"


def _create_stalled_pending_run(tmp_path):
    run_id = "objective-stalled-pending"
    repo = _create_recovery_run(tmp_path, run_id)
    repo.resume_from_recovery()
    snapshot = repo.load()
    pending = MainAgentDecision(
        decision_id="decision.pending",
        action="retrieve_materials",
        target_subagent_role="material_retrieval",
        input_artifact_ids=[],
        state_digest=canonical_digest(snapshot.model_dump(mode="json")),
        requested_budget={},
        expected_information_gain=0.8,
        action_parameters={
            "provider_id": "pubmed",
            "purpose": "verification",
            "query": {"term": "BRCA1"},
        },
    )
    repo.record_decision(
        expected_state_revision=snapshot.state_revision,
        decision=pending,
    )
    latest = max(
        repo.commit_dir.glob("*.json"),
        key=lambda item: int(item.stem),
    )
    old = (
        datetime.now(timezone.utc)
        - timedelta(seconds=OBJECTIVE_STALL_AFTER_SECONDS + 5)
    ).timestamp()
    os.utime(latest, (old, old))
    return run_id, repo


def test_stalled_pending_run_is_resumable_at_the_recorded_step(
    tmp_path,
    monkeypatch,
) -> None:
    run_id, repo = _create_stalled_pending_run(tmp_path)
    captured = {}

    def fake_resume(**kwargs):
        captured.update(kwargs)
        repo.resume_stalled(reason="test resume")
        return {
            "task_id": kwargs["run_id"],
            "status": "running",
            "status_label": "运行中",
            "run_state": "MATERIALS_COLLECTED",
        }

    monkeypatch.setattr(
        "medical_qa_data_factory.web.app._resume_objective_web_generation",
        fake_resume,
    )
    client = TestClient(_app(tmp_path))

    summary = client.get(f"/api/objective/{run_id}/summary").json()
    resumed = client.post(f"/api/objective/{run_id}/resume", json={})

    assert summary["status"] == "stalled"
    assert summary["can_resume"] is True
    assert summary["last_successful_step"] == 0
    assert summary["next_step"] == 1
    assert summary["pending_decision_id"] == "decision.pending"
    assert resumed.status_code == 200, resumed.json()
    assert captured["run_id"] == run_id


def test_recovery_summary_points_to_the_pending_recorded_step(
    tmp_path,
) -> None:
    run_id, repo = _create_stalled_pending_run(tmp_path)
    snapshot = repo.load()
    repo.set_run_state(
        expected_state_revision=snapshot.state_revision,
        target=RunState.RECOVERY_REQUIRED,
        stop_reason="OperationConflict: artifact claim_001 has different content",
    )
    client = TestClient(_app(tmp_path))

    summary = client.get(f"/api/objective/{run_id}/summary").json()

    assert summary["can_resume"] is True
    assert summary["last_successful_step"] == 0
    assert summary["next_step"] == 1
    assert summary["pending_decision_id"] == "decision.pending"


def test_pending_step_survives_an_open_slot_with_a_committed_failure(
    tmp_path,
) -> None:
    run_id, repo = _create_stalled_pending_run(tmp_path)
    snapshot = repo.load()
    repo.register_child_slot(
        expected_state_revision=snapshot.state_revision,
        slot_id="facts.decision.pending",
        parent_decision_id="decision.pending",
        subtask_revision=0,
    )
    snapshot = repo.load()
    repo.register_child_slot(
        expected_state_revision=snapshot.state_revision,
        slot_id="failure.decision.pending",
        parent_decision_id="decision.pending",
        subtask_revision=0,
    )
    repo.commit_child_result(
        slot_id="failure.decision.pending",
        expected_subtask_revision=0,
        result=SubagentResult(
            subtask_id="failure.decision.pending",
            execution_status=ExecutionStatus.FAILED,
            domain_outcome=DomainOutcome.INSUFFICIENT,
            verdict=VerificationVerdict.NOT_EMITTED,
            output_artifact_ids=[],
            evidence_need_ids=[],
            usage={},
            error_category="model_bad_request",
            stop_reason="recoverable_subagent_error",
        ),
        artifacts=[],
    )
    snapshot = repo.load()
    repo.set_run_state(
        expected_state_revision=snapshot.state_revision,
        target=RunState.RECOVERY_REQUIRED,
        stop_reason="OperationConflict: artifact claim_9796_1 has different content",
    )
    client = TestClient(_app(tmp_path))

    summary = client.get(f"/api/objective/{run_id}/summary").json()

    assert summary["last_successful_step"] == 0
    assert summary["next_step"] == 1
    assert summary["pending_decision_id"] == "decision.pending"


def test_resume_helper_continues_at_the_next_decision(tmp_path) -> None:
    run_id = "objective-resume-loop"
    repo = _create_recovery_run(tmp_path, run_id, question_count=0)

    class StopModel:
        model_config_id = "local:test"

        def generate_structured(self, _request, response_model):
            proposal = response_model(
                action="stop",
                target_subagent_role=None,
                input_artifact_ids=[],
                requested_budget={},
                expected_information_gain=0,
                action_parameters={},
                stop_reason="resume_test",
            )
            return SimpleNamespace(parsed=proposal.model_dump(mode="json"))

    payload = _resume_objective_web_generation(
        workspace=tmp_path,
        run_id=run_id,
        model=StopModel(),
        objective_providers={
            "pubmed": FakeMaterialProvider(
                "pubmed",
                "BRCA1 is located on chromosome 17.",
            )
        },
    )
    snapshot = repo.load()

    assert payload["task_id"] == run_id
    assert snapshot.run_id == run_id
    assert snapshot.resume_count == 1
    assert len(snapshot.decisions) == 1
    assert snapshot.run_state is RunState.COMPLETED
    assert snapshot.stop_reason == "target_reached"


def test_legacy_web_generation_endpoint_is_retired(tmp_path) -> None:
    client = TestClient(_app(tmp_path))

    response = client.post("/api/generate", json={})

    assert response.status_code == 410
    assert response.json()["code"] == "legacy_generation_retired"


def test_legacy_archive_is_explicitly_read_only(tmp_path) -> None:
    client = TestClient(_app(tmp_path))

    listing = client.get("/api/legacy/tasks")
    missing = client.get("/api/legacy/tasks/missing-task")

    assert listing.status_code == 200
    assert listing.json() == {"tasks": [], "task_count": 0}
    assert missing.status_code == 404
    assert missing.json()["code"] == "legacy_task_not_found"


def test_objective_trace_uses_not_found_semantics(tmp_path) -> None:
    client = TestClient(_app(tmp_path))

    response = client.get("/api/objective/never-created/trace")

    assert response.status_code == 404
    assert response.json()["code"] == "run_not_found"


def test_objective_generation_validates_entity_identity(tmp_path) -> None:
    client = TestClient(_app(tmp_path))
    response = client.post(
        "/api/objective/generate",
        json={
            "run_id": "objective-web-invalid",
            "entity_canonical_id": "not-a-provider-id",
            "entity_name": "BRCA1",
            "question_count": 1,
        },
    )

    assert response.status_code == 400
    assert "canonical_id" in response.json()["error"]


def test_objective_generation_validates_difficulty_and_style(tmp_path) -> None:
    client = TestClient(_app(tmp_path))
    base = {
        "run_id": "objective-web-invalid-options",
        "entity_canonical_id": "primekg:gene:672",
        "entity_name": "BRCA1",
        "question_count": 1,
        "providers": ["pubmed"],
    }

    difficulty = client.post(
        "/api/objective/generate",
        json={**base, "difficulty_targets": ["expert"]},
    )
    style = client.post(
        "/api/objective/generate",
        json={**base, "question_style_ids": ["missing-style"]},
    )

    assert difficulty.status_code == 400
    assert "difficulty_targets" in difficulty.json()["error"]
    assert style.status_code == 400
    assert "question style" in style.json()["error"]


def test_objective_generation_persists_failure_on_agent_error(
    tmp_path,
    monkeypatch,
) -> None:
    app = create_app(
        tmp_path,
        structured_model=object(),
        objective_providers={
            "primekg": FakeMaterialProvider(
                "primekg",
                "BRCA1 is located on human chromosome 17.",
            )
        },
    )

    def fail_run(_):
        raise RuntimeError("structured model timed out")

    monkeypatch.setattr(ObjectiveQuestionAgentLoop, "run", fail_run)
    client = TestClient(app)
    response = client.post(
        "/api/objective/generate",
        json={
            "run_id": "objective-web-failure",
            "entity_canonical_id": "primekg:gene:672",
            "entity_name": "BRCA1",
            "entity_type": "gene",
            "question_count": 1,
            "max_actions": 4,
            "providers": ["primekg"],
        },
    )

    assert response.status_code == 500
    summary = client.get(
        "/api/objective/objective-web-failure/summary"
    )
    assert summary.status_code == 200
    assert summary.json()["status"] == "failed"
    assert summary.json()["run_state"] == "RECOVERY_REQUIRED"
    assert "structured model timed out" in summary.json()["stop_reason"]


def test_objective_api_runs_the_real_agent_loop(tmp_path) -> None:
    model = objective_subagent_model()

    def main_agent(request):
        summaries = request.payload["artifact_summaries"]
        types = {item["artifact_type"] for item in summaries}
        verification_ready = any(
            item["artifact_type"] == "provider_result"
            and item.get("metadata", {}).get("purpose") == "verification"
            for item in summaries
        )
        if "evidence_material" not in types:
            return MainAgentDecisionProposal(
                action="retrieve_materials",
                target_subagent_role="material_retrieval",
                input_artifact_ids=[],
                requested_budget={},
                expected_information_gain=0.9,
                action_parameters={
                    "provider_id": "primekg",
                    "purpose": "discovery",
                    "query": {"mention": "BRCA1", "native_id": "672"},
                },
            )
        if not verification_ready:
            return MainAgentDecisionProposal(
                action="retrieve_materials",
                target_subagent_role="material_retrieval",
                input_artifact_ids=[],
                requested_budget={},
                expected_information_gain=0.8,
                action_parameters={
                    "provider_id": "pubmed",
                    "purpose": "verification",
                    "query": {"term": "BRCA1 chromosome 17"},
                },
            )
        if "candidate_claim" not in types:
            return MainAgentDecisionProposal(
                action="understand_facts",
                target_subagent_role="fact_understanding",
                input_artifact_ids=[],
                requested_budget={},
                expected_information_gain=0.8,
            )
        if "question_hypothesis" not in types:
            return MainAgentDecisionProposal(
                action="design_questions",
                target_subagent_role="objective_question_design",
                input_artifact_ids=[],
                requested_budget={},
                expected_information_gain=0.8,
            )
        if "answer_resolution" not in types:
            return MainAgentDecisionProposal(
                action="verify_answer",
                target_subagent_role="objective_answer_verification",
                input_artifact_ids=[],
                requested_budget={},
                expected_information_gain=0.8,
            )
        if "objective_question_draft" not in types:
            return MainAgentDecisionProposal(
                action="realize_question",
                target_subagent_role="objective_question_realization",
                input_artifact_ids=[],
                requested_budget={},
                expected_information_gain=0.8,
            )
        if "objective_validation_report" not in types:
            return MainAgentDecisionProposal(
                action="review_release",
                target_subagent_role="objective_release_review",
                input_artifact_ids=[],
                requested_budget={},
                expected_information_gain=0.8,
            )
        return MainAgentDecisionProposal(
            action="stop",
            target_subagent_role=None,
            input_artifact_ids=[],
            requested_budget={},
            expected_information_gain=0,
            stop_reason="objective_questions_complete",
        )

    model.set_response("objective_main_agent", main_agent)
    app = create_app(
        tmp_path,
        structured_model=model,
        objective_providers={
            "primekg": FakeMaterialProvider(
                "primekg",
                "BRCA1 is located on human chromosome 17.",
            ),
            "pubmed": FakeMaterialProvider(
                "pubmed",
                "Independent report: BRCA1 maps to chromosome 17.",
            ),
        },
    )
    client = TestClient(app)
    response = client.post(
        "/api/objective/generate",
        json={
            "run_id": "objective-api-e2e",
            "entity_canonical_id": "primekg:gene:672",
            "entity_name": "BRCA1",
            "entity_type": "gene",
            "question_count": 1,
            "max_actions": 12,
            "providers": ["primekg", "pubmed"],
        },
    )

    assert response.status_code == 200, response.json()
    payload = response.json()
    assert payload["run_state"] == "COMPLETED"
    assert payload["published_count"] == 1
    assert payload["questions"][0]["answer"] == ["chromosome 17"]
    assert payload["main_agent_steps"]
    assert {item["provider_id"] for item in payload["materials"]} == {
        "primekg",
        "pubmed",
    }
    assert any(
        "chromosome 17" in item["preview"] for item in payload["materials"]
    )
    assert {
        item["provider_id"] for item in payload["retrieval_sources"]
    } == {"primekg", "pubmed"}

    progress = client.get("/api/objective/objective-api-e2e")
    assert progress.status_code == 200
    question_id = progress.json()["questions"][0]["question_id"]
    assert question_id.startswith("draft.contract.")

    summary = client.get("/api/objective/objective-api-e2e/summary")
    assert summary.status_code == 200, summary.json()
    summary_payload = summary.json()
    assert summary_payload["published_count"] == 1
    assert summary_payload["candidate_count"] == 0
    assert summary_payload["current_action"]["action"] == "stop"
    assert summary_payload["current_action"]["action_label"] == "结束运行"
    assert "materials" not in summary_payload

    workbench = client.get("/api/objective/objective-api-e2e/workbench")
    assert workbench.status_code == 200, workbench.json()
    workbench_payload = workbench.json()
    assert workbench_payload["published_questions"][0][
        "question_id"
    ] == question_id
    assert workbench_payload["published_questions"][0][
        "status"
    ] == "published"
    assert workbench_payload["candidate_questions"] == []
    assert workbench_payload["rejected_questions"] == []
    assert workbench_payload["retrieval_sources"]
    assert any(
        item["query_label"]
        for item in workbench_payload["retrieval_sources"]
    )
    assert any(
        item["candidate_count"] is not None
        for item in workbench_payload["retrieval_sources"]
    )

    details = client.get("/api/objective/objective-api-e2e")
    assert details.status_code == 200
    steps = details.json()["main_agent_steps"]
    assert steps
    assert any(
        item["outputs"]["providers"]
        for item in steps
        if item.get("outputs")
    )
    assert any(
        item["outputs"]["claims"]
        for item in steps
        if item.get("outputs")
    )
    assert any(
        item["outputs"]["validations"]
        for item in steps
        if item.get("outputs")
    )

    dossier = client.get(f"/api/questions/{question_id}/evidence")
    assert dossier.status_code == 200, dossier.json()
    dossier_payload = dossier.json()
    assert dossier_payload["question"]["question_id"] == question_id
    assert dossier_payload["validation"]["release_allowed"] is True
    assert dossier_payload["evidence"]
    assert all(
        item["quoted_text"] for item in dossier_payload["evidence"]
    )
    assert {
        item["provider_id"] for item in dossier_payload["evidence"]
    } >= {"pubmed"}

    # Replay committed snapshots without an export, as seen while the loop is running.
    from pathlib import Path
    from shutil import copy2

    run_id = "objective-api-e2e"
    commits = Path("objective_runs") / run_id / "objective_runtime" / "commits"
    live = tmp_path / "in-progress"
    (live / commits).mkdir(parents=True)
    live_client = TestClient(_app(live))
    saw_candidate = False
    for commit in sorted((tmp_path / commits).glob("*.json")):
        copy2(commit, live / commits / commit.name)
        state = json.loads(commit.read_bytes())["snapshot"]
        if "PUBLISHED" in state["hypotheses"].values():
            break
        assert question_id not in state["artifacts"]  # Draft, report and publication are committed together.
        if not saw_candidate and state["hypotheses"]:
            assert live_client.get(f"/api/objective/{run_id}/workbench").json()["published_questions"] == []
            saw_candidate = True
    assert saw_candidate and state["run_state"] != "COMPLETED"
    calls = model.calls_for("objective_answer_verification")
    assert [call.payload["stage"] for call in calls] == ["final_blind", "final_review"]
    public_query = calls[0].payload["blind_answer_query"]
    assert set(public_query) == {"query_id", "question"}
    assert public_query["question"] == workbench_payload["published_questions"][0]["text"]
    for role in ("objective_question_realization", "objective_candidate_review", "objective_release_review"):
        assert not model.calls_for(role)
    assert live_client.get(f"/api/objective/{run_id}/summary").json()["published_count"] == 1
    assert live_client.get(f"/api/objective/{run_id}").json()["questions"][0]["question_id"] == question_id
    assert live_client.get(f"/api/objective/{run_id}/workbench").json()["published_questions"] == workbench_payload["published_questions"]
    assert live_client.get("/api/published-questions").json()["question_count"] == 1
    assert client.get("/api/published-questions").json()["question_count"] == 1


def test_pause_api_requests_boundary_stop_and_summary_reports_it(tmp_path):
    from test_objective_agent_loop import _manifest, _goal
    runtime = ObjectiveRuntimeRepository.create(tmp_path / "objective_runs" / "run-loop",
        manifest=_manifest(), budget=RuntimeBudget(max_cost=10, max_model_calls=20), goal=_goal())
    with TestClient(_app(tmp_path)) as client:
        assert client.post("/api/objective/missing-run/pause").status_code == 404
        response = client.post("/api/objective/run-loop/pause")
        assert response.status_code == 202
        assert runtime.pause_requested
        assert client.get("/api/objective/run-loop/summary").json()["pause_requested"] is True
        assert runtime.pause_at_boundary()
        summary = client.get("/api/objective/run-loop/summary").json()
        assert summary["status_label"] == "已暂停"
        assert summary["can_resume"] is True
        assert client.post("/api/objective/run-loop/pause").status_code == 409

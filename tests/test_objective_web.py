"""Web API for the evidence-first objective agent."""
from __future__ import annotations

import json
import os
import asyncio
import threading
from datetime import datetime, timedelta, timezone

import pytest
import httpx
from starlette.testclient import TestClient

from medical_qa_data_factory.generation.main_agent import (
    MainAgentDecision,
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


@pytest.mark.parametrize("include_retrieval", [False, True])
def test_published_questions_api_lists_only_public_release_rows(
    tmp_path, include_retrieval,
) -> None:
    suggestion = {
        "query": "KIF15 approved gene symbol",
        "sources": ["HGNC"],
        "expected_evidence": "The official symbol for the gene.",
        "stop_condition": "The gene record confirms the approved symbol.",
    }
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
                **({"suggested_retrieval": suggestion} if include_retrieval else {}),
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
    if include_retrieval:
        assert payload["questions"][0]["suggested_retrieval"] == suggestion
    else:
        assert "suggested_retrieval" not in payload["questions"][0]


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
        lambda _workspace, run_id, **_: {
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


def test_objective_run_list_survives_an_unreadable_run(tmp_path, monkeypatch, caplog):
    for run_id in ("readable", "unreadable"):
        (tmp_path / "objective_runs" / run_id).mkdir(parents=True)

    def read_summary(_workspace, run_id, **_):
        if run_id == "unreadable":
            raise PermissionError("checkpoint cannot be read")
        return {"run_id": run_id}

    monkeypatch.setattr("medical_qa_data_factory.web.app.build_objective_summary", read_summary)
    response = TestClient(_app(tmp_path)).get("/api/objective/runs")
    assert response.status_code == 200
    assert response.json() == {"runs": [{"run_id": "readable"}], "run_count": 1}
    assert "Cannot read history for unreadable" in caplog.text


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


def test_disconnected_request_keeps_its_worker_and_generation_lock(tmp_path, monkeypatch):
    entered = threading.Event()
    finish = threading.Event()

    def generate(**kwargs):
        if kwargs["run_id"] == "web-first":
            entered.set()
            assert finish.wait(5)
        return {"task_id": kwargs["run_id"]}

    monkeypatch.setattr("medical_qa_data_factory.web.app._run_objective_web_generation", generate)

    async def exercise():
        app = _app(tmp_path)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            def generate_request(run_id):
                return client.post("/api/objective/generate", json={
                    "run_id": run_id, "entity_canonical_id": "primekg:gene/protein:672",
                    "entity_name": "BRCA1", "providers": ["pubmed"],
                })

            first = asyncio.create_task(generate_request("web-first"))
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                first.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first
                assert (await generate_request("web-second")).status_code == 409
            finally:
                finish.set()

            for _ in range(100):
                response = await generate_request("web-second")
                if response.status_code == 200:
                    break
                await asyncio.sleep(0.01)
            assert response.json() == {"task_id": "web-second"}

    asyncio.run(exercise())


def _create_stalled_pending_run(tmp_path, run_id="objective-stalled-pending", *, age_commit=True):
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
    if age_commit:
        os.utime(latest, (old, old))
    return run_id, repo


@pytest.mark.parametrize("interrupted", [False, True])
def test_stalled_pending_run_is_resumable_at_the_recorded_step(
    tmp_path,
    monkeypatch,
    interrupted,
) -> None:
    run_id, repo = _create_stalled_pending_run(
        tmp_path, "web-interrupted" if interrupted else "objective-stalled-pending",
        age_commit=not interrupted,
    )
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
    assert summary["worker_interrupted"] is interrupted
    assert summary["last_successful_step"] == 0
    assert summary["next_step"] == 1
    assert summary["pending_decision_id"] == "decision.pending"
    assert resumed.status_code == 200, resumed.json()
    assert captured["run_id"] == run_id
    assert captured["worker_interrupted"] is interrupted


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
            raise AssertionError("The host can stop a run that has reached its quota")

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


def current_generation_fixture():
    records = [
        ("On which chromosome is human BRCA1 located?", "chromosome 17",
         "Human BRCA1 is located on chromosome 17."),
        ("What process does BRCA1 help repair in human cells?", "DNA double-strand break repair",
         "Human BRCA1 participates in DNA double-strand break repair."),
        ("Which protein forms a heterodimer with human BRCA1?", "BARD1",
         "Human BRCA1 forms a heterodimer with BARD1."),
    ]
    model = objective_subagent_model()
    by_question = {question: answer for question, answer, _ in records}

    def controller(request):
        payload = request.payload
        choices = payload["candidate_tool_targets"]
        for action in ("publish_question", "review_candidate"):
            targets = choices.get(action) or []
            if targets:
                return {"action": action, "parameters": {"hypothesis_id": targets[0]}, "rationale": action}
        for candidate, readiness in payload["verification_readiness"].items():
            if readiness["entry_ready"]:
                return {"action": "verify_answer", "parameters": {"hypothesis_id": candidate},
                        "rationale": "Independently verify the candidate using the source."}
        opportunities = payload["design_inputs"]["unused_discovery_opportunities"]
        if opportunities:
            return {"action": "design_questions", "rationale": "Draft an unused question",
                    "parameters": {"research_id": opportunities[0]["research_id"],
                                   "evidence_sufficiency_reason": "The saved source states the direct answer."}}
        reports = payload["discovery_inputs"]["research_handoffs"]
        if reports:
            return {"action": "discover_questions", "rationale": "Find separate information needs",
                    "input_artifact_ids": [reports[0]["report_id"]], "parameters": {}}
        assert not payload["prior_searches"], payload["prior_searches"]
        return {"action": "research_evidence", "rationale": "Find material for the requested entity",
                "parameters": {"information_need": "Find distinct BRCA1 facts", "preferred_provider": "pubmed"}}

    def researcher(request):
        payload = request.payload
        if not payload["history"]:
            return {"action": "retrieve_materials", "rationale": "Find candidate sources",
                    "parameters": {"provider_id": "pubmed", "query": {"term": "BRCA1"},
                                   "evidence_need": "Find BRCA1 facts"}}
        if payload["history"][-1]["action"] == "retrieve_materials":
            return {"action": "read_artifacts", "rationale": "Read the saved source",
                    "input_artifact_ids": [payload["materials"][0]["id"]]}
        return {"action": "finish_research", "rationale": "Pass original evidence to discovery",
                "summary": "The source covers chromosome location, repair and protein binding."}

    def plan(request):
        evidence = request.payload["materials"][0]["excerpts"]
        spans = {sentence["text"]: sentence["span_id"] for passage in evidence for sentence in passage["sentences"]}
        return {"opportunities": [
            {"primary_span_id": spans[quote], "source_ids": [],
             "supported_observations": [quote], "information_need": question, "value": "Direct domain lookup",
             "evidence_scope": dict.fromkeys(("population_or_genotype", "setting", "comparator", "time_period")),
             "readiness": "ready_for_design", "next_information_target": ""}
            for question, _, quote in records
        ]}

    def reader(request):
        return {"judgments": [
            {"index": item["index"], "premises": [], "verdict": "ACCEPT", "reason": "Standalone"}
            for item in request.payload["questions"]
        ]}

    def grounded_answer(request):
        payload = request.payload
        question = payload["research_context"]["approved_question"]
        return {"hypotheses": [{
            "hypothesis_id": "fixture", "question_intent": question, "proposed_public_scope": "human BRCA1",
            "proposed_answer_type": "entity", "proposed_answer_values": [by_question[question]],
            "proposed_cardinality": "EXACTLY_ONE", "evidence_basis": "source_passages",
            "discovery_anchor_ids": [payload["evidence_excerpts"][0]["anchor_id"]],
            "suggested_retrieval": {"query": question, "sources": ["PubMed"],
                                    "expected_evidence": question, "stop_condition": "Find direct support."},
        }]}

    def verify(request):
        payload = request.payload
        query = payload["blind_answer_query"]
        if payload["stage"] == "final_blind":
            return {"query_id": query["query_id"], "answer_values": [by_question[query["question"]]], "uncertain": False}
        anchor = payload["evidence_anchors"][0]
        return {
            "claim_checks": [
                {"target": target, "statement": by_question[query["question"]],
                 "source_sentence": next(quote for question, _, quote in records if question == query["question"]),
                 "anchor_ids": [anchor["anchor_id"]], "source_uncertainty_or_scope": "human BRCA1",
                 "claim_uncertainty_or_scope": "human BRCA1", "preserves_uncertainty_and_scope": True,
                 "relation": "SUPPORTED"}
                for target in ("proposed_answer", "blind_answer")
            ],
            "review": {"question_is_self_contained": True, "answer_matches_blind": True,
                       "answer_scope_complete": True, "explanation": "The source supports the answer.",
                       "conflict_found": False, "directly_supporting_anchor_ids": [anchor["anchor_id"]],
                       "support_directly_entails_answer": True},
        }

    responses = {
        "objective_react_agent": controller,
        "objective_research_agent": researcher,
        "objective_reading_summary": {"summary": "Read three facts.", "limitations": ""},
        "objective_question_opportunity": plan,
        "objective_question_discovery": lambda request: {"questions": [
            {"plan_id": p["plan_id"], "question": p["information_need"]} for p in request.payload["plans"]
        ]},
        "objective_question_form_reader": lambda request: {"judgments": [
            {"index": item["index"], "task_in_question": "Find the stated fact", "missing_required_context": [],
             "task_kind": "domain_knowledge", "verdict": "ACCEPT", "reason": "Standalone"}
            for item in request.payload["questions"]
        ]},
        "objective_question_reader": reader,
        "objective_answer_source_reading": {"requested_result": "Find the fact", "quantitative_result_needed": False,
                                             "observations": [], "missing_relationships": [], "unestablished_links": []},
        "objective_grounded_answer": grounded_answer,
        "objective_answer_verification": verify,
        "objective_claim_entailment": lambda request: {"checks": [
            {"id": pair["id"], "support": "entailed", "reason": "The anchor supports the answer."}
            for pair in request.payload["pairs"]
        ]},
    }
    for role, response in responses.items():
        model.set_response(role, response)
    return model, " ".join(quote for _, _, quote in records), by_question


@pytest.mark.parametrize("question_count", [1, 3, 3], ids=["one", "three", "three-again"])
def test_objective_api_runs_the_real_agent_loop(tmp_path, question_count) -> None:
    model, source, by_question = current_generation_fixture()
    app = create_app(tmp_path, structured_model=model, objective_providers={
        "pubmed": FakeMaterialProvider("pubmed", source),
    })
    client = TestClient(app)
    run_id = "web-current-chain"
    response = client.post("/api/objective/generate", json={
        "run_id": run_id, "entity_canonical_id": "primekg:gene:672", "entity_name": "BRCA1",
        "entity_type": "gene", "question_count": question_count, "max_actions": 20, "providers": ["pubmed"],
    })
    assert response.status_code == 200, response.json()
    payload = response.json()
    assert payload["run_state"] == "COMPLETED", [step["parameters"] for step in payload["main_agent_steps"]]
    assert payload["published_count"] == question_count
    assert payload["stop_reason"] == "target_reached"
    summary = client.get(f"/api/objective/{run_id}/summary").json()
    assert summary["counted_toward_target"] == question_count
    assert summary["current_action"]["action"] == "stop"
    workbench = client.get(f"/api/objective/{run_id}/workbench").json()
    questions = workbench["published_questions"]
    assert len({q["question_id"] for q in questions}) == question_count
    assert all(q["answer"] == [by_question[q["text"]]] for q in questions)
    published = client.get("/api/published-questions").json()
    assert len(published["questions"]) == question_count
    for question in questions:
        assert question["suggested_retrieval"]["query"] == question["text"]
        dossier = client.get(f"/api/questions/{question['question_id']}/evidence").json()
        assert dossier["validation"]["release_allowed"] is True
        assert any(question["answer"][0] in row["quoted_text"] for row in dossier["evidence"])
    assert len(model.calls_for("objective_answer_verification")) == 2 * question_count
    assert (tmp_path / "objective_releases" / run_id / "public" / "questions.jsonl").is_file()


def test_web_and_cli_share_default_generation_inputs_and_release(tmp_path, monkeypatch):
    from medical_qa_data_factory.cli import main
    from medical_qa_data_factory.generation import objective_cli

    web_model, source, _ = current_generation_fixture()
    cli_model, _, _ = current_generation_fixture()
    provider_ids = ["ensembl", "hgnc", "ncbi_gene", "primekg", "pubmed"]
    providers = {name: FakeMaterialProvider(name, source) for name in provider_ids}
    web_root = tmp_path / "web"
    client = TestClient(create_app(web_root, structured_model=web_model, objective_providers=providers))
    run_id = "web-parity"
    response = client.post("/api/objective/generate", json={
        "run_id": run_id, "entity_canonical_id": "primekg:gene/protein:672",
        "entity_name": "BRCA1", "entity_type": "gene/protein", "question_count": 3,
    })
    assert response.status_code == 200, response.json()
    assert response.json()["published_count"] == 3

    def fake_provider_map(requested, **kwargs):
        assert sorted(requested) == provider_ids
        return providers, None

    monkeypatch.setattr(objective_cli, "build_model_port", lambda **kwargs: cli_model)
    monkeypatch.setattr(objective_cli, "objective_provider_map", fake_provider_map)
    cli_output = tmp_path / "cli-release"
    assert main([
        "objective", "generate", "--workspace", str(tmp_path / "cli"),
        "--output", str(cli_output), "--run-id", run_id,
        "--entity-name", "BRCA1", "--entity-id", "672", "--entity-type", "gene",
        "--question-count", "3",
    ]) == 0

    web_request = web_model.calls_for("objective_react_agent")[0].payload
    cli_request = cli_model.calls_for("objective_react_agent")[0].payload
    for key in ("goal", "generation_strategy", "available_providers", "budget"):
        assert web_request[key] == cli_request[key]
    assert web_request["available_providers"] == provider_ids
    assert web_request["goal"]["target_entity_type"] == "gene"
    assert web_request["goal"]["difficulty_targets"] == ["easy"]
    assert web_request["goal"]["style_targets"] == ["concise"]
    assert web_request["generation_strategy"]["name"] == "free"
    assert web_request["goal"]["run_budget"] == {"max_actions": 50, "max_cost": 10, "max_model_calls": 130}

    def released_qa(root):
        rows = (root / "public" / "questions.jsonl").read_text(encoding="utf-8").splitlines()
        return {row["question"]: row["answer"] for row in map(json.loads, rows)}

    web_release = web_root / "objective_releases" / run_id
    assert len(released_qa(cli_output)) == 3
    assert released_qa(web_release) == released_qa(cli_output)


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

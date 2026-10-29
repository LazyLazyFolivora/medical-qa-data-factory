"""Web API for the evidence-first objective agent."""
from __future__ import annotations

from starlette.testclient import TestClient

from medical_qa_data_factory.generation.main_agent import (
    MainAgentDecisionProposal,
)
from medical_qa_data_factory.web.app import create_app
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
            "providers": ["primekg", "pubmed"],
        },
    )

    assert response.status_code == 200, response.json()
    payload = response.json()
    assert payload["engine"] == "objective"
    assert payload["published_count"] == 1
    assert captured["entity_canonical_id"] == "primekg:gene:672"
    assert captured["providers"] == ["primekg", "pubmed"]


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

    progress = client.get("/api/objective/objective-api-e2e")
    assert progress.status_code == 200
    assert progress.json()["questions"][0]["question_id"] == "question.brca1.chr17"

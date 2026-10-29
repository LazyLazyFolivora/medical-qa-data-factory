"""Web API for the evidence-first objective agent."""
from __future__ import annotations

from starlette.testclient import TestClient

from medical_qa_data_factory.web.app import create_app


def _app(tmp_path):
    return create_app(tmp_path, structured_model=object())


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

    assert response.status_code == 200
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

    assert response.status_code == 200
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

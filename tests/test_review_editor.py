import pytest

from starlette.testclient import TestClient

from medical_qa_data_factory.domain import ValidationStatus
from medical_qa_data_factory.repository import LocalArtifactRepository
from medical_qa_data_factory.review import review_question
from medical_qa_data_factory.web import create_app

from test_review import build_result


def client_for(tmp_path):
    result = build_result(tmp_path)
    return TestClient(create_app(tmp_path)), result


def test_editor_submit_saves_canonical_question_edit(tmp_path):
    client, result = client_for(tmp_path)
    question_id = result.questions[0].question_id

    response = client.post(
        f"/api/review/questions/{question_id}",
        json={
            "action": "return",
            "summary": "改写标准问题",
            "canonical_question": "aspirin 在该知识图谱中作用的靶点实体是什么？",
            "changed_fields": ["canonical_question"],
        },
    )

    assert response.status_code == 200
    repository = LocalArtifactRepository(tmp_path)
    latest = repository.load_latest_question(question_id)
    assert latest.canonical_question == "aspirin 在该知识图谱中作用的靶点实体是什么？"
    assert latest.changes[-1].summary == "改写标准问题"


def test_unchanged_field_is_not_recorded_as_a_change(tmp_path):
    client, result = client_for(tmp_path)
    question = result.questions[0]

    response = client.post(
        f"/api/review/questions/{question.question_id}",
        json={
            "action": "return",
            "summary": "只看不做修改",
            "canonical_question": question.canonical_question,
            "changed_fields": ["canonical_question"],
        },
    )

    assert response.status_code == 200
    latest = LocalArtifactRepository(tmp_path).load_latest_question(question.question_id)
    assert latest.canonical_question == question.canonical_question


def test_edit_invalidates_dependent_checks(tmp_path):
    client, result = client_for(tmp_path)
    question = result.questions[0]

    client.post(
        f"/api/review/questions/{question.question_id}",
        json={
            "action": "return",
            "summary": "修改标准问题",
            "canonical_question": "该种子实体直接作用的靶点实体是什么？",
            "changed_fields": ["canonical_question"],
        },
    )

    latest = LocalArtifactRepository(tmp_path).load_latest_question(question.question_id)
    by_type = {check.check_type: check.status for check in latest.validation_checks}
    assert by_type["question_leakage"] is ValidationStatus.NOT_RUN


def test_taxonomy_edit_is_applied_and_versioned(tmp_path):
    client, result = client_for(tmp_path)
    question = result.questions[0]

    response = client.post(
        f"/api/review/questions/{question.question_id}",
        json={
            "action": "return",
            "summary": "补充 hop 标注",
            "taxonomy": {"domain_intent": "drug_target_knowledge", "hop_complexity": 2},
            "changed_fields": ["taxonomy"],
        },
    )

    assert response.status_code == 200
    latest = LocalArtifactRepository(tmp_path).load_latest_question(question.question_id)
    assert latest.taxonomy.domain_intent == "drug_target_knowledge"
    assert latest.taxonomy.hop_complexity == 2


def test_unknown_changed_field_is_rejected(tmp_path):
    client, result = client_for(tmp_path)

    response = client.post(
        f"/api/review/questions/{result.questions[0].question_id}",
        json={
            "action": "return",
            "summary": "试图改不存在的字段",
            "changed_fields": ["not_a_field"],
        },
    )

    assert response.status_code == 400
    assert "not_a_field" in response.json()["error"]


def test_invalid_route_value_is_rejected(tmp_path):
    client, result = client_for(tmp_path)

    response = client.post(
        f"/api/review/questions/{result.questions[0].question_id}",
        json={"action": "return", "summary": "非法路由", "verified_route": "nope"},
    )

    assert response.status_code == 400


def test_review_page_shows_check_status_and_change_log(tmp_path):
    client, result = client_for(tmp_path)
    question = result.questions[0]

    page = client.get(f"/review/{question.question_id}").text

    assert "机器校验状态" in page
    assert "question_leakage" in page
    assert "data-original=" in page
    assert "修改记录" in page


def test_invalid_variant_payload_is_rejected(tmp_path):
    question = build_result(tmp_path).questions[0]

    with pytest.raises(Exception):
        review_question(
            question,
            "return",
            summary="变体结构非法",
            question_variants=[{"text": "缺少必填字段"}],
            changed_fields={"question_variants"},
        )

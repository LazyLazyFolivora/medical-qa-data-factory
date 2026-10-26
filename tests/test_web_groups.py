"""The grouped review page and its API.

Step 10 of the real-generation-agent plan.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from medical_qa_data_factory.domain.questions import ReviewDisposition
from medical_qa_data_factory.generation.grouping import group_key_for_single_hop
from medical_qa_data_factory.web import create_app


def _group_key() -> str:
    return group_key_for_single_hop(
        seed_provider_id="primekg",
        seed_entity_id="D001241",
        relation="treats",
        direction="forward",
    )


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "ws"))


def _seed_question(
    workspace: Path,
    *,
    question_id: str = "q1",
    group: str | None = None,
    disposition: ReviewDisposition = ReviewDisposition.PENDING,
) -> Any:
    from medical_qa_data_factory.domain import (
        EntityRef,
        LinkStatus,
    )
    from medical_qa_data_factory.domain.common import utc_now
    from medical_qa_data_factory.domain.questions import QuestionSpec
    from medical_qa_data_factory.repository import LocalArtifactRepository

    repository = LocalArtifactRepository(workspace)
    now = utc_now()
    question = QuestionSpec(
        question_id=question_id,
        logical_question_id=f"l-{question_id}",
        question_version=f"{question_id}.v1",
        bundle_ids=["b1"],
        generation_mode="automatic",
        canonical_question="Which disease does aspirin treat?",
        question_variants=[
            {
                "variant_id": "v1",
                "text": "Which disease does aspirin treat?",
                "language": "en",
                "persona": "system",
                "style": "neutral",
                "generation_source": "agent",
                "semantic_check_status": "warning",
            }
        ],
        taxonomy={
            "domain_intent": "single_fact",
            "answer_form": "fact",
            "reasoning_pattern": ["single_hop"],
        },
        query_intent={},
        answerability="answerable",
        gold_answer={"answer_mode": "fact", "short_answer": "migraine"},
        evidence_requirements={"sufficiency_rule_version": "v1"},
        expected_route="kg",
        split_group_id="s1",
        review_group_key=group,
        review_disposition=disposition,
        created_at=now,
    )
    repository.save_question(question)
    return question


def test_the_groups_page_renders(client: TestClient, tmp_path: Path) -> None:
    _seed_question(tmp_path / "ws", group=_group_key())
    response = client.get("/groups")
    assert response.status_code == 200
    body = response.text
    assert "同实体—关系组" in body
    assert "Which disease does aspirin treat?" in body


def test_a_question_without_a_group_is_not_shown(
    client: TestClient, tmp_path: Path
) -> None:
    _seed_question(tmp_path / "ws", group=None)
    response = client.get("/groups")
    assert response.status_code == 200
    assert "还没有带分组的候选" in response.text


def test_the_page_shows_the_candidates_side_by_side(
    client: TestClient, tmp_path: Path
) -> None:
    group = _group_key()
    _seed_question(tmp_path / "ws", question_id="q1", group=group)
    _seed_question(tmp_path / "ws", question_id="q2", group=group)
    body = client.get("/groups").text
    assert body.count('class="candidate') >= 2
    assert "2 个候选" in body


def test_the_page_shows_the_disposition(
    client: TestClient, tmp_path: Path
) -> None:
    _seed_question(
        tmp_path / "ws",
        group=_group_key(),
        disposition=ReviewDisposition.KEEP_INDEPENDENT,
    )
    body = client.get("/groups").text
    assert "keep_independent" in body


def test_the_page_never_says_the_system_judged_a_duplicate(
    client: TestClient, tmp_path: Path
) -> None:
    """The wording must not present a group as a verdict."""
    group = _group_key()
    _seed_question(tmp_path / "ws", question_id="q1", group=group)
    _seed_question(tmp_path / "ws", question_id="q2", group=group)
    body = client.get("/groups").text
    assert "系统已判定重复" not in body
    assert "是否重复由人工判断" in body


def test_a_keep_independent_action_is_applied(
    client: TestClient, tmp_path: Path
) -> None:
    _seed_question(tmp_path / "ws", group=_group_key())
    response = client.post(
        f"/api/groups/{_group_key()}",
        json={"action": "keep_independent", "question_id": "q1"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "committed"
    assert payload["questions"][0]["review_disposition"] == "keep_independent"


def test_a_reject_action_is_applied(client: TestClient, tmp_path: Path) -> None:
    _seed_question(tmp_path / "ws", group=_group_key())
    response = client.post(
        f"/api/groups/{_group_key()}",
        json={"action": "reject", "question_id": "q1"},
    )
    assert response.status_code == 200
    assert response.json()["questions"][0]["review_disposition"] == "rejected"


def test_an_unknown_action_is_refused(
    client: TestClient, tmp_path: Path
) -> None:
    _seed_question(tmp_path / "ws", group=_group_key())
    response = client.post(
        f"/api/groups/{_group_key()}",
        json={"action": "delete_everything", "question_id": "q1"},
    )
    assert response.status_code == 400
    assert "unknown review action" in response.json()["error"]


def test_a_variant_action_needs_a_target(
    client: TestClient, tmp_path: Path
) -> None:
    group = _group_key()
    _seed_question(tmp_path / "ws", question_id="q1", group=group)
    _seed_question(tmp_path / "ws", question_id="q2", group=group)
    response = client.post(
        f"/api/groups/{group}",
        json={"action": "set_variant_of", "question_id": "q1"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "missing_target"


def test_a_variant_action_is_applied(
    client: TestClient, tmp_path: Path
) -> None:
    group = _group_key()
    _seed_question(tmp_path / "ws", question_id="q1", group=group)
    _seed_question(tmp_path / "ws", question_id="q2", group=group)
    # The primary has to be kept independently first.
    client.post(
        f"/api/groups/{group}",
        json={"action": "keep_independent", "question_id": "q2"},
    )
    response = client.post(
        f"/api/groups/{group}",
        json={
            "action": "set_variant_of",
            "question_id": "q1",
            "target_question_id": "q2",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    dispositions = {
        item["question_id"]: item["review_disposition"]
        for item in payload["questions"]
    }
    assert dispositions["q1"] == "variant_of"


def test_a_missing_question_is_a_404(client: TestClient, tmp_path: Path) -> None:
    response = client.post(
        f"/api/groups/{_group_key()}",
        json={"action": "keep_independent", "question_id": "absent"},
    )
    assert response.status_code == 404


def test_the_page_shows_the_ranking_factors(
    client: TestClient, tmp_path: Path
) -> None:
    from medical_qa_data_factory.domain.common import utc_now
    from medical_qa_data_factory.domain.questions import RankingAssessment, RankingFactor
    from medical_qa_data_factory.repository import LocalArtifactRepository

    workspace = tmp_path / "ws"
    question = _seed_question(workspace, group=_group_key())
    repository = LocalArtifactRepository(workspace)
    updated = question.model_copy(
        update={
            "ranking_assessment": RankingAssessment(
                ranking_version="ranking.v1",
                total_score=85.0,
                factors=[
                    RankingFactor(
                        code="verifier_accepted",
                        value=1.0,
                        weight=30.0,
                        contribution=30.0,
                        explanation="the independent verifier accepted the question",
                    )
                ],
                computed_at=utc_now(),
            ),
            "question_version": f"{question.question_id}.v2",
        }
    )
    repository.save_question(updated)
    body = client.get("/groups").text
    assert "排序因素" in body
    assert "verifier_accepted" in body
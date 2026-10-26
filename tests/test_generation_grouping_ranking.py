"""Review grouping and the explainable ranking.

Step 9 of the real-generation-agent plan. The properties under test are that
a group is recomputable, that a group is not a verdict, and that the ranking
only changes the display order.
"""
from __future__ import annotations

from typing import Any

import pytest

from medical_qa_data_factory.domain.questions import ReviewDisposition
from medical_qa_data_factory.generation.grouping import (
    REVIEW_GROUP_VERSION,
    GroupPayload,
    GroupStep,
    build_groups,
    group_key,
    group_key_for_multi_paths,
    group_key_for_path,
    group_key_for_single_hop,
    is_recomputable,
)
from medical_qa_data_factory.generation.ranking import (
    BLOCKED_SCORE_CAP,
    RANKING_VERSION,
    RANKING_WEIGHTS,
    RankingSignals,
    explain,
    rank_candidate,
    sort_candidates,
)


# ---- the group key -------------------------------------------------------


def test_the_same_structure_produces_the_same_key() -> None:
    first = group_key_for_single_hop(
        seed_provider_id="primekg",
        seed_entity_id="D001241",
        relation="treats",
        direction="forward",
    )
    second = group_key_for_single_hop(
        seed_provider_id="primekg",
        seed_entity_id="D001241",
        relation="treats",
        direction="forward",
    )
    assert first == second
    assert first.startswith("group.")


def test_a_different_relation_is_a_different_group() -> None:
    treats = group_key_for_single_hop(
        seed_provider_id="primekg",
        seed_entity_id="D001241",
        relation="treats",
        direction="forward",
    )
    symptom = group_key_for_single_hop(
        seed_provider_id="primekg",
        seed_entity_id="D001241",
        relation="symptom",
        direction="forward",
    )
    assert treats != symptom


def test_a_different_direction_is_a_different_group() -> None:
    forward = group_key_for_single_hop(
        seed_provider_id="primekg",
        seed_entity_id="D001241",
        relation="treats",
        direction="forward",
    )
    reverse = group_key_for_single_hop(
        seed_provider_id="primekg",
        seed_entity_id="D001241",
        relation="treats",
        direction="reverse",
    )
    assert forward != reverse


def test_a_different_seed_is_a_different_group() -> None:
    first = group_key_for_single_hop(
        seed_provider_id="primekg",
        seed_entity_id="D001241",
        relation="treats",
        direction="forward",
    )
    second = group_key_for_single_hop(
        seed_provider_id="primekg",
        seed_entity_id="D002",
        relation="treats",
        direction="forward",
    )
    assert first != second


def test_a_different_answer_type_is_a_different_group() -> None:
    first = group_key_for_single_hop(
        seed_provider_id="primekg",
        seed_entity_id="D001241",
        relation="treats",
        direction="forward",
        answer_entity_type="disease",
    )
    second = group_key_for_single_hop(
        seed_provider_id="primekg",
        seed_entity_id="D001241",
        relation="treats",
        direction="forward",
        answer_entity_type="gene",
    )
    assert first != second


def test_the_whole_ordered_chain_is_part_of_the_key() -> None:
    """A two-hop and a three-hop question are different questions."""
    two = group_key_for_path(
        seed_provider_id="primekg",
        seed_entity_id="D001241",
        steps=[
            GroupStep(position=0, relation="treats", direction="forward"),
            GroupStep(position=1, relation="indication", direction="forward"),
        ],
    )
    three = group_key_for_path(
        seed_provider_id="primekg",
        seed_entity_id="D001241",
        steps=[
            GroupStep(position=0, relation="treats", direction="forward"),
            GroupStep(position=1, relation="indication", direction="forward"),
            GroupStep(position=2, relation="symptom", direction="forward"),
        ],
    )
    assert two != three


def test_the_chain_order_matters() -> None:
    first = group_key_for_path(
        seed_provider_id="primekg",
        seed_entity_id="D001241",
        steps=[
            GroupStep(position=0, relation="treats", direction="forward"),
            GroupStep(position=1, relation="indication", direction="forward"),
        ],
    )
    second = group_key_for_path(
        seed_provider_id="primekg",
        seed_entity_id="D001241",
        steps=[
            GroupStep(position=0, relation="indication", direction="forward"),
            GroupStep(position=1, relation="treats", direction="forward"),
        ],
    )
    assert first != second


def test_a_multi_path_key_does_not_depend_on_the_discovery_order() -> None:
    signature_a = [
        GroupStep(position=0, relation="treats", direction="forward"),
        GroupStep(position=1, relation="indication", direction="forward"),
    ]
    signature_b = [
        GroupStep(position=0, relation="symptom", direction="forward"),
    ]
    first = group_key_for_multi_paths(
        seed_provider_id="primekg",
        seed_entity_id="D001241",
        path_signatures=[signature_a, signature_b],
    )
    second = group_key_for_multi_paths(
        seed_provider_id="primekg",
        seed_entity_id="D001241",
        path_signatures=[signature_b, signature_a],
    )
    assert first == second


def test_the_key_is_recomputable_from_the_payload() -> None:
    payload = GroupPayload(
        seed_provider_id="primekg",
        seed_entity_id="D001241",
        steps=[
            GroupStep(position=0, relation="treats", direction="forward")
        ],
        answer_entity_type="disease",
    )
    assert is_recomputable(payload, group_key(payload))


def test_a_forged_key_is_not_recomputable() -> None:
    payload = GroupPayload(
        seed_provider_id="primekg",
        seed_entity_id="D001241",
        steps=[
            GroupStep(position=0, relation="treats", direction="forward")
        ],
    )
    assert not is_recomputable(payload, "group.forged")
    assert not is_recomputable(payload, None)


def test_the_key_does_not_use_display_names() -> None:
    """The key is built from ids and relations, never from wording."""
    payload = GroupPayload(
        seed_provider_id="primekg",
        seed_entity_id="D001241",
        steps=[
            GroupStep(position=0, relation="treats", direction="forward")
        ],
    )
    assert "aspirin" not in payload.canonical_json()


# ---- building the groups -------------------------------------------------


def test_candidates_with_the_same_structure_land_in_one_group() -> None:
    payload = GroupPayload(
        seed_provider_id="primekg",
        seed_entity_id="D001241",
        steps=[
            GroupStep(position=0, relation="treats", direction="forward")
        ],
    )
    other = GroupPayload(
        seed_provider_id="primekg",
        seed_entity_id="D001241",
        steps=[
            GroupStep(position=0, relation="symptom", direction="forward")
        ],
    )
    groups = build_groups([("q1", payload), ("q2", payload), ("q3", other)])
    assert len(groups) == 2
    assert sorted(len(group.question_ids) for group in groups.values()) == [1, 2]


def test_a_group_holds_its_candidate_ids() -> None:
    payload = GroupPayload(
        seed_provider_id="primekg",
        seed_entity_id="D001241",
        steps=[
            GroupStep(position=0, relation="treats", direction="forward")
        ],
    )
    groups = build_groups([("q1", payload), ("q2", payload)])
    key = group_key(payload)
    assert sorted(groups[key].question_ids) == ["q1", "q2"]


def test_the_group_version_is_recorded() -> None:
    assert REVIEW_GROUP_VERSION == "review-group-v1"


# ---- the ranking ---------------------------------------------------------


def test_the_weights_sum_to_one_hundred() -> None:
    assert sum(RANKING_WEIGHTS.values()) == 100.0


def test_a_perfect_candidate_scores_one_hundred() -> None:
    assessment = rank_candidate(
        RankingSignals(
            verifier_accepted=True,
            evidence_complete=True,
            answer_determinate=True,
            has_shorter_path=False,
            leaks_answer=False,
            naturalness_score=1.0,
            template_likeness=0.0,
        )
    )
    assert assessment.total_score == 100.0
    assert assessment.ranking_version == RANKING_VERSION


def test_every_factor_is_explained() -> None:
    assessment = rank_candidate(
        RankingSignals(
            verifier_accepted=True,
            evidence_complete=True,
            answer_determinate=True,
        )
    )
    assert len(assessment.factors) == len(RANKING_WEIGHTS)
    assert all(factor.explanation for factor in assessment.factors)


def test_a_failing_verifier_costs_thirty_points() -> None:
    good = rank_candidate(
        RankingSignals(
            verifier_accepted=True,
            evidence_complete=True,
            answer_determinate=True,
            naturalness_score=1.0,
        )
    )
    bad = rank_candidate(
        RankingSignals(
            verifier_accepted=False,
            evidence_complete=True,
            answer_determinate=True,
            naturalness_score=1.0,
        )
    )
    assert good.total_score - bad.total_score == 30.0


def test_a_leaking_answer_costs_ten_points() -> None:
    clean = rank_candidate(RankingSignals(leaks_answer=False))
    leaking = rank_candidate(RankingSignals(leaks_answer=True))
    assert clean.total_score - leaking.total_score == 10.0


def test_a_shorter_path_costs_ten_points() -> None:
    clean = rank_candidate(RankingSignals(has_shorter_path=False))
    shortcut = rank_candidate(RankingSignals(has_shorter_path=True))
    assert clean.total_score - shortcut.total_score == 10.0


def test_a_blocked_candidate_is_capped() -> None:
    assessment = rank_candidate(
        RankingSignals(
            verifier_accepted=True,
            evidence_complete=True,
            answer_determinate=True,
            naturalness_score=1.0,
            blocking_failed=True,
        )
    )
    assert assessment.total_score <= BLOCKED_SCORE_CAP


def test_the_deductions_are_visible() -> None:
    assessment = rank_candidate(
        RankingSignals(
            verifier_accepted=False,
            evidence_complete=True,
            answer_determinate=True,
            has_shorter_path=True,
            leaks_answer=True,
        )
    )
    summary = explain(assessment)
    assert "verifier_accepted" in summary
    assert "no_leakage" in summary


def test_the_sort_order_is_by_score_then_id() -> None:
    assessments = [
        ("q2", rank_candidate(RankingSignals(verifier_accepted=True))),
        ("q1", rank_candidate(RankingSignals(verifier_accepted=True))),
        ("q3", rank_candidate(RankingSignals())),
    ]
    assert sort_candidates(assessments) == ["q1", "q2", "q3"]


def test_the_sort_order_is_stable() -> None:
    assessments = [
        ("q2", rank_candidate(RankingSignals(verifier_accepted=True))),
        ("q1", rank_candidate(RankingSignals(verifier_accepted=True))),
    ]
    assert sort_candidates(assessments) == sort_candidates(
        list(reversed(assessments))
    )


def test_the_ranking_does_not_touch_the_validation_level() -> None:
    """Ranking is a display order, not a verdict."""
    from medical_qa_data_factory.domain.common import utc_now
    from medical_qa_data_factory.domain.questions import QuestionSpec

    question = QuestionSpec(
        question_id="q1",
        logical_question_id="l1",
        question_version="v1",
        bundle_ids=["b1"],
        generation_mode="automatic",
        canonical_question="Which disease?",
        question_variants=[
            {
                "variant_id": "v1",
                "text": "Which disease?",
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
        gold_answer={"answer_mode": "fact", "short_answer": "x"},
        evidence_requirements={"sufficiency_rule_version": "v1"},
        expected_route="kg",
        split_group_id="s1",
        created_at=utc_now(),
    )
    assessment = rank_candidate(RankingSignals(verifier_accepted=True))
    updated = question.model_copy(
        update={"ranking_assessment": assessment}
    )
    assert updated.validation_level is question.validation_level
    assert updated.review_disposition is ReviewDisposition.PENDING

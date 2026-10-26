import pytest

from medical_qa_data_factory.demo_adapters import (
    DemoDraftAgent,
    DemoKGTool,
    DemoLiteratureTool,
)
from medical_qa_data_factory.domain import (
    ClaimStatus,
    DatasetSplit,
    EntityRef,
    GenerationMode,
    GenerationTask,
    LinkStatus,
    Route,
    ValidationLevel,
)
from medical_qa_data_factory.pipeline import DataFactoryPipeline
from medical_qa_data_factory.repository import LocalArtifactRepository
from medical_qa_data_factory.review import InvalidReviewAction, review_claim, review_question


def build_result(tmp_path):
    repository = LocalArtifactRepository(tmp_path)
    seed = EntityRef(
        entity_ref_id="seed-aspirin",
        raw_mention="阿司匹林",
        canonical_name="aspirin",
        canonical_id="demo:aspirin",
        namespace="demo",
        entity_type="drug",
        aliases=["阿司匹林"],
        link_status=LinkStatus.EXACT,
        link_score=1.0,
        source="test",
    )
    task = GenerationTask.create(
        task_id="review-demo",
        generation_mode=GenerationMode.HYBRID,
        seed_entities=[seed],
        target_question_count=1,
    )
    return DataFactoryPipeline(
        repository=repository,
        kg_tool=DemoKGTool(),
        literature_tool=DemoLiteratureTool(),
        draft_agent=DemoDraftAgent(),
    ).run(task)


def test_confirm_creates_audited_new_version(tmp_path):
    result = build_result(tmp_path)
    question = result.questions[0]

    confirmed = review_question(
        question,
        "confirm",
        summary="证据充分，人工确认",
        verified_route=Route.HYBRID,
        gold_short_answer="抑制血小板聚集",
        dataset_split=DatasetSplit.TRAIN,
    )

    assert confirmed.question_id == question.question_id
    assert confirmed.question_version != question.question_version
    assert confirmed.validation_level is ValidationLevel.HUMAN_CONFIRMED
    assert confirmed.verified_route is Route.HYBRID
    assert confirmed.gold_answer.short_answer == "抑制血小板聚集"
    assert confirmed.dataset_split is DatasetSplit.TRAIN
    assert confirmed.changes[-1].actor_type.value == "human"
    assert confirmed.changes[-1].parent_version == question.question_version
    assert any(check.check_type == "human_confirmation" for check in confirmed.validation_checks)


def test_confirm_defaults_verified_route_to_expected(tmp_path):
    question = build_result(tmp_path).questions[0]
    confirmed = review_question(question, "confirm", summary="符合预期")
    assert confirmed.verified_route is question.expected_route


def test_return_keeps_question_draft(tmp_path):
    question = build_result(tmp_path).questions[0]
    returned = review_question(question, "return", summary="证据不足，退回重采")
    assert returned.validation_level is ValidationLevel.DRAFT
    assert any(check.check_type == "human_return" for check in returned.validation_checks)


def test_reject_blocks_further_confirmation(tmp_path):
    question = build_result(tmp_path).questions[0]
    rejected = review_question(question, "reject", summary="答案错误")
    assert rejected.validation_level is ValidationLevel.REJECTED
    with pytest.raises(InvalidReviewAction):
        review_question(rejected, "confirm", summary="反悔")


def test_unknown_action_is_rejected(tmp_path):
    question = build_result(tmp_path).questions[0]
    with pytest.raises(InvalidReviewAction):
        review_question(question, "publish", summary="非法动作")


def test_claim_review_updates_status_and_keeps_bundle_identity(tmp_path):
    result = build_result(tmp_path)
    bundle = result.bundles[0]
    claim_id = bundle.claims[0].claim_id

    updated = review_claim(
        bundle, claim_id, ClaimStatus.SUPPORTED, summary="人工复核支持"
    )

    assert updated.bundle_id == bundle.bundle_id
    assert updated.bundle_version != bundle.bundle_version
    assert updated.claims[0].status is ClaimStatus.SUPPORTED
    assert updated.claims[0].rationale == "人工复核支持"


def test_unknown_claim_is_rejected(tmp_path):
    bundle = build_result(tmp_path).bundles[0]
    with pytest.raises(InvalidReviewAction):
        review_claim(bundle, "missing", ClaimStatus.REJECTED, summary="不存在")

"""Contract boundaries, defaults, and rejection of illegal inputs."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from medical_qa_data_factory.domain.questions import AnswerForm
from medical_qa_data_factory.generation.contracts import (
    AgentStep,
    DEFAULT_MAX_HOPS,
    EntityGenerationRequest,
    GenerationEpisode,
    MAX_HOPS_CEILING,
    QuestionBlueprint,
    QuestionDraft,
    RejectionRecord,
    StopReason,
    ToolObservation,
    UpstreamEntitySeed,
    VerificationResult,
)
from medical_qa_data_factory.generation.state import EntityGenerationState
from medical_qa_data_factory.llm.contracts import (
    StructuredGenerationRequest,
    StructuredGenerationResult,
)

NOW = "2026-10-17T00:00:00Z"


def _seed(**overrides) -> UpstreamEntitySeed:
    payload = {
        "provider_id": "primekg",
        "native_id": "D001241",
        "entity_type": "exposure",
        "canonical_name": "Aspirin",
    }
    payload.update(overrides)
    return UpstreamEntitySeed(**payload)


def _request(**overrides) -> EntityGenerationRequest:
    payload = {"task_id": "t1", "workspace": "ws", "seed": _seed()}
    payload.update(overrides)
    return EntityGenerationRequest(**payload)


def _blueprint(**overrides) -> QuestionBlueprint:
    payload = {
        "blueprint_id": "bp1",
        "claim_ids": ["c1"],
        "focus_entity_id": "primekg:exposure:D001241",
        "answer_entity_id": "primekg:disease:B156",
        "answer_endpoint": "tail",
        "answer_form": AnswerForm.ENTITY,
        "relation_semantics": "association",
        "required_evidence_ids": ["e1"],
    }
    payload.update(overrides)
    return QuestionBlueprint(**payload)


@pytest.mark.parametrize("count", [1, 5, 20])
def test_question_count_accepts_documented_range(count: int) -> None:
    assert _request(question_count=count).question_count == count


@pytest.mark.parametrize("count", [0, 21, -1])
def test_question_count_rejects_out_of_range(count: int) -> None:
    with pytest.raises(ValidationError):
        _request(question_count=count)


@pytest.mark.parametrize(
    "field", ["provider_id", "native_id", "entity_type", "canonical_name"]
)
def test_seed_identity_fields_are_required(field: str) -> None:
    with pytest.raises(ValidationError):
        _seed(**{field: ""})


def test_request_defaults_and_hop_cap() -> None:
    request = _request()
    assert request.policy_version == "entity-generation-v1"
    assert request.max_hops == DEFAULT_MAX_HOPS
    assert request.max_tool_calls == 10
    assert request.max_edges_observed == 200
    assert request.max_agent_steps == 20
    assert request.max_wall_time_seconds == 180
    assert request.max_cost_amount is None
    assert request.max_total_tokens is None


@pytest.mark.parametrize("hops", [0, MAX_HOPS_CEILING + 1])
def test_request_hops_respect_the_system_ceiling(hops: int) -> None:
    with pytest.raises(ValidationError):
        _request(max_hops=hops)


@pytest.mark.parametrize("hops", [1, 2, 3, MAX_HOPS_CEILING])
def test_request_allows_up_to_the_system_ceiling(hops: int) -> None:
    assert _request(max_hops=hops).max_hops == hops


def test_contract_models_forbid_extra_fields() -> None:
    with pytest.raises(ValidationError):
        UpstreamEntitySeed(
            provider_id="p",
            native_id="1",
            entity_type="t",
            canonical_name="n",
            unexpected="x",
        )


def test_blueprint_answer_may_not_be_the_focus_entity() -> None:
    with pytest.raises(ValidationError):
        _blueprint(answer_entity_id="primekg:exposure:D001241")


def test_blueprint_requires_single_fact_and_kg_route() -> None:
    blueprint = _blueprint()
    assert blueprint.question_type == "single_fact"
    assert blueprint.expected_route == "kg"


def test_draft_carries_candidate_answer_and_episode() -> None:
    draft = QuestionDraft(
        draft_id="d1",
        blueprint_id="bp1",
        question_text="Which disease is Aspirin indicated for?",
        candidate_answer={
            "answer_mode": "entity",
            "values": [{"canonical_id": "primekg:disease:B156"}],
            "short_answer": "headache",
        },
        claim_ids=["c1"],
        evidence_ids=["e1"],
        generation_episode_id="ep1",
    )
    assert draft.candidate_answer.confidence is None
    assert draft.candidate_answer.explanation is None


@pytest.mark.parametrize(
    "verdict", ["accepted", "needs_revision", "rejected"]
)
def test_verification_verdicts(verdict: str) -> None:
    result = VerificationResult(draft_id="d1", verdict=verdict)
    assert result.blocking is False


def test_step_versioning_rules() -> None:
    started = AgentStep(
        step_id="s1",
        step_version="v1",
        episode_id="ep1",
        node="verify_seed",
        sequence=1,
        status="started",
        input_digest="abc",
        started_at=NOW,
    )
    assert started.completed_at is None
    completed = AgentStep(
        step_id="s1",
        step_version="v2",
        episode_id="ep1",
        node="verify_seed",
        sequence=1,
        status="completed",
        input_digest="abc",
        started_at=NOW,
        completed_at=NOW,
    )
    assert completed.step_id == started.step_id
    assert completed.step_version != started.step_version
    with pytest.raises(ValidationError):
        AgentStep(
            step_id="s2",
            step_version="v1",
            episode_id="ep1",
            node="x",
            sequence=2,
            status="completed",
            input_digest="abc",
            started_at=NOW,
        )


def test_episode_requires_question_target() -> None:
    episode = GenerationEpisode(
        episode_id="ep1",
        episode_version="v1",
        task_id="t1",
        seed=_seed(),
        policy_version="entity-generation-v1",
        question_target=5,
        started_at=NOW,
    )
    assert episode.stop_reason is None
    assert episode.completed_at is None


def test_stop_reason_covers_documented_set() -> None:
    assert {item.value for item in StopReason} >= {
        "target_reached",
        "evidence_exhausted",
        "no_evidence",
        "no_valid_claims",
        "no_candidate_proposed",
        "all_candidates_hard_rejected",
        "budget_exhausted",
        "invalid_upstream_identity",
        "provider_error",
        "fatal_error",
    }


def test_observation_and_rejection_shapes() -> None:
    observation = ToolObservation(
        observation_id="o1",
        episode_id="ep1",
        tool_name="kg_primekg",
        operation_key="ep1|collect_neighbors|outgoing|1",
        request_digest="abc",
        row_count=3,
        elapsed_ms=5,
    )
    assert observation.error_code is None
    rejection = RejectionRecord(
        rejection_id="r1",
        episode_id="ep1",
        stage="verifier",
        reason_codes=["answer_leakage"],
        created_at=NOW,
    )
    assert rejection.blocking is False
    assert rejection.draft_id is None


def test_model_request_and_result_contracts() -> None:
    request = StructuredGenerationRequest(
        invocation_id="inv1",
        role="question_writer",
        system_prompt_version="writer-v1",
        payload={"blueprint_id": "bp1"},
        idempotency_key="ep1|write|bp1|1",
    )
    result = StructuredGenerationResult(
        invocation_id="inv1",
        role="question_writer",
        parsed={"question_text": "q"},
        usage={"input_tokens": 0},
        model_config_id="fake-model-v1",
    )
    assert request.role != "answer_verifier"
    assert result.parsed["question_text"] == "q"


def test_state_is_json_serializable_only() -> None:
    state: EntityGenerationState = {
        "request": _request().model_dump(mode="json"),
        "episode_id": "ep1",
        "task_id": "t1",
        "current_node": "verify_seed",
        "seed_ref": None,
        "bundle_id": None,
        "bundle_version": None,
        "evidence_ids": [],
        "claim_ids": [],
        "blueprints": [],
        "draft_question_ids": [],
        "accepted_question_ids": [],
        "rejected_question_ids": [],
        "revision_counts": {},
        "tool_calls": 0,
        "edges_observed": 0,
        "steps": 0,
        "stop_reason": None,
        "errors": [],
    }
    import json

    json.dumps(state)
    assert state["episode_id"] == "ep1"


def test_generation_contracts_do_not_import_langgraph() -> None:
    import medical_qa_data_factory.generation.contracts as module

    source = module.__file__
    assert source is not None
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    assert "langgraph" not in text

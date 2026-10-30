"""Episodes and steps are versioned append-only records."""
from __future__ import annotations

from pathlib import Path

import pytest

from medical_qa_data_factory.generation.contracts import (
    AgentStep,
    GenerationEpisode,
    RejectionRecord,
    StopReason,
    ToolObservation,
    UpstreamEntitySeed,
    VerificationResult,
)
from medical_qa_data_factory.generation.repository import (
    GenerationRepository,
    IdempotencyConflictError,
)
from medical_qa_data_factory.generation.skills import (
    SkillOutcome,
    SkillRuntimeContext,
    SkillSelection,
    build_default_skill_registry,
)

NOW = "2026-10-17T00:00:00Z"
LATER = "2026-10-17T00:05:00Z"


def _seed() -> UpstreamEntitySeed:
    return UpstreamEntitySeed(
        provider_id="primekg",
        native_id="D001241",
        entity_type="exposure",
        canonical_name="Aspirin",
    )


def _episode(version: str = "v1", **overrides) -> GenerationEpisode:
    payload = {
        "episode_id": "ep1",
        "episode_version": version,
        "task_id": "t1",
        "seed": _seed(),
        "policy_version": "entity-generation-v1",
        "question_target": 5,
        "started_at": NOW,
    }
    payload.update(overrides)
    return GenerationEpisode(**payload)


def _step(version: str, status: str, **overrides) -> AgentStep:
    payload = {
        "step_id": "st1",
        "step_version": version,
        "episode_id": "ep1",
        "node": "verify_seed",
        "sequence": 1,
        "status": status,
        "input_digest": "digest",
        "started_at": NOW,
    }
    if status != "started":
        payload["completed_at"] = LATER
    payload.update(overrides)
    return AgentStep(**payload)


def test_episode_versions_accumulate(tmp_path: Path) -> None:
    repo = GenerationRepository(tmp_path)
    assert repo.save_episode(_episode("v1")) == "written"
    assert repo.save_episode(
        _episode("v2", stop_reason=StopReason.TARGET_REACHED)
    ) == "written"
    assert repo.artifacts.list_versions("episodes", "ep1") == ["v1", "v2"]
    assert repo.load_latest_episode("ep1").stop_reason is StopReason.TARGET_REACHED


def test_identical_rewrite_is_idempotent(tmp_path: Path) -> None:
    repo = GenerationRepository(tmp_path)
    repo.save_episode(_episode("v1"))
    assert repo.save_episode(_episode("v1")) == "existing"


def test_same_identity_different_content_conflicts(tmp_path: Path) -> None:
    repo = GenerationRepository(tmp_path)
    repo.save_episode(_episode("v1"))
    with pytest.raises(IdempotencyConflictError):
        repo.save_episode(_episode("v1", question_target=9))


def test_step_started_and_completed_share_one_step_id(tmp_path: Path) -> None:
    repo = GenerationRepository(tmp_path)
    repo.save_step(_step("v1", "started"))
    repo.save_step(_step("v2", "completed", elapsed_ms=12))
    assert repo.list_step_versions("st1") == ["v1", "v2"]
    latest = repo.load_step("st1", "v2")
    assert latest.status == "completed"
    assert latest.elapsed_ms == 12


def test_list_steps_by_episode_orders_by_sequence(tmp_path: Path) -> None:
    repo = GenerationRepository(tmp_path)
    repo.save_step(_step("v1", "started", node="collect_neighbors", sequence=2))
    repo.save_step(
        AgentStep(
            step_id="st2",
            step_version="v1",
            episode_id="ep1",
            node="verify_seed",
            sequence=1,
            status="completed",
            input_digest="d",
            started_at=NOW,
            completed_at=LATER,
        )
    )
    steps = repo.list_steps_by_episode("ep1")
    assert [item.node for item in steps] == ["verify_seed", "collect_neighbors"]


def test_append_only_collections_are_listable_per_episode(tmp_path: Path) -> None:
    repo = GenerationRepository(tmp_path)
    repo.save_observation(
        ToolObservation(
            observation_id="o1",
            episode_id="ep1",
            tool_name="kg_primekg",
            operation_key="ep1|collect|outgoing|1",
            request_digest="d",
            row_count=2,
            elapsed_ms=3,
        )
    )
    repo.save_verification(
        VerificationResult(draft_id="d1", verdict="accepted", episode_id="ep1")
    )
    repo.save_rejection(
        RejectionRecord(
            rejection_id="r1",
            episode_id="ep1",
            stage="verifier",
            reason_codes=["answer_leakage"],
            created_at=NOW,
        )
    )
    assert len(repo.list_observations_by_episode("ep1")) == 1
    assert len(repo.list_verifications_by_episode("ep1")) == 1
    assert len(repo.list_rejections_by_episode("ep1")) == 1
    assert repo.list_observations_by_episode("other") == []


def test_graph_thread_id_equals_episode_id(tmp_path: Path) -> None:
    repo = GenerationRepository(tmp_path)
    assert repo.graph_thread_id("ep1") == "ep1"


def test_skill_invocations_and_outcomes_are_append_only(tmp_path: Path) -> None:
    repo = GenerationRepository(tmp_path)
    registry = build_default_skill_registry()
    invocation = registry.prepare_invocation(
        SkillSelection(
            skill_id="primekg-neighbor-exploration",
            arguments={
                "subject_entity_id": "primekg:drug:1",
                "direction": "outgoing",
            },
        ),
        runtime=SkillRuntimeContext(
            available_tool_names=["kg_neighbors"],
            allowed_action_types=["query_neighbors"],
            remaining_tool_calls=2,
            remaining_edge_budget=10,
        ),
        episode_id="ep1",
        turn=1,
        state_digest="state-digest",
    )
    outcome = SkillOutcome(
        invocation_id=invocation.invocation_id,
        episode_id="ep1",
        status="success",
        summary="skill produced one evidence artifact",
        artifact_ids=["evidence-1"],
        evidence_artifact_ids=["evidence-1"],
        produced_artifact_types=["kg_evidence"],
        evidence_gain=1,
    )

    assert repo.save_skill_invocation(invocation) == "written"
    assert repo.save_skill_outcome(outcome) == "written"
    assert repo.list_skill_invocations_by_episode("ep1") == [invocation]
    assert repo.list_skill_outcomes_by_episode("ep1") == [outcome]
    replay = SkillOutcome.model_validate(
        {**outcome.model_dump(mode="json"), "created_at": LATER}
    )
    assert repo.save_skill_outcome(replay) == "existing"


def test_episode_versions_keep_history(tmp_path: Path) -> None:
    repo = GenerationRepository(tmp_path)
    repo.save_episode(_episode("v1"))
    repo.save_episode(_episode("v2", task_id="t2"))
    assert repo.list_episode_ids() == ["ep1"]
    assert repo.load_episode("ep1", "v1").task_id == "t1"
    assert repo.load_episode("ep1", "v2").task_id == "t2"

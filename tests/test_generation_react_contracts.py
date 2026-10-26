"""Step 1 contract tests: ReAct actions, ordered paths, sufficiency.

Every model here must reject the structural mistakes that would let a bag of
edges pass as a multi-hop path, or let a model invent an action outside the
whitelist.
"""
from __future__ import annotations

import copy
import json

import pytest
from pydantic import ValidationError

from medical_qa_data_factory.domain import (
    AnswerForm,
    EntityRef,
    EvidenceDirection,
    KGEvidence,
    LinkStatus,
)
from medical_qa_data_factory.generation.contracts import (
    EpisodeState,
    EvidencePath,
    EvidencePathStep,
    EvidenceSufficiencyDecision,
    ExplorationAction,
    ExplorationActionType,
    ExplorationDecision,
    ExpandAction,
    FinishAction,
    LiteratureAction,
    NeighborAction,
    PathAction,
    QuestionBlueprint,
    RelationSemantics,
    StopReason,
    validate_action,
)
from medical_qa_data_factory.llm.contracts import ModelRole

NOW = "2026-10-18T00:00:00Z"


def _ref(name: str, canonical_id: str, entity_type: str) -> EntityRef:
    return EntityRef(
        entity_ref_id=f"entity-{canonical_id}",
        raw_mention=name,
        canonical_name=name,
        canonical_id=canonical_id,
        namespace="primekg",
        entity_type=entity_type,
        aliases=[],
        link_status=LinkStatus.EXACT,
        link_score=1.0,
        source="primekg",
    )


ASPIRIN = _ref("aspirin", "primekg:exposure:D001241", "exposure")
PROTEIN = _ref("PTGS1", "primekg:protein:5742", "protein")
DISEASE = _ref("migraine", "primekg:disease:B156", "disease")


def _edge(
    head: EntityRef,
    relation: str,
    tail: EntityRef,
    edge_id: str,
    direction: EvidenceDirection = EvidenceDirection.FORWARD,
) -> KGEvidence:
    return KGEvidence(
        evidence_id=f"kg.{edge_id}",
        head=head,
        relation=relation,
        tail=tail,
        direction=direction,
        source_database="primekg",
        source_record_id=edge_id,
        confidence=1.0,
        retrieval_action={"provider_id": "primekg"},
        retrieved_at=NOW,
    )

# ---------------------------------------------------------------- actions


def test_neighbor_action_requires_its_own_fields() -> None:
    action = validate_action(
        {
            "action_type": "query_neighbors",
            "subject_entity_id": "primekg:exposure:D001241",
            "direction": "outgoing",
            "max_results": 25,
        }
    )
    assert isinstance(action, NeighborAction)
    assert action.max_results == 25
    assert action.relations == []


@pytest.mark.parametrize(
    "payload",
    [
        {"action_type": "query_neighbors", "direction": "outgoing"},
        {
            "action_type": "query_neighbors",
            "subject_entity_id": "a",
            "direction": "sideways",
        },
        {
            "action_type": "query_neighbors",
            "subject_entity_id": "a",
            "direction": "outgoing",
            "max_results": 0,
        },
        {
            "action_type": "query_neighbors",
            "subject_entity_id": "a",
            "direction": "outgoing",
            "max_results": 101,
        },
    ],
)
def test_illegal_neighbor_actions_are_rejected(payload: dict) -> None:
    with pytest.raises(ValidationError):
        validate_action(payload)


def test_actions_are_a_discriminated_union() -> None:
    """A model cannot smuggle fields from one action into another."""
    with pytest.raises(ValidationError):
        validate_action(
            {
                "action_type": "query_neighbors",
                "subject_entity_id": "a",
                "direction": "outgoing",
                "max_paths": 5,
            }
        )
    with pytest.raises(ValidationError):
        validate_action(
            {
                "action_type": "expand_entity",
                "subject_entity_id": "a",
                "direction": "outgoing",
                "cursor": "abc",
            }
        )


def test_path_action_is_capped_at_two_hops() -> None:
    action = validate_action(
        {
            "action_type": "query_paths",
            "start_entity_id": "a",
            "target_entity_id": "c",
            "max_hops": 2,
        }
    )
    assert isinstance(action, PathAction)
    assert action.max_hops == 2


def test_finish_action_carries_a_structured_stop_reason() -> None:
    action = validate_action(
        {"action_type": "finish", "stop_reason": "evidence_sufficient"}
    )
    assert isinstance(action, FinishAction)
    assert action.stop_reason is StopReason.EVIDENCE_SUFFICIENT


def test_literature_action_requires_a_query() -> None:
    with pytest.raises(ValidationError):
        validate_action({"action_type": "retrieve_literature"})
    action = validate_action(
        {"action_type": "retrieve_literature", "query": "aspirin migraine"}
    )
    assert isinstance(action, LiteratureAction)


def test_expand_action_round_trips() -> None:
    action = ExpandAction.model_validate(
        {
            "action_type": "expand_entity",
            "subject_entity_id": "primekg:exposure:D001241",
            "direction": "both",
            "relations": ["drug_protein"],
            "target_types": ["protein"],
            "max_results": 40,
        }
    )
    restored = ExpandAction.model_validate(action.model_dump(mode="json"))
    assert restored == action


def test_exploration_decision_round_trips_and_keeps_audit_fields() -> None:
    decision = ExplorationDecision(
        decision_id="dec.1",
        episode_id="ep1",
        turn=1,
        action=ExpandAction.model_validate(
            {
                "action_type": "expand_entity",
                "subject_entity_id": "primekg:exposure:D001241",
                "direction": "outgoing",
            }
        ),
        expected_information="protein neighbours of the seed",
        evidence_gap="no second-hop endpoint yet",
        cited_observation_ids=["obs.1"],
        action_hash="a" * 64,
        policy_version="entity-generation-v2",
        created_at=NOW,
    )
    restored = ExplorationDecision.model_validate(decision.model_dump(mode="json"))
    assert restored.action.action_type is ExplorationActionType.EXPAND_ENTITY
    assert restored.cited_observation_ids == ["obs.1"]

# ---------------------------------------------------------------- paths


def _step(
    position: int,
    evidence_id: str,
    traversal: str,
    start: str,
    end: str,
) -> EvidencePathStep:
    return EvidencePathStep(
        position=position,
        evidence_id=evidence_id,
        traversal_direction=traversal,
        from_entity_id=start,
        relation="drug_protein" if position == 0 else "disease_protein",
        to_entity_id=end,
        provider_id="primekg",
        snapshot_id="snap-1",
        query_trace_id=f"trace-{position}",
    )


def _path() -> EvidencePath:
    steps = [
        _step(0, "kg.e1", "forward", ASPIRIN.canonical_id, PROTEIN.canonical_id),
        _step(
            1,
            "kg.e2",
            "reverse",
            PROTEIN.canonical_id,
            DISEASE.canonical_id,
        ),
    ]
    return EvidencePath(
        path_id="path.1",
        path_version="path.1.v1",
        episode_id="ep1",
        task_id="t1",
        bundle_id="bundle.1",
        provider_id="primekg",
        snapshot_id="snap-1",
        start_entity_id=ASPIRIN.canonical_id,
        end_entity_id=DISEASE.canonical_id,
        steps=steps,
        intermediate_entity_ids=[PROTEIN.canonical_id],
        source_evidence_ids=["kg.e1", "kg.e2"],
        path_hash="h" * 64,
        query_trace_ids=["trace-0", "trace-1"],
    )


def test_ordered_two_hop_path_round_trips() -> None:
    path = _path()
    restored = EvidencePath.model_validate(path.model_dump(mode="json"))
    assert restored.steps[0].to_entity_id == restored.steps[1].from_entity_id
    assert restored.end_entity_id == DISEASE.canonical_id


def test_broken_path_is_rejected() -> None:
    """Two edges that do not meet must not form a path."""
    path = _path()
    payload = path.model_dump(mode="json")
    payload["steps"][1]["from_entity_id"] = "primekg:protein:9999"
    with pytest.raises(ValidationError) as excinfo:
        EvidencePath.model_validate(payload)
    assert "broken" in str(excinfo.value)


def test_self_loop_path_is_rejected() -> None:
    path = _path()
    payload = path.model_dump(mode="json")
    payload["steps"][1]["from_entity_id"] = ASPIRIN.canonical_id
    payload["steps"][1]["to_entity_id"] = ASPIRIN.canonical_id
    with pytest.raises(ValidationError):
        EvidencePath.model_validate(payload)


def test_path_crossing_snapshots_is_rejected() -> None:
    path = _path()
    payload = path.model_dump(mode="json")
    payload["steps"][1]["snapshot_id"] = "snap-2"
    with pytest.raises(ValidationError) as excinfo:
        EvidencePath.model_validate(payload)
    assert "cross" in str(excinfo.value)


def test_path_crossing_providers_is_rejected() -> None:
    path = _path()
    payload = path.model_dump(mode="json")
    payload["steps"][1]["provider_id"] = "other"
    with pytest.raises(ValidationError):
        EvidencePath.model_validate(payload)


def test_path_with_duplicate_edge_is_rejected() -> None:
    """The same edge twice is not two hops."""
    path = _path()
    payload = path.model_dump(mode="json")
    payload["steps"][1]["evidence_id"] = payload["steps"][0]["evidence_id"]
    payload["source_evidence_ids"] = ["kg.e1", "kg.e1"]
    with pytest.raises(ValidationError):
        EvidencePath.model_validate(payload)


def test_source_evidence_ids_must_match_ordered_steps() -> None:
    path = _path()
    payload = path.model_dump(mode="json")
    payload["source_evidence_ids"] = ["kg.e2", "kg.e1"]
    with pytest.raises(ValidationError):
        EvidencePath.model_validate(payload)


def test_intermediate_ids_must_be_derived_from_joints() -> None:
    path = _path()
    payload = path.model_dump(mode="json")
    payload["intermediate_entity_ids"] = [DISEASE.canonical_id]
    with pytest.raises(ValidationError):
        EvidencePath.model_validate(payload)


def test_step_positions_must_be_contiguous() -> None:
    path = _path()
    payload = path.model_dump(mode="json")
    payload["steps"][1]["position"] = 2
    with pytest.raises(ValidationError):
        EvidencePath.model_validate(payload)


def test_single_hop_path_is_rejected() -> None:
    path = _path()
    payload = path.model_dump(mode="json")
    payload["steps"] = payload["steps"][:1]
    payload["intermediate_entity_ids"] = []
    payload["source_evidence_ids"] = ["kg.e1"]
    payload["query_trace_ids"] = ["trace-0"]
    with pytest.raises(ValidationError):
        EvidencePath.model_validate(payload)

# ---------------------------------------------------------------- sufficiency


def test_sufficiency_decision_round_trips() -> None:
    decision = EvidenceSufficiencyDecision(
        decision_id="suf.1",
        episode_id="ep1",
        turn=2,
        verdict="insufficient",
        evidence_gaps=["missing second hop endpoint"],
        cited_evidence_ids=["kg.e1"],
        cited_path_ids=[],
        suggested_action_type=ExplorationActionType.EXPAND_ENTITY,
        rationale="only one hop observed so far",
        guardrail_codes=["hop_complexity_below_target"],
        created_at=NOW,
    )
    restored = EvidenceSufficiencyDecision.model_validate(
        decision.model_dump(mode="json")
    )
    assert restored.suggested_action_type is ExplorationActionType.EXPAND_ENTITY


# ---------------------------------------------------------------- blueprint


def test_single_hop_blueprint_defaults_stay_backward_compatible() -> None:
    """Old blueprints carry no multi-hop fields; defaults must fill them in."""
    blueprint = QuestionBlueprint(
        blueprint_id="bp.1",
        claim_ids=["claim.e1"],
        focus_entity_id=ASPIRIN.canonical_id,
        answer_entity_id=DISEASE.canonical_id,
        answer_endpoint="tail",
        answer_form=AnswerForm.ENTITY,
        relation_semantics=RelationSemantics.DIRECTIONAL_FACT,
        required_evidence_ids=["kg.e1"],
    )
    assert blueprint.question_type == "single_fact"
    assert blueprint.hop_complexity == 1
    assert blueprint.required_path_ids == []
    assert blueprint.intermediate_entity_ids == []
    assert blueprint.leakage_constraints == []
    assert blueprint.expected_route == "kg"


def test_two_hop_blueprint_requires_a_path() -> None:
    base = dict(
        blueprint_id="bp.2",
        claim_ids=["claim.e1", "claim.e2"],
        focus_entity_id=ASPIRIN.canonical_id,
        answer_entity_id=DISEASE.canonical_id,
        answer_endpoint="tail",
        answer_form=AnswerForm.ENTITY,
        relation_semantics=RelationSemantics.ASSOCIATION,
        required_evidence_ids=["kg.e1", "kg.e2"],
        hop_complexity=2,
        required_path_ids=["path.1"],
        intermediate_entity_ids=[PROTEIN.canonical_id],
    )
    assert QuestionBlueprint.model_validate(base).hop_complexity == 2

    for missing in ("required_path_ids", "intermediate_entity_ids"):
        broken = copy.deepcopy(base)
        broken[missing] = []
        with pytest.raises(ValidationError):
            QuestionBlueprint.model_validate(broken)

    broken = copy.deepcopy(base)
    broken["required_evidence_ids"] = ["kg.e1"]
    with pytest.raises(ValidationError):
        QuestionBlueprint.model_validate(broken)


def test_blueprint_expected_route_allows_literature_and_hybrid() -> None:
    base = dict(
        blueprint_id="bp.3",
        claim_ids=["claim.e1"],
        focus_entity_id=ASPIRIN.canonical_id,
        answer_entity_id=DISEASE.canonical_id,
        answer_endpoint="tail",
        answer_form=AnswerForm.ENTITY,
        relation_semantics=RelationSemantics.DIRECTIONAL_FACT,
        required_evidence_ids=["kg.e1"],
    )
    for route in ("kg", "literature", "hybrid"):
        payload = {**base, "expected_route": route}
        assert QuestionBlueprint.model_validate(payload).expected_route == route
    with pytest.raises(ValidationError):
        QuestionBlueprint.model_validate({**base, "expected_route": "oracle"})


def test_blueprint_answer_may_not_be_the_focus() -> None:
    with pytest.raises(ValidationError):
        QuestionBlueprint.model_validate(
            {
                "blueprint_id": "bp.4",
                "claim_ids": ["claim.e1"],
                "focus_entity_id": ASPIRIN.canonical_id,
                "answer_entity_id": ASPIRIN.canonical_id,
                "answer_endpoint": "tail",
                "answer_form": AnswerForm.ENTITY,
                "relation_semantics": RelationSemantics.DIRECTIONAL_FACT,
                "required_evidence_ids": ["kg.e1"],
            }
        )


# ---------------------------------------------------------------- stop reasons


def test_stop_reason_priority_is_explicit() -> None:
    reasons = [
        StopReason.NO_NEW_EVIDENCE.value,
        StopReason.BUDGET_EXHAUSTED.value,
        StopReason.FATAL_ERROR.value,
        StopReason.TARGET_REACHED.value,
    ]
    assert StopReason.dominant(reasons) == StopReason.FATAL_ERROR.value
    assert StopReason.dominant([]) is None


def test_new_stop_reasons_map_to_episode_states() -> None:
    assert StopReason.FATAL_ERROR.episode_state() is EpisodeState.FAILED
    assert StopReason.INVALID_UPSTREAM_IDENTITY.episode_state() is EpisodeState.FAILED
    assert StopReason.EVIDENCE_SUFFICIENT.episode_state() is EpisodeState.PARTIAL
    assert StopReason.MODEL_ERROR.episode_state() is EpisodeState.PARTIAL


# ---------------------------------------------------------------- model roles


def test_new_model_roles_are_declared() -> None:
    roles: list[ModelRole] = [
        "exploration_planner",
        "evidence_curator",
        "sufficiency_judge",
        "claim_builder",
        "question_planner",
        "question_writer",
        "answer_verifier",
    ]
    assert len(set(roles)) == 7


def test_writer_and_verifier_are_declared_separate_roles() -> None:
    from medical_qa_data_factory.llm.contracts import SEPARATED_ROLE_PAIRS

    assert ("question_writer", "answer_verifier") in SEPARATED_ROLE_PAIRS


# ---------------------------------------------------------------- compatibility


def test_stored_blueprints_still_load_without_multi_hop_fields() -> None:
    """Real fixtures written before Step 1 must stay readable, not just the
    new model's own round-trip."""
    from pathlib import Path

    from medical_qa_data_factory.generation.contracts import QuestionBlueprint

    root = Path(__file__).resolve().parents[1]
    files = sorted(
        path
        for path in (root / "data" / "runs").rglob("*.json")
        if "questions" in path.parts
    )
    assert files, "the project keeps real stored artifacts for this check"
    checked = 0
    from medical_qa_data_factory.domain import QuestionSpec

    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or "question_id" not in payload:
            continue
        spec = QuestionSpec.model_validate(payload)
        assert spec.schema_version == "0.1.0"
        # The new optional path requirement must be absent, not defaulted away.
        assert spec.evidence_requirements.required_path_ids == []
        assert spec.taxonomy.hop_complexity in (None, 1)
        checked += 1
    assert checked >= 1


def test_stored_observations_still_load_without_react_fields() -> None:
    """Old ToolObservation records carry only the legacy fields."""
    import json
    from pathlib import Path

    from medical_qa_data_factory.generation.contracts import ToolObservation

    root = Path(__file__).resolve().parents[1]
    directory = root / "data" / "runs" / "aspirin-primekg-smoke" / "tool_observations"
    if not directory.exists():
        import pytest

        pytest.skip("no stored observation fixtures in this checkout")
    for path in sorted(directory.glob("*.json")):
        observation = ToolObservation.model_validate_json(path.read_text(encoding="utf-8"))
        assert observation.decision_id is None
        assert observation.action_type is None
        assert observation.rows_examined == 0
        assert observation.truncated is False


def test_stored_bundle_without_paths_field_still_loads() -> None:
    import json
    from pathlib import Path

    from medical_qa_data_factory.domain import EvidenceBundle

    root = Path(__file__).resolve().parents[1]
    directory = root / "data" / "runs" / "smoke" / "bundles"
    if not directory.exists():
        import pytest

        pytest.skip("no stored bundle fixtures in this checkout")
    for path in sorted(directory.rglob("*.json")):
        bundle = EvidenceBundle.model_validate_json(path.read_text(encoding="utf-8"))
        assert bundle.paths == []

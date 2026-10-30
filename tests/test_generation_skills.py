from __future__ import annotations

import pytest

from medical_qa_data_factory.generation.contracts import (
    CtdAction,
    ExpandAction,
    ExplorationActionType,
    LiteratureAction,
)
from medical_qa_data_factory.generation.exploration import AgentDecision
from medical_qa_data_factory.generation.skills import (
    SkillOutcome,
    SkillCostEstimate,
    SkillDefinition,
    SkillRegistry,
    SkillRuntimeContext,
    SkillSelection,
    SkillUnavailableError,
    build_default_skill_registry,
)


def _runtime(**updates) -> SkillRuntimeContext:
    payload = {
        "seed_entity_type": "disease",
        "available_tool_names": ["kg_neighbors", "kg_paths"],
        "allowed_action_types": [
            ExplorationActionType.QUERY_NEIGHBORS.value,
            ExplorationActionType.EXPAND_ENTITY.value,
            ExplorationActionType.QUERY_PATHS.value,
        ],
        "remaining_tool_calls": 4,
        "remaining_edge_budget": 50,
    }
    payload.update(updates)
    return SkillRuntimeContext(**payload)


def test_skill_retrieval_is_deterministic_and_state_bounded() -> None:
    registry = build_default_skill_registry()

    first = registry.available_skills(_runtime())
    second = registry.available_skills(_runtime())

    assert [item.skill_id for item in first] == [
        "primekg-entity-expansion",
        "primekg-neighbor-exploration",
    ]
    assert [item.model_dump() for item in first] == [
        item.model_dump() for item in second
    ]
    assert all(
        "action_type" not in item.input_schema.get("properties", {})
        for item in first
    )


def test_skill_retrieval_excludes_skills_without_budget_or_capability() -> None:
    registry = build_default_skill_registry()

    assert registry.available_skills(
        _runtime(remaining_tool_calls=0)
    ) == []
    assert registry.available_skills(
        _runtime(available_tool_names=[])
    ) == []
    assert registry.available_skills(
        _runtime(remaining_edge_budget=0)
    ) == []


def test_ctd_and_pubmed_skills_are_exposed_only_with_prerequisites() -> None:
    registry = build_default_skill_registry()

    ctd = registry.available_skills(
        _runtime(
            seed_entity_type="exposure",
            available_tool_names=["ctd_lookup"],
            allowed_action_types=["query_ctd"],
            remaining_edge_budget=None,
        )
    )
    pubmed_missing_evidence = registry.available_skills(
        _runtime(
            available_tool_names=["literature_search"],
            allowed_action_types=["retrieve_literature"],
            remaining_edge_budget=None,
        )
    )
    pubmed = registry.available_skills(
        _runtime(
            artifact_types=["kg_evidence"],
            available_tool_names=["literature_search"],
            allowed_action_types=["retrieve_literature"],
            remaining_edge_budget=None,
        )
    )

    assert [item.skill_id for item in ctd] == ["ctd-direct-evidence-lookup"]
    assert pubmed_missing_evidence == []
    assert [item.skill_id for item in pubmed] == ["pubmed-evidence-search"]


def test_ctd_and_pubmed_skills_compile_to_typed_actions() -> None:
    registry = build_default_skill_registry()
    ctd_invocation = registry.prepare_invocation(
        SkillSelection(
            skill_id="ctd-direct-evidence-lookup",
            arguments={
                "chemical_entity_id": "primekg:exposure:D001241",
                "disease_name": "Headache",
                "max_results": 5,
            },
        ),
        runtime=_runtime(
            seed_entity_type="exposure",
            available_tool_names=["ctd_lookup"],
            allowed_action_types=["query_ctd"],
            remaining_edge_budget=None,
        ),
        episode_id="episode-1",
        turn=1,
        state_digest="state-digest",
    )
    pubmed_invocation = registry.prepare_invocation(
        SkillSelection(
            skill_id="pubmed-evidence-search",
            arguments={
                "query": "aspirin AND headache",
                "evidence_need_id": "need-1",
                "max_results": 5,
            },
        ),
        runtime=_runtime(
            artifact_types=["kg_evidence"],
            available_tool_names=["literature_search"],
            allowed_action_types=["retrieve_literature"],
            remaining_edge_budget=None,
        ),
        episode_id="episode-1",
        turn=2,
        state_digest="state-digest-2",
    )

    assert isinstance(ctd_invocation.action, CtdAction)
    assert ctd_invocation.action.chemical_entity_id == (
        "primekg:exposure:D001241"
    )
    assert isinstance(pubmed_invocation.action, LiteratureAction)
    assert pubmed_invocation.action.evidence_need_id == "need-1"


def test_prior_skill_artifacts_can_unlock_a_followup_skill() -> None:
    registry = SkillRegistry()
    registry.register(
        SkillDefinition(
            skill_id="evidence-guided-expansion",
            version="evidence-guided-expansion-v1",
            summary="Expand an entity only after evidence was acquired.",
            action_type=ExplorationActionType.EXPAND_ENTITY,
            required_tool_names=["kg_neighbors"],
            required_artifact_types=["kg_evidence"],
            produced_artifact_types=["kg_evidence"],
            input_schema=ExpandAction.model_json_schema(),
            cost=SkillCostEstimate(tool_calls=1),
        ),
        lambda arguments: ExpandAction.model_validate(
            {**arguments, "action_type": "expand_entity"}
        ),
    )

    assert registry.available_skills(_runtime(artifact_types=[])) == []
    available = registry.available_skills(
        _runtime(artifact_types=["kg_evidence"])
    )

    assert [skill.skill_id for skill in available] == [
        "evidence-guided-expansion"
    ]


def test_skill_selection_compiles_to_existing_typed_action() -> None:
    registry = build_default_skill_registry()

    invocation = registry.prepare_invocation(
        SkillSelection(
            skill_id="primekg-neighbor-exploration",
            arguments={
                "subject_entity_id": "disease:1",
                "direction": "outgoing",
                "relations": ["disease_protein"],
                "target_types": ["gene/protein"],
                "max_results": 25,
            },
        ),
        runtime=_runtime(),
        episode_id="episode-1",
        turn=2,
        state_digest="state-digest",
    )

    assert invocation.skill_id == "primekg-neighbor-exploration"
    assert invocation.action.action_type == "query_neighbors"
    assert invocation.action.subject_entity_id == "disease:1"
    assert invocation.action.max_results == 25
    assert invocation.invocation_id.startswith("skill-invocation-")


def test_unavailable_skill_cannot_be_prepared() -> None:
    registry = build_default_skill_registry()

    with pytest.raises(SkillUnavailableError, match="tool_budget_exhausted"):
        registry.prepare_invocation(
            SkillSelection(
                skill_id="primekg-neighbor-exploration",
                arguments={
                    "subject_entity_id": "disease:1",
                    "direction": "outgoing",
                },
            ),
            runtime=_runtime(remaining_tool_calls=0),
            episode_id="episode-1",
            turn=1,
            state_digest="state-digest",
        )


def test_agent_decision_accepts_skill_selection_instead_of_raw_action() -> None:
    decision = AgentDecision(
        decision="continue_search",
        state_digest="state-digest",
        selected_skill_id="primekg-neighbor-exploration",
        skill_arguments={
            "subject_entity_id": "disease:1",
            "direction": "outgoing",
        },
    )

    assert decision.next_action is None
    assert decision.selected_skill_id == "primekg-neighbor-exploration"


def test_agent_decision_rejects_raw_action_and_skill_together() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        AgentDecision(
            decision="continue_search",
            state_digest="state-digest",
            next_action={
                "action_type": "query_neighbors",
                "subject_entity_id": "disease:1",
                "direction": "outgoing",
            },
            selected_skill_id="primekg-neighbor-exploration",
            skill_arguments={
                "subject_entity_id": "disease:1",
                "direction": "outgoing",
            },
        )


def test_successful_skill_outcome_requires_auditable_gain() -> None:
    with pytest.raises(ValueError, match="positive evidence gain"):
        SkillOutcome(
            invocation_id="skill-invocation-1",
            episode_id="episode-1",
            status="success",
            summary="nothing found",
        )

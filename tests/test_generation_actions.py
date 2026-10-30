"""The proposal -> decision -> sealed action -> execution chain.

Step 5 of the real-generation-agent plan. The properties under test are the
ones that keep a model from acting on its own: a model proposes, the server
decides, and only a sealed action may reach a tool.
"""
from __future__ import annotations

from typing import Any

import pytest

from medical_qa_data_factory.generation.actions import (
    ModelActionProposal,
    PolicyGate,
    PolicyRejectionCode,
    RunPolicy,
    canonical_action_hash,
)
from medical_qa_data_factory.generation.executor import Executor, ToolCallError


KNOWN = {"drug:aspirin", "disease:headache", "exposure:D001241"}


def _proposal(**action_overrides: Any) -> ModelActionProposal:
    action: dict[str, Any] = {
        "action_type": "query_neighbors",
        "subject_entity_id": "drug:aspirin",
        "direction": "outgoing",
        "max_results": 10,
    }
    action.update(action_overrides)
    return ModelActionProposal(
        action=action,
        expected_information="find the diseases aspirin treats",
        evidence_gap="the seed's indication set is unknown",
    )


def _gate(**policy_overrides: Any) -> PolicyGate:
    payload: dict[str, Any] = {
        "has_evidence": True,
        "available_tool_names": ("kg_neighbors", "literature_search", "none"),
    }
    payload.update(policy_overrides)
    return PolicyGate(RunPolicy(**payload))


# ---- a valid proposal ----------------------------------------------------


def test_a_valid_proposal_is_sealed() -> None:
    sealed, rejection = _gate().evaluate(
        _proposal(),
        run_id="run1",
        episode_id="ep1",
        turn=1,
        known_entity_ids=KNOWN,
    )
    assert rejection is None
    assert sealed is not None
    assert sealed.tool_name == "kg_neighbors"
    assert sealed.request["subject_entity_id"] == "drug:aspirin"
    assert sealed.request["max_results"] == 10
    assert sealed.request_digest


def test_the_sealed_action_carries_a_stable_hash() -> None:
    sealed, _ = _gate().evaluate(
        _proposal(),
        run_id="run1",
        episode_id="ep1",
        turn=1,
        known_entity_ids=KNOWN,
    )
    assert sealed is not None
    assert sealed.canonical_action_hash == canonical_action_hash(sealed_action_of(sealed))
    assert sealed.decision_id
    assert sealed.sealed_action_id


def sealed_action_of(sealed: Any) -> Any:
    from medical_qa_data_factory.generation.contracts import validate_action

    return validate_action(
        {
            "action_type": "query_neighbors",
            "subject_entity_id": "drug:aspirin",
            "direction": "outgoing",
            "max_results": 10,
        }
    )


def test_a_proposal_cannot_carry_server_assigned_fields() -> None:
    """The model may not supply a decision id, a turn or an action hash."""
    with pytest.raises(ValueError):
        ModelActionProposal(
            action={
                "action_type": "query_neighbors",
                "subject_entity_id": "drug:aspirin",
                "direction": "outgoing",
                "max_results": 10,
            },
            expected_information="x",
            evidence_gap="y",
            decision_id="forged",
        )


def test_the_server_assigns_every_identity() -> None:
    """The sealed action's ids come from the server, never from the model."""
    sealed, rejection = _gate().evaluate(
        _proposal(),
        run_id="run1",
        episode_id="ep1",
        turn=3,
        known_entity_ids=KNOWN,
    )
    assert rejection is None
    assert sealed is not None
    assert sealed.decision_id
    assert sealed.sealed_action_id
    assert sealed.canonical_action_hash
    # The decision and the turn are recorded on the server side.
    assert sealed.decision_id != "run1"


# ---- refusals ------------------------------------------------------------


def test_an_unknown_entity_is_refused() -> None:
    sealed, rejection = _gate().evaluate(
        _proposal(subject_entity_id="drug:not_known"),
        run_id="run1",
        episode_id="ep1",
        turn=1,
        known_entity_ids=KNOWN,
    )
    assert sealed is None
    assert rejection is not None
    assert rejection.code is PolicyRejectionCode.UNKNOWN_ENTITY


def test_a_malformed_action_is_refused() -> None:
    proposal = ModelActionProposal(
        action={"action_type": "not_a_real_action"},
        expected_information="x",
        evidence_gap="y",
    )
    sealed, rejection = _gate().evaluate(
        proposal, run_id="run1", episode_id="ep1", turn=1, known_entity_ids=KNOWN
    )
    assert sealed is None
    assert rejection is not None
    assert rejection.code is PolicyRejectionCode.MALFORMED_PROPOSAL


def test_a_limit_above_the_policy_is_refused() -> None:
    sealed, rejection = _gate(max_results_per_query=10).evaluate(
        _proposal(max_results=50),
        run_id="run1",
        episode_id="ep1",
        turn=1,
        known_entity_ids=KNOWN,
    )
    assert sealed is None
    assert rejection is not None
    assert rejection.code is PolicyRejectionCode.LIMIT_TOO_LARGE


def test_a_request_above_the_remaining_edge_budget_is_refused() -> None:
    sealed, rejection = _gate(remaining_edge_budget=5).evaluate(
        _proposal(max_results=10),
        run_id="run1",
        episode_id="ep1",
        turn=1,
        known_entity_ids=KNOWN,
    )
    assert sealed is None
    assert rejection is not None
    assert rejection.code == PolicyRejectionCode.BUDGET_EXHAUSTED
    assert "remaining edge budget 5" in rejection.message


def test_a_hop_count_above_the_policy_is_refused() -> None:
    proposal = ModelActionProposal(
        action={
            "action_type": "query_paths",
            "start_entity_id": "drug:aspirin",
            "max_hops": 3,
            "max_paths": 5,
        },
        expected_information="x",
        evidence_gap="y",
    )
    sealed, rejection = _gate(max_hops=2).evaluate(
        proposal, run_id="run1", episode_id="ep1", turn=1, known_entity_ids=KNOWN
    )
    assert sealed is None
    assert rejection is not None
    assert rejection.code is PolicyRejectionCode.HOP_BUDGET_EXCEEDED


def test_a_disallowed_relation_is_refused() -> None:
    sealed, rejection = _gate(allowed_relations=("treats",)).evaluate(
        _proposal(relations=["contraindication"]),
        run_id="run1",
        episode_id="ep1",
        turn=1,
        known_entity_ids=KNOWN,
    )
    assert sealed is None
    assert rejection is not None
    assert rejection.code is PolicyRejectionCode.RELATION_NOT_ALLOWED


def test_a_disallowed_direction_is_refused() -> None:
    sealed, rejection = _gate(allowed_directions=("outgoing",)).evaluate(
        _proposal(direction="both"),
        run_id="run1",
        episode_id="ep1",
        turn=1,
        known_entity_ids=KNOWN,
    )
    assert sealed is None
    assert rejection is not None
    assert rejection.code is PolicyRejectionCode.DIRECTION_NOT_ALLOWED


def test_a_citation_of_an_unknown_observation_is_refused() -> None:
    proposal = _proposal()
    proposal = proposal.model_copy(update={"cited_observation_ids": ["obs-forged"]})
    sealed, rejection = _gate().evaluate(
        proposal,
        run_id="run1",
        episode_id="ep1",
        turn=1,
        known_entity_ids=KNOWN,
        known_observation_ids={"obs-real"},
    )
    assert sealed is None
    assert rejection is not None
    assert rejection.code is PolicyRejectionCode.UNKNOWN_OBSERVATION


def test_a_repeated_action_is_refused() -> None:
    gate = _gate(executed_action_hashes=frozenset({_hash_of()}))
    sealed, rejection = gate.evaluate(
        _proposal(),
        run_id="run1",
        episode_id="ep1",
        turn=2,
        known_entity_ids=KNOWN,
    )
    assert sealed is None
    assert rejection is not None
    assert rejection.code is PolicyRejectionCode.DUPLICATE_ACTION


def _hash_of() -> str:
    from medical_qa_data_factory.generation.contracts import validate_action

    return canonical_action_hash(
        validate_action(
            {
                "action_type": "query_neighbors",
                "subject_entity_id": "drug:aspirin",
                "direction": "outgoing",
                "max_results": 10,
            }
        )
    )


def test_an_exhausted_tool_budget_is_refused() -> None:
    sealed, rejection = _gate(remaining_tool_calls=0).evaluate(
        _proposal(),
        run_id="run1",
        episode_id="ep1",
        turn=1,
        known_entity_ids=KNOWN,
    )
    assert sealed is None
    assert rejection is not None
    assert rejection.code is PolicyRejectionCode.BUDGET_EXHAUSTED


def test_finishing_before_any_evidence_is_refused() -> None:
    proposal = ModelActionProposal(
        action={"action_type": "finish", "stop_reason": "evidence_sufficient"},
        expected_information="x",
        evidence_gap="y",
    )
    sealed, rejection = _gate(has_evidence=False).evaluate(
        proposal, run_id="run1", episode_id="ep1", turn=1, known_entity_ids=KNOWN
    )
    assert sealed is None
    assert rejection is not None
    assert rejection.code is PolicyRejectionCode.FINISH_WITHOUT_EVIDENCE


def test_finishing_after_evidence_is_allowed() -> None:
    proposal = ModelActionProposal(
        action={"action_type": "finish", "stop_reason": "evidence_sufficient"},
        expected_information="x",
        evidence_gap="y",
    )
    sealed, rejection = _gate(has_evidence=True).evaluate(
        proposal, run_id="run1", episode_id="ep1", turn=1, known_entity_ids=KNOWN
    )
    assert rejection is None
    assert sealed is not None
    assert sealed.action_type.value == "finish"


def test_finish_is_refused_below_the_generation_spec_minimums() -> None:
    proposal = ModelActionProposal(
        action={"action_type": "finish", "stop_reason": "evidence_sufficient"},
        expected_information="x",
        evidence_gap="y",
    )
    sealed, rejection = _gate(
        has_evidence=True,
        kg_evidence_count=1,
        min_total_evidence=2,
        min_kg_evidence=1,
    ).evaluate(
        proposal, run_id="run1", episode_id="ep1", turn=1, known_entity_ids=KNOWN
    )
    assert sealed is None
    assert rejection is not None
    assert rejection.code is PolicyRejectionCode.FINISH_WITHOUT_EVIDENCE


def test_an_unavailable_tool_is_refused() -> None:
    sealed, rejection = _gate(available_tool_names=("literature_search",)).evaluate(
        _proposal(),
        run_id="run1",
        episode_id="ep1",
        turn=1,
        known_entity_ids=KNOWN,
    )
    assert sealed is None
    assert rejection is not None
    assert rejection.code is PolicyRejectionCode.CAPABILITY_UNAVAILABLE


# ---- literature actions --------------------------------------------------


def test_a_literature_action_must_serve_an_open_need() -> None:
    proposal = ModelActionProposal(
        action={
            "action_type": "retrieve_literature",
            "query": "aspirin headache",
            "entity_ids": ["drug:aspirin"],
            "max_results": 5,
        },
        expected_information="x",
        evidence_gap="y",
    )
    sealed, rejection = _gate(open_need_ids=("need.1",)).evaluate(
        proposal, run_id="run1", episode_id="ep1", turn=1, known_entity_ids=KNOWN
    )
    assert sealed is None
    assert rejection is not None
    assert rejection.code is PolicyRejectionCode.NEED_NOT_FOUND


def test_a_literature_action_for_an_open_need_is_sealed() -> None:
    proposal = ModelActionProposal(
        action={
            "action_type": "retrieve_literature",
            "query": "aspirin headache",
            "entity_ids": ["need.1"],
            "max_results": 5,
        },
        expected_information="x",
        evidence_gap="y",
    )
    sealed, rejection = _gate(open_need_ids=("need.1",)).evaluate(
        proposal,
        run_id="run1",
        episode_id="ep1",
        turn=1,
        known_entity_ids={"need.1"},
    )
    assert rejection is None
    assert sealed is not None
    assert sealed.tool_name == "literature_search"
    assert sealed.request["need_ids"] == ["need.1"]


def test_the_literature_limit_is_enforced() -> None:
    proposal = ModelActionProposal(
        action={
            "action_type": "retrieve_literature",
            "query": "q",
            "entity_ids": ["need.1"],
            "max_results": 50,
        },
        expected_information="x",
        evidence_gap="y",
    )
    sealed, rejection = _gate(
        open_need_ids=("need.1",), max_literature_results=10
    ).evaluate(
        proposal,
        run_id="run1",
        episode_id="ep1",
        turn=1,
        known_entity_ids={"need.1"},
    )
    assert sealed is None
    assert rejection is not None
    assert rejection.code is PolicyRejectionCode.LIMIT_TOO_LARGE


# ---- CTD actions ---------------------------------------------------------


def _ctd_proposal(**overrides: Any) -> ModelActionProposal:
    action: dict[str, Any] = {
        "action_type": "query_ctd",
        "chemical_entity_id": "exposure:D001241",
        "max_results": 10,
    }
    action.update(overrides)
    return ModelActionProposal(
        action=action,
        expected_information="direct CTD disease evidence for aspirin",
        evidence_gap="the CTD relation set is unknown",
    )


def test_a_ctd_lookup_is_sealed_for_a_known_chemical() -> None:
    sealed, rejection = _gate(
        available_tool_names=("ctd_lookup",),
        allowed_action_types=("query_ctd",),
    ).evaluate(
        _ctd_proposal(),
        run_id="run1",
        episode_id="ep1",
        turn=1,
        known_entity_ids=KNOWN,
    )

    assert rejection is None
    assert sealed is not None
    assert sealed.tool_name == "ctd_lookup"
    assert sealed.request == {
        "operation": "query_ctd",
        "chemical_entity_id": "exposure:D001241",
        "disease_name": None,
        "direct_evidence": [],
        "max_results": 10,
    }


def test_a_ctd_lookup_for_an_unknown_chemical_is_refused() -> None:
    sealed, rejection = _gate(
        available_tool_names=("ctd_lookup",),
        allowed_action_types=("query_ctd",),
    ).evaluate(
        _ctd_proposal(chemical_entity_id="drug:unknown"),
        run_id="run1",
        episode_id="ep1",
        turn=1,
        known_entity_ids=KNOWN,
    )

    assert sealed is None
    assert rejection is not None
    assert rejection.code is PolicyRejectionCode.UNKNOWN_ENTITY


def test_a_ctd_lookup_rejects_a_known_non_exposure_entity() -> None:
    sealed, rejection = _gate(
        available_tool_names=("ctd_lookup",),
        allowed_action_types=("query_ctd",),
    ).evaluate(
        _ctd_proposal(chemical_entity_id="disease:headache"),
        run_id="run1",
        episode_id="ep1",
        turn=1,
        known_entity_ids=KNOWN,
    )

    assert sealed is None
    assert rejection is not None
    assert rejection.code is PolicyRejectionCode.TARGET_TYPE_NOT_ALLOWED


# ---- the executor --------------------------------------------------------


class _Edge:
    def __init__(self, head: str, tail: str):
        self.head = type("E", (), {"canonical_id": head})()
        self.tail = type("E", (), {"canonical_id": tail})()


class _KG:
    def __init__(self, edges: list[_Edge] | None = None):
        self.edges = edges if edges is not None else [_Edge("drug:aspirin", "disease:headache")]
        self.calls: list[dict[str, Any]] = []

    def neighbors(self, request: dict[str, Any]):
        self.calls.append(request)
        return self.edges, "trace-1"


class _BrokenKG:
    def neighbors(self, request: dict[str, Any]):
        raise ToolCallError("kg_unavailable", "the provider is down", retryable=True)


class _CtdRecord:
    source_record_locator = "ctd-record:D001241:MESH:D006261"
    content_hash = "ctd-content-hash"


class _CtdArtifact:
    evidence_id = "ctd-evidence-versioned-1"
    record = _CtdRecord()

    def planner_summary(self):
        return {
            "artifact_id": self.evidence_id,
            "disease_name": "Headache",
            "direct_evidence": ["marker/mechanism"],
        }


class _CTD:
    def __init__(self, records: list[Any] | None = None):
        self.records = records if records is not None else [_CtdArtifact()]
        self.calls: list[dict[str, Any]] = []

    def lookup(self, request: dict[str, Any]):
        self.calls.append(request)
        return self.records, "ctd-trace-1", "ctd-snapshot-1"


def _sealed_action(gate: PolicyGate) -> Any:
    sealed, _ = gate.evaluate(
        _proposal(),
        run_id="run1",
        episode_id="ep1",
        turn=1,
        known_entity_ids=KNOWN,
    )
    assert sealed is not None
    return sealed


def test_the_executor_runs_a_sealed_action() -> None:
    kg = _KG()
    executor = Executor(kg=kg)
    sealed = _sealed_action(_gate())
    outcome = executor.execute(sealed, episode_id="ep1", run_id="run1")
    assert outcome.succeeded
    assert len(kg.calls) == 1
    assert kg.calls[0]["subject_entity_id"] == "drug:aspirin"
    assert outcome.discovered_entity_ids == ["drug:aspirin", "disease:headache"]


def test_the_executor_records_an_observation() -> None:
    executor = Executor(kg=_KG())
    sealed = _sealed_action(_gate())
    outcome = executor.execute(sealed, episode_id="ep1", run_id="run1")
    observation = outcome.observation
    assert observation.decision_id == sealed.decision_id
    assert observation.action_type == "query_neighbors"
    assert observation.status == "empty"
    assert observation.row_count == 1


def test_a_tool_failure_is_recorded_as_an_error_observation() -> None:
    executor = Executor(kg=_BrokenKG())
    sealed = _sealed_action(_gate())
    outcome = executor.execute(sealed, episode_id="ep1", run_id="run1")
    assert not outcome.succeeded
    assert outcome.error_code == "kg_unavailable"
    assert outcome.observation.status == "error"
    assert outcome.observation.retryable is True


def test_the_executor_without_a_provider_fails_loudly() -> None:
    executor = Executor(kg=None)
    sealed = _sealed_action(_gate())
    outcome = executor.execute(sealed, episode_id="ep1", run_id="run1")
    assert outcome.error_code == "kg_unavailable"


def test_the_executor_remembers_what_it_ran() -> None:
    executor = Executor(kg=_KG())
    sealed = _sealed_action(_gate())
    executor.execute(sealed, episode_id="ep1", run_id="run1")
    assert executor.executed_action_hashes == [sealed.canonical_action_hash]


def test_the_executor_records_the_policy_budget_consumption() -> None:
    policy = RunPolicy(has_evidence=True, remaining_tool_calls=2)
    updated = policy.with_execution("hash-1")
    assert updated.remaining_tool_calls == 1
    assert "hash-1" in updated.executed_action_hashes
    assert updated.has_evidence is True


def test_the_executor_runs_ctd_and_records_stable_artifacts() -> None:
    gate = _gate(
        available_tool_names=("ctd_lookup",),
        allowed_action_types=("query_ctd",),
    )
    sealed, rejection = gate.evaluate(
        _ctd_proposal(disease_name="Headache", max_results=5),
        run_id="run1",
        episode_id="ep1",
        turn=1,
        known_entity_ids=KNOWN,
    )
    assert rejection is None
    assert sealed is not None
    ctd = _CTD()

    outcome = Executor(ctd=ctd).execute(
        sealed, episode_id="ep1", run_id="run1"
    )

    assert ctd.calls[0]["disease_name"] == "Headache"
    assert outcome.new_evidence_ids == ["ctd-evidence-versioned-1"]
    assert outcome.observation.status == "success"
    assert outcome.observation.provider_trace_id == "ctd-trace-1"
    assert outcome.observation.provider_snapshot_id == "ctd-snapshot-1"
    assert outcome.observation.artifact_summaries[0]["disease_name"] == (
        "Headache"
    )


def test_an_empty_ctd_lookup_is_recorded_as_empty() -> None:
    sealed, _ = _gate(
        available_tool_names=("ctd_lookup",),
        allowed_action_types=("query_ctd",),
    ).evaluate(
        _ctd_proposal(),
        run_id="run1",
        episode_id="ep1",
        turn=1,
        known_entity_ids=KNOWN,
    )
    assert sealed is not None

    outcome = Executor(ctd=_CTD(records=[])).execute(
        sealed, episode_id="ep1", run_id="run1"
    )

    assert outcome.new_evidence_ids == []
    assert outcome.observation.status == "empty"

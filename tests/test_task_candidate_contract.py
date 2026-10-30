"""Candidate contracts reject structures the compiler cannot deliver."""
from __future__ import annotations

import pytest

from medical_qa_data_factory.generation.candidate_assessment import (
    GraphTaskCandidateAssessor,
)
from medical_qa_data_factory.generation.task_compiler import (
    TaskCompilationError,
    TaskCompiler,
)
from medical_qa_data_factory.generation.task_contracts import (
    ProviderSnapshotRef,
    TaskCandidate,
    ValidationEnvironment,
)


def _candidate(**overrides):
    payload = {
        "candidate_id": "candidate-rgs20",
        "seed_entity_id": "primekg:gene/protein:8601",
        "question_type": "constrained_entity",
        "steps": [
            {
                "position": 0,
                "relation": "anatomy_protein_present",
                "direction": "forward",
                "target_entity_type": "anatomy",
                "from_entity_id": "primekg:gene/protein:8601",
            }
        ],
        "constraints": [
            {
                "constraint_id": "independent",
                "steps": [
                    {
                        "position": 0,
                        "relation": "expressed_in",
                        "direction": "forward",
                        "target_entity_type": "tissue",
                        "to_entity_id": "primekg:anatomy:brain",
                    }
                ],
                "terminal_entity_id": "primekg:anatomy:brain",
            }
        ],
        "proposed_answer_ids": ["primekg:anatomy:1954"],
        "private_answer_names": ["Ammon's horn"],
        "answer_entity_type": "anatomy",
        "snapshot_id": "snapshot-1",
        "evidence_ids": ["kg-1"],
        "discovery_source": "PrimeKG exploration",
        "generator_version": "entity-question-explorer-v3",
    }
    payload.update(overrides)
    return payload


def test_candidate_requires_exactly_one_proposed_answer() -> None:
    candidate = TaskCandidate.model_validate(
        _candidate(
            proposed_answer_ids=[
                "primekg:anatomy:1954",
                "primekg:anatomy:955",
            ],
            private_answer_names=["Ammon's horn", "brain"],
        )
    )

    with pytest.raises(
        TaskCompilationError,
        match="single-answer candidate must propose exactly one entity",
    ):
        TaskCompiler().compile(candidate, _environment())


def test_candidate_constraint_cannot_repeat_the_primary_edge() -> None:
    candidate = TaskCandidate.model_validate(
        _candidate(
            constraints=[
                {
                    "constraint_id": "duplicate-primary",
                    "steps": [
                        {
                            "position": 0,
                            "relation": "anatomy_protein_present",
                            "direction": "reverse",
                            "target_entity_type": "gene/protein",
                            "to_entity_id": "primekg:gene/protein:8601",
                        }
                    ],
                    "terminal_entity_id": "primekg:gene/protein:8601",
                }
            ]
        )
    )

    with pytest.raises(
        TaskCompilationError,
        match="constraint must add independent information",
    ):
        TaskCompiler().compile(candidate, _environment())


def test_historical_candidates_remain_readable_after_rule_changes() -> None:
    candidate = TaskCandidate.model_validate(
        _candidate(
            proposed_answer_ids=[
                "primekg:anatomy:1954",
                "primekg:anatomy:955",
            ],
            private_answer_names=["Ammon's horn", "brain"],
        )
    )

    assert len(candidate.proposed_answer_ids) == 2


def test_terminal_compilation_feedback_keeps_the_specific_error() -> None:
    class RejectingCompiler:
        def compile(self, candidate, environment):
            raise TaskCompilationError("candidate must use one answer")

    assessor = GraphTaskCandidateAssessor(
        None,
        environment=_environment(),
        compiler=RejectingCompiler(),
    )

    feedback = assessor._assess(TaskCandidate.model_validate(_candidate()))

    assert feedback.reason_codes == ["candidate_not_compilable"]
    assert feedback.message == (
        "TaskCompilationError: candidate must use one answer"
    )


def _environment() -> ValidationEnvironment:
    return ValidationEnvironment(
        environment_id="test-world",
        providers=[
            ProviderSnapshotRef(
                provider_id="primekg",
                snapshot_id="snapshot-1",
                manifest_hash="hash",
                completeness="full_snapshot_query",
            )
        ],
        completeness_scope="test",
    )

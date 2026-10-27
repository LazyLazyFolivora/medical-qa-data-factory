from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from medical_qa_data_factory.generation.logical_query import (
    LogicalDirection,
    LogicalQuery,
    QueryStep,
)
from medical_qa_data_factory.generation.task_contracts import (
    AcceptableEvidenceSet,
    AnswerContract,
    ClaimPattern,
    EvaluationTarget,
    InformationRequirement,
    PlannerQuestionInput,
    ProviderSnapshotRef,
    TaskSpec,
    ValidationEnvironment,
    ValidationReport,
    promote_validated_task,
)


def _query() -> LogicalQuery:
    created_at = datetime.now(timezone.utc)
    query = LogicalQuery(
        query_id="query-1",
        path_id="path-1",
        path_version="path-v1",
        probe_id="probe-1",
        probe_digest="probe-digest",
        start_entity_id="primekg:drug:D1",
        steps=[
            QueryStep(
                position=0,
                relation="targets",
                direction=LogicalDirection.FORWARD,
                target_entity_type="gene/protein",
            )
        ],
        answer_entity_type="gene/protein",
        expected_cardinality="one",
        snapshot_id="snapshot-1",
        dependency_digest="pending",
        created_at=created_at,
    )
    return query.model_copy(update={"dependency_digest": query.digest()})


def _spec() -> TaskSpec:
    query = _query()
    requirement = InformationRequirement(
        requirement_id="req-1",
        description="confirm the drug-target relation",
        claim_pattern=ClaimPattern(
            subject_id="primekg:drug:D1",
            predicate_family="targets",
            object_id="primekg:gene/protein:G1",
        ),
    )
    return TaskSpec(
        task_id="task-1",
        candidate_id="candidate-1",
        seed_entity_id="primekg:drug:D1",
        question_type="constrained_entity",
        query=query,
        proposed_answer_contract=AnswerContract(
            answer_type="entity",
            cardinality="exactly_one",
            canonical_answer_ids=["primekg:gene/protein:G1"],
            accepted_aliases=["Protein G1", "G1"],
            scope_statement="PrimeKG snapshot snapshot-1",
        ),
        information_requirements=[requirement],
        acceptable_evidence_sets=[
            AcceptableEvidenceSet(all_of=["req-1"])
        ],
        validation_environment=ValidationEnvironment(
            environment_id="world-1",
            providers=[
                ProviderSnapshotRef(
                    provider_id="primekg",
                    snapshot_id="snapshot-1",
                    manifest_hash="sha256:abc",
                    completeness="full_snapshot_query",
                )
            ],
            completeness_scope="all matching edges in the frozen snapshot",
        ),
    )


def _report(*, passed: bool, answers: list[str]) -> ValidationReport:
    spec = _spec()
    return ValidationReport(
        report_id="report-1",
        task_id=spec.task_id,
        query_id=spec.query.query_id,
        query_digest=spec.query.dependency_digest,
        environment_id=spec.validation_environment.environment_id,
        passed=passed,
        reason_codes=[] if passed else ["world_cardinality_mismatch"],
        world_answer_ids=answers,
        result_complete=True,
        validator_version="task-validator-v1",
    )


def test_a_failed_validation_report_cannot_promote_a_task():
    with pytest.raises(ValueError, match="did not pass"):
        promote_validated_task(
            _spec(),
            _report(
                passed=False,
                answers=[
                    "primekg:gene/protein:G1",
                    "primekg:gene/protein:G2",
                ],
            ),
        )


def test_promotion_uses_world_answers_instead_of_candidate_answers():
    validated = promote_validated_task(
        _spec(),
        _report(passed=True, answers=["primekg:gene/protein:G9"]),
    )

    assert validated.answer_contract.canonical_answer_ids == [
        "primekg:gene/protein:G9"
    ]
    assert validated.validation_report_id == "report-1"


def test_validation_report_must_match_the_task_query_and_world():
    report = _report(passed=True, answers=["primekg:gene/protein:G1"])

    with pytest.raises(ValueError, match="query digest"):
        promote_validated_task(
            _spec(), report.model_copy(update={"query_digest": "other"})
        )


def test_planner_question_input_has_no_gold_fields():
    planner = PlannerQuestionInput(
        qid="question-1",
        question="药物 D1 在给定约束下对应的靶点是哪一种？",
        qtype="constrained_entity",
        budget={"relation_access": 8, "tool_calls": 12},
        max_steps=8,
        sufficiency_threshold=1.0,
        environment_ref="world-1",
    )

    payload = planner.model_dump(mode="json")
    assert "answer_contract" not in payload
    assert "information_requirements" not in payload
    assert "gold_claim_ids" not in payload
    assert "gold_evidence_ids" not in payload
    assert "completion_rule" not in payload
    assert "evidence_catalog_ref" not in payload

    with pytest.raises(ValidationError):
        PlannerQuestionInput.model_validate(
            {**payload, "answer_contract": {"canonical_answer_ids": ["G1"]}}
        )


def test_evaluation_target_keeps_hidden_truth_outside_the_planner_view():
    validated = promote_validated_task(
        _spec(),
        _report(passed=True, answers=["primekg:gene/protein:G1"]),
    )

    target = EvaluationTarget.from_validated_task("question-1", validated)

    assert target.answer_contract.canonical_answer_ids == [
        "primekg:gene/protein:G1"
    ]
    assert target.information_requirements[0].requirement_id == "req-1"
    assert target.validation_report_ref == "report-1"

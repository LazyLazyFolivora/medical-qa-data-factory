from datetime import datetime, timezone

from medical_qa_data_factory.generation.logical_query import QueryAnswer
from medical_qa_data_factory.generation.logical_query import (
    LogicalDirection,
    QueryConstraint,
    QueryStep,
)
from medical_qa_data_factory.generation.task_contracts import (
    promote_validated_task,
)
from medical_qa_data_factory.generation.task_validation import TaskValidator

from test_generation_task_contracts import _spec


class StubEvaluator:
    def __init__(self, answer: QueryAnswer) -> None:
        self.answer_value = answer

    def answer(self, query):
        return self.answer_value


class ConstraintAwareEvaluator:
    def __init__(
        self,
        *,
        constrained_ids,
        unconstrained_ids,
        relaxed_query_id_matches=True,
    ) -> None:
        self.constrained_ids = constrained_ids
        self.unconstrained_ids = unconstrained_ids
        self.relaxed_query_id_matches = relaxed_query_id_matches

    def answer(self, query):
        ids = (
            self.constrained_ids
            if query.constraints
            else self.unconstrained_ids
        )
        answer = _answer(
            ids=ids,
            status="ambiguous" if len(ids) > 1 else "ok",
        )
        query_id = query.query_id
        if not query.constraints and not self.relaxed_query_id_matches:
            query_id = "query-other"
        return answer.model_copy(update={"query_id": query_id})


def _constrained_spec():
    spec = _spec()
    query = spec.query.model_copy(
        update={
            "constraints": [
                QueryConstraint(
                    constraint_id="constraint-disease",
                    steps=[
                        QueryStep(
                            position=0,
                            relation="associated_with",
                            direction=LogicalDirection.FORWARD,
                            target_entity_type="disease",
                        )
                    ],
                    terminal_entity_id="primekg:disease:C1",
                )
            ],
            "dependency_digest": "pending",
        }
    )
    query = query.model_copy(update={"dependency_digest": query.digest()})
    return spec.model_copy(update={"query": query})


def _answer(*, ids, status="ok", truncated=False, failure=None):
    return QueryAnswer(
        query_id="query-1",
        answer_ids=list(ids),
        status=status,
        truncated=truncated,
        failure_reason=failure,
        rows_examined=12,
        evaluated_at=datetime.now(timezone.utc),
    )


def test_validator_rejects_a_locally_unique_candidate_when_world_is_multi_answer():
    report = TaskValidator(
        StubEvaluator(
            _answer(
                ids=[
                    "primekg:gene/protein:G1",
                    "primekg:gene/protein:G2",
                ],
                status="ambiguous",
                failure="two answers",
            )
        )
    ).validate(
        _spec(), slice_answer_ids=["primekg:gene/protein:G1"]
    )

    assert report.passed is False
    assert "world_cardinality_mismatch" in report.reason_codes
    assert report.slice_answer_ids == ["primekg:gene/protein:G1"]
    assert report.world_answer_ids == [
        "primekg:gene/protein:G1",
        "primekg:gene/protein:G2",
    ]


def test_validator_rejects_incomplete_or_truncated_world_results():
    report = TaskValidator(
        StubEvaluator(
            _answer(
                ids=["primekg:gene/protein:G1"],
                status="incomplete",
                truncated=True,
                failure="page limit",
            )
        )
    ).validate(_spec())

    assert report.passed is False
    assert "answer_set_truncated" in report.reason_codes
    assert "incomplete_world_query" in report.reason_codes
    assert report.result_complete is False


def test_validator_fails_closed_for_an_unknown_evaluator_status():
    report = TaskValidator(
        StubEvaluator(
            _answer(ids=["primekg:gene/protein:G1"], status="failed")
        )
    ).validate(_spec())

    assert report.passed is False
    assert "invalid_evaluator_status" in report.reason_codes
    assert report.result_complete is False


def test_validator_rejects_a_candidate_answer_that_world_execution_disproves():
    report = TaskValidator(
        StubEvaluator(_answer(ids=["primekg:gene/protein:G9"]))
    ).validate(_spec())

    assert report.passed is False
    assert "candidate_answer_mismatch" in report.reason_codes


def test_validator_passes_one_complete_matching_world_answer():
    report = TaskValidator(
        StubEvaluator(_answer(ids=["primekg:gene/protein:G1"]))
    ).validate(_spec())

    assert report.passed is True
    assert report.reason_codes == []
    validated = promote_validated_task(_spec(), report)
    assert validated.answer_contract.canonical_answer_ids == [
        "primekg:gene/protein:G1"
    ]


def test_validator_rejects_a_constraint_that_does_not_reduce_the_answer_set():
    report = TaskValidator(
        ConstraintAwareEvaluator(
            constrained_ids=["primekg:gene/protein:G1"],
            unconstrained_ids=["primekg:gene/protein:G1"],
        )
    ).validate(
        _constrained_spec(),
        slice_answer_ids=["primekg:gene/protein:G1"],
    )

    assert report.passed is False
    assert "shortcut_detected" in report.reason_codes


def test_validator_accepts_a_constraint_that_resolves_real_ambiguity():
    report = TaskValidator(
        ConstraintAwareEvaluator(
            constrained_ids=["primekg:gene/protein:G1"],
            unconstrained_ids=[
                "primekg:gene/protein:G1",
                "primekg:gene/protein:G2",
            ],
        )
    ).validate(
        _constrained_spec(),
        slice_answer_ids=["primekg:gene/protein:G1"],
    )

    assert report.passed is True
    assert report.reason_codes == []


def test_validator_rejects_a_relaxed_answer_for_another_query():
    report = TaskValidator(
        ConstraintAwareEvaluator(
            constrained_ids=["primekg:gene/protein:G1"],
            unconstrained_ids=[
                "primekg:gene/protein:G1",
                "primekg:gene/protein:G2",
            ],
            relaxed_query_id_matches=False,
        )
    ).validate(
        _constrained_spec(),
        slice_answer_ids=["primekg:gene/protein:G1"],
    )

    assert report.passed is False
    assert "relaxed_evaluator_query_mismatch" in report.reason_codes


def test_validator_rejects_an_evaluator_response_for_another_query():
    wrong = _answer(ids=["primekg:gene/protein:G1"]).model_copy(
        update={"query_id": "query-other"}
    )
    report = TaskValidator(StubEvaluator(wrong)).validate(_spec())

    assert report.passed is False
    assert "evaluator_query_mismatch" in report.reason_codes


def test_validator_rejects_a_stale_query_digest():
    spec = _spec().model_copy(
        update={
            "query": _spec().query.model_copy(
                update={"dependency_digest": "stale"}
            )
        }
    )
    answer = _answer(ids=["primekg:gene/protein:G1"])
    report = TaskValidator(StubEvaluator(answer)).validate(spec)

    assert report.passed is False
    assert "query_digest_mismatch" in report.reason_codes


def test_report_identity_changes_when_the_validation_result_changes():
    accepted = TaskValidator(
        StubEvaluator(_answer(ids=["primekg:gene/protein:G1"]))
    ).validate(_spec())
    rejected = TaskValidator(
        StubEvaluator(
            _answer(
                ids=[
                    "primekg:gene/protein:G1",
                    "primekg:gene/protein:G2",
                ],
                status="ambiguous",
                failure="two answers",
            )
        )
    ).validate(_spec())

    assert accepted.report_id != rejected.report_id

from medical_qa_data_factory.generation.task_realization import (
    TaskQuestionWriter,
    TaskTextSemanticVerifier,
)
from medical_qa_data_factory.generation.task_contracts import (
    PlannerQuestionInput,
    promote_validated_task,
)
from medical_qa_data_factory.generation.logical_query import (
    LogicalDirection,
    QueryConstraint,
    QueryStep,
)

from test_generation_task_contracts import _report, _spec


def _validated():
    spec = _spec().model_copy(
        update={
            "public_entity_names": {
                "primekg:drug:D1": "药物D1",
                "primekg:disease:C1": "疾病C1",
            },
            "query": _spec().query.model_copy(
                update={
                    "constraints": [],
                }
            ),
        }
    )
    # Build a real constrained query for the writer.
    query = spec.query.model_copy(
        update={
            "constraints": [
                QueryConstraint(
                    constraint_id="constraint-1",
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
    spec = spec.model_copy(update={"query": query})
    report = _report(
        passed=True, answers=["primekg:gene/protein:G1"]
    ).model_copy(
        update={
            "query_digest": query.dependency_digest,
            "query_id": query.query_id,
        }
    )
    return promote_validated_task(spec, report)


def _validated_primekg_disease_pathway():
    validated = _validated()
    query = validated.task.query.model_copy(
        update={
            "start_entity_id": "primekg:disease:D1",
            "steps": [
                QueryStep(
                    position=0,
                    relation="disease_protein",
                    direction=LogicalDirection.FORWARD,
                    target_entity_type="gene/protein",
                )
            ],
            "constraints": [
                QueryConstraint(
                    constraint_id="constraint-pathway",
                    steps=[
                        QueryStep(
                            position=0,
                            relation="pathway_protein",
                            direction=LogicalDirection.REVERSE,
                            target_entity_type="pathway",
                        )
                    ],
                    terminal_entity_id="primekg:pathway:PW1",
                )
            ],
            "dependency_digest": "pending",
        }
    )
    query = query.model_copy(update={"dependency_digest": query.digest()})
    spec = validated.task.model_copy(
        update={
            "seed_entity_id": "primekg:disease:D1",
            "query": query,
            "public_entity_names": {
                "primekg:disease:D1": "乳腺癌",
                "primekg:pathway:PW1": "PI3K-Akt 信号通路",
            },
        }
    )
    report = _report(
        passed=True, answers=["primekg:gene/protein:G1"]
    ).model_copy(
        update={
            "task_id": spec.task_id,
            "candidate_id": spec.candidate_id,
            "query_digest": query.dependency_digest,
            "query_id": query.query_id,
            "environment_id": spec.validation_environment.environment_id,
        }
    )
    return promote_validated_task(spec, report)


def test_writer_realizes_a_validated_constraint_task_without_exposing_the_answer():
    validated = _validated()
    variant = TaskQuestionWriter().write(validated, qid="question-1")

    assert variant.question_text == "药物D1所靶向且与疾病C1相关的蛋白是哪一种？"
    assert "G1" not in variant.question_text

    report = TaskTextSemanticVerifier().verify(
        variant,
        validated,
    )
    assert report.passed is True
    assert report.reason_codes == []
    assert report.reconstructed_query_digest == validated.task.query.dependency_digest


def test_writer_realizes_the_reviewed_primekg_disease_pathway_relation_pair():
    validated = _validated_primekg_disease_pathway()

    variant = TaskQuestionWriter().write(validated, qid="question-primekg-1")
    report = TaskTextSemanticVerifier().verify(variant, validated)

    assert (
        variant.question_text
        == "与乳腺癌相关且参与PI3K-Akt 信号通路的蛋白是哪一种？"
    )
    assert report.passed is True
    assert report.reconstructed_query_digest == validated.task.query.dependency_digest


def test_verifier_rejects_a_question_that_drops_the_disambiguating_constraint():
    validated = _validated()
    variant = TaskQuestionWriter().write(validated, qid="question-1")
    weakened = variant.model_copy(
        update={"question_text": "药物D1靶向哪种蛋白？"}
    )

    report = TaskTextSemanticVerifier().verify(
        weakened,
        validated,
    )

    assert report.passed is False
    assert "constraint_missing" in report.reason_codes
    assert report.reconstructed_query_digest is None


def test_verifier_rejects_answer_leakage():
    validated = _validated()
    variant = TaskQuestionWriter().write(validated, qid="question-1")
    leaked = variant.model_copy(
        update={"question_text": "药物D1所靶向的是Protein G1吗？"}
    )

    report = TaskTextSemanticVerifier().verify(
        leaked,
        validated,
    )

    assert report.passed is False
    assert "answer_leaked" in report.reason_codes


def test_round_trip_parser_rejects_negated_relation_wording():
    validated = _validated()
    variant = TaskQuestionWriter().write(validated, qid="question-1")
    negated = variant.model_copy(
        update={
            "question_text": "药物D1所不靶向且与疾病C1相关的蛋白是哪一种？"
        }
    )

    report = TaskTextSemanticVerifier().verify(negated, validated)

    assert report.passed is False
    assert "round_trip_parse_failed" in report.reason_codes
    assert report.reconstructed_query_digest is None


def test_verified_variant_exports_only_the_public_planner_view():
    validated = _validated()
    variant = TaskQuestionWriter().write(validated, qid="question-1")
    semantic = TaskTextSemanticVerifier().verify(
        variant,
        validated,
    )

    planner = PlannerQuestionInput(
        qid=variant.qid,
        question=variant.question_text,
        qtype=validated.task.question_type,
        budget={"relation_access": 8},
        environment_ref=validated.task.validation_environment.environment_id,
    )

    assert semantic.passed is True
    assert "answer_contract" not in planner.model_dump()

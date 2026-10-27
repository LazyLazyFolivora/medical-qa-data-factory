from __future__ import annotations

import gzip
from pathlib import Path

from medical_qa_data_factory.evidence_enrichment.ctd import (
    CtdEvidenceIndex,
    build_ctd_index,
)
from medical_qa_data_factory.generation.ctd_world_evaluation import (
    CtdWorldEvaluator,
    StaticCtdDiseaseCategoryResolver,
    StaticCtdDiseaseIdentityResolver,
)
from medical_qa_data_factory.generation.task_candidates import (
    CtdStudyContextCandidateSource,
)
from medical_qa_data_factory.generation.task_compiler import TaskCompiler
from medical_qa_data_factory.generation.task_contracts import (
    AnswerCandidateOutcome,
    ProviderSnapshotRef,
    StudyContext,
    ValidationEnvironment,
)
from medical_qa_data_factory.generation.task_validation import (
    CtdTaskValidator,
)


SEED_ID = "primekg:exposure:D001241"


def _context(**updates) -> StudyContext:
    payload = {
        "population": "adults",
        "exposure_context": "aspirin exposure",
        "dose_or_regimen": "low dose",
        "exposure_duration": "at least two years",
        "study_type": "observational cohort",
        "sample_type": "serum",
        "disease_category": "digestive cancer",
        "temporal_scope": "long-term follow-up",
        "relation_intent": "risk_decrease",
        "polarity": "decrease",
    }
    payload.update(updates)
    return StudyContext(**payload)


def _write_rows(path: Path, rows: list[tuple[str, str]]) -> Path:
    source = path / "ctd.csv.gz"
    with gzip.open(source, "wt", encoding="utf-8", newline="") as handle:
        handle.write("# CTD test fixture\n")
        for name, disease_id in rows:
            handle.write(
                "\t".join(
                    (
                        "Aspirin",
                        "D001241",
                        "",
                        name,
                        disease_id,
                        "marker/mechanism",
                        "PTGS1",
                        "1.0",
                        "",
                        "123",
                    )
                )
                + "\n"
            )
    return source


def _index(tmp_path: Path) -> CtdEvidenceIndex:
    index_path = tmp_path / "ctd.sqlite"
    build_ctd_index(
        _write_rows(
            tmp_path,
            [("Cancer A", "MESH:A"), ("Cancer B", "MESH:B")],
        ),
        index_path,
    )
    return CtdEvidenceIndex(index_path)


def _outcome(answer_id: str, status: str) -> AnswerCandidateOutcome:
    if status == "matched":
        suffix = answer_id.rsplit(":", 1)[-1]
        return AnswerCandidateOutcome(
            answer_id=answer_id,
            status="matched",
            claim_ids=[f"claim-{suffix}"],
            evidence_ids=[f"ctd-{suffix}", f"pubmed-{suffix}"],
        )
    return AnswerCandidateOutcome(
        answer_id=answer_id,
        status=status,
        reason_codes=[f"{status}_reason"],
    )


class _ContextAssessor:
    def __init__(self, matcher) -> None:
        self.matcher = matcher

    def assess(self, *, record, context, answer_id):
        return _outcome(
            answer_id,
            self.matcher(record, context, answer_id),
        )


def _evaluator(
    index: CtdEvidenceIndex,
    assessor,
) -> CtdWorldEvaluator:
    return CtdWorldEvaluator(
        index=index,
        identity_resolver=StaticCtdDiseaseIdentityResolver(
            {
                "MESH:A": "primekg:disease:MESH:A",
                "MESH:B": "primekg:disease:MESH:B",
            }
        ),
        category_resolver=StaticCtdDiseaseCategoryResolver(
            {
                "primekg:disease:MESH:A": ["digestive cancer"],
                "primekg:disease:MESH:B": ["digestive cancer"],
            }
        ),
        assessor=assessor,
    )


def _environment(index: CtdEvidenceIndex) -> ValidationEnvironment:
    return ValidationEnvironment(
        environment_id="ctd-world-1",
        providers=[
            ProviderSnapshotRef(
                provider_id="ctd",
                snapshot_id=index.metadata()["snapshot_id"],
                completeness="full_chemical_disease_enumeration",
            ),
            ProviderSnapshotRef(
                provider_id="pubmed",
                snapshot_id="pubmed:test",
                completeness="fixed_read_back_corpus",
            ),
        ],
        completeness_scope="all CTD diseases for the chemical",
    )


def _proposal(index: CtdEvidenceIndex, assessor):
    evaluator = _evaluator(index, assessor)
    source = CtdStudyContextCandidateSource(evaluator)
    return source.propose(
        seed_entity_id=SEED_ID,
        chemical_id="D001241",
        snapshot_id=index.metadata()["snapshot_id"],
        context=_context(),
    )


def test_candidate_is_created_only_after_complete_single_answer_evaluation(
    tmp_path: Path,
) -> None:
    index = _index(tmp_path)
    assessor = _ContextAssessor(
        lambda _record, context, answer_id: (
            "matched"
            if context.population == "adults"
            and answer_id.endswith(":MESH:A")
            else "matched"
            if context.population != "adults"
            else "not_matched"
        )
    )

    result = _proposal(index, assessor)

    assert result.candidate is not None
    assert result.evaluation.result_complete is True
    assert result.candidate.proposed_answer_ids == [
        "primekg:disease:MESH:A"
    ]
    assert result.candidate.public_study_context == _context()
    assert any(item.discriminates for item in result.counterfactuals)


def test_multiple_world_answers_are_rejected(tmp_path: Path) -> None:
    index = _index(tmp_path)
    assessor = _ContextAssessor(
        lambda _record, _context, _answer_id: "matched"
    )

    result = _proposal(index, assessor)

    assert result.candidate is None
    assert "cardinality_mismatch" in result.reason_codes


def test_unknown_competitor_is_rejected(tmp_path: Path) -> None:
    index = _index(tmp_path)
    assessor = _ContextAssessor(
        lambda _record, _context, answer_id: (
            "not_evaluated" if answer_id.endswith(":MESH:B") else "matched"
        )
    )

    result = _proposal(index, assessor)

    assert result.candidate is None
    assert "unknown_competitor" in result.reason_codes


def test_a_decorative_qualifier_is_rejected_as_a_shortcut(
    tmp_path: Path,
) -> None:
    index = _index(tmp_path)
    assessor = _ContextAssessor(
        lambda _record, _context, answer_id: (
            "matched" if answer_id.endswith(":MESH:A") else "not_matched"
        )
    )

    result = _proposal(index, assessor)

    assert result.candidate is None
    assert "shortcut_detected" in result.reason_codes


def test_candidate_compiles_and_validates_against_its_competitor_report(
    tmp_path: Path,
) -> None:
    index = _index(tmp_path)
    assessor = _ContextAssessor(
        lambda _record, context, answer_id: (
            "matched"
            if context.population == "adults"
            and answer_id.endswith(":MESH:A")
            else "matched"
            if context.population != "adults"
            else "not_matched"
        )
    )
    evaluator = _evaluator(index, assessor)
    proposal = CtdStudyContextCandidateSource(evaluator).propose(
        seed_entity_id=SEED_ID,
        chemical_id="D001241",
        snapshot_id=index.metadata()["snapshot_id"],
        context=_context(),
    )
    assert proposal.candidate is not None
    task = TaskCompiler().compile(proposal.candidate, _environment(index))
    report = CtdTaskValidator(evaluator).validate(task)

    assert task.question_type == "study_context_qualified_disease"
    assert len(report.competitor_outcomes) == 2
    assert report.result_complete is True
    assert report.passed is True
    assert report.world_answer_ids == ["primekg:disease:MESH:A"]
    assert report.matched_claim_ids == ["claim-MESH:A"]
    assert report.evidence_dependency_digest == (
        task.evidence_dependency_digest()
    )


def test_validator_rejects_a_candidate_answer_that_differs_from_the_world(
    tmp_path: Path,
) -> None:
    index = _index(tmp_path)
    assessor = _ContextAssessor(
        lambda _record, context, answer_id: (
            "matched"
            if context.population == "adults"
            and answer_id.endswith(":MESH:A")
            else "matched"
            if context.population != "adults"
            else "not_matched"
        )
    )
    evaluator = _evaluator(index, assessor)
    proposal = CtdStudyContextCandidateSource(evaluator).propose(
        seed_entity_id=SEED_ID,
        chemical_id="D001241",
        snapshot_id=index.metadata()["snapshot_id"],
        context=_context(),
    )
    assert proposal.candidate is not None
    payload = proposal.candidate.model_dump(mode="python")
    payload["proposed_answer_ids"] = ["primekg:disease:MESH:B"]
    payload["private_answer_names"] = ["Cancer B"]
    candidate = type(proposal.candidate).model_validate(payload)
    task = TaskCompiler().compile(candidate, _environment(index))

    report = CtdTaskValidator(evaluator).validate(task)

    assert report.passed is False
    assert "candidate_answer_mismatch" in report.reason_codes


def test_validator_rejects_a_shortcut_even_if_a_candidate_was_compiled(
    tmp_path: Path,
) -> None:
    index = _index(tmp_path)
    assessor = _ContextAssessor(
        lambda _record, _context, answer_id: (
            "matched" if answer_id.endswith(":MESH:A") else "not_matched"
        )
    )
    evaluator = _evaluator(index, assessor)
    source = CtdStudyContextCandidateSource(
        evaluator,
        enforce_qualifier_value=False,
    )
    proposal = source.propose(
        seed_entity_id=SEED_ID,
        chemical_id="D001241",
        snapshot_id=index.metadata()["snapshot_id"],
        context=_context(),
    )
    assert proposal.candidate is not None
    task = TaskCompiler().compile(proposal.candidate, _environment(index))

    report = CtdTaskValidator(evaluator).validate(task)

    assert report.passed is False
    assert "shortcut_detected" in report.reason_codes

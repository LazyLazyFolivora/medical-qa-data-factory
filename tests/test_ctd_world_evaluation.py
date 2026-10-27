from __future__ import annotations

import gzip
from pathlib import Path

from medical_qa_data_factory.evidence_enrichment.ctd import (
    CtdEvidenceIndex,
    build_ctd_index,
)
from medical_qa_data_factory.evidence_enrichment.models import (
    CtdEvidenceRecord,
)
from medical_qa_data_factory.generation.ctd_world_evaluation import (
    CtdWorldEvaluator,
    StaticCtdDiseaseCategoryResolver,
    StaticCtdDiseaseIdentityResolver,
)
from medical_qa_data_factory.generation.task_contracts import (
    AnswerCandidateOutcome,
    StudyContext,
)


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


def _write_rows(path: Path, rows: list[tuple[str, ...]]) -> Path:
    source = path / "ctd.csv.gz"
    with gzip.open(source, "wt", encoding="utf-8", newline="") as handle:
        handle.write("# CTD test fixture\n")
        for row in rows:
            handle.write("\t".join(row) + "\n")
    return source


def _row(
    disease_name: str,
    disease_id: str,
    *,
    pubmed_ids: str = "123",
) -> tuple[str, ...]:
    return (
        "Aspirin",
        "D001241",
        "",
        disease_name,
        disease_id,
        "marker/mechanism",
        "PTGS1",
        "1.0",
        "",
        pubmed_ids,
    )


def _index(tmp_path: Path, records: list[tuple[str, str]]) -> CtdEvidenceIndex:
    source = _write_rows(
        tmp_path,
        [_row(name, disease_id) for name, disease_id in records],
    )
    index_path = tmp_path / "ctd.sqlite"
    build_ctd_index(source, index_path)
    return CtdEvidenceIndex(index_path)


def _outcome(
    answer_id: str,
    status: str,
) -> AnswerCandidateOutcome:
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
        self.calls: list[tuple[CtdEvidenceRecord, StudyContext, str]] = []

    def assess(self, *, record, context, answer_id):
        self.calls.append((record, context, answer_id))
        status = self.matcher(record, context, answer_id)
        return _outcome(answer_id, status)


def _evaluator(
    index: CtdEvidenceIndex,
    assessor,
    *,
    category_map: dict[str, list[str]] | None = None,
    identity_map: dict[str, str | list[str]] | None = None,
) -> CtdWorldEvaluator:
    records = index.enumerate_diseases(chemical_id="D001241")
    identities = identity_map or {
        (item.disease_id or item.disease_name): (
            f"primekg:disease:{item.disease_id or item.disease_name}"
        )
        for item in records
    }
    categories = category_map or {
        answer_id: ["digestive cancer"]
        for value in identities.values()
        for answer_id in ([value] if isinstance(value, str) else value)
    }
    return CtdWorldEvaluator(
        index=index,
        identity_resolver=StaticCtdDiseaseIdentityResolver(identities),
        category_resolver=StaticCtdDiseaseCategoryResolver(categories),
        assessor=assessor,
    )


def test_world_evaluation_records_every_competitor(tmp_path: Path) -> None:
    index = _index(
        tmp_path,
        [("Cancer A", "MESH:A"), ("Cancer B", "MESH:B")],
    )
    assessor = _ContextAssessor(
        lambda _record, _context, answer_id: (
            "matched" if answer_id.endswith(":MESH:A") else "not_matched"
        )
    )
    evaluator = _evaluator(index, assessor)

    result = evaluator.evaluate(
        chemical_id="D001241",
        snapshot_id=index.metadata()["snapshot_id"],
        context=_context(),
    )

    assert result.result_complete is True
    assert result.world_answer_ids == ["primekg:disease:MESH:A"]
    assert {item.status for item in result.competitor_outcomes} == {
        "matched",
        "not_matched",
    }
    assert len(result.competitor_outcomes) == 2
    assert len(assessor.calls) == 2


def test_unknown_competitor_makes_the_world_incomplete(tmp_path: Path) -> None:
    index = _index(
        tmp_path,
        [("Cancer A", "MESH:A"), ("Cancer B", "MESH:B")],
    )
    assessor = _ContextAssessor(
        lambda _record, _context, answer_id: (
            "matched" if answer_id.endswith(":MESH:A") else "not_evaluated"
        )
    )

    result = _evaluator(index, assessor).evaluate(
        chemical_id="D001241",
        snapshot_id=index.metadata()["snapshot_id"],
        context=_context(),
    )

    assert result.result_complete is False
    assert "unknown_competitor" in result.reason_codes
    assert any(
        item.status == "not_evaluated"
        for item in result.competitor_outcomes
    )


def test_alias_records_merge_but_same_name_with_different_ids_do_not(
    tmp_path: Path,
) -> None:
    source = _write_rows(
        tmp_path,
        [
            _row("Cancer alias", "MESH:A"),
            _row("Cancer canonical", "MESH:A"),
            _row("Same disease", "MESH:B"),
            _row("Same disease", "MESH:C"),
        ],
    )
    index_path = tmp_path / "ctd.sqlite"
    build_ctd_index(source, index_path)
    index = CtdEvidenceIndex(index_path)
    assessor = _ContextAssessor(
        lambda _record, _context, _answer_id: "matched"
    )
    evaluator = _evaluator(index, assessor)

    result = evaluator.evaluate(
        chemical_id="D001241",
        snapshot_id=index.metadata()["snapshot_id"],
        context=_context(),
    )

    assert result.result_complete is True
    assert result.world_answer_ids == [
        "primekg:disease:MESH:A",
        "primekg:disease:MESH:B",
        "primekg:disease:MESH:C",
    ]


def test_unknown_or_ambiguous_identity_blocks_completion(tmp_path: Path) -> None:
    index = _index(
        tmp_path,
        [("Cancer A", "MESH:A"), ("Cancer B", "MESH:B")],
    )
    assessor = _ContextAssessor(
        lambda _record, _context, _answer_id: "matched"
    )
    unresolved = _evaluator(
        index,
        assessor,
        identity_map={"MESH:A": "primekg:disease:A"},
        category_map={"primekg:disease:A": ["digestive cancer"]},
    )
    ambiguous = _evaluator(
        index,
        assessor,
        identity_map={
            "MESH:A": "primekg:disease:A",
            "MESH:B": ["primekg:disease:B1", "primekg:disease:B2"],
        },
        category_map={
            "primekg:disease:A": ["digestive cancer"],
            "primekg:disease:B1": ["digestive cancer"],
            "primekg:disease:B2": ["digestive cancer"],
        },
    )

    unresolved_result = unresolved.evaluate(
        chemical_id="D001241",
        snapshot_id=index.metadata()["snapshot_id"],
        context=_context(),
    )
    ambiguous_result = ambiguous.evaluate(
        chemical_id="D001241",
        snapshot_id=index.metadata()["snapshot_id"],
        context=_context(),
    )

    assert unresolved_result.result_complete is False
    assert any(
        "ctd_record_unmapped" in item.reason_codes
        for item in unresolved_result.competitor_outcomes
    )
    assert ambiguous_result.result_complete is False
    assert any(
        item.status == "ambiguous"
        and "ctd_mapping_ambiguous" in item.reason_codes
        for item in ambiguous_result.competitor_outcomes
    )


def test_category_unknown_and_provider_failure_are_not_treated_as_negatives(
    tmp_path: Path,
) -> None:
    index = _index(
        tmp_path,
        [("Cancer A", "MESH:A"), ("Cancer B", "MESH:B")],
    )
    assessor = _ContextAssessor(
        lambda _record, _context, answer_id: (
            "provider_failed"
            if answer_id.endswith(":MESH:B")
            else "matched"
        )
    )
    evaluator = _evaluator(
        index,
        assessor,
        category_map={"primekg:disease:MESH:A": ["digestive cancer"]},
    )

    result = evaluator.evaluate(
        chemical_id="D001241",
        snapshot_id=index.metadata()["snapshot_id"],
        context=_context(),
    )

    assert result.result_complete is False
    assert {
        item.status for item in result.competitor_outcomes
    } == {"matched", "not_evaluated"}
    assert any(
        "ctd_disease_category_unknown" in item.reason_codes
        for item in result.competitor_outcomes
    )


def test_snapshot_mismatch_fails_closed(tmp_path: Path) -> None:
    index = _index(tmp_path, [("Cancer A", "MESH:A")])
    evaluator = _evaluator(
        index,
        _ContextAssessor(
            lambda _record, _context, _answer_id: "matched"
        ),
    )

    result = evaluator.evaluate(
        chemical_id="D001241",
        snapshot_id="ctd:other",
        context=_context(),
    )

    assert result.result_complete is False
    assert result.reason_codes == ["ctd_snapshot_mismatch"]
    assert result.competitor_outcomes == []

from __future__ import annotations

import json
import gzip
import hashlib
from pathlib import Path

import pytest

from medical_qa_data_factory.cli import main
from medical_qa_data_factory.domain.common import utc_now
from medical_qa_data_factory.evidence_enrichment.ctd import (
    CtdEvidenceIndex,
    build_ctd_index,
)
from medical_qa_data_factory.evidence_enrichment.models import (
    PubMedEvidenceRecord,
)
from medical_qa_data_factory.evidence_enrichment.pubmed_evidence import (
    PubMedSpanCitation,
)
from medical_qa_data_factory.generation.evidence_fusion import (
    CtdEvidenceExtraction,
    PubMedFinding,
    PubMedFindingVerdict,
    StudyContextIntent,
    StudyContextQualifiers,
    fuse_ctd_pubmed_evidence,
)
from medical_qa_data_factory.generation.task_candidates import (
    CtdStudyContextCandidateSource,
)
from medical_qa_data_factory.generation.task_compiler import TaskCompiler
from medical_qa_data_factory.generation.task_release import TaskReleaseExporter
from medical_qa_data_factory.generation.task_service import (
    CtdTaskGenerationService,
    CtdTaskRunSpec,
)
from medical_qa_data_factory.generation.task_realization import (
    CtdTaskQuestionWriter,
    CtdTaskTextSemanticVerifier,
)
from medical_qa_data_factory.generation.task_validation import CtdTaskValidator

from test_ctd_candidate_source import (
    SEED_ID,
    _context,
    _environment,
    _evaluator,
    _index,
)


def _assessor():
    from test_ctd_candidate_source import _ContextAssessor

    return _ContextAssessor(
        lambda _record, context, answer_id: (
            "matched"
            if context.population == "adults"
            and answer_id.endswith(":MESH:A")
            else "matched"
            if context.population != "adults"
            else "not_matched"
        )
    )


def _service(index, assessor):
    evaluator = _evaluator(index, assessor)
    return CtdTaskGenerationService(
        candidate_source=CtdStudyContextCandidateSource(evaluator),
        compiler=TaskCompiler(),
        validator=CtdTaskValidator(evaluator),
        evidence_by_record={},
    )


def test_service_generates_one_validated_ctd_release_task(
    tmp_path: Path,
) -> None:
    index = _index(tmp_path)
    environment = _environment(index)
    result = _service(index, _assessor()).generate(
        seed_entity_id=SEED_ID,
        chemical_id="D001241",
        snapshot_id=index.metadata()["snapshot_id"],
        context=_context(),
        environment=environment,
    )

    assert len(result.published) == 1
    published = result.published[0]
    assert published.planner_input.qtype == "study_context_qualified_disease"
    assert "answer_contract" not in published.planner_input.model_dump()
    assert published.evaluation_target.gold_claim_ids == ["claim-A"]
    assert result.episode.stop_reason == "question_targets_met"
    assert result.episode.search_complete is True
    assert result.episode.competitor_count == 2
    assert (
        result.episode.provenance["validator_version"]
        == "ctd-task-validator-v1"
    )


def test_service_records_a_rejected_incomplete_world(tmp_path: Path) -> None:
    from test_ctd_candidate_source import _ContextAssessor

    index = _index(tmp_path)
    assessor = _ContextAssessor(
        lambda _record, _context_value, answer_id: (
            "not_evaluated" if answer_id.endswith(":MESH:B") else "matched"
        )
    )
    result = _service(index, assessor).generate(
        seed_entity_id=SEED_ID,
        chemical_id="D001241",
        snapshot_id=index.metadata()["snapshot_id"],
        context=_context(),
        environment=_environment(index),
    )

    assert result.published == []
    assert result.rejections[0].stage == "candidate_source"
    assert "unknown_competitor" in result.rejections[0].reason_codes
    assert result.episode.stop_reason == "search_incomplete"
    assert result.episode.search_complete is False


def test_ctd_writer_and_verifier_reject_missing_names_and_leaks(
    tmp_path: Path,
) -> None:
    index = _index(tmp_path)
    result = _service(index, _assessor()).generate(
        seed_entity_id=SEED_ID,
        chemical_id="D001241",
        snapshot_id=index.metadata()["snapshot_id"],
        context=_context(),
        environment=_environment(index),
    )
    assert result.published
    published = result.published[0]
    without_name = published.validated_task.model_copy(
        update={
            "task": published.validated_task.task.model_copy(
                update={"public_entity_names": {}}
            )
        }
    )
    with pytest.raises(ValueError, match="chemical name"):
        CtdTaskQuestionWriter().write(
            without_name,
            qid="question-without-name",
        )
    leaked = published.question_variant.model_copy(
        update={
            "question_text": (
                published.question_variant.question_text
                + " Cancer A"
            )
        }
    )

    report = CtdTaskTextSemanticVerifier().verify(
        leaked,
        published.validated_task,
    )

    assert report.passed is False
    assert "answer_leakage" in report.reason_codes


def test_ctd_verifier_rejects_internal_scope_language(
    tmp_path: Path,
) -> None:
    index = _index(tmp_path)
    result = _service(index, _assessor()).generate(
        seed_entity_id=SEED_ID,
        chemical_id="D001241",
        snapshot_id=index.metadata()["snapshot_id"],
        context=_context(),
        environment=_environment(index),
    )
    published = result.published[0]
    bad = published.question_variant.model_copy(
        update={
            "question_text": (
                "在当前证据范围内，"
                + published.question_variant.question_text
            )
        }
    )

    report = CtdTaskTextSemanticVerifier().verify(
        bad,
        published.validated_task,
    )

    assert report.passed is False
    assert "database_trivia_or_internal_term" in report.reason_codes


def test_ctd_release_keeps_public_and_hidden_views_isolated(
    tmp_path: Path,
) -> None:
    index = _index(tmp_path)
    result = _service(index, _assessor()).generate(
        seed_entity_id=SEED_ID,
        chemical_id="D001241",
        snapshot_id=index.metadata()["snapshot_id"],
        context=_context(),
        environment=_environment(index),
    )
    output = tmp_path / "ctd-release"

    summary = TaskReleaseExporter().export(result, output)

    assert summary.published_count == 1
    planner_rows = [
        json.loads(line)
        for line in (output / "public" / "planner_questions.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    target_rows = [
        json.loads(line)
        for line in (output / "hidden" / "evaluation_targets.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert planner_rows[0]["qtype"] == "study_context_qualified_disease"
    assert "answer_contract" not in planner_rows[0]
    assert target_rows[0]["answer_contract"]["canonical_answer_ids"] == [
        "primekg:disease:MESH:A"
    ]
    catalog = [
        json.loads(line)
        for line in (output / "hidden" / "evidence_catalog.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert catalog
    assert catalog[0]["record"]["disease_id"] == "MESH:A"
    episodes = [
        json.loads(line)
        for line in (output / "hidden" / "generation_episodes.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert episodes[0]["stop_reason"] == "question_targets_met"
    public_manifest = json.loads(
        (output / "public" / "dataset_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert public_manifest["provenance"]["writer_version"]


def test_generate_validated_ctd_profile_exports_one_command(
    tmp_path: Path,
    capsys,
) -> None:
    source = tmp_path / "ctd.csv.gz"
    with gzip.open(source, "wt", encoding="utf-8", newline="") as handle:
        handle.write("# CTD test fixture\n")
        for name, disease_id, pmid in (
            ("Cancer A", "MESH:A", "100"),
            ("Cancer B", "MESH:B", "200"),
            ("Cancer C", "MESH:C", "300"),
        ):
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
                        pmid,
                    )
                )
                + "\n"
            )
    index_path = tmp_path / "ctd.sqlite"
    build_ctd_index(source, index_path)
    index = CtdEvidenceIndex(index_path)
    context = _context()
    fusions = {}
    for record in index.enumerate_diseases(chemical_id="D001241"):
        answer_id = f"primekg:disease:{record.disease_id}"
        pmid = record.pubmed_ids[0]
        abstract = (
            "In the selected population, the disease risk was lower "
            "in the observational cohort."
        )
        quote = "the disease risk was lower"
        start = abstract.index(quote)
        pubmed = PubMedEvidenceRecord(
            pmid=pmid,
            title="Study title",
            abstract=abstract,
            snapshot_id="pubmed:test",
            content_hash=hashlib.sha256(
                f"Study title\n{abstract}".encode()
            ).hexdigest(),
            retrieved_at=utc_now(),
        )
        intent = (
            StudyContextIntent.RISK_INCREASE
            if record.disease_id == "MESH:B"
            else StudyContextIntent.RISK_DECREASE
        )
        polarity = (
            "increase" if record.disease_id == "MESH:B" else "decrease"
        )
        population = (
            "children" if record.disease_id == "MESH:C" else "adults"
        )
        fusion = fuse_ctd_pubmed_evidence(
            ctd_records=[record],
            pubmed_records=[pubmed],
            extraction=CtdEvidenceExtraction(
                findings=[
                    PubMedFinding(
                        pmid=pmid,
                        verdict=PubMedFindingVerdict.SUPPORTS,
                        intent=intent,
                        polarity=polarity,
                        support_level="literature_supported",
                        claim_text=quote,
                        qualifiers=StudyContextQualifiers(
                            population=population,
                            exposure_context=context.exposure_context,
                            dose_exposure=context.dose_or_regimen,
                            exposure_duration=context.exposure_duration,
                            study_type="observational cohort",
                            sample_type=context.sample_type,
                            temporal_scope=context.temporal_scope,
                            disease_category="digestive cancer",
                        ),
                        citation=PubMedSpanCitation(
                            pmid=pmid,
                            start=start,
                            end=start + len(quote),
                            quote=quote,
                        ),
                    )
                ]
            ),
            seed_entity_id=SEED_ID,
            answer_entity_id=answer_id,
        )
        assert fusion.status.value == "eligible"
        fusions[record.source_record_locator or record.content_hash] = fusion

    run_spec = CtdTaskRunSpec(
        seed_entity_id=SEED_ID,
        chemical_id="D001241",
        snapshot_id=index.metadata()["snapshot_id"],
        context=context,
        identity_mapping={
            "MESH:A": "primekg:disease:MESH:A",
            "MESH:B": "primekg:disease:MESH:B",
            "MESH:C": "primekg:disease:MESH:C",
        },
        category_mapping={
            "primekg:disease:MESH:A": ["digestive cancer"],
            "primekg:disease:MESH:B": ["digestive cancer"],
            "primekg:disease:MESH:C": ["digestive cancer"],
        },
        fusions=fusions,
        environment=_environment(index),
    )
    run_spec_path = tmp_path / "ctd-run.json"
    run_spec_path.write_text(
        run_spec.model_dump_json(indent=2),
        encoding="utf-8",
    )
    output = tmp_path / "cli-release"

    code = main(
        [
            "generate",
            "validated",
            "--profile",
            "ctd",
            "--output",
            str(output),
            "--ctd-index",
            str(index_path),
            "--ctd-run-spec",
            str(run_spec_path),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["profile"] == "ctd"
    assert payload["published_count"] == 1
    assert (output / "public" / "planner_questions.jsonl").exists()
    assert (output / "hidden" / "generation_episodes.jsonl").exists()

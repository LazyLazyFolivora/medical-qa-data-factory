from __future__ import annotations

import json
from pathlib import Path

from medical_qa_data_factory.generation.task_candidates import (
    CtdStudyContextCandidateSource,
)
from medical_qa_data_factory.generation.task_compiler import TaskCompiler
from medical_qa_data_factory.generation.task_release import TaskReleaseExporter
from medical_qa_data_factory.generation.task_service import (
    CtdTaskGenerationService,
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
    assert result.episode.stop_reason == "required_searches_completed"
    assert result.episode.search_complete is False


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

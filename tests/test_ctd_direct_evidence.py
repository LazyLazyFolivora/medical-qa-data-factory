from __future__ import annotations

import gzip
import json
from pathlib import Path

from medical_qa_data_factory.cli import main
from medical_qa_data_factory.evidence_enrichment.ctd import (
    CtdEvidenceIndex,
    build_ctd_index,
)
from medical_qa_data_factory.generation.ctd_direct_evidence import (
    CtdDirectEvidenceService,
)
from medical_qa_data_factory.generation.task_release import TaskReleaseExporter


def _write_index(
    tmp_path: Path,
    rows: list[tuple[str, str, str, str]],
) -> CtdEvidenceIndex:
    source = tmp_path / "ctd-direct.csv.gz"
    with gzip.open(source, "wt", encoding="utf-8", newline="") as handle:
        handle.write("# CTD direct evidence fixture\n")
        for disease_name, disease_id, direct, pmid in rows:
            handle.write(
                "\t".join(
                    (
                        "Test chemical",
                        "C000001",
                        "",
                        disease_name,
                        disease_id,
                        direct,
                        "GENE1",
                        "1.0",
                        "",
                        pmid,
                    )
                )
                + "\n"
            )
    index_path = tmp_path / "ctd-direct.sqlite"
    build_ctd_index(source, index_path)
    return CtdEvidenceIndex(index_path)


def _base_rows() -> list[tuple[str, str, str, str]]:
    return [
        ("Disease A", "MESH:A", "marker/mechanism", "100"),
        ("Disease B", "MESH:B", "", "200"),
        ("Disease C", "MESH:C", "therapeutic", "300"),
    ]


def test_marker_mechanism_question_uses_ctd_answer_world(
    tmp_path: Path,
) -> None:
    index = _write_index(tmp_path, _base_rows())
    result = CtdDirectEvidenceService(index).generate(
        chemical_id="C000001",
        evidence_kind="marker/mechanism",
    )

    assert len(result.published) == 1
    published = result.published[0]
    assert published.planner_input.qtype == "ctd_direct_evidence_disease"
    assert published.planner_input.question == (
        "Test chemical 在 CTD 被记录与哪种疾病存在标记物或机制研究关联？"
    )
    assert published.evaluation_target.answer_contract.canonical_answer_ids == [
        "ctd:disease:MESH:A"
    ]
    assert published.evaluation_target.gold_evidence_ids
    assert result.episode.search_complete is True
    assert (
        published.validated_task.task.information_requirements[
            0
        ].claim_pattern.subject_id
        == "ctd:chemical:C000001"
    )
    assert (
        published.evaluation_target.answer_contract.accepted_aliases
        == ["Disease A"]
    )


def test_therapeutic_question_never_upgrades_to_efficacy(
    tmp_path: Path,
) -> None:
    index = _write_index(tmp_path, _base_rows())
    result = CtdDirectEvidenceService(index).generate(
        chemical_id="C000001",
        evidence_kind="therapeutic",
    )

    assert len(result.published) == 1
    assert result.published[0].planner_input.question == (
        "Test chemical 在 CTD 记录中被研究用于哪种疾病？"
    )
    assert "治疗有效" not in result.published[0].planner_input.question


def test_multiple_direct_diseases_are_rejected(tmp_path: Path) -> None:
    rows = [
        *_base_rows(),
        ("Disease D", "MESH:D", "marker/mechanism", "400"),
    ]
    result = CtdDirectEvidenceService(
        _write_index(tmp_path, rows)
    ).generate(
        chemical_id="C000001",
        evidence_kind="marker/mechanism",
    )

    assert result.published == []
    assert result.rejections[0].reason_codes == [
        "world_cardinality_mismatch"
    ]


def test_inference_only_disease_does_not_become_direct_evidence(
    tmp_path: Path,
) -> None:
    rows = [("Disease B", "MESH:B", "", "200")]
    result = CtdDirectEvidenceService(
        _write_index(tmp_path, rows)
    ).generate(
        chemical_id="C000001",
        evidence_kind="marker/mechanism",
    )

    assert result.published == []
    assert "world_answer_missing" in result.rejections[0].reason_codes


def test_missing_ctd_disease_id_blocks_publication(tmp_path: Path) -> None:
    rows = [
        ("Disease A", "MESH:A", "marker/mechanism", "100"),
        ("Nameless disease", "", "marker/mechanism", "500"),
    ]
    result = CtdDirectEvidenceService(
        _write_index(tmp_path, rows)
    ).generate(
        chemical_id="C000001",
        evidence_kind="marker/mechanism",
    )

    assert result.published == []
    assert "unknown_competitor" in result.rejections[0].reason_codes


def test_release_keeps_ctd_answers_hidden(tmp_path: Path) -> None:
    index = _write_index(tmp_path, _base_rows())
    result = CtdDirectEvidenceService(index).generate(
        chemical_id="C000001",
        evidence_kind="marker/mechanism",
    )
    output = tmp_path / "ctd-direct-release"

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
    assert "ctd:disease:MESH:A" not in json.dumps(planner_rows)
    assert target_rows[0]["answer_contract"]["canonical_answer_ids"] == [
        "ctd:disease:MESH:A"
    ]
    catalog = [
        json.loads(line)
        for line in (output / "hidden" / "evidence_catalog.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert any(
        row["record"]["disease_id"] == "MESH:A" for row in catalog
    )


def test_ctd_direct_cli_generates_release(tmp_path: Path, capsys) -> None:
    index = _write_index(tmp_path, _base_rows())
    index_path = tmp_path / "ctd-direct.sqlite"
    output = tmp_path / "cli-release"

    code = main(
        [
            "generate",
            "ctd-direct",
            "--output",
            str(output),
            "--ctd-index",
            str(index_path),
            "--chemical-id",
            "C000001",
            "--evidence-kind",
            "marker/mechanism",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert index.metadata()["snapshot_id"]
    assert code == 0
    assert payload["profile"] == "ctd-direct"
    assert payload["published_count"] == 1

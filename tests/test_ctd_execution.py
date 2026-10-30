from __future__ import annotations

import gzip

from medical_qa_data_factory.evidence_enrichment.ctd import (
    CtdEvidenceIndex,
    build_ctd_index,
)
from medical_qa_data_factory.generation.ctd_execution import (
    CtdEvidenceExecutor,
    ctd_artifact_id,
)


def test_ctd_executor_reads_filters_caps_and_emits_stable_trace(tmp_path) -> None:
    source = tmp_path / "ctd.csv.gz"
    with gzip.open(source, "wt", encoding="utf-8", newline="") as handle:
        handle.write(
            "Chemical One\tD000001\t\tAlpha disease\tMESH:D000010\t"
            "marker/mechanism\tGENE1\t1.0\t\t100\n"
        )
        handle.write(
            "Chemical One\tD000001\t\tBeta disease\tMESH:D000020\t"
            "therapeutic\tGENE2\t1.0\t\t200\n"
        )
    index_path = tmp_path / "ctd.sqlite"
    build_ctd_index(source, index_path)
    persisted = []
    attempts = []
    executor = CtdEvidenceExecutor(
        index=CtdEvidenceIndex(index_path),
        on_record=lambda artifact_id, record: persisted.append(
            (artifact_id, record.disease_name)
        ),
        on_attempt=lambda: attempts.append(True),
    )
    request = {
        "chemical_entity_id": "primekg:exposure:D000001",
        "direct_evidence": ["therapeutic"],
        "max_results": 1,
    }

    first = executor.lookup(request)
    second = executor.lookup(request)

    assert [item.record.disease_name for item in first[0]] == ["Beta disease"]
    assert first[1:] == second[1:]
    assert first[2].startswith("ctd:")
    assert persisted[0][0].startswith("ctd-evidence-")
    assert first[0][0].planner_summary()["direct_evidence"] == [
        "therapeutic"
    ]
    assert attempts == [True, True]
    assert executor.calls[0]["result_count"] == 1
    original = first[0][0].record
    changed = original.model_copy(update={"content_hash": "f" * 64})
    assert original.source_record_locator == changed.source_record_locator
    assert ctd_artifact_id(original) != ctd_artifact_id(changed)

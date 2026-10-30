"""Bounded, auditable execution over a pinned CTD evidence sidecar."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Callable

from ..evidence_enrichment.ctd import CtdEvidenceIndex
from ..evidence_enrichment.models import CtdEvidenceRecord


@dataclass(frozen=True)
class CtdEvidenceArtifact:
    """Version-bound CTD record returned to the generic executor."""

    evidence_id: str
    record: CtdEvidenceRecord

    def planner_summary(self) -> dict[str, object]:
        return {
            "artifact_id": self.evidence_id,
            "disease_id": self.record.disease_id,
            "disease_name": self.record.disease_name,
            "direct_evidence": list(self.record.direct_evidence),
            "pubmed_ids": list(self.record.pubmed_ids[:20]),
            "pubmed_count": len(self.record.pubmed_ids),
        }


def ctd_artifact_id(record: CtdEvidenceRecord) -> str:
    """Bind snapshot, source locator and content hash into one stable ID."""

    identity = "|".join(
        [
            record.source_snapshot_id,
            record.source_record_locator or "",
            record.content_hash,
        ]
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"ctd-evidence-{digest}"


@dataclass
class CtdEvidenceExecutor:
    """Resolve one chemical to CTD records without model-side SQL or paths."""

    index: CtdEvidenceIndex
    on_record: Callable[[str, CtdEvidenceRecord], None] | None = None
    on_attempt: Callable[[], None] | None = None
    calls: list[dict[str, object]] = field(default_factory=list)

    def lookup(
        self, request: dict[str, object]
    ) -> tuple[list[CtdEvidenceArtifact], str, str | None]:
        chemical_entity_id = str(request["chemical_entity_id"])
        chemical_id = chemical_entity_id.rsplit(":", 1)[-1]
        disease_name = request.get("disease_name")
        try:
            if disease_name:
                records = self.index.lookup(
                    chemical_id=chemical_id,
                    disease_name=str(disease_name),
                )
            else:
                records = self.index.enumerate_diseases(
                    chemical_id=chemical_id
                )
        finally:
            if self.on_attempt is not None:
                self.on_attempt()

        required_labels = {
            str(item) for item in request.get("direct_evidence", [])
        }
        if required_labels:
            records = [
                record
                for record in records
                if required_labels.intersection(record.direct_evidence)
            ]
        records = records[: int(request.get("max_results", 20))]
        artifacts = [
            CtdEvidenceArtifact(
                evidence_id=ctd_artifact_id(record), record=record
            )
            for record in records
        ]
        for artifact in artifacts:
            if self.on_record is not None:
                self.on_record(artifact.evidence_id, artifact.record)

        snapshot_id = self.index.metadata().get("snapshot_id")
        trace_payload = {
            "snapshot_id": snapshot_id,
            "chemical_id": chemical_id,
            "disease_name": str(disease_name or ""),
            "direct_evidence": sorted(required_labels),
            "max_results": int(request.get("max_results", 20)),
        }
        trace_digest = hashlib.sha256(
            json.dumps(
                trace_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:24]
        self.calls.append(
            {
                **trace_payload,
                "result_count": len(records),
                "trace_id": f"ctd-query-{trace_digest}",
            }
        )
        return artifacts, f"ctd-query-{trace_digest}", snapshot_id

"""Build an auditable PrimeKG-to-CTD identity snapshot from frozen sources."""
from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from pydantic import Field, model_validator

from ..domain.common import ContractModel, UtcDateTime, utc_now
from ..knowledge_graph.contracts import GraphEdge
from .ctd import (
    CtdDiseaseCrosswalk,
    CtdDiseaseCrosswalkEntry,
    CtdEvidenceIndex,
    CtdMappingAuditRecord,
    CtdMappingStatus,
    audit_primekg_ctd_edges,
)
from .models import CtdEvidenceRecord


IDENTITY_SNAPSHOT_VERSION = "primekg-ctd-identity-snapshot-v1"
IDENTITY_METHOD = "primekg_ctd_source_exact_alias_v1"


class PrimeKgCtdChemicalCrosswalkEntry(ContractModel):
    primekg_chemical_id: str = Field(min_length=1)
    ctd_chemical_id: str = Field(min_length=1)
    source_edge_ids: list[str] = Field(min_length=1)


class PrimeKgCtdChemicalCrosswalk(ContractModel):
    schema_version: str = "primekg-ctd-chemical-crosswalk-v1"
    entries: list[PrimeKgCtdChemicalCrosswalkEntry] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def primekg_chemical_ids_are_unique(self) -> "PrimeKgCtdChemicalCrosswalk":
        ids = [item.primekg_chemical_id for item in self.entries]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate PrimeKG chemical in CTD crosswalk")
        return self

    def ctd_ids_for(self, primekg_chemical_id: str) -> set[str]:
        return {
            item.ctd_chemical_id
            for item in self.entries
            if item.primekg_chemical_id == primekg_chemical_id
        }

    @classmethod
    def from_json(cls, path: str | Path) -> "PrimeKgCtdChemicalCrosswalk":
        return cls.model_validate_json(
            Path(path).read_text(encoding="utf-8")
        )


class PrimeKgCtdIdentitySnapshot(ContractModel):
    schema_version: str = IDENTITY_SNAPSHOT_VERSION
    primekg_snapshot_id: str = Field(min_length=1)
    ctd_snapshot_id: str = Field(min_length=1)
    method: str = IDENTITY_METHOD
    chemical_crosswalk: PrimeKgCtdChemicalCrosswalk
    disease_crosswalk: CtdDiseaseCrosswalk
    fully_mapped_chemical_ids: list[str] = Field(default_factory=list)
    audit_counts: dict[str, int] = Field(default_factory=dict)
    created_at: UtcDateTime = Field(default_factory=utc_now)

    @classmethod
    def from_json(cls, path: str | Path) -> "PrimeKgCtdIdentitySnapshot":
        return cls.model_validate_json(
            Path(path).read_text(encoding="utf-8")
        )

    def write_json(self, path: str | Path) -> Path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(
                self.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return destination


def build_primekg_ctd_identity_snapshot(
    edges: Iterable[GraphEdge],
    *,
    index: CtdEvidenceIndex,
    primekg_snapshot_id: str,
) -> tuple[PrimeKgCtdIdentitySnapshot, list[CtdMappingAuditRecord]]:
    """Build identity mappings only from exact frozen-source aliases.

    A disease name shared by multiple CTD IDs is ambiguous and is deliberately
    omitted. The formal task service then fails closed rather than guessing.
    """

    edge_list = list(edges)
    records_by_chemical: dict[str, list[CtdEvidenceRecord]] = {}
    for chemical_id in sorted(
        {
            edge.head.native_id
            for edge in edge_list
            if edge.head.native_id
        }
    ):
        records_by_chemical[chemical_id] = index.enumerate_diseases(
            chemical_id=chemical_id
        )
    records_by_locator = {
        record.source_record_locator: record
        for records in records_by_chemical.values()
        for record in records
        if record.source_record_locator
    }
    audit = audit_primekg_ctd_edges(edge_list, index=index)

    disease_uses: dict[str, set[str]] = {}
    chemical_edges: dict[str, set[str]] = {}
    chemical_ids_by_primekg: dict[str, str] = {}
    for edge, item in zip(edge_list, audit, strict=True):
        chemical_id = edge.head.native_id
        if chemical_id and records_by_chemical.get(chemical_id):
            chemical_edges.setdefault(edge.head.canonical_id, set()).add(
                edge.edge_id
            )
            chemical_ids_by_primekg[edge.head.canonical_id] = chemical_id
        if item.status is not CtdMappingStatus.MAPPED_EXACT:
            continue
        mapped_ids = {
            record.disease_id or record.source_record_locator
            for locator in item.ctd_record_locators
            if (record := records_by_locator.get(locator)) is not None
        }
        mapped_ids.discard(None)
        if len(mapped_ids) == 1:
            disease_uses.setdefault(edge.tail.canonical_id, set()).update(
                value for value in mapped_ids if value is not None
            )

    counts = {status.value: 0 for status in CtdMappingStatus}
    for item in audit:
        counts[item.status.value] += 1
    answers_by_chemical: dict[str, set[str]] = {}
    for edge in edge_list:
        answers_by_chemical.setdefault(
            edge.head.canonical_id, set()
        ).add(edge.tail.canonical_id)
    mapped_diseases = {
        primekg_id
        for primekg_id, ctd_ids in disease_uses.items()
        if len(ctd_ids) == 1
    }
    fully_mapped = sorted(
        chemical_id
        for chemical_id, disease_ids in answers_by_chemical.items()
        if chemical_id in chemical_edges
        and disease_ids
        and disease_ids.issubset(mapped_diseases)
    )
    snapshot = PrimeKgCtdIdentitySnapshot(
        primekg_snapshot_id=primekg_snapshot_id,
        ctd_snapshot_id=index.metadata()["snapshot_id"],
        created_at=index.metadata()["built_at"],
        chemical_crosswalk=PrimeKgCtdChemicalCrosswalk(
            entries=[
                PrimeKgCtdChemicalCrosswalkEntry(
                    primekg_chemical_id=primekg_id,
                    ctd_chemical_id=chemical_ids_by_primekg[primekg_id],
                    source_edge_ids=sorted(edge_ids),
                )
                for primekg_id, edge_ids in sorted(chemical_edges.items())
            ]
        ),
        disease_crosswalk=CtdDiseaseCrosswalk(
            entries=[
                CtdDiseaseCrosswalkEntry(
                    primekg_disease_id=primekg_id,
                    ctd_disease_ids=sorted(ctd_ids),
                )
                for primekg_id, ctd_ids in sorted(disease_uses.items())
                if len(ctd_ids) == 1
            ]
        ),
        fully_mapped_chemical_ids=fully_mapped,
        audit_counts=dict(sorted(counts.items())),
    )
    return snapshot, audit

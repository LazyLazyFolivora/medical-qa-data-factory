"""Build and query a read-only index of CTD chemical-disease records."""
from __future__ import annotations

import csv
import gzip
import hashlib
import itertools
import json
import os
import re
import sqlite3
import uuid
from contextlib import closing
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Literal
from urllib.parse import quote, unquote

from pydantic import Field, model_validator

from ..domain.common import ContractModel, utc_now
from ..knowledge_graph.contracts import GraphEdge
from .models import CtdEvidenceRecord


CTD_INDEX_VERSION = "ctd-evidence-index-v3"
SUPPORTED_CTD_INDEX_VERSIONS = {
    "ctd-evidence-index-v2",
    CTD_INDEX_VERSION,
}
_NORMALIZE = re.compile(r"[\W_]+", re.UNICODE)
_DISEASE_TERM_ALIASES = {
    "cancers": "neoplasm",
    "cancer": "neoplasm",
    "carcinomas": "neoplasm",
    "carcinoma": "neoplasm",
    "neoplasms": "neoplasm",
    "neoplasm": "neoplasm",
    "tumors": "neoplasm",
    "tumor": "neoplasm",
    "tumours": "neoplasm",
    "tumour": "neoplasm",
    "prostatic": "prostate",
}
DEFAULT_CTD_COLUMNS = [
    "ChemicalName",
    "ChemicalID",
    "CasRN",
    "DiseaseName",
    "DiseaseID",
    "DirectEvidence",
    "InferenceGeneSymbol",
    "InferenceScore",
    "OmimIDs",
    "PubMedIDs",
]


def _normalize_name(value: str) -> str:
    tokens = _NORMALIZE.sub(" ", value.casefold()).strip().split()
    return " ".join(_DISEASE_TERM_ALIASES.get(token, token) for token in tokens)


def _hash_record(payload: dict[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _split_values(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split("|") if item.strip()]


def _field(row: dict[str, str], *names: str) -> str:
    for name in names:
        value = row.get(name)
        if value:
            return value.strip()
    return ""


def _record_locator(
    chemical_id: str,
    *,
    disease_id: str | None,
    normalized_disease_name: str,
) -> str:
    identity_kind = "id" if disease_id else "name"
    identity = disease_id or normalized_disease_name
    return ":".join(
        (
            "ctd-record-v1",
            quote(chemical_id, safe=""),
            identity_kind,
            quote(identity, safe=""),
        )
    )


def _parse_record_locator(locator: str) -> tuple[str, str, str]:
    parts = locator.split(":", 3)
    if len(parts) != 4 or parts[0] != "ctd-record-v1":
        raise ValueError("invalid CTD source record locator")
    _, chemical_id, identity_kind, identity = parts
    if identity_kind not in {"id", "name"}:
        raise ValueError("invalid CTD source record locator identity")
    return unquote(chemical_id), identity_kind, unquote(identity)


class CtdMappingStatus(str, Enum):
    MAPPED_EXACT = "mapped_exact"
    MAPPED_CROSSWALK = "mapped_crosswalk"
    AMBIGUOUS = "ambiguous"
    UNMAPPED = "unmapped"


class CtdDiseaseCrosswalkEntry(ContractModel):
    primekg_disease_id: str = Field(min_length=1)
    ctd_disease_ids: list[str] = Field(min_length=1)


class CtdDiseaseCrosswalk(ContractModel):
    schema_version: Literal["ctd-disease-crosswalk-v1"] = (
        "ctd-disease-crosswalk-v1"
    )
    entries: list[CtdDiseaseCrosswalkEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def primekg_ids_are_unique(self) -> "CtdDiseaseCrosswalk":
        ids = [entry.primekg_disease_id for entry in self.entries]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate PrimeKG disease ID in CTD crosswalk")
        if any(
            len(entry.ctd_disease_ids) != len(set(entry.ctd_disease_ids))
            for entry in self.entries
        ):
            raise ValueError("duplicate CTD disease ID in crosswalk entry")
        return self

    def ctd_ids_for(self, primekg_disease_id: str | None) -> set[str]:
        if not primekg_disease_id:
            return set()
        for entry in self.entries:
            if entry.primekg_disease_id == primekg_disease_id:
                return set(entry.ctd_disease_ids)
        return set()

    @classmethod
    def from_json(cls, path: str | Path) -> "CtdDiseaseCrosswalk":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


class CtdMappingAuditRecord(ContractModel):
    primekg_edge_id: str = Field(min_length=1)
    chemical_id: str | None = None
    chemical_name: str = Field(min_length=1)
    primekg_disease_id: str | None = None
    primekg_disease_name: str = Field(min_length=1)
    status: CtdMappingStatus
    ctd_record_locators: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)


def build_ctd_index(
    source: str | Path,
    destination: str | Path,
    *,
    source_release: str | None = None,
) -> dict[str, Any]:
    """Parse the CTD chemical-disease CSV and write an atomic sidecar index."""
    source_path = Path(source)
    destination_path = Path(destination)
    if not source_path.is_file():
        raise FileNotFoundError(f"CTD source not found: {source_path}")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination_path.with_name(
        f".{destination_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    source_sha256 = _sha256_file(source_path)
    snapshot_id = f"ctd:{source_sha256[:16]}"
    connection = sqlite3.connect(temporary)
    rows_processed = 0
    try:
        connection.executescript(
            """
            PRAGMA journal_mode = OFF;
            PRAGMA synchronous = OFF;
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE ctd_records (
                chemical_id TEXT NOT NULL,
                chemical_name TEXT NOT NULL,
                disease_id TEXT,
                disease_name TEXT NOT NULL,
                normalized_disease_name TEXT NOT NULL,
                direct_evidence TEXT NOT NULL,
                inference_genes TEXT NOT NULL,
                inference_score REAL,
                pubmed_ids TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                PRIMARY KEY (chemical_id, disease_name, content_hash)
            );
            """
        )
        opener = gzip.open if source_path.suffix == ".gz" else open
        batch: list[tuple[Any, ...]] = []
        with opener(source_path, "rt", encoding="utf-8", newline="") as handle:
            data_lines = (
                line
                for line in handle
                if line.strip() and not line.startswith("#")
            )
            try:
                first_line = next(data_lines)
            except StopIteration as exc:
                raise ValueError("CTD file contains no data rows") from exc
            delimiter = "\t" if first_line.count("\t") > first_line.count(",") else ","
            reader = csv.reader(
                itertools.chain([first_line], data_lines),
                delimiter=delimiter,
            )
            first_fields = next(reader)
            required = {"ChemicalID", "ChemicalName", "DiseaseName"}
            if required <= set(first_fields):
                columns = first_fields
                data_rows = reader
            else:
                if len(first_fields) != len(DEFAULT_CTD_COLUMNS):
                    raise ValueError(
                        "CTD file has neither a recognized header nor the "
                        f"expected {len(DEFAULT_CTD_COLUMNS)} tab-separated fields"
                    )
                columns = DEFAULT_CTD_COLUMNS
                data_rows = itertools.chain([first_fields], reader)
            for values in data_rows:
                if len(values) != len(columns):
                    continue
                row = dict(zip(columns, values, strict=True))
                chemical_id = _field(row, "ChemicalID")
                chemical_name = _field(row, "ChemicalName")
                disease_name = _field(row, "DiseaseName")
                if not chemical_id or not chemical_name or not disease_name:
                    continue
                raw = {
                    "chemical_id": chemical_id,
                    "chemical_name": chemical_name,
                    "disease_id": _field(row, "DiseaseID") or None,
                    "disease_name": disease_name,
                    "direct_evidence": _split_values(
                        _field(row, "DirectEvidence")
                    ),
                    "inference_genes": _split_values(
                        _field(row, "InferenceGeneSymbol")
                    ),
                    "inference_score": _field(row, "InferenceScore") or None,
                    "pubmed_ids": _split_values(_field(row, "PubMedIDs")),
                }
                score = raw["inference_score"]
                try:
                    numeric_score = float(score) if score else None
                except ValueError:
                    numeric_score = None
                batch.append(
                    (
                        chemical_id,
                        chemical_name,
                        raw["disease_id"],
                        disease_name,
                        _normalize_name(disease_name),
                        "|".join(raw["direct_evidence"]),
                        "|".join(raw["inference_genes"]),
                        numeric_score,
                        "|".join(raw["pubmed_ids"]),
                        _hash_record(raw),
                    )
                )
                if len(batch) >= 2_000:
                    connection.executemany(
                        "INSERT OR IGNORE INTO ctd_records VALUES "
                        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        batch,
                    )
                    rows_processed += len(batch)
                    batch.clear()
        if batch:
            connection.executemany(
                "INSERT OR IGNORE INTO ctd_records VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                batch,
            )
            rows_processed += len(batch)
        connection.executescript(
            """
            CREATE INDEX idx_ctd_chemical
                ON ctd_records(chemical_id);
            CREATE INDEX idx_ctd_disease
                ON ctd_records(normalized_disease_name);
            CREATE INDEX idx_ctd_pair
                ON ctd_records(chemical_id, normalized_disease_name);
            """
        )
        row_count = int(
            connection.execute("SELECT COUNT(*) FROM ctd_records").fetchone()[0]
        )
        metadata = {
            "schema_version": CTD_INDEX_VERSION,
            "snapshot_id": snapshot_id,
            "source_path": str(source_path.resolve()),
            "source_release": source_release or source_path.name,
            "source_sha256": source_sha256,
            "source_size": source_path.stat().st_size,
            "rows_processed": rows_processed,
            "row_count": row_count,
            "built_at": utc_now().isoformat(),
        }
        connection.executemany(
            "INSERT INTO metadata (key, value) VALUES (?, ?)",
            [(key, str(value)) for key, value in metadata.items()],
        )
        connection.commit()
    finally:
        connection.close()
    os.replace(temporary, destination_path)
    ((destination_path.parent) / f"{destination_path.name}.manifest.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return metadata


class CtdEvidenceIndex:
    """Read-only lookup API over a CTD sidecar."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(f"CTD evidence index not found: {self.path}")
        self._metadata = self._read_metadata()
        self._validate_index()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"file:{self.path.as_posix()}?mode=ro", uri=True
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection

    def _read_metadata(self) -> dict[str, str]:
        try:
            with closing(self._connect()) as db:
                return dict(
                    db.execute("SELECT key, value FROM metadata").fetchall()
                )
        except sqlite3.DatabaseError as exc:
            raise ValueError(f"invalid CTD index metadata: {exc}") from exc

    def _validate_index(self) -> None:
        schema_version = self._metadata.get("schema_version")
        if schema_version not in SUPPORTED_CTD_INDEX_VERSIONS:
            raise ValueError(
                f"unsupported CTD index schema version: {schema_version}"
            )
        required_metadata = {
            "snapshot_id",
            "source_path",
            "source_sha256",
            "source_size",
            "rows_processed",
            "built_at",
        }
        if schema_version == CTD_INDEX_VERSION:
            required_metadata.update({"row_count", "source_release"})
        if missing := sorted(required_metadata - self._metadata.keys()):
            raise ValueError(f"CTD index metadata is missing: {missing}")
        manifest_path = self.path.parent / f"{self.path.name}.manifest.json"
        if not manifest_path.is_file():
            raise ValueError("CTD index manifest is missing")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid CTD index manifest: {exc}") from exc
        if not isinstance(manifest, dict):
            raise ValueError("invalid CTD index manifest: expected an object")
        for key in (
            "schema_version",
            "snapshot_id",
            "source_sha256",
            "source_size",
            "rows_processed",
        ):
            if str(manifest.get(key)) != self._metadata.get(key):
                raise ValueError(f"CTD index manifest mismatch for {key}")
        with closing(self._connect()) as db:
            columns = {
                row["name"]
                for row in db.execute("PRAGMA table_info(ctd_records)")
            }
            required_columns = {
                "chemical_id",
                "chemical_name",
                "disease_id",
                "disease_name",
                "normalized_disease_name",
                "direct_evidence",
                "inference_genes",
                "inference_score",
                "pubmed_ids",
                "content_hash",
            }
            if missing := sorted(required_columns - columns):
                raise ValueError(f"CTD index schema is missing columns: {missing}")
            actual_row_count = int(
                db.execute("SELECT COUNT(*) FROM ctd_records").fetchone()[0]
            )
        declared_row_count = int(
            self._metadata.get("row_count", self._metadata["rows_processed"])
        )
        if actual_row_count != declared_row_count:
            raise ValueError("CTD index row count does not match metadata")
        if "row_count" in self._metadata and str(
            manifest.get("row_count")
        ) != self._metadata.get("row_count"):
            raise ValueError("CTD index manifest mismatch for row_count")

    def metadata(self) -> dict[str, str]:
        return dict(self._metadata)

    def lookup(
        self,
        *,
        chemical_id: str,
        disease_name: str,
    ) -> list[CtdEvidenceRecord]:
        normalized = _normalize_name(disease_name)
        return [
            record
            for record in self.enumerate_diseases(chemical_id=chemical_id)
            if normalized
            in {_normalize_name(alias) for alias in record.disease_aliases}
        ]

    def enumerate_diseases(
        self, *, chemical_id: str
    ) -> list[CtdEvidenceRecord]:
        """Return every canonical disease record for a chemical, without a limit."""
        with closing(self._connect()) as db:
            rows = db.execute(
                """
                SELECT * FROM ctd_records
                WHERE chemical_id = ?
                ORDER BY disease_id, normalized_disease_name, disease_name,
                         content_hash
                """,
                (chemical_id,),
            ).fetchall()
        return self._records_from_rows(rows)

    def _records_from_rows(
        self, rows: list[sqlite3.Row]
    ) -> list[CtdEvidenceRecord]:
        grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in rows:
            identity_kind = "id" if row["disease_id"] else "name"
            identity = row["disease_id"] or row["normalized_disease_name"]
            key = (row["chemical_id"], identity_kind, identity)
            item = grouped.setdefault(
                key,
                {
                    "chemical_name": row["chemical_name"],
                    "disease_id": row["disease_id"],
                    "disease_names": set(),
                    "normalized_disease_name": row[
                        "normalized_disease_name"
                    ],
                    "direct_evidence": set(),
                    "inference_genes": set(),
                    "inference_scores": [],
                    "pubmed_ids": set(),
                    "content_hashes": [],
                },
            )
            item["disease_names"].add(row["disease_name"])
            item["direct_evidence"].update(_split_values(row["direct_evidence"]))
            item["inference_genes"].update(_split_values(row["inference_genes"]))
            if row["inference_score"] is not None:
                item["inference_scores"].append(float(row["inference_score"]))
            item["pubmed_ids"].update(_split_values(row["pubmed_ids"]))
            item["content_hashes"].append(row["content_hash"])
        records: list[CtdEvidenceRecord] = []
        for (chemical_id, _identity_kind, _identity), item in sorted(
            grouped.items(), key=lambda pair: pair[0]
        ):
            disease_names = sorted(
                item["disease_names"], key=lambda value: (value.casefold(), value)
            )
            disease_name = disease_names[0]
            disease_id = item["disease_id"]
            content_hash = _hash_record(
                {
                    "chemical_id": chemical_id,
                    "disease_name": disease_name,
                    "disease_aliases": disease_names,
                    "disease_id": disease_id,
                    "direct_evidence": sorted(item["direct_evidence"]),
                    "inference_genes": sorted(item["inference_genes"]),
                    "inference_score": max(
                        item["inference_scores"], default=None
                    ),
                    "pubmed_ids": sorted(item["pubmed_ids"]),
                    "row_hashes": sorted(item["content_hashes"]),
                }
            )
            records.append(
                CtdEvidenceRecord(
                    chemical_id=chemical_id,
                    chemical_name=item["chemical_name"],
                    disease_id=disease_id,
                    disease_name=disease_name,
                    disease_aliases=disease_names,
                    direct_evidence=sorted(item["direct_evidence"]),
                    inference_genes=sorted(item["inference_genes"]),
                    inference_score=max(
                        item["inference_scores"], default=None
                    ),
                    pubmed_ids=sorted(item["pubmed_ids"]),
                    source_snapshot_id=self._metadata["snapshot_id"],
                    source_record_locator=_record_locator(
                        chemical_id,
                        disease_id=disease_id,
                        normalized_disease_name=item[
                            "normalized_disease_name"
                        ],
                    ),
                    content_hash=content_hash,
                )
            )
        return records

    def read_record(
        self,
        *,
        source_snapshot_id: str,
        source_record_locator: str,
        content_hash: str,
    ) -> CtdEvidenceRecord:
        if source_snapshot_id != self._metadata["snapshot_id"]:
            raise ValueError("CTD source snapshot does not match index")
        chemical_id, identity_kind, identity = _parse_record_locator(
            source_record_locator
        )
        matches = []
        for record in self.enumerate_diseases(chemical_id=chemical_id):
            matches_id = (
                identity_kind == "id" and record.disease_id == identity
            )
            matches_name = (
                identity_kind == "name"
                and record.disease_id is None
                and _normalize_name(record.disease_name) == identity
            )
            if matches_id or matches_name:
                matches.append(record)
        if len(matches) != 1:
            raise ValueError("CTD source record locator is not unique")
        if matches[0].content_hash != content_hash:
            raise ValueError("CTD source record content hash does not match")
        return matches[0]


def audit_primekg_ctd_edge(
    edge: GraphEdge,
    *,
    index: CtdEvidenceIndex,
    crosswalk: CtdDiseaseCrosswalk | None = None,
    candidate_records: list[CtdEvidenceRecord] | None = None,
) -> CtdMappingAuditRecord:
    """Map one PrimeKG CTD exposure-disease edge to the CTD snapshot."""
    chemical_id = edge.head.native_id
    base = {
        "primekg_edge_id": edge.edge_id,
        "chemical_id": chemical_id,
        "chemical_name": edge.head.name,
        "primekg_disease_id": edge.tail.native_id,
        "primekg_disease_name": edge.tail.name,
    }
    if (
        edge.source != "CTD"
        or edge.relation != "exposure_disease"
        or edge.head.entity_type != "exposure"
        or edge.tail.entity_type != "disease"
        or not chemical_id
    ):
        return CtdMappingAuditRecord(
            **base,
            status=CtdMappingStatus.UNMAPPED,
            reason_codes=["ctd_edge_outside_supported_scope"],
        )
    candidates = (
        candidate_records
        if candidate_records is not None
        else index.enumerate_diseases(chemical_id=chemical_id)
    )
    disease_id = edge.tail.native_id
    if candidate_records is not None and any(
        record.chemical_id != chemical_id for record in candidate_records
    ):
        raise ValueError("CTD audit candidates belong to another chemical")
    normalized_name = _normalize_name(edge.tail.name)
    name_matches = [
        record
        for record in candidates
        if normalized_name
        in {_normalize_name(alias) for alias in record.disease_aliases}
    ]
    crosswalk_ids = (crosswalk or CtdDiseaseCrosswalk()).ctd_ids_for(
        disease_id
    )
    crosswalk_matches = [
        record for record in candidates if record.disease_id in crosswalk_ids
    ]
    if crosswalk_ids and len(crosswalk_matches) == 1:
        if name_matches and {
            record.source_record_locator for record in name_matches
        } != {crosswalk_matches[0].source_record_locator}:
            return _audit_ambiguous(
                base,
                [*name_matches, *crosswalk_matches],
                reason_code="ctd_crosswalk_name_conflict",
            )
        return _audit_match(
            base, CtdMappingStatus.MAPPED_CROSSWALK, crosswalk_matches
        )
    if crosswalk_ids and len(crosswalk_matches) > 1:
        return _audit_ambiguous(base, crosswalk_matches)
    if crosswalk_ids:
        return CtdMappingAuditRecord(
            **base,
            status=CtdMappingStatus.UNMAPPED,
            reason_codes=["ctd_crosswalk_unresolved"],
        )
    if len(name_matches) == 1:
        return _audit_match(base, CtdMappingStatus.MAPPED_EXACT, name_matches)
    if len(name_matches) > 1:
        return _audit_ambiguous(base, name_matches)
    return CtdMappingAuditRecord(
        **base,
        status=CtdMappingStatus.UNMAPPED,
        reason_codes=["ctd_mapping_unmapped"],
    )


def audit_primekg_ctd_edges(
    edges: Iterable[GraphEdge],
    *,
    index: CtdEvidenceIndex,
    crosswalk: CtdDiseaseCrosswalk | None = None,
) -> list[CtdMappingAuditRecord]:
    """Audit logical edges while enumerating each chemical only once."""
    edge_list = list(edges)
    positions_by_chemical: dict[str | None, list[int]] = {}
    for position, edge in enumerate(edge_list):
        positions_by_chemical.setdefault(edge.head.native_id, []).append(
            position
        )
    results: list[CtdMappingAuditRecord | None] = [None] * len(edge_list)
    for chemical_id, positions in positions_by_chemical.items():
        candidates = (
            index.enumerate_diseases(chemical_id=chemical_id)
            if chemical_id
            else []
        )
        for position in positions:
            results[position] = audit_primekg_ctd_edge(
                edge_list[position],
                index=index,
                crosswalk=crosswalk,
                candidate_records=candidates,
            )
    if any(record is None for record in results):
        raise RuntimeError("CTD mapping audit did not evaluate every edge")
    return [record for record in results if record is not None]


def _audit_match(
    base: dict[str, Any],
    status: CtdMappingStatus,
    records: list[CtdEvidenceRecord],
) -> CtdMappingAuditRecord:
    return CtdMappingAuditRecord(
        **base,
        status=status,
        ctd_record_locators=[
            record.source_record_locator
            for record in records
            if record.source_record_locator is not None
        ],
    )


def _audit_ambiguous(
    base: dict[str, Any],
    records: list[CtdEvidenceRecord],
    *,
    reason_code: str = "ctd_mapping_ambiguous",
) -> CtdMappingAuditRecord:
    return CtdMappingAuditRecord(
        **base,
        status=CtdMappingStatus.AMBIGUOUS,
        ctd_record_locators=sorted(
            {
                record.source_record_locator
                for record in records
                if record.source_record_locator is not None
            }
        ),
        reason_codes=[reason_code],
    )

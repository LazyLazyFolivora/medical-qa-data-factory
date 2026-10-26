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
from pathlib import Path
from typing import Any

from ..domain.common import utc_now
from .models import CtdEvidenceRecord


CTD_INDEX_VERSION = "ctd-evidence-index-v2"
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


def build_ctd_index(source: str | Path, destination: str | Path) -> dict[str, Any]:
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
    rows_written = 0
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
                    rows_written += len(batch)
                    batch.clear()
        if batch:
            connection.executemany(
                "INSERT OR IGNORE INTO ctd_records VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                batch,
            )
            rows_written += len(batch)
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
        metadata = {
            "schema_version": CTD_INDEX_VERSION,
            "snapshot_id": snapshot_id,
            "source_path": str(source_path.resolve()),
            "source_sha256": source_sha256,
            "source_size": source_path.stat().st_size,
            "rows_processed": rows_written,
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

    def metadata(self) -> dict[str, str]:
        with sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True) as db:
            return dict(db.execute("SELECT key, value FROM metadata").fetchall())

    def lookup(
        self,
        *,
        chemical_id: str,
        disease_name: str,
    ) -> list[CtdEvidenceRecord]:
        snapshot_id = self.metadata()["snapshot_id"]
        normalized = _normalize_name(disease_name)
        with sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                """
                SELECT * FROM ctd_records
                WHERE chemical_id = ? AND normalized_disease_name = ?
                ORDER BY content_hash
                """,
                (chemical_id, normalized),
            ).fetchall()
        grouped: dict[tuple[str, str, str | None], dict[str, Any]] = {}
        for row in rows:
            key = (row["chemical_id"], row["disease_name"], row["disease_id"])
            item = grouped.setdefault(
                key,
                {
                    "chemical_name": row["chemical_name"],
                    "direct_evidence": set(),
                    "inference_genes": set(),
                    "inference_scores": [],
                    "pubmed_ids": set(),
                    "content_hashes": [],
                },
            )
            item["direct_evidence"].update(_split_values(row["direct_evidence"]))
            item["inference_genes"].update(_split_values(row["inference_genes"]))
            if row["inference_score"] is not None:
                item["inference_scores"].append(float(row["inference_score"]))
            item["pubmed_ids"].update(_split_values(row["pubmed_ids"]))
            item["content_hashes"].append(row["content_hash"])
        records: list[CtdEvidenceRecord] = []
        for (chemical_id, disease_name, disease_id), item in sorted(
            grouped.items()
        ):
            content_hash = _hash_record(
                {
                    "chemical_id": chemical_id,
                    "disease_name": disease_name,
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
                    direct_evidence=sorted(item["direct_evidence"]),
                    inference_genes=sorted(item["inference_genes"]),
                    inference_score=max(
                        item["inference_scores"], default=None
                    ),
                    pubmed_ids=sorted(item["pubmed_ids"]),
                    source_snapshot_id=snapshot_id,
                    content_hash=content_hash,
                )
            )
        return records

"""Derived, read-only entity catalog for PrimeKG search.

PrimeKG stores entities only as repeated ``head`` and ``tail`` columns on an
8-million-row edge table. Exact lookups use the source indexes, while global
substring search needs a compact entity projection. The projection is built in
a sidecar SQLite database; the source database is never modified.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from ..domain.common import utc_now
from .contracts import GraphEntity
from .errors import GraphDataIntegrityError, GraphUnavailableError


CATALOG_SCHEMA_VERSION = "primekg-entity-catalog-v2"
SOURCE_BATCH_SIZE = 50_000


_TYPE_SEPARATOR = re.compile(r"[\s_]+")


def normalize_entity_type(value: str | None) -> str:
    """Mirror PrimeKG's canonical entity-type normalization exactly."""
    return _TYPE_SEPARATOR.sub(
        "-", (value or "unknown").strip().lower()
    )


def _normalize_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class PrimeKGEntityCatalog:
    """A snapshot-keyed entity search index derived from PrimeKG's edge table."""

    def __init__(
        self,
        database_path: str | Path,
        catalog_path: str | Path,
        *,
        provider_id: str = "primekg",
    ) -> None:
        self._database_path = Path(database_path)
        self._catalog_path = Path(catalog_path)
        self._provider_id = provider_id

    @property
    def path(self) -> Path:
        return self._catalog_path

    def ensure(self, *, snapshot_id: str) -> None:
        if self._is_current(snapshot_id):
            return
        self._build(snapshot_id)

    def _is_current(self, snapshot_id: str) -> bool:
        if not self._catalog_path.exists():
            return False
        try:
            with sqlite3.connect(
                f"file:{self._catalog_path.as_posix()}?mode=ro", uri=True
            ) as connection:
                row = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'schema_version'"
                ).fetchone()
                current = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'snapshot_id'"
                ).fetchone()
        except sqlite3.Error:
            return False
        return bool(
            row
            and current
            and row[0] == CATALOG_SCHEMA_VERSION
            and current[0] == snapshot_id
        )

    def _build(self, snapshot_id: str) -> None:
        if not self._database_path.exists():
            raise GraphUnavailableError(
                f"primekg database not found: {self._database_path}"
            )
        self._catalog_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._catalog_path.with_name(
            f".{self._catalog_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        source = sqlite3.connect(
            f"file:{self._database_path.as_posix()}?mode=ro", uri=True
        )
        target = sqlite3.connect(temporary)
        error: GraphDataIntegrityError | None = None
        try:
            source.execute("PRAGMA query_only = ON")
            target.executescript(
                """
                PRAGMA journal_mode = OFF;
                PRAGMA synchronous = OFF;
                CREATE TABLE entities (
                    canonical_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL,
                    native_id TEXT NOT NULL,
                    normalized_id TEXT NOT NULL,
                    entity_type TEXT NOT NULL
                );
                CREATE TABLE metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            cursor = source.execute(
                "SELECT head, head_id, head_type, tail, tail_id, tail_type "
                "FROM edges ORDER BY rowid"
            )
            inserted = 0
            while True:
                rows = cursor.fetchmany(SOURCE_BATCH_SIZE)
                if not rows:
                    break
                values: list[tuple[str, str, str, str, str, str]] = []
                for row in rows:
                    for name, native_id, entity_type in (
                        (row[0], row[1], row[2]),
                        (row[3], row[4], row[5]),
                    ):
                        clean_name = str(name or "").strip()
                        clean_id = str(native_id or "").strip()
                        clean_type = str(entity_type or "").strip()
                        if not clean_name or not clean_id or not clean_type:
                            continue
                        canonical_id = (
                            f"{self._provider_id}:"
                            f"{normalize_entity_type(clean_type)}:{clean_id}"
                        )
                        values.append(
                            (
                                canonical_id,
                                clean_name,
                                _normalize_text(clean_name),
                                clean_id,
                                clean_id.casefold(),
                                clean_type,
                            )
                        )
                if values:
                    target.executemany(
                        """
                        INSERT OR IGNORE INTO entities (
                            canonical_id, name, normalized_name, native_id,
                            normalized_id, entity_type
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        values,
                    )
                    inserted += len(values)
                target.commit()
            target.executescript(
                """
                CREATE INDEX idx_entities_normalized_name
                    ON entities(normalized_name);
                CREATE INDEX idx_entities_normalized_id
                    ON entities(normalized_id);
                CREATE INDEX idx_entities_type
                    ON entities(entity_type);
                """
            )
            built_at = utc_now().isoformat()
            target.executemany(
                "INSERT INTO metadata (key, value) VALUES (?, ?)",
                [
                    ("schema_version", CATALOG_SCHEMA_VERSION),
                    ("snapshot_id", snapshot_id),
                    ("built_at", built_at),
                    ("source_rows_processed", str(inserted)),
                ],
            )
            target.commit()
        except sqlite3.Error as exc:
            error = GraphDataIntegrityError(
                f"cannot build primekg entity catalog: {exc}"
            )
        finally:
            source.close()
            target.close()
        if error is not None:
            temporary.unlink(missing_ok=True)
            raise error
        try:
            os.replace(temporary, self._catalog_path)
        except OSError as exc:
            raise GraphUnavailableError(
                f"cannot publish primekg entity catalog: {exc}"
            ) from exc
        finally:
            temporary.unlink(missing_ok=True)

    def search(
        self,
        query: str,
        *,
        limit: int,
        entity_types: list[str] | None = None,
    ) -> tuple[list[GraphEntity], bool]:
        normalized_query = _normalize_text(query)
        if not normalized_query:
            return [], False
        escaped = _escape_like(normalized_query)
        contains = f"%{escaped}%"
        prefix = f"{escaped}%"
        clauses = [
            "(normalized_name LIKE ? ESCAPE '\\' "
            "OR normalized_id LIKE ? ESCAPE '\\')"
        ]
        params: list[Any] = [contains, contains]
        normalized_types = [
            normalize_entity_type(item) for item in entity_types or []
        ]
        if normalized_types:
            placeholders = ", ".join("?" for _ in normalized_types)
            clauses.append(
                "REPLACE(REPLACE(LOWER(entity_type), '_', '-'), ' ', '-') "
                f"IN ({placeholders})"
            )
            params.extend(normalized_types)
        params.extend([normalized_query, normalized_query, prefix, prefix])
        params.append(limit + 1)
        sql = (
            "SELECT canonical_id, name, native_id, entity_type "
            "FROM entities WHERE "
            + " AND ".join(clauses)
            + " ORDER BY CASE "
            "WHEN normalized_name = ? OR normalized_id = ? THEN 0 "
            "WHEN normalized_name LIKE ? ESCAPE '\\' "
            "OR normalized_id LIKE ? ESCAPE '\\' THEN 1 "
            "ELSE 2 END, entity_type COLLATE NOCASE, "
            "name COLLATE NOCASE, native_id LIMIT ?"
        )
        try:
            with sqlite3.connect(
                f"file:{self._catalog_path.as_posix()}?mode=ro", uri=True
            ) as connection:
                rows = connection.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            raise GraphDataIntegrityError(
                f"cannot query primekg entity catalog: {exc}"
            ) from exc
        truncated = len(rows) > limit
        entities = [
            GraphEntity(
                provider_id=self._provider_id,
                native_id=row[2],
                canonical_id=row[0],
                name=row[1],
                entity_type=row[3],
                metadata={"entity_type_raw": row[3]},
            )
            for row in rows[:limit]
        ]
        return entities, truncated

    def catalog_digest(self) -> str | None:
        if not self._catalog_path.exists():
            return None
        digest = hashlib.sha256()
        with self._catalog_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(8 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()

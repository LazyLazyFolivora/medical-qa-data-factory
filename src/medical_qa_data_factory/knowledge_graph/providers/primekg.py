"""Read-only PrimeKG SQLite adapter.

The database is treated as an immutable snapshot: read-only URI,
``PRAGMA query_only = ON``, parameterized SQL, and an explicit ``LIMIT`` on
every statement. Nothing here writes to, indexes, or vacuums the source.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ...domain.common import new_id, utc_now
from ..contracts import (
    EntityCandidate,
    EntityLookup,
    EntityResolution,
    EntitySearchQuery,
    EntitySearchResult,
    GraphCapabilities,
    GraphEdge,
    GraphEntity,
    GraphHealth,
    GraphQueryResult,
    GraphQueryTrace,
    GraphSnapshot,
    NeighborQuery,
    PathQuery,
)
from ..entity_catalog import PrimeKGEntityCatalog, normalize_entity_type
from ..errors import (
    GraphCapabilityError,
    GraphConfigurationError,
    GraphDataIntegrityError,
    GraphQueryError,
    GraphQueryTimeoutError,
    GraphUnavailableError,
)


ADAPTER_VERSION = "primekg-sqlite-v1"
PROVIDER_ID = "primekg"

#: Files above this size must carry an explicit manifest. Hashing the 1.63 GiB
#: production snapshot inside a normal query is never acceptable; hashing a test
#: fixture a few kilobytes wide is.
MANIFEST_REQUIRED_ABOVE_BYTES = 64 * 1024 * 1024

MANIFEST_SCHEMA_FIELDS = ("path", "size", "mtime_ns", "sha256", "created_at")

REQUIRED_COLUMNS = (
    "head",
    "relation",
    "tail",
    "head_id",
    "tail_id",
    "head_type",
    "tail_type",
)

SORT_COLUMNS = (
    "relation",
    "head_type",
    "head",
    "tail_type",
    "tail",
    "source",
    "head_id",
    "tail_id",
)

CURSOR_SEPARATOR = "\x1f"
def manifest_path_for(database_path: Path) -> Path:
    """Manifest lives beside the database as ``<database-name>.manifest.json``."""
    return database_path.with_name(f"{database_path.name}.manifest.json")


def read_manifest(database_path: Path) -> dict[str, Any] | None:
    path = manifest_path_for(database_path)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict):
        return None
    if any(field not in payload for field in MANIFEST_SCHEMA_FIELDS):
        return None
    return payload


def write_manifest(database_path: Path, *, sha256: str) -> dict[str, Any]:
    stat = database_path.stat()
    payload = {
        "path": str(database_path.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": sha256,
        "created_at": utc_now().isoformat(),
    }
    manifest_path_for(database_path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return payload


def init_manifest(database_path: Path) -> dict[str, Any]:
    """Compute the digest once, refusing to overwrite an existing manifest."""
    if not database_path.exists():
        raise GraphConfigurationError(f"primekg database not found: {database_path}")
    if manifest_path_for(database_path).exists():
        raise GraphConfigurationError(
            "manifest already exists; use `kg snapshot refresh` to recompute"
        )
    return write_manifest(database_path, sha256=_sha256_file(database_path))


def refresh_manifest(database_path: Path) -> dict[str, Any]:
    """Recompute the digest, keeping an audit backup of the previous manifest."""
    if not database_path.exists():
        raise GraphConfigurationError(f"primekg database not found: {database_path}")
    path = manifest_path_for(database_path)
    if path.exists():
        backup = path.with_name(f"{path.stem}.previous.json")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    return write_manifest(database_path, sha256=_sha256_file(database_path))


def _normalize_type(value: str | None) -> str:
    return normalize_entity_type(value)


def _stable_hash(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _sha256_file(path: Path, *, chunk_size: int = 8 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dedupe_edges(edges: Iterable[GraphEdge]) -> list[GraphEdge]:
    """Identical rows collapse into one logical edge, preserving first order."""
    seen: set[str] = set()
    result: list[GraphEdge] = []
    for edge in edges:
        if edge.edge_id in seen:
            continue
        seen.add(edge.edge_id)
        result.append(edge)
    return result


class PrimeKGSQLiteProvider:
    """Provider adapter over the local ``edges`` table."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        default_limit: int = 100,
        max_limit: int = 1000,
        max_hops: int = 2,
        query_timeout_ms: int = 10000,
        entity_catalog_path: str | Path | None = None,
        sha256: str | None = None,
        provider_id: str = PROVIDER_ID,
        snapshot_stale_tolerance: str = "error",
    ) -> None:
        self._database_path = Path(database_path)
        self._default_limit = default_limit
        self._max_limit = max_limit
        self._max_hops = max_hops
        self._query_timeout_ms = query_timeout_ms
        self._provider_id = provider_id
        self._connection: sqlite3.Connection | None = None
        self._snapshot: GraphSnapshot | None = None
        self._config_hash = _stable_hash(
            {
                "driver": "primekg_sqlite",
                "database_path": str(self._database_path.resolve()),
                "default_limit": default_limit,
                "max_limit": max_limit,
                "max_hops": max_hops,
                "query_timeout_ms": query_timeout_ms,
            }
        )
        self._snapshot_stale_tolerance = snapshot_stale_tolerance
        self._entity_catalog = (
            PrimeKGEntityCatalog(
                self._database_path,
                Path(entity_catalog_path),
                provider_id=provider_id,
            )
            if entity_catalog_path is not None
            else None
        )
        self._entity_search_lock = threading.Lock()
        self._file_sha256: str | None = sha256
        self._manifest_used = sha256 is not None
        self._stale_reason: str | None = None
        # Fail fast on a missing database rather than deferring the error into
        # the middle of a generation episode.
        if not self._database_path.exists():
            raise GraphConfigurationError(
                f"primekg database not found: {self._database_path}"
            )
        if not self._manifest_used:
            self._load_manifest_state()

    def _load_manifest_state(self) -> None:
        """Resolve the snapshot digest from a manifest, never from a full scan."""
        path = self._database_path
        if not path.exists():
            return
        manifest = read_manifest(path)
        if manifest is None:
            if path.stat().st_size >= MANIFEST_REQUIRED_ABOVE_BYTES:
                # A large snapshot without a manifest must not be hashed implicitly.
                self._stale_reason = "snapshot_manifest_missing"
            else:
                # A test-sized fixture is cheap to hash, so keep diagnostics honest.
                self._file_sha256 = _sha256_file(path)
                self._manifest_used = True
            return
        self._manifest_used = True
        stat = path.stat()
        if manifest.get("size") != stat.st_size or manifest.get("mtime_ns") != stat.st_mtime_ns:
            self._file_sha256 = None
            self._manifest_used = False
            self._stale_reason = "snapshot_stale"
            return
        self._file_sha256 = str(manifest.get("sha256") or "") or None

    def snapshot_state(self) -> dict[str, Any]:
        """Diagnostic view of how the snapshot digest was resolved."""
        return {
            "manifest_present": manifest_path_for(self._database_path).exists(),
            "manifest_used": self._manifest_used,
            "stale_reason": self._stale_reason,
            "sha256": self._file_sha256,
        }

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def database_path(self) -> Path:
        return self._database_path

    @property
    def config_hash(self) -> str:
        return self._config_hash

    def capabilities(self) -> GraphCapabilities:
        return GraphCapabilities(
            entity_lookup_by_id=True,
            entity_lookup_by_name=True,
            alias_lookup=False,
            fuzzy_lookup=False,
            entity_search=True,
            incoming_edges=True,
            outgoing_edges=True,
            # Filtered neighbor queries are what a project-side bounded
            # expansion is built from, so this capability is honest.
            bounded_expansion=True,
            # `paths()` ignores relation/type filters and cannot return a full
            # ordered path, so the capability is honestly advertised as absent.
            path_query=False,
            max_hops=0,
        )

    def _connect(self) -> sqlite3.Connection:
        if self._connection is not None:
            return self._connection
        if not self._database_path.exists():
            raise GraphConfigurationError(
                f"primekg database not found: {self._database_path}"
            )
        uri = f"file:{self._database_path.as_posix()}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
        except sqlite3.Error as exc:
            raise GraphUnavailableError(f"cannot open primekg database: {exc}") from exc
        self._connection = connection
        return connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def iter_edges_by_source(
        self,
        *,
        source: str,
        relation: str | None = None,
        page_size: int = 1_000,
    ) -> Iterable[GraphEdge]:
        """Yield every matching edge through deterministic bounded pages."""
        if page_size < 1:
            raise ValueError("page_size must be positive")
        connection = self._connect()
        last_rowid = 0
        while True:
            clauses = ["source = ?", "rowid > ?"]
            params: list[Any] = [source, last_rowid]
            if relation is not None:
                clauses.append("relation = ?")
                params.append(relation)
            params.append(page_size)
            rows = connection.execute(
                "SELECT rowid AS source_rowid, * FROM edges WHERE "
                + " AND ".join(clauses)
                + " ORDER BY rowid LIMIT ?",
                params,
            ).fetchall()
            if not rows:
                return
            for row in rows:
                yield self._edge_from_row(row)
            last_rowid = int(rows[-1]["source_rowid"])

    def __enter__(self) -> "PrimeKGSQLiteProvider":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def snapshot(self) -> GraphSnapshot:
        if self._snapshot is not None:
            return self._snapshot
        stat = self._database_path.stat()
        self._snapshot = GraphSnapshot(
            provider_id=self._provider_id,
            adapter_version=ADAPTER_VERSION,
            data_snapshot_id=_stable_hash(
                {
                    "sha256": self._file_sha256,
                    "size_bytes": stat.st_size,
                    "mtime": int(stat.st_mtime),
                }
            )[:32],
            sha256=self._file_sha256,
            size_bytes=stat.st_size,
            modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            config_hash=self._config_hash,
        )
        return self._snapshot

    def healthcheck(self) -> GraphHealth:
        try:
            connection = self._connect()
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                )
            }
            if "edges" not in tables:
                raise GraphDataIntegrityError("primekg database has no 'edges' table")
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(edges)")
            }
            missing = [name for name in REQUIRED_COLUMNS if name not in columns]
            if missing:
                raise GraphDataIntegrityError(
                    f"primekg 'edges' table missing columns: {missing}"
                )
            indexes = {
                row["name"] for row in connection.execute("PRAGMA index_list(edges)")
            }
            edge_count = connection.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            relation_count = connection.execute(
                "SELECT COUNT(DISTINCT relation) FROM edges"
            ).fetchone()[0]
            return GraphHealth(
                provider_id=self._provider_id,
                healthy=True,
                database_path=str(self._database_path),
                detail={
                    "edge_count": edge_count,
                    "relation_count": relation_count,
                    "indexes": sorted(indexes),
                    "adapter_version": ADAPTER_VERSION,
                    "read_only": True,
                },
                snapshot=self.snapshot(),
            )
        except Exception as exc:  # noqa: BLE001 - healthcheck never raises
            return GraphHealth(
                provider_id=self._provider_id,
                healthy=False,
                database_path=str(self._database_path),
                error={
                    "code": getattr(exc, "code", "kg_error"),
                    "message": str(exc),
                    "retryable": getattr(exc, "retryable", False),
                },
            )

    def _entity_from_row(
        self, *, name: str, native_id: str | None, entity_type: str | None
    ) -> GraphEntity:
        normalized_type = _normalize_type(entity_type)
        metadata: dict[str, Any] = {"entity_type_raw": entity_type}
        if native_id:
            canonical_id = f"{self._provider_id}:{normalized_type}:{native_id}"
        else:
            digest = hashlib.sha256(
                f"{normalized_type}|{name}".encode("utf-8")
            ).hexdigest()[:32]
            canonical_id = (
                f"{self._provider_id}:{normalized_type}:name-sha256:{digest}"
            )
            metadata["identity_fallback"] = True
        return GraphEntity(
            provider_id=self._provider_id,
            native_id=native_id,
            canonical_id=canonical_id,
            name=name,
            entity_type=entity_type,
            metadata=metadata,
        )

    def _edge_from_row(self, row: sqlite3.Row) -> GraphEdge:
        head = self._entity_from_row(
            name=row["head"],
            native_id=row["head_id"],
            entity_type=row["head_type"],
        )
        tail = self._entity_from_row(
            name=row["tail"],
            native_id=row["tail_id"],
            entity_type=row["tail_type"],
        )
        payload = CURSOR_SEPARATOR.join(
            [
                self._provider_id,
                head.canonical_id,
                row["relation"],
                tail.canonical_id,
                row["source"] or "",
            ]
        )
        return GraphEdge(
            provider_id=self._provider_id,
            edge_id=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            head=head,
            relation=row["relation"],
            tail=tail,
            source=row["source"],
            metadata={
                "head_type": row["head_type"],
                "tail_type": row["tail_type"],
                "native_head_id": row["head_id"],
                "native_tail_id": row["tail_id"],
            },
        )

    def _trace(
        self,
        *,
        operation: str,
        request: dict[str, Any],
        started: float,
        started_at: datetime,
        records_returned: int,
        truncated: bool,
        rows_examined: int | None = None,
    ) -> GraphQueryTrace:
        return GraphQueryTrace(
            query_id=new_id("kg_query"),
            provider_id=self._provider_id,
            operation=operation,
            normalized_request=request,
            started_at=started_at,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            rows_examined=rows_examined,
            records_returned=records_returned,
            truncated=truncated,
        )

    def _select(
        self, sql: str, params: Iterable[Any], limit: int
    ) -> tuple[list[sqlite3.Row], bool]:
        connection = self._connect()
        deadline = time.perf_counter() + self._query_timeout_ms / 1000
        try:
            connection.set_progress_handler(
                lambda: 1 if time.perf_counter() > deadline else 0, 2000
            )
            rows = connection.execute(sql, tuple(params)).fetchall()
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "interrupted" in message or "timeout" in message:
                raise GraphQueryTimeoutError(str(exc)) from exc
            raise GraphQueryError(f"primekg query failed: {exc}") from exc
        except sqlite3.DatabaseError as exc:
            raise GraphDataIntegrityError(f"primekg database error: {exc}") from exc
        finally:
            connection.set_progress_handler(None, 0)
        truncated = len(rows) > limit
        return rows[:limit], truncated

    def _select_ordered(
        self,
        *,
        clauses: list[str],
        params: list[Any],
        limit: int,
        cursor: str | None = None,
    ) -> tuple[list[sqlite3.Row], bool]:
        where = " AND ".join(clauses)
        sql = f"SELECT * FROM edges WHERE {where}"
        if cursor:
            # Cursor carries the last seen sort key; callers must not parse it.
            key = cursor.split(CURSOR_SEPARATOR)
            if len(key) != len(SORT_COLUMNS):
                raise GraphQueryError("malformed cursor")
            placeholders = ", ".join("?" * len(SORT_COLUMNS))
            sql += f" AND ({', '.join(SORT_COLUMNS)}) > ({placeholders})"
            params = [*params, *key]
        sql += f" ORDER BY {', '.join(SORT_COLUMNS)} LIMIT ?"
        return self._select(sql, [*params, limit + 1], limit)

    def resolve_entity(self, query: EntityLookup) -> EntityResolution:
        started = time.perf_counter()
        started_at = utc_now()
        clauses: list[str] = []
        params: list[Any] = []
        # Identity and type must be correlated on the *same* endpoint. Two
        # independent ORs would let disease:8315 match a gene/protein:8315 row.
        normalized_types = [_normalize_type(item) for item in query.entity_types]
        type_sql = ""
        if normalized_types:
            placeholders = ", ".join("?" * len(normalized_types))
            # Mirror ``_normalize_type`` in SQL so the database narrows rows
            # using exactly the same form the Python post-filter compares.
            type_sql = (
                " AND REPLACE(LOWER({side}_type), '_', '-') "
                f"IN ({placeholders})"
            )
        side_clauses: list[str] = []
        for side in ("head", "tail"):
            if query.native_id:
                identity = f"{side}_id = ?"
                values: list[Any] = [query.native_id]
            else:
                identity = f"{side} = ? COLLATE NOCASE"
                values = [query.mention]
            rendered = type_sql.format(side=side)
            side_clauses.append(f"({identity}{rendered})")
            params.extend(values)
            params.extend(normalized_types)
        clauses.append("(" + " OR ".join(side_clauses) + ")")
        rows, truncated = self._select_ordered(
            clauses=clauses,
            params=params,
            limit=min(query.limit * 20, self._max_limit),
        )
        found: dict[str, GraphEntity] = {}
        for row in rows:
            for side in ("head", "tail"):
                if query.native_id:
                    if row[f"{side}_id"] != query.native_id:
                        continue
                elif row[side].strip().lower() != query.mention.strip().lower():
                    continue
                if normalized_types and (
                    _normalize_type(row[f"{side}_type"]) not in normalized_types
                ):
                    continue
                entity = self._entity_from_row(
                    name=row[side],
                    native_id=row[f"{side}_id"],
                    entity_type=row[f"{side}_type"],
                )
                found[entity.canonical_id] = entity
        candidates = [
            EntityCandidate(
                entity=entity,
                match_kind="id" if query.native_id else "exact",
                score=1.0,
            )
            for entity in found.values()
        ]
        candidates.sort(
            key=lambda item: (
                item.entity.entity_type or "",
                item.entity.name,
                item.entity.canonical_id,
            )
        )
        if len(candidates) > query.limit:
            candidates = candidates[: query.limit]
            truncated = True
        trace = self._trace(
            operation="resolve_entity",
            request={
                "mention": query.mention,
                "native_id": query.native_id,
                "entity_types": query.entity_types,
                "limit": query.limit,
            },
            started=started,
            started_at=started_at,
            records_returned=len(candidates),
            truncated=truncated,
            rows_examined=len(rows),
        )
        return EntityResolution(
            candidates=candidates,
            selected=candidates[0].entity if len(candidates) == 1 else None,
            ambiguous=len(candidates) > 1,
            trace=trace,
        )

    def search_entities(self, query: EntitySearchQuery) -> EntitySearchResult:
        """Browse the first usable endpoints or search the full entity catalog."""
        started = time.perf_counter()
        started_at = utc_now()
        limit = min(query.limit, self._max_limit)
        text = query.query.strip()
        rows_examined: int | None = None
        if text:
            if self._entity_catalog is None:
                raise GraphConfigurationError(
                    "primekg entity search requires an entity catalog path"
                )
            with self._entity_search_lock:
                self._entity_catalog.ensure(
                    snapshot_id=self.snapshot().data_snapshot_id
                )
                entities, truncated = self._entity_catalog.search(
                    text,
                    limit=limit,
                    entity_types=query.entity_types,
                )
        else:
            entities, truncated, rows_examined = self._browse_entities(
                limit=limit, entity_types=query.entity_types
            )
        trace = self._trace(
            operation="search_entities",
            request={
                "query": text,
                "entity_types": query.entity_types,
                "limit": limit,
            },
            started=started,
            started_at=started_at,
            records_returned=len(entities),
            truncated=truncated,
            rows_examined=rows_examined,
        )
        return EntitySearchResult(
            entities=entities,
            trace=trace,
            snapshot=self.snapshot(),
            truncated=truncated,
        )

    def _browse_entities(
        self, *, limit: int, entity_types: list[str]
    ) -> tuple[list[GraphEntity], bool, int]:
        """Read a bounded edge window and return its first unique endpoints."""
        wanted_types = {
            _normalize_type(item).replace("-", "_") for item in entity_types
        }
        connection = self._connect()
        last_rowid = 0
        entities: list[GraphEntity] = []
        seen: set[str] = set()
        rows_examined = 0
        while len(entities) <= limit and rows_examined < 400:
            rows = connection.execute(
                "SELECT rowid, head, head_id, head_type, tail, tail_id, tail_type "
                "FROM edges WHERE rowid > ? ORDER BY rowid LIMIT 40",
                (last_rowid,),
            ).fetchall()
            if not rows:
                break
            rows_examined += len(rows)
            last_rowid = int(rows[-1]["rowid"])
            for row in rows:
                for name, native_id, entity_type in (
                    (row["head"], row["head_id"], row["head_type"]),
                    (row["tail"], row["tail_id"], row["tail_type"]),
                ):
                    if not name or not native_id or not entity_type:
                        continue
                    normalized_type = _normalize_type(entity_type)
                    if (
                        wanted_types
                        and normalized_type.replace("-", "_") not in wanted_types
                    ):
                        continue
                    entity = self._entity_from_row(
                        name=str(name),
                        native_id=str(native_id),
                        entity_type=str(entity_type),
                    )
                    if entity.canonical_id in seen:
                        continue
                    seen.add(entity.canonical_id)
                    entities.append(entity)
                    if len(entities) > limit:
                        return entities[:limit], True, rows_examined
        return entities[:limit], False, rows_examined

    def neighbors(self, query: NeighborQuery) -> GraphQueryResult:
        started = time.perf_counter()
        started_at = utc_now()
        entity = query.entity
        limit = min(query.limit, self._max_limit)
        if query.target_types and query.direction == "both":
            # A target type can only bind one endpoint. Split into two bounded
            # queries, each with its own budget, then merge and deduplicate.
            outgoing_budget = limit // 2 + limit % 2
            incoming_budget = limit // 2
            parts: list[GraphQueryResult] = []
            for direction, budget in (
                ("outgoing", outgoing_budget),
                ("incoming", incoming_budget),
            ):
                if budget <= 0:
                    continue
                parts.append(
                    self.neighbors(
                        NeighborQuery(
                            entity=entity,
                            direction=direction,  # type: ignore[arg-type]
                            relations=list(query.relations),
                            target_types=list(query.target_types),
                            limit=budget,
                            cursor=query.cursor,
                        )
                    )
                )
            merged = _dedupe_edges(
                edge for part in parts for edge in part.edges
            )
            return GraphQueryResult(
                edges=merged,
                trace=self._trace(
                    operation="neighbors",
                    request={
                        "entity": entity.canonical_id,
                        "direction": "both",
                        "relations": query.relations,
                        "target_types": query.target_types,
                        "limit": limit,
                        "cursor": query.cursor,
                        "split": True,
                    },
                    started=started,
                    started_at=started_at,
                    records_returned=len(merged),
                    truncated=any(part.truncated for part in parts),
                    rows_examined=sum(
                        part.trace.rows_examined or 0 for part in parts
                    ),
                ),
                snapshot=self.snapshot(),
                truncated=any(part.truncated for part in parts),
            )
        clauses: list[str] = []
        params: list[Any] = []
        sides: list[tuple[str, Any]] = []
        if entity.native_id:
            if query.direction in ("outgoing", "both"):
                sides.append(("head_id = ?", entity.native_id))
            if query.direction in ("incoming", "both"):
                sides.append(("tail_id = ?", entity.native_id))
        else:
            if query.direction == "outgoing":
                sides.append(("head = ?", entity.name))
            elif query.direction == "incoming":
                sides.append(("tail = ?", entity.name))
            else:
                sides.append(("head = ?", entity.name))
                sides.append(("tail = ?", entity.name))
        bound_columns: list[str] = []
        if entity.native_id:
            if query.direction in ("outgoing", "both"):
                bound_columns.append("head_type")
            if query.direction in ("incoming", "both"):
                bound_columns.append("tail_type")
        elif query.direction == "outgoing":
            bound_columns.append("head_type")
        elif query.direction == "incoming":
            bound_columns.append("tail_type")
        else:
            bound_columns.extend(["head_type", "tail_type"])
        if entity.entity_type:
            # The seed's own type constrains the *bound* endpoint only, so a
            # drug never collects gene/protein rows that share a native id.
            # Each type predicate stays paired with its own identity predicate.
            if bound_columns:
                pairs: list[str] = []
                values: list[Any] = []
                for (side_sql, side_value), column in zip(
                    sides, bound_columns, strict=False
                ):
                    pairs.append(
                        f"({side_sql} AND REPLACE(LOWER({column}), '_', '-') = ?)"
                    )
                    values.append(side_value)
                    values.append(_normalize_type(entity.entity_type))
                clauses.append("(" + " OR ".join(pairs) + ")")
                params.extend(values)
        else:
            clauses.append(
                "(" + " OR ".join(item[0] for item in sides) + ")"
            )
            params.extend(item[1] for item in sides)
        if query.relations:
            placeholders = ", ".join("?" * len(query.relations))
            clauses.append(f"relation IN ({placeholders})")
            params.extend(query.relations)
        if query.target_types:
            # An outgoing query binds the head identity, so only the tail may
            # be filtered; an incoming query binds the tail identity, so only
            # the head may be filtered.
            target_side = "tail_type" if query.direction == "outgoing" else "head_type"
            placeholders = ", ".join("?" * len(query.target_types))
            clauses.append(
                f"REPLACE(LOWER({target_side}), '_', '-') IN ({placeholders})"
            )
            params.extend(_normalize_type(item) for item in query.target_types)
        rows, truncated = self._select_ordered(
            clauses=clauses, params=params, limit=limit, cursor=query.cursor
        )
        edges = _dedupe_edges(self._edge_from_row(row) for row in rows)
        next_cursor = None
        if truncated and rows:
            last = rows[-1]
            next_cursor = CURSOR_SEPARATOR.join(
                str(last[column] or "") for column in SORT_COLUMNS
            )
        trace = self._trace(
            operation="neighbors",
            request={
                "entity": entity.canonical_id,
                "direction": query.direction,
                "relations": query.relations,
                "target_types": query.target_types,
                "limit": limit,
                "cursor": query.cursor,
            },
            started=started,
            started_at=started_at,
            records_returned=len(edges),
            truncated=truncated,
            rows_examined=len(rows),
        )
        return GraphQueryResult(
            edges=edges,
            trace=trace,
            snapshot=self.snapshot(),
            next_cursor=next_cursor,
            truncated=truncated,
        )

    def paths(self, query: PathQuery) -> GraphQueryResult:
        """Path traversal is disabled; see FR-003 in the implementation spec."""
        raise GraphCapabilityError(
            "primekg path query is not reliable and is disabled; "
            "query bounded 1-hop neighbors instead"
        )

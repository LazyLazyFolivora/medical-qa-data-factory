"""Bounded SQLite navigation for PrimeKG, separate from evidence materialization.

API concepts informed by K-Dense's PrimeKG helper; implementation uses the
existing read-only SQLite provider, not pandas or the upstream CSV schema.
"""
import base64
import hashlib
import json

from .contracts import EntityLookup, EntitySearchQuery, GraphQueryResult
from .errors import GraphQueryError


NAVIGATION_OPERATIONS = {"overview", "search_nodes", "list_neighbors"}


def _identity(canonical_id, provider_id):
    if not isinstance(canonical_id, str):
        raise GraphQueryError("entity_id and target_ids entries must be canonical ID strings")
    parts = canonical_id.split(":", 2)
    if len(parts) != 3 or parts[0] != provider_id or not all(parts[1:]):
        raise GraphQueryError("Use a canonical entity ID returned by this PrimeKG snapshot")
    return parts[2], parts[1]


class PrimeKGBrowser:
    def __init__(self, provider):
        self.provider = provider

    def _select(self, sql, params, limit):
        return self.provider._select(sql, params, limit)

    @staticmethod
    def validate_query(query):
        if not isinstance(query, dict):
            raise GraphQueryError("PrimeKG query must be an object")
        for field in ("operation", "entity_id", "mention", "native_id", "direction", "cursor"):
            if field in query and not isinstance(query[field], str):
                raise GraphQueryError(f"query.{field} must be text")
        for field in ("entity_types", "target_types", "relations", "target_ids"):
            if field in query and (not isinstance(query[field], list) or
                    any(not isinstance(item, str) or not item for item in query[field])):
                raise GraphQueryError(f"query.{field} must be an array of nonempty strings")
        cursors = query.get("cursors", {})
        if not isinstance(cursors, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in cursors.items()):
            raise GraphQueryError("query.cursors must map relation names to cursor strings")

    def resolve(self, query):
        native_id, types = query.get("native_id"), query.get("entity_types", [])
        if query.get("entity_id"):
            native_id, kind = _identity(query["entity_id"], self.provider.provider_id)
            types = [kind]
        return self.provider.resolve_entity(EntityLookup(
            mention=query.get("mention") or native_id or "unknown", native_id=native_id,
            entity_types=types, limit=10))

    def _adjacency(self, entity, direction):
        if direction not in {"both", "incoming", "outgoing"}:
            raise GraphQueryError("direction must be both, incoming or outgoing")
        statements, params = [], []
        for side, other, label in (("head", "tail", "outgoing"), ("tail", "head", "incoming")):
            if direction not in {"both", label}:
                continue
            statements.append(f"SELECT relation, {other} AS name, {other}_id AS native_id, "
                f"{other}_type AS entity_type FROM edges WHERE {side}_id = ? "
                f"AND REPLACE(LOWER({side}_type), '_', '-') = ?")
            params.extend([entity.native_id, entity.entity_type.lower().replace("_", "-")])
        return " UNION ALL ".join(statements), params

    def _scope(self, query):
        value = {k: v for k, v in query.items() if k not in {"cursor", "cursors", "page_size"}}
        value["snapshot"] = self.provider.snapshot().data_snapshot_id
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:24]

    def _cursor(self, query, key):
        return base64.urlsafe_b64encode(json.dumps({"scope": self._scope(query), "key": key}).encode()).decode()

    def _read_cursor(self, query, token):
        try:
            value = json.loads(base64.urlsafe_b64decode(token.encode()))
            if value["scope"] != self._scope(query):
                raise ValueError("scope")
            return value["key"]
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise GraphQueryError("Invalid cursor or cursor belongs to another query/snapshot") from exc

    def overview(self, query):
        resolution = self.resolve(query)
        result = {"resolution": resolution.model_dump(mode="json"), "relations": []}
        if not resolution.selected:
            return result
        entity = resolution.selected
        adjacency, params = self._adjacency(entity, query.get("direction", "both"))
        rows, truncated = self._select(
            "WITH adjacency AS (" + adjacency + "), objects AS ("
            "SELECT relation, entity_type, native_id, COUNT(*) AS stored_rows FROM adjacency "
            "GROUP BY relation, entity_type, native_id) "
            "SELECT relation, entity_type AS target_type, COUNT(*) AS distinct_objects, "
            "SUM(stored_rows) AS stored_endpoint_rows FROM objects "
            "GROUP BY relation, entity_type ORDER BY relation, entity_type LIMIT 201", params, 200)
        result.update(entity=entity.model_dump(mode="json"), relations=[dict(r) for r in rows],
                      truncated=truncated, snapshot_id=self.provider.snapshot().data_snapshot_id)
        return result

    def navigate(self, query):
        self.validate_query(query)
        operation = query.get("operation")
        if operation == "overview":
            result = self.overview(query)
        elif operation == "search_nodes":
            search = self.provider.search_entities(EntitySearchQuery(query=query.get("mention", ""),
                entity_types=query.get("entity_types", []), limit=self._page_size(query)))
            result = {"entities": [e.model_dump(mode="json") for e in search.entities],
                      "truncated": search.truncated,
                      "instruction": "If truncated, narrow the name/type; this search is not the complete entity universe."}
        elif operation == "list_neighbors":
            result = self.list_neighbors(query)
        else:
            raise GraphQueryError("Unknown PrimeKG navigation operation")
        return {"provider_id": self.provider.provider_id, "operation": operation,
                "status": "navigation_not_evidence", "snapshot_id": self.provider.snapshot().data_snapshot_id,
                "identity_policy": "Current SQLite identity is type + native_id; original per-node namespaces/display_relation are not stored.",
                "meaning": "Catalog for choosing research leads, not proof of causality or clinical efficacy. Stored orientation is not biological direction.",
                **result}

    @staticmethod
    def _page_size(query):
        size = query.get("page_size", 20)
        if type(size) is not int or not 1 <= size <= 50:
            raise GraphQueryError("page_size must be 1-50 per relation")
        return size

    def list_neighbors(self, query):
        resolution = self.resolve(query)
        if not resolution.selected:
            return {"resolution": resolution.model_dump(mode="json"), "groups": []}
        relations = query.get("relations", [])
        if not isinstance(relations, list) or not 1 <= len(relations) <= 6 or any(not isinstance(r, str) or not r for r in relations):
            raise GraphQueryError("Choose 1-6 relation names from the overview")
        page_size = self._page_size(query)
        entity = resolution.selected
        adjacency, params = self._adjacency(entity, query.get("direction", "both"))
        groups = []
        for relation in dict.fromkeys(relations):
            scope = {**query, "relations": [relation]}
            clauses, values = ["relation = ?"], [*params, relation]
            types = query.get("target_types", [])
            if types:
                clauses.append("entity_type IN (" + ",".join("?" for _ in types) + ")")
                values.extend(types)
            cte = "WITH adjacency AS (" + adjacency + "), objects AS (SELECT native_id, entity_type, MIN(name) AS name, COUNT(DISTINCT name) AS label_count FROM adjacency WHERE " + " AND ".join(clauses) + " GROUP BY native_id, entity_type) "
            totals, _ = self._select(cte + "SELECT COUNT(*) AS total FROM objects LIMIT 1", values, 1)
            token = query.get("cursors", {}).get(relation)
            after = ""
            if token:
                key = self._read_cursor(scope, token)
                if not isinstance(key, list) or len(key) != 3 or any(not isinstance(v, str) for v in key):
                    raise GraphQueryError("Invalid neighbor cursor key")
                after = " WHERE (name, entity_type, native_id) > (?, ?, ?)"
                values.extend(key)
            rows, truncated = self._select(cte + "SELECT * FROM objects" + after +
                " ORDER BY name, entity_type, native_id LIMIT ?", [*values, page_size + 1], page_size)
            entries = []
            for row in rows:
                node = self.provider._entity_from_row(name=row["name"], native_id=row["native_id"], entity_type=row["entity_type"])
                entries.append({"entity_id": node.canonical_id, "native_id": node.native_id,
                                "entity_type": node.entity_type, "name": node.name, "label_count": row["label_count"]})
            next_cursor = self._cursor(scope, [rows[-1][k] for k in ("name", "entity_type", "native_id")]) if truncated else None
            groups.append({"relation": relation, "total_distinct_objects": totals[0]["total"],
                           "returned_count": len(entries), "objects": entries,
                           "truncated": truncated, "next_cursor": next_cursor})
        return {"entity": entity.model_dump(mode="json"), "groups": groups,
                "ordering": "Name/type/ID order for navigation, not importance or relevance. Each relation has an independent page."}

    def get_relations(self, query, limit):
        self.validate_query(query)
        resolution = self.resolve(query)
        if not resolution.selected:
            raise GraphQueryError("Entity unresolved or ambiguous; use overview/search_nodes and select a typed ID")
        entity = resolution.selected
        targets = query.get("target_ids", [])
        if not isinstance(targets, list) or not 1 <= len(targets) <= 8:
            raise GraphQueryError("get_relations requires 1-8 target_ids from the directory")
        direction = query.get("direction", "both")
        if direction not in {"both", "incoming", "outgoing"}:
            raise GraphQueryError("Invalid direction")
        clauses, params = [], []
        for side, other, label in (("head", "tail", "outgoing"), ("tail", "head", "incoming")):
            if direction not in {"both", label}:
                continue
            target_clauses = []
            values = [entity.native_id, entity.entity_type.lower().replace("_", "-")]
            for target in targets:
                native_id, kind = _identity(target, self.provider.provider_id)
                target_clauses.append(f"({other}_id = ? AND REPLACE(LOWER({other}_type), '_', '-') = ?)")
                values.extend([native_id, kind])
            clauses.append(f"({side}_id = ? AND REPLACE(LOWER({side}_type), '_', '-') = ? AND (" + " OR ".join(target_clauses) + "))")
            params.extend(values)
        where = "(" + " OR ".join(clauses) + ")"
        relations = query.get("relations", [])
        if relations:
            where += " AND relation IN (" + ",".join("?" for _ in relations) + ")"
            params.extend(relations)
        if query.get("cursor"):
            key = self._read_cursor(query, query["cursor"])
            if type(key) is not int or key < 0:
                raise GraphQueryError("Invalid relation cursor")
            where += " AND rowid > ?"
            params.append(key)
        rows, truncated = self._select("SELECT rowid AS stored_rowid, * FROM edges WHERE " + where +
            " ORDER BY rowid LIMIT ?", [*params, limit + 1], limit)
        # Preserve stored orientation; reverse records are not independent evidence.
        edges = {e.edge_id: e for e in (self.provider._edge_from_row(row) for row in rows)}
        from ..domain.common import utc_now
        from .contracts import GraphQueryTrace
        return GraphQueryResult(edges=list(edges.values()), snapshot=self.provider.snapshot(),
            truncated=truncated, next_cursor=self._cursor(query, rows[-1]["stored_rowid"]) if truncated else None,
            trace=GraphQueryTrace(query_id="primekg-selected", operation="get_relations", normalized_request=query,
                provider_id=self.provider.provider_id, started_at=utc_now(), records_returned=len(edges),
                rows_examined=len(rows), truncated=truncated))

"""Incremental PostgreSQL runtime records and immutable filesystem content.

The runtime still exposes full domain snapshots in memory. Persistence separates
collections into rows, externalizes large values, and appends only changed rows.
No automatic garbage collection is performed by this module.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
from threading import Lock

from psycopg2.extras import Json

from .objective_question_contracts import canonical_digest


COLLECTIONS = frozenset({
    "artifacts", "decisions", "operations", "child_slots", "hypotheses",
    "aggregation_commits",
})
ZERO_DIGEST = "0" * 64
_databases = {}
_database_lock = Lock()


class StorageIntegrityError(RuntimeError):
    pass


class StorageConflict(RuntimeError):
    pass


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def sync_directory(path):
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def atomic_write(path, content):
    """Publish a complete file; temporary and destination share a filesystem."""
    path = Path(path)
    missing = []
    directory = path.parent
    while not directory.exists():
        missing.append(directory)
        directory = directory.parent
    for directory in reversed(missing):
        directory.mkdir(exist_ok=True)
        sync_directory(directory.parent)
    descriptor, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class ContentStore:
    def __init__(self, root, *, threshold=16384):
        self.root = Path(root)
        self.threshold = threshold

    def path(self, digest):
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise StorageIntegrityError("invalid content digest")
        return self.root / digest[:2] / digest[2:4] / digest

    def pack(self, value):
        content = encode(value)
        if len(content) <= self.threshold:
            return {"inline": value}
        digest = sha256(content).hexdigest()
        path = self.path(digest)
        if path.exists():
            if sha256(path.read_bytes()).hexdigest() != digest:
                raise StorageIntegrityError(f"content digest differs: {digest}")
        else:
            atomic_write(path, content)
        return {"blob": digest, "bytes": len(content)}

    def unpack(self, reference):
        if set(reference) == {"inline"}:
            return deepcopy(reference["inline"])
        if set(reference) != {"blob", "bytes"}:
            raise StorageIntegrityError("invalid content reference")
        path = self.path(reference["blob"])
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise StorageIntegrityError(f"content unavailable: {reference['blob']}") from exc
        if len(content) != reference["bytes"] or sha256(content).hexdigest() != reference["blob"]:
            raise StorageIntegrityError(f"content digest differs: {reference['blob']}")
        return json.loads(content)


def pack_snapshot(snapshot, content):
    def pack_item(key, item):
        if key == "artifacts":
            return {"envelope": {k: v for k, v in item.items() if k != "payload"},
                    "payload": content.pack(item["payload"])}
        return content.pack(item)

    return {
        key: ({item_key: pack_item(key, item) for item_key, item in value.items()}
              if key in COLLECTIONS else content.pack(value))
        for key, value in snapshot.items()
    }


def unpack_snapshot(snapshot, content):
    def unpack_item(key, item):
        if key == "artifacts":
            return {**deepcopy(item["envelope"]), "payload": content.unpack(item["payload"])}
        return content.unpack(item)

    return {
        key: ({item_key: unpack_item(key, item) for item_key, item in value.items()}
              if key in COLLECTIONS else content.unpack(value))
        for key, value in snapshot.items()
    }


def difference(previous, current):
    """Explicit replacement/removal deltas; absent and null remain distinct."""
    changes = {}
    for key in previous.keys() | current.keys():
        if key not in current:
            changes[key] = {"remove": True}
        elif key in COLLECTIONS:
            old, new = previous.get(key, {}), current[key]
            updates = {k: v for k, v in new.items() if k not in old or old[k] != v}
            removed = sorted(old.keys() - new.keys())
            if updates or removed or key not in previous:
                changes[key] = {"items": updates, "removed": removed}
        elif key not in previous or previous[key] != current[key]:
            changes[key] = {"value": current[key]}
    return changes


def apply_delta(previous, changes):
    updated = deepcopy(previous)
    for key, change in changes.items():
        if change.get("remove"):
            updated.pop(key, None)
        elif key in COLLECTIONS:
            items = updated.setdefault(key, {})
            for removed in change["removed"]:
                items.pop(removed, None)
            items.update(change["items"])
        else:
            updated[key] = change["value"]
    return updated


def register_database(database):
    """Share the application's pool; never persist credentials in run markers."""
    with _database_lock:
        _databases[database.schema] = database


def database_for_workspace(workspace):
    from ..web.production_db import ProductionDatabase
    candidate = ProductionDatabase(workspace)
    with _database_lock:
        return _databases.setdefault(candidate.schema, candidate)


def runtime_updated_at(root, run_id):
    store = PostgresRuntimeStore.open(root, run_id)
    if store is not None:
        return store.updated_at()
    return None


class PostgresRuntimeStore:
    def __init__(self, database, run_id, content):
        self.database = database
        self.run_id = run_id
        self.content = content

    @staticmethod
    def marker(root):
        return Path(root) / "objective_runtime" / "storage.json"

    @classmethod
    def prepare(cls, root, run_id, database, content_root):
        root = Path(root)
        marker = cls.marker(root)
        if marker.exists():
            raise FileExistsError("runtime storage already exists")
        cls.write_marker(root, run_id, database, content_root)
        return cls(database, run_id, ContentStore(content_root))

    @classmethod
    def write_marker(cls, root, run_id, database, content_root):
        root = Path(root)
        register_database(database)
        content_root = Path(content_root).resolve()
        try:
            location = os.path.relpath(content_root, root.resolve())
        except ValueError:
            # Windows cannot express a relative path between different drives.
            location = str(content_root)
        payload = {"format": "postgres-incremental-v1", "run_id": run_id,
                   "schema": database.schema,
                   "content_root": location}
        atomic_write(cls.marker(root), encode(payload))

    @classmethod
    def open(cls, root, run_id):
        marker = cls.marker(root)
        if not marker.exists():
            return None
        payload = json.loads(marker.read_text(encoding="utf-8"))
        if payload.get("format") != "postgres-incremental-v1" or payload.get("run_id") != run_id:
            raise StorageIntegrityError("runtime storage marker differs")
        schema = payload["schema"]
        if not re.fullmatch(r"qa_production_[0-9a-f]{32}", schema):
            raise StorageIntegrityError("invalid runtime schema")
        with _database_lock:
            database = _databases.get(schema)
            if database is None:
                from ..web.production_db import ProductionDatabase
                database = ProductionDatabase(root)
                database.schema = schema
                _databases[schema] = database
        return cls(database, run_id, ContentStore(Path(root) / payload["content_root"]))

    def _read(self, cursor, *, lock=False):
        cursor.execute("SELECT sequence,digest,state FROM runtime_heads WHERE run_id=%s"
                       + (" FOR UPDATE" if lock else ""), (self.run_id,))
        head = cursor.fetchone()
        if head is None:
            return None
        state = dict(head["state"])
        cursor.execute("SELECT collection,item_key,value FROM runtime_items WHERE run_id=%s",
                       (self.run_id,))
        for item in cursor.fetchall():
            state[item["collection"]][item["item_key"]] = item["value"]
        if canonical_digest(state) != head["digest"]:
            raise StorageIntegrityError("runtime checkpoint digest differs")
        return head["sequence"], head["digest"], state

    def load(self):
        with self.database.transaction() as cursor:
            # A row lock keeps the head and item reads at the same revision.
            row = self._read(cursor, lock=True)
        if row is None:
            raise FileNotFoundError("objective runtime has no checkpoint")
        snapshot = unpack_snapshot(row[2], self.content)
        if snapshot.get("run_id") != self.run_id or snapshot.get("commit_sequence") != row[0]:
            raise StorageIntegrityError("runtime checkpoint identity differs")
        return snapshot

    def append(self, snapshot, expected_sequence):
        if snapshot["run_id"] != self.run_id or snapshot["commit_sequence"] != expected_sequence + 1:
            raise StorageConflict("invalid runtime commit identity")
        packed = pack_snapshot(snapshot, self.content)
        digest = canonical_digest(packed)
        sequence = expected_sequence + 1
        with self.database.transaction() as cursor:
            # Also serialize creation, where no head row exists yet.
            key = int.from_bytes(sha256((self.database.schema + self.run_id).encode()).digest()[:8],
                                 "big", signed=True)
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (key,))
            old = self._read(cursor, lock=True)
            if (old[0] if old else 0) != expected_sequence:
                raise StorageConflict("runtime revision changed before commit")
            previous_digest = old[1] if old else ZERO_DIGEST
            delta = difference(old[2] if old else {}, packed)
            state = {k: {} if k in COLLECTIONS else v for k, v in packed.items()}
            cursor.execute("""INSERT INTO runtime_heads(run_id,sequence,digest,state)
                VALUES (%s,%s,%s,%s) ON CONFLICT(run_id) DO UPDATE SET
                sequence=EXCLUDED.sequence,digest=EXCLUDED.digest,state=EXCLUDED.state,
                updated_at=clock_timestamp()""", (self.run_id, sequence, digest, Json(state)))
            for collection, change in delta.items():
                if collection not in COLLECTIONS:
                    continue
                if change.get("remove"):
                    cursor.execute("DELETE FROM runtime_items WHERE run_id=%s AND collection=%s",
                                   (self.run_id, collection))
                    continue
                for item_key in change["removed"]:
                    cursor.execute("DELETE FROM runtime_items WHERE run_id=%s AND collection=%s AND item_key=%s",
                                   (self.run_id, collection, item_key))
                for item_key, value in change["items"].items():
                    cursor.execute("""INSERT INTO runtime_items(run_id,collection,item_key,value)
                        VALUES(%s,%s,%s,%s) ON CONFLICT(run_id,collection,item_key)
                        DO UPDATE SET value=EXCLUDED.value""",
                                   (self.run_id, collection, item_key, Json(value)))
            cursor.execute("""INSERT INTO runtime_events(run_id,sequence,previous_digest,digest,delta)
                VALUES(%s,%s,%s,%s,%s)""", (self.run_id, sequence, previous_digest, digest, Json(delta)))

    def history(self):
        with self.database.transaction() as cursor:
            cursor.execute("SELECT sequence,previous_digest,digest,delta FROM runtime_events "
                           "WHERE run_id=%s ORDER BY sequence", (self.run_id,))
            return [dict(row) for row in cursor.fetchall()]

    def decision_ids(self):
        seen = set()
        ordered = []
        with self.database.transaction() as cursor:
            cursor.execute("SELECT delta->'decisions' AS decisions FROM runtime_events "
                           "WHERE run_id=%s AND delta ? 'decisions' ORDER BY sequence", (self.run_id,))
            events = cursor.fetchall()
        for event in events:
            for key in event["decisions"].get("items", {}):
                if key not in seen:
                    seen.add(key)
                    ordered.append(key)
        return ordered

    def updated_at(self):
        with self.database.transaction() as cursor:
            cursor.execute("SELECT updated_at FROM runtime_heads WHERE run_id=%s", (self.run_id,))
            row = cursor.fetchone()
            return row[0] if row else None

    def verify(self):
        with self.database.transaction() as cursor:
            head = self._read(cursor, lock=True)
            cursor.execute("SELECT sequence,previous_digest,digest,delta FROM runtime_events "
                           "WHERE run_id=%s ORDER BY sequence", (self.run_id,))
            events = cursor.fetchall()
        state, digest = {}, ZERO_DIGEST
        for sequence, event in enumerate(events, 1):
            state = apply_delta(state, event["delta"])
            if (event["sequence"] != sequence or event["previous_digest"] != digest
                    or canonical_digest(state) != event["digest"]):
                raise StorageIntegrityError("runtime event chain differs")
            digest = event["digest"]
            # Historical references must remain readable too.
            for key, change in event["delta"].items():
                if change.get("remove"):
                    continue
                refs = change["items"].values() if key in COLLECTIONS else [change["value"]]
                for ref in refs:
                    self.content.unpack(ref["payload"] if key == "artifacts" else ref)
        if head is None or head[0] != len(events) or head[1] != digest or head[2] != state:
            raise StorageIntegrityError("runtime checkpoint and event history differ")
        return True

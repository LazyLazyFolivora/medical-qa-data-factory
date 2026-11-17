"""Persistent continuous production with one coordinator per workspace."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3
import threading
import time
from uuid import uuid4

from pydantic import BaseModel, Field
from typing import Literal

from ..generation.objective_question_contracts import SuggestedRetrieval
from ..generation.question_design import public_retrieval_suggestion


class ProductionConfig(BaseModel):
    target_count: int = Field(default=30, ge=1, le=10000)
    per_entity_count: int = Field(default=1, ge=1, le=20)
    difficulty: Literal["any", "easy", "medium", "hard"] = "any"
    generation_strategy: Literal["auto", "easy", "medium", "hard", "free"] = "free"
    workers: int = Field(default=2, ge=1, le=4)
    entity_type: Literal["gene/protein", "disease", "drug", "effect/phenotype"] = "gene/protein"
    max_entities: int = Field(default=100, ge=1, le=10000)
    max_actions: int = Field(default=50, ge=2, le=100)
    revisit_processed: bool = False

    @property
    def difficulty_targets(self):
        if self.difficulty == "any":
            return ["easy", "medium", "hard"]
        return [self.difficulty]


_CREATE_ENTITIES = """
    CREATE TABLE IF NOT EXISTS entities (
        identity TEXT NOT NULL, trigger_id TEXT NOT NULL, run_id TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL, entity_type TEXT NOT NULL, state TEXT NOT NULL,
        quota INTEGER NOT NULL, error TEXT, progress TEXT,
        PRIMARY KEY (trigger_id, identity)
    )
"""


class ProductionService:
    def __init__(self, root, *, source, execute, progress, pause, entity_for_run=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "production.sqlite"
        self.source, self.execute = source, execute
        self.progress, self.pause_run = progress, pause
        self.entity_for_run = entity_for_run
        self.guard = threading.RLock()
        self.thread = None
        self.shutdown = threading.Event()
        self.imported_releases = {}
        # A separate database holds a process-wide coordinator lock. SQLite
        # releases it on process death; task writes use another database.
        self.owner = None
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS triggers (
                    id TEXT PRIMARY KEY, config TEXT NOT NULL, state TEXT NOT NULL,
                    reason TEXT, cursor TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS questions (
                    id TEXT PRIMARY KEY, trigger_id TEXT NOT NULL, run_id TEXT NOT NULL,
                    payload TEXT NOT NULL, counted INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS production_questions_run ON questions(run_id);
                CREATE INDEX IF NOT EXISTS production_questions_trigger ON questions(trigger_id);
            """)
            db.execute("BEGIN IMMEDIATE")
            columns = list(db.execute("PRAGMA table_info(entities)"))
            if any(column["name"] == "trigger_id" and column["pk"] == 0 for column in columns):
                db.execute("ALTER TABLE entities RENAME TO legacy_entities")
                db.execute(_CREATE_ENTITIES)
                db.execute("INSERT INTO entities SELECT * FROM legacy_entities ORDER BY rowid")
                db.execute("DROP TABLE legacy_entities")
            else:
                db.execute(_CREATE_ENTITIES)
            db.execute("CREATE INDEX IF NOT EXISTS production_entities_identity ON entities(identity)")

    @contextmanager
    def db(self, *, streaming=False):
        connection = sqlite3.connect(self.path, timeout=10, check_same_thread=not streaming)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def acquire(self):
        if self.owner is not None:
            return
        owner = sqlite3.connect(self.root / "production-owner.sqlite", timeout=0, check_same_thread=False)
        try:
            owner.execute("BEGIN EXCLUSIVE")
        except sqlite3.Error:
            owner.close()
            raise ValueError("另一个后端进程正在管理持续生产，请使用原后端")
        self.owner = owner
        with self.db() as db:
            db.execute("UPDATE triggers SET state='paused', reason='interrupted' WHERE state='running'")
            db.execute("UPDATE triggers SET state='paused', reason='paused' WHERE state='pausing'")
            db.execute("UPDATE entities SET state='paused' WHERE state='running'")

    def recover(self):
        with self.guard:
            self.acquire()
            if self.thread and self.thread.is_alive():
                return
            with self.db() as db:
                trigger = db.execute("SELECT id FROM triggers WHERE state='paused' AND reason='interrupted' "
                                     "ORDER BY rowid DESC LIMIT 1").fetchone()
                if trigger is None:
                    return
                db.execute("UPDATE triggers SET state='running',reason=NULL WHERE id=?", (trigger["id"],))
            self.launch(trigger["id"])

    def start(self, config):
        with self.guard:
            self.acquire()
            self.import_releases()
            with self.db() as db:
                if db.execute("SELECT 1 FROM triggers WHERE state IN ('running','pausing')").fetchone():
                    raise ValueError("已有持续生产任务运行中")
                trigger_id = "production-" + uuid4().hex
                db.execute("INSERT INTO triggers(id,config,state) VALUES(?,?,'running')",
                           (trigger_id, config.model_dump_json()))
            self.launch(trigger_id)
            return trigger_id

    def launch(self, trigger_id):
        self.thread = threading.Thread(target=self.loop, args=(trigger_id,), daemon=True)
        self.thread.start()

    def control(self, trigger_id, action):
        if action not in {"pause", "resume"}:
            raise ValueError("不支持的生产操作")
        with self.guard:
            self.acquire()
            with self.db() as db:
                trigger = db.execute("SELECT * FROM triggers WHERE id=?", (trigger_id,)).fetchone()
                if trigger is None:
                    raise ValueError("触发记录不存在")
                if action == "pause":
                    if trigger["state"] == "running":
                        db.execute("UPDATE triggers SET state='pausing' WHERE id=?", (trigger_id,))
                    return
                if trigger["state"] == "running":
                    return
                if trigger["state"] not in {"paused", "failed"}:
                    raise ValueError("该任务当前无法恢复")
                if self.thread and self.thread.is_alive():
                    raise ValueError("等待当前 worker 保存断点后再恢复")
                db.execute("UPDATE triggers SET state='running', reason=NULL WHERE id=?", (trigger_id,))
                db.execute("UPDATE entities SET state='paused', error=NULL WHERE trigger_id=? AND state='failed'", (trigger_id,))
            self.launch(trigger_id)

    def claim(self, trigger_id, config, reserved):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT state FROM triggers WHERE id=?", (trigger_id,)).fetchone()[0] != "running":
                return None, "paused"
            row = db.execute("SELECT * FROM entities WHERE trigger_id=? AND state='paused' LIMIT 1", (trigger_id,)).fetchone()
            if row:
                db.execute("UPDATE entities SET state='running' WHERE run_id=?", (row["run_id"],))
                return dict(row)
            remaining = config.target_count - db.execute(
                "SELECT COALESCE(SUM(counted),0) FROM questions WHERE trigger_id=?", (trigger_id,)).fetchone()[0] - reserved
            if remaining <= 0:
                return None, "target_reached"
            count = db.execute("SELECT COUNT(*) FROM entities WHERE trigger_id=?", (trigger_id,)).fetchone()[0]
            if count >= config.max_entities:
                return None, "entity_budget_exhausted"
            cursor = db.execute("SELECT cursor FROM triggers WHERE id=?", (trigger_id,)).fetchone()[0]
            quota = min(remaining, config.per_entity_count)
            if config.revisit_processed:
                prior = self.revisit_candidate(db, trigger_id, config.entity_type)
                if prior:
                    return self.add_attempt(db, trigger_id, prior, quota)
        # Source traversal is outside the task transaction.
        while not self.shutdown.is_set():
            page = self.source(config.entity_type, cursor)
            with self.db() as db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT state FROM triggers WHERE id=?", (trigger_id,)).fetchone()[0] != "running":
                    return None, "paused"
                if not page:
                    prior = self.revisit_candidate(db, trigger_id, config.entity_type)
                    if prior:
                        return self.add_attempt(db, trigger_id, prior, quota)
                    return None, "source_exhausted"
                for entity in page:
                    cursor = entity["canonical_id"]
                    if not db.execute("SELECT 1 FROM entities WHERE identity=?", (cursor,)).fetchone():
                        db.execute("UPDATE triggers SET cursor=? WHERE id=?", (cursor, trigger_id))
                        return self.add_attempt(db, trigger_id, entity, quota)
                db.execute("UPDATE triggers SET cursor=? WHERE id=?", (cursor, trigger_id))
        return None, "paused"

    def revisit_candidate(self, db, trigger_id, entity_type):
        return db.execute("""
            WITH latest AS (
                SELECT identity, MAX(rowid) AS attempt
                FROM entities WHERE entity_type=? GROUP BY identity
            )
            SELECT e.identity AS canonical_id, e.name, e.entity_type
            FROM latest JOIN entities e ON e.rowid=latest.attempt
            WHERE e.state IN ('completed','failed')
            AND NOT EXISTS (
                SELECT 1 FROM entities prior WHERE prior.identity=e.identity
                AND (prior.trigger_id=? OR prior.state IN ('running','paused'))
            )
            ORDER BY latest.attempt LIMIT 1
        """, (entity_type, trigger_id)).fetchone()

    def add_attempt(self, db, trigger_id, entity, quota):
        row = dict(identity=entity["canonical_id"], trigger_id=trigger_id, run_id="web-" + str(uuid4()),
                   name=entity["name"], entity_type=entity["entity_type"], quota=quota)
        db.execute("INSERT INTO entities(identity,trigger_id,run_id,name,entity_type,state,quota) "
                   "VALUES(:identity,:trigger_id,:run_id,:name,:entity_type,'running',:quota)", row)
        return row

    def state(self, trigger_id):
        with self.db() as db:
            return db.execute("SELECT state FROM triggers WHERE id=?", (trigger_id,)).fetchone()[0]

    def sync(self, entity, config):
        progress = self.progress(entity["run_id"])
        if progress is None:
            return 0
        with self.db() as db:
            summary = {key: value for key, value in progress.items() if key != "questions"}
            db.execute("UPDATE entities SET progress=? WHERE run_id=?", (json.dumps(summary, ensure_ascii=False), entity["run_id"]))
            for question in progress.get("questions", []):
                difficulty = question.get("assessed_difficulty")
                counted = int(difficulty in config.difficulty_targets)
                db.execute("INSERT OR IGNORE INTO questions VALUES(?,?,?,?,?)",
                           (question["question_id"], entity["trigger_id"], entity["run_id"], json.dumps(question, ensure_ascii=False), counted))
            return db.execute("SELECT COALESCE(SUM(counted),0) FROM questions WHERE run_id=?", (entity["run_id"],)).fetchone()[0]

    def loop(self, trigger_id):
        futures = {}
        reason = None
        try:
            with self.db() as db:
                config = ProductionConfig.model_validate_json(db.execute("SELECT config FROM triggers WHERE id=?", (trigger_id,)).fetchone()[0])
            with ThreadPoolExecutor(max_workers=config.workers) as pool:
                try:
                    while True:
                        running = self.state(trigger_id) == "running" and not self.shutdown.is_set()
                        for future, entity in list(futures.items()):
                            if future.done():
                                error = None
                                try:
                                    result = future.result()
                                    state = "completed" if result.get("is_terminal") and result.get("run_state") in {"COMPLETED", "COMPLETED_WITH_ZERO_QUESTIONS"} else "paused"
                                    if state == "paused" and running:
                                        state, error = "failed", result.get("stop_reason") or "任务需要恢复"
                                except Exception as exc:
                                    state, error = "failed", f"{type(exc).__name__}: {exc}"
                                self.sync(entity, config)
                                with self.db() as db:
                                    db.execute("UPDATE entities SET state=?,error=? WHERE run_id=?", (state, error, entity["run_id"]))
                                del futures[future]
                            else:
                                entity["counted"] = self.sync(entity, config)
                                if not running:
                                    self.pause_run(entity["run_id"])
                        while running and len(futures) < config.workers:
                            reserved = sum(max(0, entity["quota"] - entity.get("counted", 0)) for entity in futures.values())
                            claimed = self.claim(trigger_id, config, reserved)
                            if isinstance(claimed, tuple):
                                _, reason = claimed
                                break
                            futures[pool.submit(self.execute, claimed, config)] = claimed
                        if not futures:
                            break
                        time.sleep(0.5)
                finally:
                    for entity in futures.values():
                        self.pause_run(entity["run_id"])
            with self.db() as db:
                db.execute("BEGIN IMMEDIATE")
                state = db.execute("SELECT state FROM triggers WHERE id=?", (trigger_id,)).fetchone()[0]
                final = "paused" if state == "pausing" or self.shutdown.is_set() else "completed"
                if final == "paused":
                    reason = "paused" if state == "pausing" else "interrupted"
                db.execute("UPDATE triggers SET state=?,reason=? WHERE id=?", (final, reason, trigger_id))
        except Exception as exc:
            for entity in futures.values():
                self.pause_run(entity["run_id"])
            with self.db() as db:
                db.execute("UPDATE entities SET state='paused' WHERE trigger_id=? AND state='running'", (trigger_id,))
                db.execute("UPDATE triggers SET state='failed',reason=? WHERE id=?", (f"{type(exc).__name__}: {exc}", trigger_id))

    def list_triggers(self):
        self.import_releases()
        with self.db() as db:
            rows = db.execute("SELECT * FROM triggers ORDER BY created_at DESC, rowid DESC LIMIT 100").fetchall()
            results = []
            for row in rows:
                item = dict(row)
                item["config"] = json.loads(item["config"])
                item["entities"] = [dict(e) for e in db.execute("""
                    SELECT e.identity,e.run_id,e.name,e.state,e.error,e.progress,
                           (SELECT COUNT(*) FROM questions WHERE run_id=e.run_id) AS published_count
                    FROM entities e WHERE e.trigger_id=?
                    ORDER BY (e.state='running') DESC, e.rowid DESC LIMIT 20
                """, (item["id"],))]
                for entity in item["entities"]:
                    entity["progress"] = json.loads(entity["progress"] or "null")
                    if entity["progress"]:
                        entity["progress"].pop("questions", None)
                item["entity_counts"] = dict(db.execute("SELECT state,COUNT(*) FROM entities WHERE trigger_id=? GROUP BY state", (item["id"],)))
                counts = db.execute("SELECT COUNT(*),COALESCE(SUM(counted),0) FROM questions WHERE trigger_id=?", (item["id"],)).fetchone()
                item["published_count"], item["counted_count"] = counts
                results.append(item)
            return results

    def entity_history(self, *, search="", page=1):
        self.import_releases()
        where = "WHERE instr(lower(e.name), lower(?)) > 0 OR instr(lower(e.identity), lower(?)) > 0"
        with self.db() as db:
            params = (search, search)
            total = db.execute(f"SELECT COUNT(DISTINCT e.identity) FROM entities e {where}", params).fetchone()[0]
            rows = db.execute(f"""
                WITH history AS (
                    SELECT e.identity, COUNT(*) AS attempts, MAX(e.rowid) AS last_attempt
                    FROM entities e {where} GROUP BY e.identity
                    ORDER BY last_attempt DESC LIMIT 20 OFFSET ?
                )
                SELECT e.identity, e.name, e.entity_type, e.run_id, e.state, e.error,
                       h.attempts, (SELECT COUNT(*) FROM entities attempt
                           JOIN questions q ON q.run_id=attempt.run_id
                           WHERE attempt.identity=e.identity) AS published_count
                FROM history h JOIN entities e ON e.rowid=h.last_attempt
                ORDER BY h.last_attempt DESC
            """, (*params, (page - 1) * 20))
            return {"total": total, "page": page, "entities": [dict(row) for row in rows]}

    def questions(self, trigger_id=None, page=1, difficulty=None, search=""):
        self.import_releases()
        clauses, params = [], []
        if trigger_id:
            clauses.append("trigger_id=?")
            params.append(trigger_id)
        if difficulty:
            clauses.append("json_extract(payload,'$.assessed_difficulty')=?")
            params.append(difficulty)
        if search:
            clauses.append("instr(json_extract(payload,'$.question'),?) > 0")
            params.append(search)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.db() as db:
            total = db.execute("SELECT COUNT(*) FROM questions" + where, params).fetchone()[0]
            rows = db.execute("SELECT * FROM questions" + where + " ORDER BY rowid DESC LIMIT 20 OFFSET ?", [*params, (page-1)*20]).fetchall()
            return {"total": total, "page": page, "questions": [{**json.loads(row["payload"]), "trigger_id": row["trigger_id"], "run_id": row["run_id"]} for row in rows]}

    def dataset_rows(self, trigger_id=None):
        where = " WHERE q.trigger_id=?" if trigger_id else ""
        sources_by_run = {}
        with self.db(streaming=True) as db:
            rows = db.execute("""
                SELECT q.*,e.identity,e.name,e.entity_type FROM questions q
                LEFT JOIN entities e ON e.run_id=q.run_id
            """ + where + " ORDER BY q.rowid", (trigger_id,) if trigger_id else ())
            for row in rows:
                question = json.loads(row["payload"])
                if question.get("suggested_retrieval"):
                    question["suggested_retrieval"] = public_retrieval_suggestion(
                        question["question"], SuggestedRetrieval.model_validate(question["suggested_retrieval"]),
                    ).model_dump(mode="json")
                if "answer_sources" not in question:
                    run_id = row["run_id"]
                    if run_id not in sources_by_run:
                        progress = self.progress(run_id) or {}
                        sources_by_run[run_id] = {
                            q["question_id"]: q.get("answer_sources", []) for q in progress.get("questions", [])
                        }
                    question["answer_sources"] = sources_by_run[run_id].get(row["id"], [])
                question.update(
                    trigger_id=row["trigger_id"], run_id=row["run_id"],
                    entity_id=row["identity"], entity_name=row["name"], entity_type=row["entity_type"],
                    evidence_url=f"/api/questions/{row['id']}/evidence",
                    run_trace_url=f"/api/objective/{row['run_id']}/workbench",
                )
                yield json.dumps(question, ensure_ascii=False) + "\n"

    def owns_run(self, run_id):
        with self.db() as db:
            return db.execute("SELECT 1 FROM entities WHERE run_id=?", (run_id,)).fetchone() is not None

    def import_releases(self):
        """Include existing single-entity publications, grouped by their run ID."""
        with self.guard:
            for path in (self.root / "objective_releases").glob("*/public/questions.jsonl"):
                try:
                    info = path.stat()
                except FileNotFoundError:
                    continue
                stamp = (info.st_mtime_ns, info.st_size)
                if self.imported_releases.get(path) == stamp:
                    continue
                run_id = path.parent.parent.name
                with self.db() as db:
                    owner = db.execute("SELECT trigger_id FROM entities WHERE run_id=?", (run_id,)).fetchone()
                    if owner and owner[0] != run_id:
                        self.imported_releases[path] = stamp
                        continue
                    try:
                        questions = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
                    except (FileNotFoundError, json.JSONDecodeError):
                        continue
                    if questions:
                        db.execute("INSERT OR IGNORE INTO triggers(id,config,state,reason) VALUES(?,?,'completed','历史单实体运行')",
                                   (run_id, json.dumps({"target_count": len(questions), "difficulty": "mixed", "legacy": True})))
                        entity = self.entity_for_run(run_id) if self.entity_for_run and not owner else None
                        if entity:
                            db.execute("""
                                INSERT OR IGNORE INTO entities(identity,trigger_id,run_id,name,entity_type,state,quota)
                                VALUES (?,?,?, ?,?,'completed',?)
                            """, (entity[0], run_id, run_id, entity[1], entity[2], len(questions)))
                    for question in questions:
                        db.execute("INSERT OR IGNORE INTO questions VALUES(?,?,?,?,?)",
                                   (question["question_id"], run_id, run_id, json.dumps(question, ensure_ascii=False), 1))
                self.imported_releases[path] = stamp

    def close(self):
        self.shutdown.set()
        if self.thread:
            self.thread.join()
        if self.owner:
            self.owner.close()
            self.owner = None

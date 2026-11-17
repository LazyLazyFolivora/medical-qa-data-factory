import threading
import time
import json

import pytest

from medical_qa_data_factory.web.production import ProductionConfig, ProductionService


def wait_until(predicate):
    deadline = time.monotonic() + 8
    while not predicate():
        assert time.monotonic() < deadline, "production did not reach expected state"
        time.sleep(.02)


def make_service(tmp_path, execute=None):
    entities = [dict(canonical_id=f"primekg:gene/protein:{i}", name=f"Gene{i}", entity_type="gene/protein") for i in range(1, 6)]
    results = {}
    calls = []

    def default_execute(entity, config):
        calls.append(entity["identity"])
        results[entity["run_id"]] = {"is_terminal": True, "run_state": "COMPLETED", "questions": [
            dict(question_id=entity["run_id"], question=entity["name"], answer=["answer"], assessed_difficulty="easy")
        ]}
        return results[entity["run_id"]]

    service = ProductionService(tmp_path, source=lambda kind, after: [e for e in entities if e["canonical_id"] > after],
        execute=execute or default_execute, progress=results.get, pause=lambda run_id: None)
    return service, calls, results


def test_production_continues_and_skips_processed_entities_across_triggers(tmp_path):
    service, calls, results = make_service(tmp_path)
    try:
        first = service.start(ProductionConfig(target_count=2, per_entity_count=1, workers=2, difficulty="easy"))
        wait_until(lambda: service.state(first) == "completed")
        assert len(calls) == 2
        assert service.questions(first)["total"] == 2
        second = service.start(ProductionConfig(target_count=1, per_entity_count=1, difficulty="easy"))
        wait_until(lambda: service.state(second) == "completed")
        assert len(set(calls)) == 3
        assert service.questions()["total"] == 3
        assert service.questions(search="Gene3")["total"] == 1
    finally:
        service.close()


def test_workers_overlap_and_pause_preserves_run_identity(tmp_path):
    started = threading.Barrier(3)
    finish = threading.Event()
    runs = []

    def execute(entity, config):
        runs.append(entity["run_id"])
        started.wait(timeout=5)
        finish.wait(timeout=5)
        return {"run_state": "RECOVERY_REQUIRED", "is_terminal": False}

    service, _, _ = make_service(tmp_path, execute)
    try:
        trigger = service.start(ProductionConfig(target_count=2, per_entity_count=1, workers=2))
        started.wait(timeout=5)
        service.control(trigger, "pause")
        finish.set()
        wait_until(lambda: service.state(trigger) == "paused")
        with service.db() as db:
            assert {row[0] for row in db.execute("SELECT run_id FROM entities")} == set(runs)
            assert all(row[0] == "paused" for row in db.execute("SELECT state FROM entities"))
        resumed = []
        service.execute = lambda entity, config: resumed.append(entity["run_id"]) or {"run_state": "COMPLETED_WITH_ZERO_QUESTIONS", "is_terminal": True}
        service.control(trigger, "resume")
        wait_until(lambda: service.state(trigger) == "completed")
        assert set(runs) <= set(resumed)
        assert len(resumed) == len(set(resumed))
    finally:
        finish.set()
        service.close()


def test_repeated_worker_failures_leave_partial_questions_and_continue(tmp_path):
    service, _, results = make_service(tmp_path)

    def execute(entity, config):
        result = {"is_terminal": True, "run_state": "COMPLETED", "questions": [
            dict(question_id=entity["run_id"], question=entity["name"], answer=["answer"], assessed_difficulty="easy"),
        ]}
        if entity["name"] in {"Gene1", "Gene5"}:
            results[entity["run_id"]] = result
        if entity["name"] != "Gene5":
            raise RuntimeError("PubMed read timed out")
        return result

    service.execute = execute
    try:
        trigger = service.start(ProductionConfig(target_count=2, per_entity_count=1, workers=2))
        wait_until(lambda: service.state(trigger) == "completed")
        row = service.list_triggers()[0]
        assert row["reason"] == "target_reached"
        assert row["entity_counts"] == {"completed": 1, "failed": 4}
        assert {q["question"] for q in service.questions(trigger)["questions"]} == {"Gene1", "Gene5"}
        assert all("PubMed read timed out" in e["error"] for e in row["entities"] if e["state"] == "failed")
    finally:
        service.close()


def test_other_coordinator_cannot_reset_active_tasks(tmp_path):
    first, _, _ = make_service(tmp_path)
    second, _, _ = make_service(tmp_path)
    try:
        first.acquire()
        with pytest.raises(ValueError, match="另一个后端"):
            second.acquire()
    finally:
        first.close()
        second.close()


def test_restarted_coordinator_resumes_interrupted_run_and_leaves_manual_pause(tmp_path):
    first, _, _ = make_service(tmp_path)
    first.acquire()
    with first.db() as db:
        db.execute("INSERT INTO triggers(id,config,state) VALUES('prior',?,'running')", (ProductionConfig().model_dump_json(),))
        db.execute("INSERT INTO triggers(id,config,state) VALUES('manual-pause',?,'pausing')", (ProductionConfig().model_dump_json(),))
        db.execute("INSERT INTO entities(identity,trigger_id,run_id,name,entity_type,state,quota) VALUES('gene:1','prior','run-1','G1','gene/protein','running',1)")
    first.close()
    resumed = []
    second, _, _ = make_service(tmp_path, lambda entity, config: resumed.append(entity["run_id"]) or {
        "run_state": "COMPLETED_WITH_ZERO_QUESTIONS", "is_terminal": True,
    })
    try:
        second.recover()
        wait_until(lambda: second.state("prior") == "completed")
        assert resumed.count("run-1") == 1
        assert second.state("manual-pause") == "paused"
        assert second.entity_history(search="G1")["entities"][0]["attempts"] == 1
    finally:
        second.close()


def test_can_revisit_a_record_from_the_old_schema(tmp_path):
    import sqlite3

    with sqlite3.connect(tmp_path / "production.sqlite") as db:
        db.executescript("""
            CREATE TABLE entities (
                identity TEXT PRIMARY KEY, trigger_id TEXT NOT NULL, run_id TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL, entity_type TEXT NOT NULL, state TEXT NOT NULL,
                quota INTEGER NOT NULL, error TEXT, progress TEXT
            );
            INSERT INTO entities VALUES
                ('primekg:gene/protein:1','prior','run-old','Gene1','gene/protein','completed',1,NULL,NULL);
        """)
    service, calls, _ = make_service(tmp_path)
    try:
        trigger = service.start(ProductionConfig(target_count=1, revisit_processed=True))
        wait_until(lambda: service.state(trigger) == "completed")
        [entity] = service.entity_history(search="gene1")["entities"]
        assert calls == ["primekg:gene/protein:1"]
        assert entity["attempts"] == 2
        assert entity["published_count"] == 1
        assert entity["run_id"] != "run-old"
        with service.db() as db:
            assert db.execute("SELECT state FROM entities WHERE run_id='run-old'").fetchone()[0] == "completed"
    finally:
        service.close()


def test_dataset_uses_question_only_retrieval_proposal_for_old_runs(tmp_path):
    service, _, _ = make_service(tmp_path)
    question = "In which tissues is human NAT2 primarily expressed?"
    with service.db() as db:
        db.execute("INSERT INTO questions VALUES(?,?,?,?,?)", (
            "q1", "prior", "run-old", json.dumps({
                "question": question, "answer": ["liver", "gut"],
                "suggested_retrieval": {
                    "query": "NAT2 liver gut", "sources": ["PubMed", "PMID 24467436"],
                    "expected_evidence": "NAT2 expression in liver and gut.", "stop_condition": "Find gut and liver.",
                },
            }), 1,
        ))
    [row] = map(json.loads, service.dataset_rows("prior"))
    assert row["suggested_retrieval"]["query"] == question
    assert row["suggested_retrieval"]["sources"] == ["PubMed"]
    assert "liver" not in json.dumps(row["suggested_retrieval"])
    assert row["answer"] == ["liver", "gut"]


def test_production_api_runs_real_single_entity_pipeline(tmp_path):
    from starlette.testclient import TestClient
    from medical_qa_data_factory.web.app import create_app
    from test_objective_agent_loop import FakeMaterialProvider
    from test_objective_web import current_generation_fixture

    class Source:
        def production_entities(self, entity_type, after):
            if after:
                return []
            return [dict(canonical_id="primekg:gene/protein:672", name="BRCA1", entity_type="gene/protein")]

        def close(self):
            pass

    model, source, _ = current_generation_fixture()
    app = create_app(tmp_path, entity_provider=Source(), structured_model=model, objective_providers={
        "pubmed": FakeMaterialProvider("pubmed", source),
    })
    with TestClient(app) as client:
        assert client.post("/api/production", json={"workers": 0}).status_code == 400
        response = client.post("/api/production", json={"target_count": 1, "per_entity_count": 1, "workers": 1})
        assert response.status_code == 202
        trigger_id = response.json()["trigger_id"]
        wait_until(lambda: client.get("/api/production").json()["triggers"][0]["state"] in {"completed", "failed", "paused"})
        trigger = client.get("/api/production").json()["triggers"][0]
        assert trigger["state"] == "completed", trigger
        questions = client.get("/api/production/questions", params={"trigger_id": trigger_id}).json()
        assert questions["total"] == 1
        assert questions["questions"][0]["trigger_id"] == trigger_id
        assert trigger["counted_count"] == 1
        run_id = questions["questions"][0]["run_id"]
        from medical_qa_data_factory.generation.objective_question_runtime import ObjectiveRuntimeRepository
        snapshot = ObjectiveRuntimeRepository(tmp_path / "objective_runs" / run_id, run_id=run_id).load()
        assessment = next(a for a in snapshot.artifacts.values() if a.artifact_type == "difficulty_assessment")
        assert assessment.producer_role == "objective_difficulty_assessment"
        assert assessment.payload["generation_strategy"] == "free"
        report = next(a for a in snapshot.artifacts.values() if a.artifact_type == "objective_validation_report")
        assert report.payload["difficulty_assessment_id"] == assessment.artifact_id
        rows = client.get("/api/production/dataset", params={"trigger_id": trigger_id})
        assert rows.status_code == 200
        [row] = [json.loads(line) for line in rows.text.splitlines()]
        assert row["entity_id"] == "primekg:gene/protein:672"
        assert row["question_id"] == questions["questions"][0]["question_id"]
        assert row["answer_sources"][0]["source_record_id"]
        assert client.get(row["evidence_url"]).status_code == 200
        assert client.get(row["run_trace_url"]).status_code == 200
        assert client.post(f"/api/production/{trigger_id}/delete").status_code == 409

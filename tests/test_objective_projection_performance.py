"""Decision ordering is obtained from the database event sequence."""
from types import SimpleNamespace

from medical_qa_data_factory.web.objective_projection import _latest_decision, objective_decision_order
from medical_qa_data_factory.generation.runtime_storage import PostgresRuntimeStore


def test_database_order_is_used_instead_of_dictionary_order(tmp_path, monkeypatch):
    ids = ["z-old", "a-new"]
    store = SimpleNamespace(decision_ids=lambda: ids)
    monkeypatch.setattr(PostgresRuntimeStore, "open", lambda *args: store)
    snapshot = SimpleNamespace(decisions={"a-new": "new", "z-old": "old"})
    assert objective_decision_order(tmp_path, "run", snapshot) == ["old", "new"]
    assert _latest_decision(tmp_path, "run", snapshot) == "new"
    ids.append("b-newest")
    snapshot.decisions["b-newest"] = "newest"
    assert _latest_decision(tmp_path, "run", snapshot) == "newest"

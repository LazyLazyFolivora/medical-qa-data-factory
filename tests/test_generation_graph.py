"""End-to-end LangGraph run over a tiny provider fixture."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from medical_qa_data_factory.generation.contracts import (
    EntityGenerationRequest,
    StopReason,
    UpstreamEntitySeed,
)
from medical_qa_data_factory.generation.graph import (
    validated_task_realization_stop_reason,
)
from medical_qa_data_factory.generation.repository import GenerationRepository
from medical_qa_data_factory.generation.service import (
    EntityGenerationService,
    checkpoint_path,
    episode_id_for,
)
from medical_qa_data_factory.knowledge_graph.errors import GraphQueryTimeoutError
from medical_qa_data_factory.knowledge_graph.providers.primekg import (
    PrimeKGSQLiteProvider,
)
from medical_qa_data_factory.llm import FakeStructuredModel
from medical_qa_data_factory.repository import LocalArtifactRepository

ROWS = [
    ("aspirin", "exposure_disease", "migraine", "D001241", "B156", "exposure", "disease", "DrugBank"),
    ("aspirin", "exposure_disease", "headache", "D001241", "B001", "exposure", "disease", "DrugBank"),
    ("aspirin", "contraindication", "warfarin", "D001241", "D002", "exposure", "exposure", "DrugBank"),
    ("ibuprofen", "exposure_disease", "aspirin", "D00521", "D001241", "exposure", "exposure", "DrugBank"),
    ("BRAP", "exposure_disease", "aspirin", "8315", "D001241", "gene", "exposure", "DrugBank"),
]


def test_realization_distinguishes_no_candidate_from_rejected_candidates() -> None:
    assert validated_task_realization_stop_reason(
        {"react_candidate_statuses": {}}, accepted_question_ids=[]
    ) == StopReason.NO_CANDIDATE_PROPOSED.value
    assert validated_task_realization_stop_reason(
        {"react_candidate_statuses": {"candidate-1": "rejected"}},
        accepted_question_ids=[],
    ) == StopReason.ALL_CANDIDATES_HARD_REJECTED.value


def test_realization_preserves_nonterminal_candidate_stop_reason() -> None:
    assert validated_task_realization_stop_reason(
        {
            "react_candidate_statuses": {"candidate-1": "needs_evidence"},
            "react_stop_reason": StopReason.BUDGET_EXHAUSTED.value,
        },
        accepted_question_ids=[],
    ) == StopReason.BUDGET_EXHAUSTED.value


def _write_config(tmp_path: Path, database: Path) -> Path:
    import json

    config = tmp_path / "knowledge_graphs.json"
    config.write_text(
        json.dumps(
            {
                "default_provider": "primekg",
                "providers": {
                    "primekg": {
                        "driver": "primekg_sqlite",
                        "database_path": database.as_posix(),
                        "default_limit": 100,
                        "max_limit": 1000,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return config


@pytest.fixture
def primekg_db(tmp_path: Path) -> Path:
    path = tmp_path / "tiny.sqlite"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE edges (head TEXT, relation TEXT, tail TEXT, head_id TEXT,"
        " tail_id TEXT, head_type TEXT, tail_type TEXT, source TEXT)"
    )
    connection.executemany("INSERT INTO edges VALUES (?,?,?,?,?,?,?,?)", ROWS)
    connection.commit()
    connection.close()
    return path


def _service(tmp_path: Path, primekg_db: Path, monkeypatch) -> EntityGenerationService:
    config = _write_config(tmp_path, primekg_db)
    monkeypatch.setenv("MEDICAL_QA_KG_CONFIG", str(config))
    return EntityGenerationService(workspace=tmp_path / "ws", provider_id="primekg")


def _request(tmp_path: Path, **overrides) -> EntityGenerationRequest:
    payload = {
        "task_id": "aspirin-v1",
        "workspace": str(tmp_path / "ws"),
        "seed": UpstreamEntitySeed(
            provider_id="primekg",
            native_id="D001241",
            entity_type="exposure",
            canonical_name="aspirin",
        ),
        "question_count": 3,
    }
    payload.update(overrides)
    return EntityGenerationRequest(**payload)


def _seed(**overrides) -> UpstreamEntitySeed:
    payload = {
        "provider_id": "primekg",
        "native_id": "D001241",
        "entity_type": "exposure",
        "canonical_name": "aspirin",
    }
    payload.update(overrides)
    return UpstreamEntitySeed(**payload)


def test_full_graph_run_produces_bundle_claims_and_questions(
    tmp_path, primekg_db, monkeypatch
) -> None:
    result = _service(tmp_path, primekg_db, monkeypatch).run(_request(tmp_path))
    assert result.status in {"completed", "partial"}
    assert result.bundle_ids
    assert result.claim_ids
    assert result.question_ids

    repository = LocalArtifactRepository(tmp_path / "ws")
    bundle = repository.load_latest_bundle(result.bundle_ids[0])
    assert bundle.kg_evidence
    assert bundle.claims
    # curate_evidence saves v1; build_claims derives v2 from it.
    assert repository.list_versions("bundles", bundle.bundle_id) == [
        f"{bundle.bundle_id}.v1",
        f"{bundle.bundle_id}.v2",
    ]


@pytest.mark.parametrize("model_mode", ["fake", "structured"])
def test_model_mode_uses_model_to_choose_formal_evidence(
    tmp_path, primekg_db, monkeypatch, model_mode
) -> None:
    """The formal entry point must let the model drive evidence collection."""

    def proposal(request):
        context = request.payload
        if not context["observations"]:
            return {
                "action": {
                    "action_type": "query_neighbors",
                    "subject_entity_id": context["seed_entity_id"],
                    "direction": "outgoing",
                    "relations": ["exposure_disease"],
                    "max_results": 10,
                },
                "expected_information": "diseases linked to the seed",
                "evidence_gap": "no disease evidence has been collected",
            }
        return {
            "action": {
                "action_type": "finish",
                "stop_reason": "evidence_sufficient",
            },
            "expected_information": "the selected evidence is enough",
            "evidence_gap": "none",
            "cited_observation_ids": [
                context["observations"][-1]["observation_id"]
            ],
        }

    model = FakeStructuredModel(
        responses={"exploration_planner": proposal}
    )
    config = _write_config(tmp_path, primekg_db)
    monkeypatch.setenv("MEDICAL_QA_KG_CONFIG", str(config))
    service = EntityGenerationService(
        workspace=tmp_path / "ws",
        provider_id="primekg",
        model_mode=model_mode,
        model=model,
    )

    result = service.run(_request(tmp_path))

    assert result.question_ids
    assert len(model.calls_for("exploration_planner")) == 2
    assert model.payloads_for("exploration_planner")[1]["observations"]
    repository = LocalArtifactRepository(tmp_path / "ws")
    bundle = repository.load_latest_bundle(result.bundle_ids[0])
    assert {edge.relation for edge in bundle.kg_evidence} == {
        "exposure_disease"
    }
    observations = GenerationRepository(
        tmp_path / "ws"
    ).list_observations_by_episode(result.episode_id)
    cited = {
        evidence_id
        for observation in observations
        for evidence_id in observation.new_evidence_ids
    }
    assert cited
    assert cited <= set(repository.list_record_ids("kg_evidence"))
    steps = GenerationRepository(tmp_path / "ws").list_steps_by_episode(
        result.episode_id
    )
    assert "agent_explore" in {step.node for step in steps}


def test_formal_model_error_is_reported_without_rule_based_fallback(
    tmp_path, primekg_db, monkeypatch
) -> None:
    config = _write_config(tmp_path, primekg_db)
    monkeypatch.setenv("MEDICAL_QA_KG_CONFIG", str(config))
    service = EntityGenerationService(
        workspace=tmp_path / "ws",
        provider_id="primekg",
        model_mode="fake",
        model=FakeStructuredModel(),
    )

    result = service.run(_request(tmp_path))

    assert result.status == "partial"
    assert result.stop_reason == StopReason.MODEL_ERROR.value
    assert result.question_ids == []
    task = LocalArtifactRepository(tmp_path / "ws").load_latest_task(
        result.task_id
    )
    assert task.current_stage == "generation_stopped"
    agent_errors = [
        item for item in task.errors if item.get("node") == "agent_explore"
    ]
    assert agent_errors
    assert "no fake response registered for role 'exploration_planner'" in (
        agent_errors[0]["message"]
    )


def test_structured_mode_builds_registry_model_for_formal_entry(
    tmp_path, primekg_db, monkeypatch
) -> None:
    def proposal(request):
        context = request.payload
        if context["observations"]:
            return {
                "action": {
                    "action_type": "finish",
                    "stop_reason": "evidence_sufficient",
                },
                "expected_information": "enough evidence",
                "evidence_gap": "none",
            }
        return {
            "action": {
                "action_type": "query_neighbors",
                "subject_entity_id": context["seed_entity_id"],
                "direction": "outgoing",
                "max_results": 5,
            },
            "expected_information": "one bounded neighborhood",
            "evidence_gap": "no evidence yet",
        }

    model = FakeStructuredModel(
        responses={"exploration_planner": proposal}
    )
    config = _write_config(tmp_path, primekg_db)
    monkeypatch.setenv("MEDICAL_QA_KG_CONFIG", str(config))
    monkeypatch.setattr(
        "medical_qa_data_factory.llm.registry.build_model_port",
        lambda: model,
    )
    service = EntityGenerationService(
        workspace=tmp_path / "ws",
        provider_id="primekg",
        model_mode="structured",
    )

    result = service.run(_request(tmp_path))

    assert result.question_ids
    assert len(model.calls_for("exploration_planner")) == 2


def test_generated_questions_reference_real_entities_and_evidence(
    tmp_path, primekg_db, monkeypatch
) -> None:
    result = _service(tmp_path, primekg_db, monkeypatch).run(_request(tmp_path))
    repository = LocalArtifactRepository(tmp_path / "ws")
    bundle = repository.load_latest_bundle(result.bundle_ids[0])
    for question_id in result.question_ids:
        spec = repository.load_latest_question(question_id)
        assert spec.generation_episode_id == result.episode_id
        assert spec.gold_answer.short_answer
        assert spec.gold_answer.short_answer.lower() not in spec.canonical_question.lower()
        assert set(spec.evidence_requirements.required_kg_evidence_ids) <= {
            item.evidence_id for item in bundle.kg_evidence
        }
        assert set(spec.evidence_requirements.required_claim_ids) <= {
            item.claim_id for item in bundle.claims
        }


def test_answer_is_never_the_seed_entity(tmp_path, primekg_db, monkeypatch) -> None:
    result = _service(tmp_path, primekg_db, monkeypatch).run(_request(tmp_path))
    repository = LocalArtifactRepository(tmp_path / "ws")
    for question_id in result.question_ids:
        spec = repository.load_latest_question(question_id)
        answer_ids = {value.get("canonical_id") for value in spec.gold_answer.values}
        assert "primekg:exposure:D001241" not in answer_ids


def test_gene_row_sharing_the_native_id_is_never_used(
    tmp_path, primekg_db, monkeypatch
) -> None:
    result = _service(tmp_path, primekg_db, monkeypatch).run(_request(tmp_path))
    repository = LocalArtifactRepository(tmp_path / "ws")
    bundle = repository.load_latest_bundle(result.bundle_ids[0])
    names = {
        ref.canonical_name
        for edge in bundle.kg_evidence
        for ref in (edge.head, edge.tail)
    }
    assert "BRAP" not in names


def test_every_node_records_a_step_with_increasing_sequence(
    tmp_path, primekg_db, monkeypatch
) -> None:
    result = _service(tmp_path, primekg_db, monkeypatch).run(_request(tmp_path))
    steps = GenerationRepository(tmp_path / "ws").list_steps_by_episode(result.episode_id)
    assert steps
    sequences = [item.sequence for item in steps]
    assert sequences == sorted(sequences)
    nodes = {item.node for item in steps}
    assert {
        "verify_seed",
        "collect_neighbors",
        "curate_evidence",
        "build_claims",
        "write_questions",
        "machine_validate",
        "finalize",
    } <= nodes


def test_each_node_step_has_a_started_and_terminal_version(
    tmp_path, primekg_db, monkeypatch
) -> None:
    result = _service(tmp_path, primekg_db, monkeypatch).run(_request(tmp_path))
    episodes = GenerationRepository(tmp_path / "ws")
    for step in episodes.list_steps_by_episode(result.episode_id):
        versions = episodes.list_step_versions(step.step_id)
        assert len(versions) == 2
        assert episodes.load_step(step.step_id, versions[0]).status == "started"
        assert episodes.load_step(step.step_id, versions[1]).status in {
            "completed",
            "failed",
        }


def test_thread_id_equals_episode_id_and_checkpoint_exists(
    tmp_path, primekg_db, monkeypatch
) -> None:
    request = _request(tmp_path)
    result = _service(tmp_path, primekg_db, monkeypatch).run(request)
    assert result.episode_id == episode_id_for(request)
    assert checkpoint_path(tmp_path / "ws").exists()


def test_stop_reason_is_persisted(tmp_path, primekg_db, monkeypatch) -> None:
    result = _service(tmp_path, primekg_db, monkeypatch).run(_request(tmp_path))
    episode = GenerationRepository(tmp_path / "ws").load_latest_episode(result.episode_id)
    assert episode.stop_reason is not None


def test_invalid_upstream_identity_fails_fast(tmp_path, primekg_db, monkeypatch) -> None:
    result = _service(tmp_path, primekg_db, monkeypatch).run(
        _request(tmp_path, seed=_seed(entity_type="pathway"))
    )
    assert result.status == "failed"
    assert result.stop_reason == StopReason.INVALID_UPSTREAM_IDENTITY.value
    assert result.question_ids == []


def test_missing_native_id_fails_fast(tmp_path, primekg_db, monkeypatch) -> None:
    result = _service(tmp_path, primekg_db, monkeypatch).run(
        _request(
            tmp_path,
            seed=_seed(native_id="D999999", canonical_name="nonexistent"),
        )
    )
    assert result.status == "failed"
    assert result.stop_reason == StopReason.INVALID_UPSTREAM_IDENTITY.value


def test_seed_query_timeout_fails_without_entering_exploration(
    tmp_path, primekg_db, monkeypatch
) -> None:
    def timeout(self, query):
        raise GraphQueryTimeoutError("primekg seed lookup timed out")

    monkeypatch.setattr(PrimeKGSQLiteProvider, "resolve_entity", timeout)

    result = _service(tmp_path, primekg_db, monkeypatch).run(_request(tmp_path))

    assert result.status == "failed"
    assert result.stop_reason == StopReason.PROVIDER_ERROR.value
    steps = GenerationRepository(tmp_path / "ws").list_steps_by_episode(
        result.episode_id
    )
    assert {step.node for step in steps} == {"verify_seed", "fail"}


def test_repeated_edges_from_a_filtered_query_are_not_saved_twice(
    tmp_path, primekg_db, monkeypatch
) -> None:
    config = _write_config(tmp_path, primekg_db)
    monkeypatch.setenv("MEDICAL_QA_KG_CONFIG", str(config))
    turns = {"value": 0}

    def planner(request):
        turns["value"] += 1
        if turns["value"] == 1:
            return {
                "decision": "continue_search",
                "next_action": {
                    "action_type": "expand_entity",
                    "subject_entity_id": "primekg:exposure:D001241",
                    "direction": "both",
                    "max_results": 5,
                },
                "expected_information": ["broad neighborhood"],
            }
        if turns["value"] == 2:
            return {
                "decision": "continue_search",
                "next_action": {
                    "action_type": "query_neighbors",
                    "subject_entity_id": "primekg:exposure:D001241",
                    "direction": "both",
                    "max_results": 5,
                },
                "expected_information": ["narrow duplicate set"],
            }
        raise RuntimeError("finish test")

    model = FakeStructuredModel(
        responses={"entity_question_exploration_planner": planner}
    )
    service = EntityGenerationService(
        workspace=tmp_path / "ws",
        provider_id="primekg",
        model_mode="structured",
        model=model,
    )

    result = service.run(_request(tmp_path))

    observations = GenerationRepository(
        tmp_path / "ws"
    ).list_observations_by_episode(result.episode_id)
    action_observations = [
        item for item in observations if item.action_type is not None
    ]
    assert len(action_observations) == 2
    by_action = {
        item.action_type: item for item in action_observations
    }
    assert by_action["expand_entity"].status == "success"
    assert by_action["query_neighbors"].status == "empty"
    assert all(
        item.error_code != "idempotency_conflict"
        for item in action_observations
    )


def test_provider_is_closed_on_success(tmp_path, primekg_db, monkeypatch) -> None:
    closed: list[bool] = []
    original = PrimeKGSQLiteProvider.close

    def _tracking(self):  # pragma: no cover - observation helper
        closed.append(True)
        original(self)

    monkeypatch.setattr(PrimeKGSQLiteProvider, "close", _tracking)
    _service(tmp_path, primekg_db, monkeypatch).run(_request(tmp_path))
    assert closed


def test_provider_is_closed_on_failure(tmp_path, primekg_db, monkeypatch) -> None:
    closed: list[bool] = []
    original = PrimeKGSQLiteProvider.close

    def _tracking(self):  # pragma: no cover - observation helper
        closed.append(True)
        original(self)

    monkeypatch.setattr(PrimeKGSQLiteProvider, "close", _tracking)
    _service(tmp_path, primekg_db, monkeypatch).run(
        _request(tmp_path, seed=_seed(entity_type="pathway"))
    )
    assert closed


def test_machine_validation_ran_on_generated_questions(
    tmp_path, primekg_db, monkeypatch
) -> None:
    result = _service(tmp_path, primekg_db, monkeypatch).run(_request(tmp_path))
    repository = LocalArtifactRepository(tmp_path / "ws")
    for question_id in result.question_ids:
        spec = repository.load_latest_question(question_id)
        assert spec.validation_checks
        assert any(
            check.validator_version.startswith("validators.")
            for check in spec.validation_checks
        )

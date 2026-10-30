"""End-to-end acceptance slice for entity-first validated question output."""
from __future__ import annotations

import json
import sqlite3

from medical_qa_data_factory.generation.contracts import (
    EntityGenerationRequest,
    UpstreamEntitySeed,
)
from medical_qa_data_factory.generation.service import EntityGenerationService
from medical_qa_data_factory.generation.graph import literature_enrichment_id
from medical_qa_data_factory.generation.repository import GenerationRepository
from medical_qa_data_factory.llm import FakeStructuredModel
from medical_qa_data_factory.repository import LocalArtifactRepository
from medical_qa_data_factory.web.app import _generation_progress


def test_pubmed_retry_queries_have_distinct_enrichment_identities() -> None:
    first = literature_enrichment_id(
        "episode-1", "kg-edge-1", "aspirin AND headache"
    )
    replay = literature_enrichment_id(
        "episode-1", "kg-edge-1", "aspirin AND headache"
    )
    retry = literature_enrichment_id(
        "episode-1", "kg-edge-1", "aspirin AND migraine"
    )

    assert first == replay
    assert first != retry


def test_structured_entity_loop_publishes_only_world_validated_task(
    tmp_path, monkeypatch
) -> None:
    database = tmp_path / "primekg.sqlite"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE edges (head TEXT, relation TEXT, tail TEXT, "
            "head_id TEXT, tail_id TEXT, head_type TEXT, tail_type TEXT, "
            "source TEXT)"
        )
        connection.executemany(
            "INSERT INTO edges VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    "Breast cancer",
                    "disease_protein",
                    "ERBB2",
                    "D1",
                    "P1",
                    "disease",
                    "gene/protein",
                    "test",
                ),
                (
                    "Breast cancer",
                    "disease_protein",
                    "PIK3CA",
                    "D1",
                    "P2",
                    "disease",
                    "gene/protein",
                    "test",
                ),
                (
                    "PI3K-Akt signaling pathway",
                    "pathway_protein",
                    "ERBB2",
                    "PW1",
                    "P1",
                    "pathway",
                    "gene/protein",
                    "test",
                ),
                (
                    "MAPK signaling pathway",
                    "pathway_protein",
                    "PIK3CA",
                    "PW2",
                    "P2",
                    "pathway",
                    "gene/protein",
                    "test",
                ),
            ],
        )
        connection.commit()
    finally:
        connection.close()

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
    monkeypatch.setenv("MEDICAL_QA_KG_CONFIG", str(config))

    def decide(request):
        state = request.payload["exploration_state"]
        context = request.payload["planner_context"]
        observations = context["observations"]
        if not observations:
            assert {
                item["skill_id"] for item in context["available_skills"]
            } == {
                "primekg-entity-expansion",
                "primekg-neighbor-exploration",
            }
            return {
                "decision": "continue_search",
                "state_digest": _state_digest(state),
                "selected_skill_id": "primekg-neighbor-exploration",
                "skill_arguments": {
                    "subject_entity_id": context["seed_entity_id"],
                    "direction": "outgoing",
                    "relations": ["disease_protein"],
                    "max_results": 10,
                },
                "expected_information": ["candidate proteins"],
            }
        if len(observations) == 1:
            primary = next(
                item
                for item in context["evidence_summary"]["sample_edges"]
                if item["relation"] == "disease_protein"
                and item["tail_name"] == "ERBB2"
            )
            return {
                "decision": "continue_search",
                "state_digest": _state_digest(state),
                "selected_skill_id": "primekg-neighbor-exploration",
                "skill_arguments": {
                    "subject_entity_id": primary["tail_id"],
                    "direction": "incoming",
                    "relations": ["pathway_protein"],
                    "max_results": 10,
                },
                "expected_information": ["a discriminating pathway constraint"],
            }
        primary = next(
            item
            for item in context["evidence_summary"]["sample_edges"]
            if item["relation"] == "disease_protein"
            and item["tail_name"] == "ERBB2"
        )
        constraint = next(
            item
            for item in context["evidence_summary"]["sample_edges"]
            if item["relation"] == "pathway_protein"
            and item["tail_id"] == primary["tail_id"]
        )
        return {
            "decision": "propose_candidate",
            "state_digest": _state_digest(state),
            "candidates": [
                {
                    "candidate_id": "candidate-erbb2-pi3k",
                    "seed_entity_id": context["seed_entity_id"],
                    "question_type": "constrained_entity",
                    "steps": [
                        {
                            "position": 0,
                            "relation": "disease_protein",
                            "direction": "forward",
                            "target_entity_type": "gene/protein",
                        }
                    ],
                    "constraints": [
                        {
                            "constraint_id": "constraint-pi3k",
                            "steps": [
                                {
                                    "position": 0,
                                    "relation": "pathway_protein",
                                    "direction": "reverse",
                                    "target_entity_type": "pathway",
                                }
                            ],
                            "terminal_entity_id": constraint["head_id"],
                        }
                    ],
                    "proposed_answer_ids": [primary["tail_id"]],
                    "private_answer_names": [primary["tail_name"]],
                    "answer_entity_type": "gene/protein",
                    "snapshot_id": state["provider_status"]["kg_snapshot_id"],
                    "evidence_ids": [
                        primary["evidence_id"],
                        constraint["evidence_id"],
                    ],
                    "public_entity_names": {
                        context["seed_entity_id"]: "Breast cancer",
                        constraint["head_id"]: "PI3K-Akt signaling pathway",
                    },
                    "discovery_source": "react",
                    "generator_version": "entity-react-v1",
                }
            ],
            "expected_information": ["complete-world validation"],
        }

    model = FakeStructuredModel(
        responses={"entity_question_exploration_planner": decide}
    )
    workspace = tmp_path / "workspace"
    result = EntityGenerationService(
        workspace=workspace,
        provider_id="primekg",
        model_mode="structured",
        model=model,
    ).run(
        EntityGenerationRequest(
            task_id="breast-cancer-v2",
            workspace=str(workspace),
            seed=UpstreamEntitySeed(
                provider_id="primekg",
                native_id="D1",
                entity_type="disease",
                canonical_name="Breast cancer",
            ),
            question_count=1,
        )
    )

    assert len(result.question_ids) == 1, [
        (item.role, item.payload) for item in model.invocations
    ]
    question = LocalArtifactRepository(workspace).load_latest_question(
        result.question_ids[0]
    )
    assert question.validation_level.value == "machine_validated"
    assert question.canonical_question == (
        "与Breast cancer相关且参与PI3K-Akt signaling pathway的蛋白是哪一种？"
    )
    assert question.gold_answer.short_answer == "ERBB2"
    assert question.query_intent["validation_report_id"]
    generation_repository = GenerationRepository(workspace)
    decisions = generation_repository.list_agent_decisions_by_episode(
        result.episode_id
    )
    feedback = generation_repository.list_validation_feedback_by_episode(
        result.episode_id
    )
    assert [item.decision.decision for item in decisions] == [
        "continue_search",
        "continue_search",
        "propose_candidate",
    ]
    assert [item.feedback.status for item in feedback] == ["validated"]
    invocations = generation_repository.list_skill_invocations_by_episode(
        result.episode_id
    )
    outcomes = generation_repository.list_skill_outcomes_by_episode(
        result.episode_id
    )
    assert [item.skill_id for item in invocations] == [
        "primekg-neighbor-exploration",
        "primekg-neighbor-exploration",
    ]
    assert [item.status for item in outcomes] == ["success", "success"]
    report_id = question.query_intent["validation_report_id"]
    assert LocalArtifactRepository(workspace).list_versions(
        "validation_reports", report_id
    ) == [report_id]
    progress = _generation_progress(
        LocalArtifactRepository(workspace), "breast-cancer-v2"
    )
    skill_steps = [
        item for item in progress["planner_steps"] if item.get("skill_id")
    ]
    assert [item["skill_id"] for item in skill_steps] == [
        "primekg-neighbor-exploration",
        "primekg-neighbor-exploration",
    ]
    assert [item["action_label"] for item in skill_steps] == [
        "Skill：PrimeKG 邻域探索",
        "Skill：PrimeKG 邻域探索",
    ]
    assert [
        item["action_label"]
        for item in progress["planner_steps"]
        if item["action_type"]
        in {"propose_candidate", "candidate_validation"}
    ] == ["提出候选任务", "候选验证通过"]


def _state_digest(payload: dict) -> str:
    import hashlib

    payload = {key: value for key, value in payload.items() if value is not None}
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

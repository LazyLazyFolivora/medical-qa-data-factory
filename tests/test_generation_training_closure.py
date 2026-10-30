"""Requirements-v2 closure: KG -> RAG -> Writer -> Verifier -> datasets."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from medical_qa_data_factory.generation.contracts import (
    EntityGenerationRequest,
    GenerationSpec,
    UpstreamEntitySeed,
)
from medical_qa_data_factory.generation.service import EntityGenerationService
from medical_qa_data_factory.generation.training import (
    PlannerStep,
    PlannerTrainingSample,
    QAItem,
    derive_training_samples,
)
from medical_qa_data_factory.generation.training_export import (
    export_training_data,
)
from medical_qa_data_factory.generation.training_repository import (
    TrainingRepository,
)
from medical_qa_data_factory.literature.contracts import (
    LiteratureCallStatus,
    LiteratureCapability,
    LiteratureChunkReadResult,
    LiteratureRetrievalResult,
    LiteratureSnapshot,
    RetrievedChunk,
)
from medical_qa_data_factory.llm import FakeStructuredModel


class _PinnedLiterature:
    """Small deterministic provider with a genuinely separate read-back call."""

    def __init__(self) -> None:
        self.search_calls = 0
        self.read_calls = 0
        self.content = (
            "Aspirin treatment is used for migraine. Aspirin contraindicates "
            "asthma in the supplied record."
        )

    def _snapshot(self) -> LiteratureSnapshot:
        return LiteratureSnapshot(
            provider="fake-literature",
            capability_version="fake-v1",
            snapshot_id="snapshot-1",
            knowledge_base_ids=["kb1"],
            pinned=True,
            retrieved_at="2026-10-21T00:00:00Z",
        )

    def preflight(self) -> LiteratureCapability:
        return LiteratureCapability(
            provider="fake-literature",
            capability_version="fake-v1",
            supports_search=True,
            supports_read_chunk=True,
            supports_pinned_snapshot=True,
            supports_source_offsets=True,
            max_limit=10,
            snapshot=self._snapshot(),
        )

    def search(self, request: Any) -> LiteratureRetrievalResult:
        self.search_calls += 1
        return LiteratureRetrievalResult(
            request_id=request.request_id,
            need_id=request.need_id,
            status=LiteratureCallStatus.SUCCESS,
            chunks=[
                RetrievedChunk(
                    chunk_id="chunk-1",
                    document_id="doc-1",
                    content=self.content,
                    title="Aspirin exposure record",
                    chunk_index=0,
                    start_at=10,
                    end_at=10 + len(self.content),
                )
            ],
            snapshot=self._snapshot(),
            trace_id="search-trace-1",
        )

    def read_chunk(self, request: Any) -> LiteratureChunkReadResult:
        self.read_calls += 1
        return LiteratureChunkReadResult(
            request_id=request.request_id,
            chunk_id=request.chunk_id,
            status=LiteratureCallStatus.SUCCESS,
            document_id="doc-1",
            content=self.content,
            chunk_index=0,
            start_at=10,
            end_at=10 + len(self.content),
            snapshot=self._snapshot(),
            trace_id="read-trace-1",
        )


def _model() -> FakeStructuredModel:
    def planner(request: Any) -> dict[str, Any]:
        context = request.payload
        if not context["observations"]:
            return {
                "action": {
                    "action_type": "query_neighbors",
                    "subject_entity_id": context["seed_entity_id"],
                    "direction": "outgoing",
                    "max_results": 10,
                },
                "expected_information": "a seed-linked fact",
                "evidence_gap": "no KG fact has been collected",
            }
        if any(
            item.get("action_type") == "retrieve_literature"
            for item in context["observations"]
        ):
            return {
                "action": {
                    "action_type": "finish",
                    "stop_reason": "evidence_sufficient",
                },
                "expected_information": "the frozen evidence is enough",
                "evidence_gap": "none",
                "cited_observation_ids": [
                    context["observations"][-1]["observation_id"]
                ],
            }
        if context["open_needs"]:
            need = context["open_needs"][0]
            return {
                "action": {
                    "action_type": "retrieve_literature",
                    "query": need["information_need"],
                    "entity_ids": [
                        need["subject_entity_id"],
                        need["object_entity_id"],
                    ],
                    "evidence_need_id": need["need_id"],
                    "max_results": 3,
                },
                "expected_information": "text support for the KG fact",
                "evidence_gap": need["information_need"],
                "cited_observation_ids": [
                    context["observations"][-1]["observation_id"]
                ],
            }
        return {
            "action": {
                "action_type": "finish",
                "stop_reason": "evidence_sufficient",
            },
            "expected_information": "the frozen evidence is enough",
            "evidence_gap": "none",
            "cited_observation_ids": [
                context["observations"][-1]["observation_id"]
            ],
        }

    writer_calls = {"count": 0}

    def writer(request: Any) -> dict[str, Any]:
        payload = request.payload
        writer_calls["count"] += 1
        literature_ids = []
        if writer_calls["count"] == 1 and payload["literature_evidence"]:
            literature_ids = [
                payload["literature_evidence"][0]["evidence_id"]
            ]
        return {
            "question_text": (
                "在给定医学记录中，与种子实体相关的是哪个实体？"
            ),
            "answer_entity_id": payload["candidate_answer"][
                "canonical_entity_id"
            ],
            "kg_evidence_ids": payload["blueprint"]["required_evidence_ids"],
            "literature_evidence_ids": literature_ids,
            "question_type": (
                "constrained_entity" if literature_ids else "single_fact"
            ),
            "language": "zh-CN",
        }

    def verifier(request: Any) -> dict[str, Any]:
        payload = request.payload
        required = set(payload["required_kg_evidence_ids"])
        edge = next(
            item
            for item in payload["kg_evidence"]
            if item["evidence_id"] in required
        )
        seed = payload["seed_entity_id"]
        answer_id = (
            edge["tail"]["canonical_id"]
            if edge["head"]["canonical_id"] == seed
            else edge["head"]["canonical_id"]
        )
        return {
            "draft_id": payload["draft_id"],
            "recomputed_answer_ids": [answer_id],
            "alternative_answer_ids": [],
            "evidence_supported": True,
            "answer_leaked": False,
            "difficulty": "medium" if payload["literature_evidence"] else "easy",
            "reason_codes": [],
        }

    def style_judge(request: Any) -> dict[str, Any]:
        return {
            "naturalness_score": 0.9,
            "persona_fit_score": 0.9,
            "intent_preserved": True,
            "unsupported_details": False,
            "technical_jargon": False,
            "answer_leaked": False,
            "reason_codes": [],
            "feedback": "natural and intent preserving",
        }

    return FakeStructuredModel(
        responses={
            "exploration_planner": planner,
            "question_writer": writer,
            "style_judge": style_judge,
            "answer_verifier": verifier,
        },
        model_config_id="fake-requirements-v2",
    )


def test_fake_rag_episode_persists_qa_items_and_training_samples(
    tmp_path: Path, tiny_primekg: Path, monkeypatch
) -> None:
    config = tmp_path / "kg.json"
    config.write_text(
        json.dumps(
            {
                "default_provider": "primekg",
                "providers": {
                    "primekg": {
                        "driver": "primekg_sqlite",
                        "database_path": tiny_primekg.as_posix(),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MEDICAL_QA_KG_CONFIG", str(config))
    literature = _PinnedLiterature()
    service = EntityGenerationService(
        workspace=tmp_path / "ws",
        provider_id="primekg",
        model_mode="fake",
        model=_model(),
        literature_provider=literature,
        literature_provider_id="fake-literature",
        literature_knowledge_base_ids=("kb1",),
        literature_snapshot_id="snapshot-1",
    )
    request = EntityGenerationRequest(
        task_id="rag-closure",
        workspace=str(tmp_path / "ws"),
        seed=UpstreamEntitySeed(
            provider_id="primekg",
            native_id="1191",
            entity_type="drug",
            canonical_name="aspirin",
        ),
        question_count=2,
        generation_spec=GenerationSpec(
            question_count=2,
            difficulty_targets=["easy", "medium"],
        ),
    )

    result = service.run(request)

    assert result.status == "completed"
    assert result.target_difficulties_satisfied is True
    assert len(result.qa_item_ids) == 2
    assert result.planner_trajectory_id
    assert literature.search_calls == 1
    assert literature.read_calls == 1
    training = TrainingRepository(str(tmp_path / "ws"))
    trajectory = training.load_trajectory(result.planner_trajectory_id)
    assert [step.selected_action.action_type for step in trajectory.steps] == [
        "query_neighbors",
        "retrieve_literature",
        "finish",
    ]
    assert trajectory.steps[1].outcome.evidence_gain == 1
    assert "min_total_evidence" in trajectory.steps[0].outcome.satisfied_constraints
    items = [training._generation.artifacts.load_latest(
        "qa_items", item_id, QAItem
    ) for item_id in result.qa_item_ids]
    assert {item.difficulty for item in items} == {"easy", "medium"}
    assert any(
        item.reasoning.required_literature_evidence_count == 1
        for item in items
    )
    assert all(item.evidence.provider_snapshot_ids for item in items)
    samples = derive_training_samples(trajectory)
    assert samples
    for sample in samples:
        assert isinstance(sample, PlannerTrainingSample)
        assert sample.input_state.available_actions
        assert sample.quality == "positive"
        training.save_sample(sample)
    assert all(isinstance(step, PlannerStep) for step in trajectory.steps)
    export_dir = export_training_data(tmp_path / "ws", tmp_path / "export")
    manifest = json.loads(
        (export_dir / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["qa_item_count"] == 2
    assert manifest["planner_sample_count"] == 3
    assert manifest["leakage_check"] == "passed"
    assert manifest["artifact_role"] == "generation_exploration"
    assert manifest["blind_solver_training_compatible"] is False
    exported_items = [
        json.loads(line)
        for line in (export_dir / "qa_items.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert {item["dataset_split"] for item in exported_items} <= {
        "train",
        "validation",
        "test",
    }

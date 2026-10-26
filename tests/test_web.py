from pathlib import Path
from types import SimpleNamespace
import importlib

import pytest
from starlette.testclient import TestClient

from medical_qa_data_factory.demo_adapters import (
    DemoDraftAgent,
    DemoKGTool,
    DemoLiteratureTool,
)
from medical_qa_data_factory.domain import (
    EntityRef,
    GenerationMode,
    GenerationTask,
    LinkStatus,
)
from medical_qa_data_factory.pipeline import DataFactoryPipeline
from medical_qa_data_factory.repository import LocalArtifactRepository
from medical_qa_data_factory.domain.common import utc_now
from medical_qa_data_factory.generation.contracts import (
    AgentStep,
    EpisodeState,
    GenerationEpisode,
    StopReason,
    ToolObservation,
    UpstreamEntitySeed,
)
from medical_qa_data_factory.generation.repository import GenerationRepository
from medical_qa_data_factory.evidence_enrichment.models import (
    EnrichmentStatus,
    EvidencePolarity,
    EvidenceSupportLevel,
    NormalizedEvidenceClaim,
    QuestionIntent,
)
from medical_qa_data_factory.evidence_enrichment.service import (
    EvidenceEnrichmentResult,
)
from medical_qa_data_factory.knowledge_graph.providers.primekg import (
    PrimeKGSQLiteProvider,
)
from medical_qa_data_factory.llm import FakeStructuredModel
from medical_qa_data_factory.web import create_app


web_app_module = importlib.import_module("medical_qa_data_factory.web.app")


@pytest.fixture(autouse=True)
def tiny_kg_provider(monkeypatch, tiny_primekg, tmp_path):
    provider = PrimeKGSQLiteProvider(
        tiny_primekg,
        entity_catalog_path=tmp_path / "web-entities.sqlite",
    )
    monkeypatch.setattr(
        web_app_module, "provider_from_config", lambda: provider
    )
    monkeypatch.setattr(
        web_app_module,
        "_structured_model_status",
        lambda: {
            "ready": False,
            "provider_id": None,
            "model_id": None,
            "detail": "未配置模型",
        },
    )
    yield provider
    provider.close()


def build_demo_workspace(root):
    repository = LocalArtifactRepository(root)
    seed = EntityRef(
        entity_ref_id="seed-aspirin",
        raw_mention="阿司匹林",
        canonical_name="aspirin",
        canonical_id="demo:aspirin",
        namespace="demo",
        entity_type="drug",
        aliases=["阿司匹林"],
        link_status=LinkStatus.EXACT,
        link_score=1.0,
        source="test",
    )
    task = GenerationTask.create(
        task_id="aspirin-web-demo",
        generation_mode=GenerationMode.HYBRID,
        seed_entities=[seed],
        target_question_count=1,
    )
    result = DataFactoryPipeline(
        repository=repository,
        kg_tool=DemoKGTool(),
        literature_tool=DemoLiteratureTool(),
        draft_agent=DemoDraftAgent(),
    ).run(task)
    return result


def test_generation_workspace_is_the_home_page(tmp_path):
    build_demo_workspace(tmp_path)
    client = TestClient(create_app(tmp_path))

    response = client.get("/")

    assert response.status_code == 200
    assert "生成医学问题" in response.text
    assert "生成的问题" in response.text
    assert "阿司匹林" in response.text
    assert "PTGS1" in response.text
    assert "DEMO" in response.text


def test_empty_workspace_exposes_primekg_seed_candidates(tmp_path):
    client = TestClient(create_app(tmp_path))

    response = client.get("/")

    assert response.status_code == 200
    assert 'data-entity-search' in response.text
    assert 'data-model-mode' in response.text
    assert 'data-question-style' in response.text
    assert "普通用户搜索" in response.text
    assert "医学学习者" in response.text
    assert "未配置模型" in response.text
    assert 'value="primekg:drug:1191"' in response.text
    assert "headache" in response.text


def test_generation_progress_endpoint_reports_persisted_steps(tmp_path):
    build_demo_workspace(tmp_path)
    now = utc_now()
    seed = UpstreamEntitySeed(
        provider_id="demo",
        native_id="aspirin",
        entity_type="drug",
        canonical_name="aspirin",
    )
    audit = GenerationRepository(tmp_path)
    audit.save_episode(
        GenerationEpisode(
            episode_id="episode-live-progress",
            episode_version="episode-live-progress.v1",
            task_id="aspirin-web-demo",
            seed=seed,
            state=EpisodeState.RUNNING,
            policy_version="test",
            question_target=1,
            step_ids=["step-live-progress"],
            started_at=now,
        )
    )
    audit.save_step(
        AgentStep(
            step_id="step-live-progress",
            step_version="step-live-progress.v1",
            episode_id="episode-live-progress",
            node="agent_explore",
            sequence=1,
            status="started",
            input_digest="digest",
            started_at=now,
        )
    )
    client = TestClient(create_app(tmp_path))

    response = client.get("/api/generation/aspirin-web-demo")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "running"
    assert payload["operations"]
    assert payload["current_step"]["node"] == "agent_explore"


def test_generation_progress_endpoint_is_pending_before_task_creation(tmp_path):
    client = TestClient(create_app(tmp_path))

    response = client.get("/api/generation/not-created-yet")

    assert response.status_code == 200
    assert response.json()["status"] == "pending"


def test_entity_search_endpoint_searches_primekg(tmp_path):
    client = TestClient(create_app(tmp_path))

    response = client.get("/api/entities", params={"q": "5742", "limit": 10})

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "primekg"
    assert payload["entities"][0]["name"] == "PTGS1"
    assert payload["entities"][0]["native_id"] == "5742"


def test_workbench_generation_accepts_graph_entity_without_workspace_record(
    tmp_path, monkeypatch
):
    captured = {}

    class FakeGenerationService:
        def __init__(self, **kwargs):
            pass

        def run(self, request):
            captured["request"] = request
            return SimpleNamespace(
                task_id=request.task_id,
                episode_id="episode-graph-seed",
                status="completed",
                question_ids=["question-graph-seed"],
                target_question_count=request.question_count,
            )

    monkeypatch.setattr(
        web_app_module, "EntityGenerationService", FakeGenerationService
    )
    client = TestClient(create_app(tmp_path))

    response = client.post(
        "/api/generate",
        json={
            "entity_canonical_id": "primekg:drug:1191",
            "question_count": 1,
            "difficulty_targets": ["easy"],
        },
    )

    assert response.status_code == 200
    request = captured["request"]
    assert request.seed.provider_id == "primekg"
    assert request.seed.native_id == "1191"
    assert request.seed.entity_type == "drug"


def test_workbench_rejects_unready_agent_before_writing_a_task(tmp_path):
    client = TestClient(create_app(tmp_path))

    response = client.post(
        "/api/generate",
        json={
            "entity_canonical_id": "primekg:drug:1191",
            "model_mode": "structured",
            "question_count": 1,
            "difficulty_targets": ["easy"],
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "model_config_error"
    assert "未配置模型" in response.json()["error"]
    assert LocalArtifactRepository(tmp_path).list_latest_tasks() == []


def test_workbench_can_run_structured_agent_path(
    tmp_path, tiny_kg_provider, monkeypatch
):
    service_module = importlib.import_module(
        "medical_qa_data_factory.generation.service"
    )
    monkeypatch.setattr(
        service_module,
        "provider_from_config",
        lambda provider_id=None: tiny_kg_provider,
    )

    def planner(request):
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
                "evidence_gap": "no graph fact has been collected",
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

    def writer(request):
        payload = request.payload
        profile = payload["style"]["profile"]
        questions = {
            "short_search": "阿司匹林与哪种疾病相关？",
            "conversational": "我想知道阿司匹林和哪种疾病有关。",
            "contextual": "我在查看阿司匹林的资料，想确认它和哪种疾病有关。",
        }
        return {
            "question_text": questions[profile],
            "answer_entity_id": payload["candidate_answer"][
                "canonical_entity_id"
            ],
            "kg_evidence_ids": payload["blueprint"]["required_evidence_ids"],
            "literature_evidence_ids": [],
            "question_type": "single_fact",
            "language": "zh-CN",
        }

    def verifier(request):
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
            "difficulty": "easy",
            "reason_codes": [],
        }

    def style_judge(request):
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

    model = FakeStructuredModel(
        responses={
            "exploration_planner": planner,
            "question_writer": writer,
            "style_judge": style_judge,
            "answer_verifier": verifier,
        },
        model_config_id="fake-web-agent",
    )

    class FakeEnricher:
        def enrich(self, edge, *, seed_entity_id, answer_entity_id):
            claim = NormalizedEvidenceClaim(
                claim_id=f"claim-enrichment-{edge.evidence_id}",
                kg_edge_id=edge.evidence_id,
                seed_entity_id=seed_entity_id,
                answer_entity_id=answer_entity_id,
                answer_entity_type="disease",
                relation=edge.relation,
                question_intent=QuestionIntent.ASSOCIATION_RESEARCH,
                support_level=EvidenceSupportLevel.LITERATURE_SUPPORTED,
                polarity=EvidencePolarity.UNKNOWN,
                claim_text="A fake abstract supports this pair.",
                source_snapshot_ids=["fake-pubmed"],
            )
            return EvidenceEnrichmentResult(
                edge_id=edge.evidence_id,
                status=EnrichmentStatus.SUPPORTED,
                claim=claim,
            )

    client = TestClient(
        create_app(
            tmp_path,
            structured_model=model,
            evidence_enricher=FakeEnricher(),
        )
    )

    response = client.post(
        "/api/generate",
        json={
            "entity_canonical_id": "primekg:drug:1191",
            "model_mode": "structured",
            "question_count": 1,
            "difficulty_targets": ["easy"],
            "language": "zh-CN",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["model_mode"] == "structured"
    assert len(response.json()["question_ids"]) == 3
    assert model.calls_for("exploration_planner")
    assert model.calls_for("question_writer")
    assert model.calls_for("style_judge")
    assert model.calls_for("answer_verifier")
    question = LocalArtifactRepository(tmp_path).load_latest_question(
        response.json()["question_ids"][0]
    )
    assert question.gold_answer.answer_mode.value == "entity"
    assert question.query_intent["question_intent"] == "association_research"
    assert question.query_intent["answer_candidate_count"] == 1


def test_entity_workspace_remains_available_as_the_question_library(tmp_path):
    build_demo_workspace(tmp_path)
    client = TestClient(create_app(tmp_path))

    response = client.get("/entities")

    assert response.status_code == 200
    assert "种子实体工作台" in response.text
    assert "阿司匹林" in response.text


def test_workbench_generation_runs_the_generation_service(tmp_path, monkeypatch):
    build_demo_workspace(tmp_path)
    captured = {}

    class FakeGenerationService:
        def __init__(self, **kwargs):
            captured["service"] = kwargs

        def run(self, request):
            captured["request"] = request
            return SimpleNamespace(
                task_id=request.task_id,
                episode_id="episode-web",
                status="completed",
                question_ids=["question-web"],
                target_question_count=request.question_count,
            )

    monkeypatch.setattr(
        web_app_module, "EntityGenerationService", FakeGenerationService
    )
    client = TestClient(create_app(tmp_path))

    response = client.post(
        "/api/generate",
        json={
            "entity_ref_id": "seed-aspirin",
            "run_id": "web-test-run",
            "question_style_ids": ["medical-learner-v1"],
            "question_count": 2,
            "difficulty_targets": ["easy", "hard"],
            "language": "zh-CN",
        },
    )

    assert response.status_code == 200
    assert response.json()["question_ids"] == ["question-web"]
    assert captured["service"]["provider_id"] == "demo"
    assert captured["service"]["model_mode"] == "rule-based"
    request = captured["request"]
    assert request.task_id == "web-test-run"
    assert request.seed.native_id == "aspirin"
    assert request.question_count == 2
    assert [item.value for item in request.generation_spec.difficulty_targets] == [
        "easy",
        "hard",
    ]
    assert [item.value for item in request.generation_spec.allowed_sources] == [
        "knowledge_graph"
    ]
    assert request.generation_spec.question_style_ids == ["medical-learner-v1"]
    assert request.generation_spec.variants_per_blueprint == 3


def test_workbench_generation_validates_inputs(tmp_path):
    build_demo_workspace(tmp_path)
    client = TestClient(create_app(tmp_path))

    response = client.post(
        "/api/generate",
        json={"entity_ref_id": "seed-aspirin", "question_count": 21},
    )

    assert response.status_code == 400
    assert "1 到 20" in response.json()["error"]


def test_workbench_generation_rejects_unknown_entity(tmp_path):
    build_demo_workspace(tmp_path)
    client = TestClient(create_app(tmp_path))

    response = client.post(
        "/api/generate",
        json={"entity_ref_id": "missing", "question_count": 2},
    )

    assert response.status_code == 404


def test_generation_workspace_visualizes_persisted_execution_trace(tmp_path):
    build_demo_workspace(tmp_path)
    now = utc_now()
    audit = GenerationRepository(tmp_path)
    seed = UpstreamEntitySeed(
        provider_id="demo",
        native_id="aspirin",
        entity_type="drug",
        canonical_name="aspirin",
    )
    audit.save_episode(
        GenerationEpisode(
            episode_id="episode-web-trace",
            episode_version="episode-web-trace.v1",
            task_id="aspirin-web-demo",
            seed=seed,
            state=EpisodeState.COMPLETED,
            policy_version="test",
            question_target=1,
            step_ids=["step-web-trace"],
            question_ids=["question-web-trace"],
            stop_reason=StopReason.EVIDENCE_SUFFICIENT,
            started_at=now,
            completed_at=now,
        )
    )
    audit.save_observation(
        ToolObservation(
            observation_id="observation-web-trace",
            episode_id="episode-web-trace",
            tool_name="kg_demo",
            operation_key="episode-web-trace|collect_neighbors|outgoing|1",
            request_digest="digest",
            row_count=7,
            elapsed_ms=420,
        )
    )
    audit.save_step(
        AgentStep(
            step_id="step-web-trace",
            step_version="step-web-trace.v2",
            episode_id="episode-web-trace",
            node="collect_neighbors",
            sequence=1,
            status="completed",
            input_digest="digest",
            output_artifact_ids=["bundle-web-trace"],
            tool_observation_ids=["observation-web-trace"],
            elapsed_ms=420,
            started_at=now,
            completed_at=now,
        )
    )
    client = TestClient(create_app(tmp_path))

    response = client.get("/")

    assert response.status_code == 200
    assert "本次生成做了什么" in response.text
    assert "探索图谱关系" in response.text
    assert "1 次查询" in response.text
    assert "7 条返回" in response.text
    assert "420 ms" in response.text


def test_runs_page_reads_latest_workspace_records(tmp_path):
    build_demo_workspace(tmp_path)
    client = TestClient(create_app(tmp_path))

    response = client.get("/tasks")

    assert response.status_code == 200
    assert "任务视图" in response.text
    assert "aspirin-web-demo" in response.text
    assert "completed" in response.text
    assert 'class="section-block artifacts task-questions"' not in response.text


def test_question_view_lists_questions_and_status_filter(tmp_path):
    result = build_demo_workspace(tmp_path)
    client = TestClient(create_app(tmp_path))

    response = client.get("/questions")

    assert response.status_code == 200
    assert "题目视图" in response.text
    assert 'data-table-status-target="question-table"' in response.text
    assert 'value="machine_validated"' in response.text
    assert result.questions[0].question_id in response.text
    assert result.questions[0].canonical_question in response.text
    assert result.task.task_id in response.text


def test_legacy_runs_route_remains_available(tmp_path):
    build_demo_workspace(tmp_path)
    client = TestClient(create_app(tmp_path))

    response = client.get("/runs")

    assert response.status_code == 200
    assert "任务视图" in response.text


def test_entity_detail_collects_evidence_questions_and_runs(tmp_path):
    result = build_demo_workspace(tmp_path)
    client = TestClient(create_app(tmp_path))

    response = client.get("/entities/seed-aspirin")

    assert response.status_code == 200
    assert "实体产出" in response.text
    assert "aspirin-web-demo" in response.text
    assert result.questions[0].question_id in response.text
    assert "PTGS1" in response.text


def test_task_detail_links_generated_artifacts(tmp_path):
    result = build_demo_workspace(tmp_path)
    client = TestClient(create_app(tmp_path))

    response = client.get(f"/tasks/{result.task.task_id}")

    assert response.status_code == 200
    assert "流水线状态" in response.text
    assert "对应生成的题目" in response.text
    assert result.bundles[0].bundle_id in response.text
    assert result.questions[0].question_id in response.text


def test_review_page_renders_evidence_spine(tmp_path):
    result = build_demo_workspace(tmp_path)
    client = TestClient(create_app(tmp_path))

    response = client.get(f"/review/{result.questions[0].question_id}")

    assert response.status_code == 200
    assert "Claim 脊柱" in response.text
    assert "PTGS1" in response.text
    assert "Demo evidence" in response.text
    assert 'data-evidence-id=' in response.text


def test_unknown_task_returns_404(tmp_path):
    client = TestClient(create_app(tmp_path))

    response = client.get("/tasks/missing")

    assert response.status_code == 404
    assert "没有找到这条记录" in response.text


def test_review_page_exposes_write_actions(tmp_path):
    result = build_demo_workspace(tmp_path)
    client = TestClient(create_app(tmp_path))

    response = client.get(f"/review/{result.questions[0].question_id}")

    assert 'data-review-action="confirm"' in response.text
    assert 'id="review-summary"' in response.text
    assert "只读预览" not in response.text


def test_confirm_question_persists_new_version(tmp_path):
    result = build_demo_workspace(tmp_path)
    question_id = result.questions[0].question_id
    client = TestClient(create_app(tmp_path))

    response = client.post(
        f"/api/review/questions/{question_id}",
        json={"action": "confirm", "summary": "人工确认", "verified_route": "hybrid"},
    )

    assert response.status_code == 200
    repository = LocalArtifactRepository(tmp_path)
    latest = repository.load_latest_question(question_id)
    assert latest.validation_level.value == "human_confirmed"
    assert len(repository.list_versions("questions", question_id)) == 2
    page = client.get(f"/review/{question_id}")
    assert "human_confirmed" in page.text


def test_review_requires_summary(tmp_path):
    result = build_demo_workspace(tmp_path)
    client = TestClient(create_app(tmp_path))

    response = client.post(
        f"/api/review/questions/{result.questions[0].question_id}",
        json={"action": "confirm", "summary": "  "},
    )

    assert response.status_code == 400
    assert "summary" in response.json()["error"]


def test_claim_review_endpoint_persists_bundle_version(tmp_path):
    result = build_demo_workspace(tmp_path)
    bundle = result.bundles[0]
    client = TestClient(create_app(tmp_path))

    response = client.post(
        f"/api/review/bundles/{bundle.claims[0].claim_id}",
        json={"status": "supported", "summary": "人工复核"},
    )

    assert response.status_code == 200
    repository = LocalArtifactRepository(tmp_path)
    assert len(repository.list_versions("bundles", bundle.bundle_id)) == 2


def test_claim_review_rejects_unknown_status(tmp_path):
    bundle = build_demo_workspace(tmp_path).bundles[0]
    client = TestClient(create_app(tmp_path))

    response = client.post(
        f"/api/review/bundles/{bundle.claims[0].claim_id}",
        json={"status": "maybe", "summary": "人工复核"},
    )

    assert response.status_code == 400


def test_dataset_page_lists_export_blockers(tmp_path):
    build_demo_workspace(tmp_path)
    client = TestClient(create_app(tmp_path))

    response = client.get("/dataset")

    assert response.status_code == 200
    assert "导出阻塞清单" in response.text
    assert "包含 mock 工具产物" in response.text


def test_formal_export_is_refused_for_mock_workspace(tmp_path):
    build_demo_workspace(tmp_path)
    client = TestClient(create_app(tmp_path))

    response = client.post("/api/export", json={"development": False})

    assert response.status_code == 409
    assert "blocked" in response.json()["error"]


def test_development_export_is_allowed(tmp_path):
    build_demo_workspace(tmp_path)
    client = TestClient(create_app(tmp_path))

    response = client.post("/api/export", json={"development": True})

    assert response.status_code == 200
    assert Path(response.json()["path"]).exists()

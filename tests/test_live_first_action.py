"""Opt-in live observation: one controller decision and its production tool action.

Run with MEDICAL_QA_LIVE_FIRST_ACTION=1; requires configured model and sources.
Results remain in data/runs/first-action-<uuid>, outside normal web tasks.
"""
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from medical_qa_data_factory.generation.agent_loop import ObjectiveQuestionAgentLoop
from medical_qa_data_factory.generation.objective_question_contracts import GenerationGoal, RunManifest
from medical_qa_data_factory.generation.objective_question_persistence import ObjectiveQuestionPersistence
from medical_qa_data_factory.generation.objective_question_runtime import ObjectiveRuntimeRepository, RuntimeBudget
from medical_qa_data_factory.generation.react_main_agent import REACT_WORKFLOW, ReactMainAgent
from medical_qa_data_factory.llm.registry import build_model_port
from medical_qa_data_factory.web.app import _objective_provider_map


@pytest.mark.skipif(os.getenv("MEDICAL_QA_LIVE_FIRST_ACTION") != "1", reason="Explicit live model/source opt-in required")
def test_live_first_action():
    run_id = "first-action-" + uuid4().hex[:12]
    root = Path(__file__).resolve().parents[1] / "data" / "runs" / run_id
    root.mkdir(parents=True, exist_ok=False)
    print(f"LIVE_OUTPUT={root}", flush=True)
    providers, graph = _objective_provider_map(["hgnc", "ncbi_gene", "ensembl", "primekg", "pubmed"], None)
    try:
        model = build_model_port()
        manifest = RunManifest(
            manifest_id=f"manifest.{run_id}", run_id=run_id,
            orchestrator_version=REACT_WORKFLOW, main_agent_version=REACT_WORKFLOW,
            subagent_role_versions={"all": "v1"},
            model_versions={"configured": getattr(model, "model_config_id", "configured-model")},
            schema_versions={"contracts": "objective-question-v1"}, validation_policy_version="final-question-v2",
            data_snapshot_or_retrieval_time_policy="live-retrieval", runtime_bundle_id=run_id,
            runtime_bundle_digest="0" * 64,
        )
        goal = GenerationGoal(
            run_id=run_id, target_entity="EGFR", target_entity_type="gene",
            question_count_upper_bound=1, generation_strategy="free",
            difficulty_targets=["easy", "medium", "hard"], style_targets=["concise"],
            allowed_fact_types=["experimental_observation", "comparison", "mechanism"], language="zh",
            run_budget={"max_actions": 24}, run_manifest_id=manifest.manifest_id, runtime_bundle_id=run_id,
        )
        # A normal task budget is visible to the model; the experiment stops after one action.
        runtime = ObjectiveRuntimeRepository.create(root, manifest=manifest, goal=goal,
                                                    budget=RuntimeBudget(max_cost=10, max_model_calls=72))
        (root / "experiment.json").write_text(json.dumps({
            "goal": goal.model_dump(mode="json"), "providers": sorted(providers),
            "live_model": True, "live_sources": True, "experiment_action_limit": 1,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        result = ObjectiveQuestionAgentLoop(
            runtime=runtime, persistence=ObjectiveQuestionPersistence(root), goal=goal,
            main_agent=ReactMainAgent(model, provider_ids=sorted(providers), memory_root=root),
            model=model, providers=providers, max_actions=1,
        ).run()
        decisions = runtime.ordered_decisions()
        summary = {
            "stop_reason": result.stop_reason,
            "decisions": [d.model_dump(mode="json") for d in decisions],
            "results": [{"type": a.artifact_type, "id": a.artifact_id, "payload": a.payload}
                        for a in result.runtime.artifacts.values()
                        if a.artifact_type in {"provider_result", "react_observation", "subagent_failure"}],
            "materials": [a.payload for a in result.runtime.artifacts.values() if a.artifact_type == "evidence_material"],
            "published_count": len(result.published_questions),
        }
        (root / "result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        assert len(decisions) == 1, "Experiment must not advance to a second controller decision"
        assert not result.published_questions
        # Successful harness execution is not a claim that retrieval or research succeeded.
        print(f"FIRST_ACTION={decisions[0].action}; MATERIALS={len(summary['materials'])}", flush=True)
    finally:
        if graph is not None:
            graph.close()

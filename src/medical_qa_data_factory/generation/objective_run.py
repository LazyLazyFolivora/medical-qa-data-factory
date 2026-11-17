"""Shared setup and execution for objective question runs."""
from __future__ import annotations

import json
import os
from pathlib import Path

from ..evidence_enrichment.ctd import CtdEvidenceIndex
from ..knowledge_graph import provider_from_config
from .agent_loop import ObjectiveQuestionAgentLoop
from .fact_source_policy import default_objective_fact_types
from .objective_provider_adapters import CtdMaterialProvider, PrimeKgMaterialProvider, PubMedMaterialProvider
from .objective_question_contracts import GenerationGoal, RunManifest, canonical_digest
from .objective_question_persistence import ObjectiveQuestionPersistence
from .objective_question_runtime import ObjectiveRuntimeRepository, RuntimeBudget
from .react_main_agent import REACT_WORKFLOW, ReactMainAgent
from .structured_biomedical_providers import EnsemblMaterialProvider, HgncMaterialProvider, NcbiGeneMaterialProvider

DEFAULT_PROVIDERS = ("hgnc", "ncbi_gene", "ensembl", "primekg", "pubmed")
DEFAULT_DIFFICULTIES = ("easy",)
DEFAULT_GENERATION_STRATEGY = "free"
DEFAULT_STYLE_TARGETS = ("concise",)
DEFAULT_MAX_ACTIONS = 50
DEFAULT_MAX_COST = 10.0
DEFAULT_MAX_MODEL_CALLS = 130


def objective_provider_map(
    providers,
    objective_providers=None,
    *,
    ctd_index_path=None,
) -> tuple[dict[str, object], object | None]:
    provider_map = dict(objective_providers or {})
    kg_provider = None
    if "pubmed" in providers and "pubmed" not in provider_map:
        provider_map["pubmed"] = PubMedMaterialProvider()
    if "ctd" in providers and "ctd" not in provider_map:
        ctd_index_path = ctd_index_path or os.environ.get("MEDICAL_QA_CTD_INDEX")
        if not ctd_index_path:
            evidence_config = Path("config/evidence_enrichment.json")
            if evidence_config.is_file():
                ctd_index_path = json.loads(
                    evidence_config.read_text(encoding="utf-8")
                ).get("ctd_index_path")
        if not ctd_index_path:
            raise ValueError(
                "CTD provider requires MEDICAL_QA_CTD_INDEX or "
                "config/evidence_enrichment.json"
            )
        provider_map["ctd"] = CtdMaterialProvider(
            CtdEvidenceIndex(ctd_index_path)
        )
    if "hgnc" in providers and "hgnc" not in provider_map:
        provider_map["hgnc"] = HgncMaterialProvider()
    if "ncbi_gene" in providers and "ncbi_gene" not in provider_map:
        provider_map["ncbi_gene"] = NcbiGeneMaterialProvider()
    if "ensembl" in providers and "ensembl" not in provider_map:
        provider_map["ensembl"] = EnsemblMaterialProvider()
    if "primekg" in providers and "primekg" not in provider_map:
        kg_provider = provider_from_config("primekg")
        provider_map["primekg"] = PrimeKgMaterialProvider(kg_provider)
    selected = {
        name: provider_map[name]
        for name in providers
        if name in provider_map
    }
    if not selected:
        raise ValueError("no usable objective provider configured")
    return selected, kg_provider


def create_objective_run(
    root,
    *,
    run_id,
    entity_name,
    entity_id,
    entity_type,
    question_count,
    model,
    providers,
    model_provider_id=None,
    difficulty_targets=DEFAULT_DIFFICULTIES,
    generation_strategy=DEFAULT_GENERATION_STRATEGY,
    style_targets=DEFAULT_STYLE_TARGETS,
    allowed_fact_types=None,
    max_actions=DEFAULT_MAX_ACTIONS,
    max_cost=DEFAULT_MAX_COST,
    max_model_calls=DEFAULT_MAX_MODEL_CALLS,
):
    model_versions = {"structured": getattr(model, "model_config_id", "structured-model")}
    if model_provider_id:
        model_versions["provider_id"] = model_provider_id
    provider_ids = sorted(providers)
    if allowed_fact_types is None:
        allowed_fact_types = default_objective_fact_types(entity_type, available_providers=provider_ids)
    manifest = RunManifest(
        manifest_id=f"manifest.{run_id}",
        run_id=run_id,
        orchestrator_version=REACT_WORKFLOW,
        main_agent_version=REACT_WORKFLOW,
        subagent_role_versions={
            "material_retrieval": "material-retrieval-v1",
            "fact_understanding": "fact-understanding-v1",
            "objective_research_agent": "research-v1",
            "objective_question_discovery": "discovery-v1",
            "objective_question_design": "strategy-v1",
            "objective_difficulty_assessment": "difficulty-v1",
            "objective_answer_verification": "final-question-v2",
            "objective_question_realization": "objective-question-realization-v1",
            "objective_release_review": "objective-release-review-v1",
        },
        model_versions=model_versions,
        provider_adapter_versions=dict.fromkeys(provider_ids, "v1"),
        schema_versions={
            "objective_question_contracts": "objective-question-v1",
            "runtime": "objective-runtime-v1",
        },
        validation_policy_version="final-question-v2",
        data_snapshot_or_retrieval_time_policy="pinned-per-run",
        runtime_bundle_id="objective-runtime-v1",
        runtime_bundle_digest=canonical_digest({
            "orchestrator": REACT_WORKFLOW,
            "model": model_versions["structured"],
            "providers": provider_ids,
        }),
    )
    goal = GenerationGoal(
        run_id=run_id,
        target_entity=entity_name,
        target_entity_id=entity_id,
        target_entity_type="gene" if entity_type == "gene/protein" else entity_type,
        question_count_upper_bound=question_count,
        difficulty_targets=list(difficulty_targets),
        generation_strategy=generation_strategy,
        style_targets=list(style_targets),
        allowed_fact_types=allowed_fact_types,
        run_budget={
            "max_actions": max_actions,
            "max_cost": max_cost,
            "max_model_calls": max_model_calls,
        },
        run_manifest_id=manifest.manifest_id,
        runtime_bundle_id=manifest.runtime_bundle_id,
    )
    return ObjectiveRuntimeRepository.create(
        root, manifest=manifest, goal=goal,
        budget=RuntimeBudget(max_cost=max_cost, max_model_calls=max_model_calls),
    )


def run_objective_loop(runtime, goal, model, providers, *, max_actions=None, main_agent=None):
    if max_actions is None:
        # Older checkpoints may omit the action limit.
        max_actions = int(goal.run_budget.get("max_actions", 100))
    return ObjectiveQuestionAgentLoop(
        runtime=runtime,
        persistence=ObjectiveQuestionPersistence(runtime.root),
        goal=goal,
        main_agent=main_agent or ReactMainAgent(
            model, provider_ids=sorted(providers), memory_root=runtime.root,
        ),
        model=model,
        providers=providers,
        max_actions=max_actions,
    ).run()

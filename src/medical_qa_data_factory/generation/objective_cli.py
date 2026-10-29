"""CLI assembly for the objective-question main-agent loop."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..knowledge_graph import provider_from_config
from ..llm.registry import build_model_port
from .agent_loop import ObjectiveQuestionAgentLoop
from .main_agent import StructuredMainAgent
from .objective_provider_adapters import (
    CtdMaterialProvider,
    PrimeKgMaterialProvider,
    PubMedMaterialProvider,
)
from .structured_biomedical_providers import (
    EnsemblMaterialProvider,
    HgncMaterialProvider,
    NcbiGeneMaterialProvider,
)
from ..evidence_enrichment.ctd import CtdEvidenceIndex
from .objective_question_contracts import (
    GenerationGoal,
    RunManifest,
)
from .objective_question_persistence import ObjectiveQuestionPersistence
from .objective_question_release import export_objective_release
from .objective_question_runtime import (
    ObjectiveRuntimeRepository,
    RuntimeBudget,
)


def run_objective_generation_command(args) -> int:
    output = Path(args.output)
    workspace = Path(args.workspace)
    providers_requested = list(
        dict.fromkeys(
            args.provider
            or [
                "hgnc",
                "ncbi_gene",
                "ensembl",
                "primekg",
                "pubmed",
            ]
        )
    )
    kg_provider = None
    try:
        model = build_model_port(config_path=args.model_config)
        providers = {}
        if "primekg" in providers_requested:
            kg_provider = provider_from_config("primekg")
            providers["primekg"] = PrimeKgMaterialProvider(kg_provider)
        if "pubmed" in providers_requested:
            providers["pubmed"] = PubMedMaterialProvider()
        if "hgnc" in providers_requested:
            providers["hgnc"] = HgncMaterialProvider()
        if "ncbi_gene" in providers_requested:
            providers["ncbi_gene"] = NcbiGeneMaterialProvider()
        if "ensembl" in providers_requested:
            providers["ensembl"] = EnsemblMaterialProvider()
        if "ctd" in providers_requested:
            ctd_index_path = args.ctd_index
            if not ctd_index_path:
                evidence_config = Path("config/evidence_enrichment.json")
                if evidence_config.is_file():
                    ctd_index_path = json.loads(
                        evidence_config.read_text(encoding="utf-8")
                    ).get("ctd_index_path")
            if not ctd_index_path:
                raise ValueError(
                    "CTD provider requires --ctd-index or ctd_index_path config"
                )
            providers["ctd"] = CtdMaterialProvider(
                CtdEvidenceIndex(ctd_index_path)
            )
        if not providers:
            raise ValueError("at least one material provider is required")

        manifest_payload = {
            "orchestrator": "objective-agent-loop-v1",
            "model": getattr(model, "model_config_id", "structured-model"),
            "providers": {
                key: value.capability.model_dump(mode="json")
                for key, value in providers.items()
            },
        }
        bundle_digest = hashlib.sha256(
            json.dumps(
                manifest_payload,
                sort_keys=True,
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        manifest = RunManifest(
            manifest_id=f"manifest.{args.run_id}",
            run_id=args.run_id,
            orchestrator_version="objective-agent-loop-v1",
            main_agent_version="objective-main-agent-v1",
            subagent_role_versions={
                "material_retrieval": "material-retrieval-v1",
                "fact_understanding": "fact-understanding-v1",
                "objective_question_design": "objective-question-design-v1",
                "objective_answer_verification": (
                    "objective-answer-verification-v1"
                ),
                "objective_question_realization": (
                    "objective-question-realization-v1"
                ),
                "objective_release_review": "objective-release-review-v1",
            },
            model_versions={
                "structured": getattr(
                    model,
                    "model_config_id",
                    "structured-model",
                )
            },
            schema_versions={
                "objective_question_contracts": "objective-question-v1",
                "runtime": "objective-runtime-v1",
            },
            validation_policy_version="objective-policy-v1",
            data_snapshot_or_retrieval_time_policy="pinned-per-run",
            runtime_bundle_id="objective-runtime-v1",
            runtime_bundle_digest=bundle_digest,
        )
        runtime = ObjectiveRuntimeRepository.create(
            workspace,
            manifest=manifest,
            goal=_goal(args, bundle_digest),
            budget=RuntimeBudget(
                max_cost=args.max_cost,
                max_model_calls=args.max_model_calls,
            ),
        )
        loop = ObjectiveQuestionAgentLoop(
            runtime=runtime,
            persistence=ObjectiveQuestionPersistence(workspace),
            goal=_goal(args, bundle_digest),
            main_agent=StructuredMainAgent(
                model,
                provider_ids=sorted(providers),
            ),
            model=model,
            providers=providers,
            max_actions=args.max_actions,
        )
        result = loop.run()
        export_objective_release(result, output)
        print(
            json.dumps(
                {
                    "run_id": result.run_id,
                    "status": result.run_state.value,
                    "stop_reason": result.stop_reason,
                    "published_count": len(result.published_questions),
                    "output": str(output.resolve()),
                    "providers": sorted(providers),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "code": str(getattr(exc, "code", "objective_generation_error")),
                    "message": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    finally:
        if kg_provider is not None:
            kg_provider.close()


def _goal(args, bundle_digest: str) -> GenerationGoal:
    return GenerationGoal(
        run_id=args.run_id,
        target_entity=args.entity_name,
        target_entity_id=args.entity_id,
        target_entity_type=args.entity_type,
        question_count_upper_bound=args.question_count,
        difficulty_targets=["easy"],
        style_targets=["concise"],
        allowed_fact_types=["entity_identity", "classification", "gene_location"],
        run_budget={
            "max_actions": args.max_actions,
            "max_cost": args.max_cost,
            "max_model_calls": args.max_model_calls,
        },
        run_manifest_id=f"manifest.{args.run_id}",
        runtime_bundle_id="objective-runtime-v1",
    )

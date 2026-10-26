"""Command-line entry point for local data-factory operations."""
from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from .demo_adapters import DemoDraftAgent, DemoKGTool, DemoLiteratureTool
from .domain import EntityRef, GenerationMode, GenerationTask, LinkStatus
from .pipeline import DataFactoryPipeline
from .repository import LocalArtifactRepository
from .schema_export import export_json_schemas

SUPPORTED_MODEL_MODES = ("rule-based", "fake", "structured")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="medical-qa-data-factory")
    commands = parser.add_subparsers(dest="command", required=True)

    schema = commands.add_parser("schema", help="Manage public data contracts")
    schema_commands = schema.add_subparsers(dest="schema_command", required=True)
    export = schema_commands.add_parser("export", help="Export JSON Schema files")
    export.add_argument("--output", required=True, help="Destination directory")

    demo = commands.add_parser("demo", help="Run development-only mock flows")
    demo_commands = demo.add_subparsers(dest="demo_command", required=True)
    run = demo_commands.add_parser("run", help="Run seed-to-draft demo pipeline")
    run.add_argument("--output", required=True, help="Artifact repository directory")
    run.add_argument("--task-id", required=True)
    run.add_argument("--seed", required=True, help="Seed entity name")

    web = commands.add_parser("web", help="Run the local visual workbench")
    web_commands = web.add_subparsers(dest="web_command", required=True)
    serve = web_commands.add_parser("serve", help="Serve the local workbench")
    serve.add_argument("--workspace", required=True, help="Artifact repository")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)

    export = commands.add_parser("export", help="Export a gated dataset snapshot")
    export.add_argument("--workspace", required=True, help="Artifact repository")
    export.add_argument("--output", required=True, help="Export destination")
    export.add_argument(
        "--development",
        action="store_true",
        help="Allow mock or unconfirmed records in a watermarked snapshot",
    )
    export.add_argument(
        "--level",
        dest="level",
        choices=["development", "machine_validated", "human_confirmed"],
        default=None,
        help=(
            "The export level. It is chosen explicitly and never inferred "
            "from the records or the output directory."
        ),
    )

    validate = commands.add_parser(
        "validate", help="Run automated validators against stored questions"
    )
    validate_commands = validate.add_subparsers(
        dest="validate_command", required=True
    )
    run_validators = validate_commands.add_parser(
        "run", help="Run the full validator suite and store new versions"
    )
    run_validators.add_argument("--workspace", required=True)
    run_validators.add_argument(
        "--question-id",
        action="append",
        help="Only validate this question id (repeatable)",
    )
    status_parser = validate_commands.add_parser(
        "status", help="Report check status per question without writing"
    )
    status_parser.add_argument("--workspace", required=True)
    status_parser.add_argument(
        "--question-id",
        action="append",
        help="Only report this question id (repeatable)",
    )

    evaluation = commands.add_parser(
        "eval", help="Run a pinned acceptance manifest and write a report"
    )
    evaluation_commands = evaluation.add_subparsers(
        dest="evaluation_command", required=True
    )
    evaluation_run = evaluation_commands.add_parser(
        "run", help="Run the manifest's seeds and write the acceptance report"
    )
    evaluation_run.add_argument("--manifest", required=True)
    evaluation_run.add_argument("--workspace", required=True)
    evaluation_run.add_argument("--provider", default="primekg")
    evaluation_run.add_argument(
        "--model-mode", dest="model_mode", default="rule-based"
    )
    evaluation_run.add_argument("--report", default=None)
    evaluation_react = evaluation_commands.add_parser(
        "react",
        help="Measure the real graph's multi-hop reachability for the manifest",
    )
    evaluation_react.add_argument("--manifest", required=True)
    evaluation_react.add_argument("--provider", default="primekg")
    evaluation_react.add_argument("--report", default=None)

    literature = commands.add_parser(
        "literature", help="Diagnose the configured literature providers"
    )
    literature_commands = literature.add_subparsers(
        dest="literature_command", required=True
    )
    literature_providers = literature_commands.add_parser(
        "providers", help="List configured literature providers without their secrets"
    )
    literature_providers.add_argument(
        "--config",
        dest="config_path",
        default=None,
        help="Configuration file; defaults to MEDICAL_QA_LITERATURE_CONFIG",
    )
    literature_preflight = literature_commands.add_parser(
        "preflight", help="Report what the provider can do, including snapshot pinning"
    )
    literature_preflight.add_argument(
        "--config", dest="config_path", default=None
    )
    literature_preflight.add_argument("--provider", default=None)

    evidence = commands.add_parser(
        "evidence", help="Build and inspect evidence-enrichment sources"
    )
    evidence_commands = evidence.add_subparsers(
        dest="evidence_command", required=True
    )
    evidence_ctd = evidence_commands.add_parser(
        "ctd", help="Manage the CTD chemical-disease evidence index"
    )
    evidence_ctd_commands = evidence_ctd.add_subparsers(
        dest="ctd_command", required=True
    )
    evidence_ctd_build = evidence_ctd_commands.add_parser(
        "build", help="Build a read-only CTD sidecar from the official CSV"
    )
    evidence_ctd_build.add_argument("--source", required=True)
    evidence_ctd_build.add_argument("--output", required=True)
    evidence_ctd_lookup = evidence_ctd_commands.add_parser(
        "lookup", help="Look up one exact chemical/disease pair"
    )
    evidence_ctd_lookup.add_argument("--index", required=True)
    evidence_ctd_lookup.add_argument("--chemical-id", required=True)
    evidence_ctd_lookup.add_argument("--disease", required=True)

    evidence_pubmed = evidence_commands.add_parser(
        "pubmed", help="Read PubMed metadata and abstracts"
    )
    evidence_pubmed_commands = evidence_pubmed.add_subparsers(
        dest="pubmed_command", required=True
    )
    evidence_pubmed_fetch = evidence_pubmed_commands.add_parser(
        "fetch", help="Fetch PubMed abstracts by PMID"
    )
    evidence_pubmed_fetch.add_argument("--pmid", action="append", required=True)
    evidence_pubmed_fetch.add_argument("--limit", type=int, default=5)
    evidence_pubmed_search = evidence_pubmed_commands.add_parser(
        "search", help="Search PubMed and print matching PMIDs"
    )
    evidence_pubmed_search.add_argument("--query", required=True)
    evidence_pubmed_search.add_argument("--limit", type=int, default=10)

    training = commands.add_parser(
        "training", help="Export Query Planner training artifacts"
    )
    training_commands = training.add_subparsers(
        dest="training_command", required=True
    )
    training_export = training_commands.add_parser(
        "export", help="Export QAItems and Planner samples as JSONL"
    )
    training_export.add_argument("--workspace", required=True)
    training_export.add_argument("--output", required=True)

    model = commands.add_parser(
        "model", help="Diagnose the configured structured model providers"
    )
    model_commands = model.add_subparsers(dest="model_command", required=True)
    model_providers = model_commands.add_parser(
        "providers", help="List configured model providers without their secrets"
    )
    model_providers.add_argument(
        "--config",
        dest="config_path",
        default=None,
        help="Configuration file; defaults to MEDICAL_QA_MODEL_CONFIG",
    )

    kg = commands.add_parser("kg", help="Diagnose configured knowledge graphs")
    kg_commands = kg.add_subparsers(dest="kg_command", required=True)
    kg_commands.add_parser("providers", help="List configured providers")

    kg_snapshot = kg_commands.add_parser(
        "snapshot", help="Manage the database snapshot manifest"
    )
    kg_snapshot_commands = kg_snapshot.add_subparsers(
        dest="snapshot_command", required=True
    )
    kg_snapshot_init = kg_snapshot_commands.add_parser(
        "init", help="Compute the database digest once (refuses to overwrite)"
    )
    kg_snapshot_init.add_argument("--database", required=True)
    kg_snapshot_refresh = kg_snapshot_commands.add_parser(
        "refresh", help="Recompute the digest and back up the previous manifest"
    )
    kg_snapshot_refresh.add_argument("--database", required=True)

    kg_health = kg_commands.add_parser("health", help="Verify database and schema")
    kg_health.add_argument("--provider")

    kg_resolve = kg_commands.add_parser("resolve", help="Resolve an entity mention")
    kg_resolve.add_argument("--provider")
    kg_resolve.add_argument("--entity", required=True)
    kg_resolve.add_argument("--native-id", dest="native_id")
    kg_resolve.add_argument("--type", dest="entity_type", action="append", default=[])
    kg_resolve.add_argument("--limit", type=int, default=10)

    kg_neighbors = kg_commands.add_parser("neighbors", help="List bounded neighbors")
    kg_neighbors.add_argument("--provider")
    kg_neighbors.add_argument("--entity", required=True)
    kg_neighbors.add_argument("--native-id", dest="native_id")
    kg_neighbors.add_argument(
        "--direction", choices=["outgoing", "incoming", "both"], default="both"
    )
    kg_neighbors.add_argument("--relation", action="append", default=[])
    kg_neighbors.add_argument("--target-type", dest="target_types", action="append", default=[])
    kg_neighbors.add_argument("--limit", type=int, default=20)

    kg_paths = kg_commands.add_parser("paths", help="Run a bounded path query")
    kg_paths.add_argument("--provider")
    kg_paths.add_argument("--start", required=True)
    kg_paths.add_argument("--end")
    kg_paths.add_argument("--max-hops", dest="max_hops", type=int, default=2)
    kg_paths.add_argument("--max-paths", dest="max_paths", type=int, default=50)

    generate = commands.add_parser(
        "generate", help="Generate reviewable questions from an upstream entity"
    )
    generate_commands = generate.add_subparsers(
        dest="generate_command", required=True
    )
    entity = generate_commands.add_parser(
        "entity", help="Run entity-anchored question generation"
    )
    entity.add_argument("--workspace", required=True)
    entity.add_argument("--task-id", required=True)
    entity.add_argument("--provider", default="primekg")
    entity.add_argument("--entity-name", dest="entity_name", required=True)
    entity.add_argument("--entity-id", dest="entity_id", required=True)
    entity.add_argument("--entity-type", dest="entity_type", required=True)
    entity.add_argument("--question-count", dest="question_count", type=int, default=None)
    entity.add_argument(
        "--generation-spec",
        dest="generation_spec",
        default=None,
        help="JSON GenerationSpec file",
    )
    entity.add_argument(
        "--difficulty",
        action="append",
        choices=["easy", "medium", "hard"],
        default=None,
        help="Target difficulty (repeatable); overrides the spec file",
    )
    entity.add_argument(
        "--literature-policy",
        choices=["forbidden", "when_needed", "required"],
        default=None,
        help="Whether RAG is forbidden, chosen on demand, or mandatory",
    )
    entity.add_argument(
        "--language", default=None, help="Output language, e.g. zh-CN"
    )
    entity.add_argument(
        "--safety-budget",
        dest="safety_budget",
        default=None,
        help="JSON SafetyBudget file, separate from GenerationSpec",
    )
    entity.add_argument(
        "--model-mode",
        dest="model_mode",
        choices=list(SUPPORTED_MODEL_MODES),
        default="rule-based",
        help=(
            "rule-based: deterministic single-hop baseline (no ReAct); "
            "fake: scripted model double; structured: real configured model"
        ),
    )
    entity.add_argument("--raw-mention", dest="raw_mention")

    validated = generate_commands.add_parser(
        "validated",
        help="Generate a frozen, world-validated task release",
    )
    validated.add_argument("--output", required=True)
    validated.add_argument("--provider", default="primekg")
    validated.add_argument("--entity-name", dest="entity_name", required=True)
    validated.add_argument("--entity-id", dest="entity_id", required=True)
    validated.add_argument("--entity-type", dest="entity_type", required=True)
    validated.add_argument(
        "--kg-config",
        dest="kg_config",
        default=None,
        help="Knowledge graph provider configuration file",
    )
    validated.add_argument(
        "--page-size",
        dest="page_size",
        type=int,
        choices=range(1, 1001),
        default=500,
    )

    resume = generate_commands.add_parser(
        "resume", help="Resume a previous generation episode"
    )
    resume.add_argument("--workspace", required=True)
    resume.add_argument("--episode-id", dest="episode_id", required=True)
    resume.add_argument("--task-id", dest="task_id", required=True)
    resume.add_argument("--provider", default="primekg")
    resume.add_argument("--entity-name", dest="entity_name", required=True)
    resume.add_argument("--entity-id", dest="entity_id", required=True)
    resume.add_argument("--entity-type", dest="entity_type", required=True)
    resume.add_argument("--question-count", dest="question_count", type=int, default=None)
    resume.add_argument("--generation-spec", dest="generation_spec", default=None)
    resume.add_argument(
        "--difficulty",
        action="append",
        choices=["easy", "medium", "hard"],
        default=None,
    )
    resume.add_argument(
        "--literature-policy",
        choices=["forbidden", "when_needed", "required"],
        default=None,
    )
    resume.add_argument("--language", default=None)
    resume.add_argument("--safety-budget", dest="safety_budget", default=None)
    resume.add_argument(
        "--model-mode",
        dest="model_mode",
        choices=list(SUPPORTED_MODEL_MODES),
        default="rule-based",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """The CLI entry point."""

    args = build_parser().parse_args(argv)
    if args.command == "schema" and args.schema_command == "export":
        paths = export_json_schemas(args.output)
        print(f"Exported {len(paths)} schemas to {args.output}")
        return 0
    if args.command == "demo" and args.demo_command == "run":
        seed = EntityRef(
            entity_ref_id=f"demo-{args.seed}",
            raw_mention=args.seed,
            canonical_name=args.seed,
            canonical_id=f"demo:{args.seed}",
            namespace="demo",
            entity_type="unknown",
            aliases=[],
            link_status=LinkStatus.EXACT,
            link_score=1.0,
            source="cli",
        )
        task = GenerationTask.create(
            task_id=args.task_id,
            generation_mode=GenerationMode.AUTOMATIC,
            seed_entities=[seed],
            target_question_count=1,
        )
        pipeline = DataFactoryPipeline(
            repository=LocalArtifactRepository(args.output),
            kg_tool=DemoKGTool(),
            literature_tool=DemoLiteratureTool(),
            draft_agent=DemoDraftAgent(),
        )
        result = pipeline.run(task)
        print(
            json.dumps(
                {
                    "task_id": result.task.task_id,
                    "status": result.task.status.value,
                    "bundle_count": len(result.bundles),
                    "question_count": len(result.questions),
                    "artifact_ids": result.task.artifact_ids,
                    "warning": "demo adapters only; artifacts remain draft",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "web" and args.web_command == "serve":
        import uvicorn

        from .web import create_app

        uvicorn.run(
            create_app(args.workspace),
            host=args.host,
            port=args.port,
            log_level="info",
        )
        return 0
    if args.command == "export":
        from .export import (
            ExportBlockedError,
            ExportLevel,
            write_dataset_export,
        )

        level = ExportLevel(args.level) if args.level else None
        try:
            target = write_dataset_export(
                LocalArtifactRepository(args.workspace),
                args.output,
                development=args.development,
                level=level,
            )
        except ExportBlockedError as exc:
            print(f"export blocked: {exc}")
            return 1
        print(f"Exported dataset to {target}")
        return 0
    if args.command == "validate":
        return _run_validate_command(args)
    if args.command == "eval":
        from .evaluation.cli import evaluation_command, react_evaluation_command

        if args.evaluation_command == "react":
            return react_evaluation_command(
                args.manifest,
                provider_id=args.provider,
                report_path=args.report,
            )
        return evaluation_command(
            args.manifest,
            workspace=args.workspace,
            provider_id=args.provider,
            model_mode=args.model_mode,
            report_path=args.report,
        )
    if args.command == "literature":
        return _run_literature_command(args)
    if args.command == "evidence":
        return _run_evidence_command(args)
    if args.command == "training":
        from .generation.training_export import (
            SplitLeakageError,
            export_training_data,
        )

        try:
            target = export_training_data(args.workspace, args.output)
        except SplitLeakageError as exc:
            _generation_error(exc.code, str(exc))
            return 1
        except FileExistsError as exc:
            _generation_error("output_exists", str(exc))
            return 2
        _emit(
            {
                "status": "exported",
                "output": str(target),
                "manifest": str(target / "manifest.json"),
            }
        )
        return 0
    if args.command == "model":
        return _run_model_command(args)
    if args.command == "kg":
        return _run_kg_command(args)
    if args.command == "generate":
        if args.generate_command == "validated":
            return _run_validated_generation_command(args)
        return _run_generate_command(args)
    raise AssertionError("unreachable command")


#: exit 2 = argument or identity error, 3 = capability/config, 4 = transient
#: provider, 1 = internal.
GENERATION_EXIT_CODES = {
    "invalid_arguments": 2,
    "invalid_upstream_identity": 2,
    "capability_error": 3,
    "environment_error": 3,
    "model_config_error": 3,
    "literature_config_error": 3,
    "kg_config_error": 3,
    "kg_capability_error": 3,
    "episode_not_found": 2,
    "checkpoint_not_found": 2,
    "episode_mismatch": 2,
    "kg_unavailable": 4,
    "kg_query_timeout": 4,
    "kg_query_error": 4,
    "kg_data_integrity": 3,
    "output_exists": 2,
    "idempotency_conflict": 1,
}


def _run_validated_generation_command(args: argparse.Namespace) -> int:
    """Build and export a task-first release from a pinned graph snapshot."""
    from .generation.task_contracts import (
        ProviderSnapshotRef,
        ValidationEnvironment,
    )
    from .generation.task_release import TaskReleaseExporter
    from .generation.task_service import ConstrainedTaskGenerationService
    from .knowledge_graph import EntityLookup
    from .knowledge_graph.errors import KnowledgeGraphError
    from .knowledge_graph.registry import provider_from_config

    provider = None
    try:
        provider = provider_from_config(
            args.provider,
            config_path=args.kg_config,
        )
        resolution = provider.resolve_entity(
            EntityLookup(
                mention=args.entity_name,
                native_id=args.entity_id,
                entity_types=[args.entity_type],
                limit=10,
            )
        )
        if resolution.ambiguous or resolution.selected is None:
            _generation_error(
                "invalid_upstream_identity",
                "the entity id and type did not resolve to one graph entity",
            )
            return 2

        snapshot = provider.snapshot()
        environment = ValidationEnvironment(
            environment_id=(
                f"world-{snapshot.provider_id}-{snapshot.data_snapshot_id}"
            ),
            providers=[
                ProviderSnapshotRef(
                    provider_id=snapshot.provider_id,
                    snapshot_id=snapshot.data_snapshot_id,
                    schema_version=snapshot.adapter_version,
                    manifest_hash=snapshot.sha256,
                    completeness="complete_paginated_snapshot_query",
                    detail={
                        "config_hash": snapshot.config_hash,
                        "size_bytes": snapshot.size_bytes,
                    },
                )
            ],
            completeness_scope=(
                "all matching edges returned by complete paginated queries "
                "against the frozen graph snapshot"
            ),
        )
        result = ConstrainedTaskGenerationService(
            provider,
            page_size=args.page_size,
        ).generate(resolution.selected, environment=environment)
        summary = TaskReleaseExporter().export(result, args.output)
    except FileExistsError as exc:
        _generation_error("output_exists", str(exc))
        return 2
    except KnowledgeGraphError as exc:
        _generation_error(exc.code, exc.message, retryable=exc.retryable)
        return GENERATION_EXIT_CODES.get(exc.code, 1)
    except (TypeError, ValueError) as exc:
        _generation_error("invalid_arguments", str(exc))
        return 2
    finally:
        if provider is not None:
            provider.close()

    _emit(
        {
            "status": "exported",
            "release_id": summary.release_id,
            "output": summary.output_dir,
            "published_count": summary.published_count,
            "rejected_count": summary.rejected_count,
            "validation_report_count": summary.validation_report_count,
        }
    )
    return 0


def _generation_error(code: str, message: str, *, retryable: bool = False) -> None:
    import sys

    print(
        json.dumps(
            {
                "error": {
                    "code": code,
                    "message": message,
                    "retryable": retryable,
                    "episode_id": None,
                }
            },
            ensure_ascii=False,
        ),
        file=sys.stderr,
    )


def _run_generate_command(args: argparse.Namespace) -> int:
    from .generation.contracts import (
        EntityGenerationRequest,
        GenerationSpec,
        SafetyBudget,
        UpstreamEntitySeed,
    )
    from .generation.service import EntityGenerationService, GenerationError

    # Step 0: the wrong interpreter must fail before any artifact is written.
    try:
        from .environment import assert_environment

        assert_environment()
    except RuntimeError as exc:
        _generation_error("environment_error", str(exc))
        return 3

    try:
        generation_spec = _load_generation_spec(args)
        requested_count = getattr(args, "question_count", None)
        question_count = (
            requested_count
            if requested_count is not None
            else (
                generation_spec.question_count
                if generation_spec is not None
                else 5
            )
        )
        if (
            args.generate_command == "resume"
            and requested_count is None
            and generation_spec is None
        ):
            from .generation.repository import GenerationRepository

            try:
                existing_episode = GenerationRepository(
                    args.workspace
                ).load_latest_episode(args.episode_id)
                question_count = existing_episode.question_target
            except Exception:
                # The service reports the documented episode_not_found code.
                pass
        if generation_spec is not None:
            generation_spec = generation_spec.model_copy(
                update={"question_count": question_count}
            )
        safety_budget = _load_json_contract(
            getattr(args, "safety_budget", None), SafetyBudget
        )
        seed = UpstreamEntitySeed(
            provider_id=args.provider,
            native_id=args.entity_id,
            entity_type=args.entity_type,
            canonical_name=args.entity_name,
            raw_mention=getattr(args, "raw_mention", None),
        )
        request = EntityGenerationRequest(
            task_id=args.task_id,
            workspace=args.workspace,
            seed=seed,
            question_count=question_count,
            generation_spec=generation_spec,
            safety_budget=safety_budget,
        )
    except Exception as exc:  # pydantic validation of CLI arguments
        _generation_error("invalid_arguments", str(exc))
        return 2

    service = EntityGenerationService(
        workspace=args.workspace,
        provider_id=args.provider,
        model_mode=args.model_mode,
    )
    try:
        if args.generate_command == "resume":
            result = service.resume(request, args.episode_id)
        else:
            result = service.run(request)
    except GenerationError as exc:
        _generation_error(exc.code, exc.message, retryable=exc.retryable)
        return GENERATION_EXIT_CODES.get(exc.code, 1)
    except Exception as exc:
        code = str(getattr(exc, "code", "internal_error"))
        _generation_error(
            code, str(exc), retryable=bool(getattr(exc, "retryable", False))
        )
        return GENERATION_EXIT_CODES.get(code, 1)

    _emit(
        {
            "task_id": result.task_id,
            "episode_id": result.episode_id,
            "status": result.status,
            "stop_reason": result.stop_reason,
            "bundle_ids": result.bundle_ids,
            "qa_item_ids": result.qa_item_ids,
            "planner_trajectory_id": result.planner_trajectory_id,
            "claim_ids": result.claim_ids,
            "question_ids": result.question_ids,
            "rejection_record_ids": result.rejection_record_ids,
            "review_queue_size": len(result.question_ids),
            "target_question_count": result.target_question_count,
            "target_difficulties_satisfied": result.target_difficulties_satisfied,
            "model_mode": args.model_mode,
            "model_capability": model_capability_label(args.model_mode),
            "note": (
                "候选答案未经人工确认，不能作为正式 gold 导出；"
                "请在 web serve 打开工作台审核。"
            ),
        }
    )
    return 0


def _load_json_contract(path: str | None, model: type):
    if not path:
        return None
    from pathlib import Path as _Path

    payload = json.loads(_Path(path).read_text(encoding="utf-8"))
    return model.model_validate(payload)


def _load_generation_spec(args: argparse.Namespace):
    from .generation.contracts import GenerationSpec

    path = getattr(args, "generation_spec", None)
    base = _load_json_contract(path, GenerationSpec)
    overrides = {}
    if getattr(args, "difficulty", None):
        overrides["difficulty_targets"] = list(args.difficulty)
    if getattr(args, "literature_policy", None):
        constraints = (
            base.evidence_constraints.model_copy(
                update={
                    "literature_policy": args.literature_policy
                }
            )
            if base is not None
            else {
                "literature_policy": args.literature_policy
            }
        )
        overrides["evidence_constraints"] = constraints
    if getattr(args, "language", None):
        overrides["language"] = args.language
    if base is None and not overrides:
        return None
    if base is None:
        base = GenerationSpec()
    return base.model_copy(update=overrides)


def _run_literature_command(args: argparse.Namespace) -> int:
    """Report the literature providers and their capabilities, without secrets."""
    from .literature.errors import LiteratureConfigurationError
    from .literature.registry import LiteratureRegistry, load_literature_config

    try:
        registry = LiteratureRegistry(
            config=load_literature_config(args.config_path)
        )
    except LiteratureConfigurationError as exc:
        _generation_error(exc.code, exc.message, retryable=False)
        return GENERATION_EXIT_CODES.get(exc.code, 1)
    if args.literature_command == "providers":
        _emit(
            {
                "default_provider": registry.config.default_provider,
                "providers": registry.describe(),
            }
        )
        return 0
    # preflight
    try:
        provider = registry.build(args.provider)
        capability = provider.preflight()
    except LiteratureConfigurationError as exc:
        _generation_error(exc.code, exc.message, retryable=False)
        return GENERATION_EXIT_CODES.get(exc.code, 1)
    _emit(
        {
            "provider": capability.provider,
            "capability_version": capability.capability_version,
            "supports_search": capability.supports_search,
            "supports_read_chunk": capability.supports_read_chunk,
            "supports_pinned_snapshot": capability.supports_pinned_snapshot,
            "supports_source_offsets": capability.supports_source_offsets,
            "max_limit": capability.max_limit,
            "snapshot_id": (
                capability.snapshot.snapshot_id if capability.snapshot else None
            ),
            "notes": capability.notes,
        }
    )
    return 0


def _run_evidence_command(args: argparse.Namespace) -> int:
    if args.evidence_command == "ctd":
        from .evidence_enrichment.ctd import (
            CtdEvidenceIndex,
            build_ctd_index,
        )

        if args.ctd_command == "build":
            metadata = build_ctd_index(args.source, args.output)
            _emit({"status": "built", **metadata})
            return 0
        records = CtdEvidenceIndex(args.index).lookup(
            chemical_id=args.chemical_id,
            disease_name=args.disease,
        )
        _emit(
            {
                "index": args.index,
                "records": [
                    item.model_dump(mode="json") for item in records
                ],
            }
        )
        return 0
    from .evidence_enrichment.pubmed import PubMedClient

    client = PubMedClient()
    if args.pubmed_command == "search":
        _emit(
            {
                "query": args.query,
                "pmids": client.search(args.query, limit=args.limit),
            }
        )
        return 0
    records = client.fetch(args.pmid, limit=args.limit)
    _emit(
        {
            "records": [item.model_dump(mode="json") for item in records],
        }
    )
    return 0


def _run_model_command(args: argparse.Namespace) -> int:
    """Report the configured providers without revealing any secret."""
    from .llm.errors import ModelError
    from .llm.registry import ProviderRegistry

    try:
        registry = ProviderRegistry.from_config(args.config_path)
    except ModelError as exc:
        _generation_error(exc.code.value, exc.message, retryable=exc.retryable)
        return GENERATION_EXIT_CODES.get(exc.code.value, 1)
    _emit(
        {
            "default_provider": registry.config.default_provider,
            "providers": registry.describe(),
        }
    )
    return 0


def model_capability_label(mode: str) -> str:
    """Honest capability label. Never call the baseline a ReAct agent."""
    return {
        "rule-based": "deterministic_baseline",
        "fake": "scripted_model",
        "structured": "real_model",
    }[mode]


KG_EXIT_CODES = {
    "kg_config_error": 2,
    "kg_unavailable": 3,
    "kg_query_timeout": 4,
    "kg_query_error": 5,
    "kg_data_integrity": 6,
    "kg_capability_error": 7,
    "kg_error": 8,
}


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _run_kg_command(args: argparse.Namespace) -> int:
    from .knowledge_graph import (
        EntityLookup,
        NeighborQuery,
        PathQuery,
        provider_from_config,
    )
    from .knowledge_graph.errors import KnowledgeGraphError
    from .knowledge_graph.registry import KnowledgeGraphProviderRegistry

    if args.kg_command == "providers":
        registry = KnowledgeGraphProviderRegistry()
        _emit(
            {
                "default_provider": registry.default_provider_id,
                "providers": registry.available(),
            }
        )
        return 0
    if args.kg_command == "snapshot":
        from pathlib import Path as _Path

        from .knowledge_graph import init_manifest, refresh_manifest
        from .knowledge_graph.errors import KnowledgeGraphError

        try:
            if args.snapshot_command == "init":
                payload = init_manifest(_Path(args.database))
            else:
                payload = refresh_manifest(_Path(args.database))
        except KnowledgeGraphError as exc:
            import sys

            print(
                json.dumps({"error": exc.as_dict()}, ensure_ascii=False),
                file=sys.stderr,
            )
            return KG_EXIT_CODES.get(exc.code, 8)
        _emit(payload)
        return 0
    provider = None
    try:
        provider = provider_from_config(args.provider)
        if args.kg_command == "health":
            health = provider.healthcheck()
            _emit(json.loads(health.model_dump_json()))
            return 0 if health.healthy else 1
        if args.kg_command == "resolve":
            resolution = provider.resolve_entity(
                EntityLookup(
                    mention=args.entity,
                    native_id=args.native_id,
                    entity_types=list(args.entity_type or []),
                    limit=args.limit,
                )
            )
            payload = json.loads(resolution.model_dump_json())
            _emit(payload)
            if resolution.ambiguous:
                return 9
            return 0 if resolution.candidates else 10
        if args.kg_command == "neighbors":
            if not provider.capabilities().outgoing_edges and not provider.capabilities().incoming_edges:
                _emit(
                    {
                        "error": {
                            "code": "kg_capability_error",
                            "message": "provider does not support edge queries",
                            "retryable": False,
                        }
                    }
                )
                return KG_EXIT_CODES["kg_capability_error"]
            lookup = EntityLookup(mention=args.entity, native_id=args.native_id)
            resolution = provider.resolve_entity(lookup)
            if not resolution.candidates:
                _emit({"entity": None, "edges": [], "note": "no exact match"})
                return 10
            entity = resolution.selected or resolution.candidates[0].entity
            result = provider.neighbors(
                NeighborQuery(
                    entity=entity,
                    direction=args.direction,
                    relations=list(args.relation or []),
                    target_types=list(args.target_types or []),
                    limit=args.limit,
                )
            )
            _emit(json.loads(result.model_dump_json()))
            return 0
        if args.kg_command == "paths":
            start_resolution = provider.resolve_entity(
                EntityLookup(mention=args.start)
            )
            if not start_resolution.candidates:
                _emit({"error": {"code": "kg_unresolved_entity", "mention": args.start}})
                return 10
            start = start_resolution.selected or start_resolution.candidates[0].entity
            end = None
            if args.end:
                end_resolution = provider.resolve_entity(EntityLookup(mention=args.end))
                if not end_resolution.candidates:
                    _emit({"error": {"code": "kg_unresolved_entity", "mention": args.end}})
                    return 10
                end = end_resolution.selected or end_resolution.candidates[0].entity
            result = provider.paths(
                PathQuery(start=start, end=end, max_hops=args.max_hops, max_paths=args.max_paths)
            )
            _emit(json.loads(result.model_dump_json()))
            return 0
    except KnowledgeGraphError as exc:
        import sys

        print(
            json.dumps({"error": exc.as_dict()}, ensure_ascii=False),
            file=sys.stderr,
        )
        return KG_EXIT_CODES.get(exc.code, 8)
    finally:
        if provider is not None:
            provider.close()
    return 1


def _run_validate_command(args: argparse.Namespace) -> int:
    from .machine_validation import (
        REQUIRED_FOR_FORMAL_EXPORT,
        apply_machine_validation,
        check_requirements,
    )
    from .validators import summarize_checks

    repository = LocalArtifactRepository(args.workspace)
    questions = repository.list_latest_questions()
    if getattr(args, "question_id", None):
        wanted = set(args.question_id)
        questions = [item for item in questions if item.question_id in wanted]

    if args.validate_command == "status":
        report = []
        for question in questions:
            report.append(
                {
                    "question_id": question.question_id,
                    "validation_level": question.validation_level.value,
                    "summary": summarize_checks(list(question.validation_checks)),
                    "required_checks": [
                        {
                            "check_type": item.check_type,
                            "status": item.status.value,
                            "validator_version": item.validator_version,
                            "message": item.message,
                        }
                        for item in check_requirements(list(question.validation_checks))
                    ],
                }
            )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    bundles = {bundle.bundle_id: bundle for bundle in repository.list_latest_bundles()}
    written: list[dict[str, object]] = []
    for question in questions:
        linked = [
            bundles[bundle_id]
            for bundle_id in question.bundle_ids
            if bundle_id in bundles
        ]
        peers = [
            item
            for item in questions
            if item.split_group_id == question.split_group_id
            and item.question_id != question.question_id
        ]
        updated = apply_machine_validation(question, linked, peers=peers)
        repository.save_question(updated)
        failures = [
            item.check_type
            for item in check_requirements(list(updated.validation_checks))
            if item.status.value != "passed"
        ]
        written.append(
            {
                "question_id": updated.question_id,
                "question_version": updated.question_version,
                "validation_level": updated.validation_level.value,
                "failed_required_checks": failures,
            }
        )
    print(
        json.dumps(
            {
                "required_checks": list(REQUIRED_FOR_FORMAL_EXPORT),
                "validated": written,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


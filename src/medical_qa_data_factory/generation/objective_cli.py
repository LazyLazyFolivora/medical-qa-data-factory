"""CLI assembly for the objective-question main-agent loop."""
from __future__ import annotations

import json
from pathlib import Path

from ..llm.registry import build_model_port
from .objective_question_release import export_objective_release
from .objective_run import (
    DEFAULT_PROVIDERS,
    create_objective_run,
    objective_provider_map,
    run_objective_loop,
)


def run_objective_generation_command(args) -> int:
    output = Path(args.output)
    workspace = Path(args.workspace)
    providers_requested = list(dict.fromkeys(args.provider or DEFAULT_PROVIDERS))
    kg_provider = None
    try:
        model = build_model_port(config_path=args.model_config)
        providers, kg_provider = objective_provider_map(
            providers_requested, ctd_index_path=args.ctd_index,
        )
        runtime = create_objective_run(
            workspace,
            run_id=args.run_id,
            entity_name=args.entity_name,
            entity_id=args.entity_id,
            entity_type=args.entity_type,
            question_count=args.question_count,
            difficulty_targets=[args.difficulty],
            generation_strategy=args.generation_strategy,
            max_actions=args.max_actions,
            max_cost=args.max_cost,
            max_model_calls=args.max_model_calls,
            model=model,
            providers=providers,
        )
        result = run_objective_loop(runtime, runtime.load().goal, model, providers)
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

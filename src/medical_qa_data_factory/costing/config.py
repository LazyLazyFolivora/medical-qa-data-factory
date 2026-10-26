"""Load a cost-model snapshot from a version-controlled JSON document."""
from __future__ import annotations

import json
from pathlib import Path

from .models import CostModelSnapshot


def load_cost_model_snapshot(path: str | Path) -> CostModelSnapshot:
    config_path = Path(path)
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Cost model config not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid cost model JSON: {config_path}") from exc

    try:
        return CostModelSnapshot(**payload)
    except TypeError as exc:
        raise ValueError(f"Invalid cost model fields: {config_path}") from exc


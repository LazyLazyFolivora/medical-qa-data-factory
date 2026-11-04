"""Preflight budgets for complete serialized requests, without clipping evidence."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Callable

from .config import ModelProviderConfig
from .errors import ModelError, ModelErrorCode


def estimate_tokens(body: dict[str, Any]) -> int:
    """Heuristic, not a model tokenizer; include schema, skills and chat overhead.

    Count ASCII at three characters/token and other text at two tokens/character.
    The safety reserve and server error handling cover estimation uncertainty.
    """
    text = json.dumps(body, ensure_ascii=False, default=str)
    ascii_count = sum(ord(char) < 128 for char in text)
    return math.ceil(ascii_count / 3) + (len(text) - ascii_count) * 2 + 128


@dataclass
class ContextManager:
    config: ModelProviderConfig

    def input_budget(self, body):
        output = body.get("max_completion_tokens", body.get("max_tokens", self.config.output_reserve_tokens))
        if not isinstance(output, int) or isinstance(output, bool) or output < 1:
            raise ModelError(ModelErrorCode.BAD_REQUEST, "invalid output token limit", retryable=False)
        return self.config.context_window_tokens - output - self.config.context_safety_tokens

    def prepare(self, request, build: Callable, compact: Callable):
        body = build(request)
        budget = self.input_budget(body)
        before = estimate_tokens(body)
        after = before
        rounds = 0
        if after > budget * self.config.compact_trigger_ratio:
            for round_number in (1, 2):
                candidate = compact(request, round_number=round_number)
                candidate_body = build(candidate)
                size = estimate_tokens(candidate_body)
                if size < after:
                    request, body, after = candidate, candidate_body, size
                    rounds += 1
                if after <= budget * self.config.compact_target_ratio:
                    break
        report = {"estimator": "heuristic_ascii3_unicode2", "estimated_before": before,
                  "estimated_after": after, "input_budget": budget, "compaction_rounds": rounds}
        if after > budget:
            raise ModelError(ModelErrorCode.BAD_REQUEST,
                "context budget exceeded; split the evidence batch or increase the configured server window: "
                + json.dumps(report), retryable=False)
        return request, body, report

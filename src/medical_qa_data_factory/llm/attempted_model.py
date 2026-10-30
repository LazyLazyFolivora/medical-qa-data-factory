"""A model port that charges every physical attempt to the ledger.

The adapter itself knows nothing about budgets. This wrapper does: it
reserves before the call, charges after it, and turns a provider failure into
the right attempt outcome.

A schema repair is two physical calls, so it is charged twice. That is why the
wrapper listens to the adapter's per-physical-call hook instead of counting
``generate_structured`` calls: the repair happens inside the adapter.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..domain.common import ContractModel
from ..generation.attempt_ledger import (
    AttemptLedger,
    AttemptScope,
    BudgetExhaustedError,
)
from ..generation.run_identity import (
    AttemptOutcome,
    AttemptRecord,
    AttemptStatus,
)
from .contracts import StructuredGenerationRequest, StructuredGenerationResult
from .errors import ModelError, ModelErrorCode


@dataclass
class LedgeredModel:
    """Wraps a model port so every call is reserved, charged and recorded."""

    model: Any
    ledger: AttemptLedger
    scope: str = AttemptScope.RUN.value
    owner_id: str = ""
    run_id: str | None = None
    episode_id: str | None = None
    #: Units reserved per physical attempt.
    units_per_attempt: float = 1.0
    attempt_records: list[AttemptRecord] = field(default_factory=list)
    #: Attempts that are currently in flight, keyed by physical call index.
    _open: list[AttemptRecord] = field(default_factory=list, init=False)

    def _provider_id(self) -> str:
        config = getattr(self.model, "config", None)
        return getattr(config, "provider_id", None) or "provider"

    def _install_hook(self) -> None:
        """Hook the adapter so a repair is a second reserved attempt."""
        if getattr(self.model, "on_physical_call", None) is not None:
            return
        self.model.on_physical_call = self._on_physical_call

    def _on_physical_call(
        self, request: StructuredGenerationRequest, repaired: bool
    ) -> None:
        owner = self.owner_id or request.invocation_id
        attempt = self.ledger.reserve(
            scope=self.scope,
            owner_id=owner,
            tool_name=f"model:{self._provider_id()}",
            operation=(
                f"generate_structured:{request.role}"
                + (":repair" if repaired else "")
            ),
            request_digest=(
                f"{request.idempotency_key}:{int(repaired)}:"
                f"{len(self.attempt_records)}"
            ),
            units=self.units_per_attempt,
            run_id=self.run_id,
            episode_id=self.episode_id,
        )
        self.attempt_records.append(attempt)
        self._open.append(attempt)

    def _settle(self, index: int, settled: AttemptRecord) -> None:
        self.attempt_records[index] = settled
        if 0 <= index < len(self._open):
            self._open[index] = settled

    def generate_structured(
        self,
        request: StructuredGenerationRequest,
        response_model: type[ContractModel],
    ) -> StructuredGenerationResult:
        self._install_hook()
        owner = self.owner_id or request.invocation_id
        if not hasattr(self.model, "on_physical_call"):
            # The wrapped model does not report physical calls. Fall back to
            # one attempt for the whole call, so the ledger still records it.
            self._on_physical_call(request, repaired=False)
        try:
            result = self.model.generate_structured(request, response_model)
        except ModelError as exc:
            self._settle_all(
                exc.outcome,
                error_code=exc.code.value,
                retryable=exc.retryable,
            )
            raise
        except BudgetExhaustedError:
            # A budget refusal is not a provider failure. It must surface as
            # itself, or a run would look like it had hit a broken provider.
            self._settle_all(
                AttemptOutcome.POLICY_REJECTED,
                error_code="budget_exhausted",
                retryable=False,
            )
            raise
        except Exception as exc:  # unexpected provider failure
            self._settle_all(
                AttemptOutcome.PROVIDER_ERROR,
                error_code=ModelErrorCode.INTERNAL_ERROR.value,
                retryable=False,
            )
            raise ModelError(
                ModelErrorCode.INTERNAL_ERROR,
                f"model call failed: {type(exc).__name__}: {exc}",
            ) from exc
        self._settle_all(AttemptOutcome.SUCCESS, charged=self.units_per_attempt)
        return result

    def _settle_all(
        self,
        outcome: AttemptOutcome,
        *,
        error_code: str | None = None,
        retryable: bool = False,
        charged: float | None = None,
    ) -> None:
        for index, attempt in enumerate(list(self._open)):
            if attempt.status in (
                AttemptStatus.COMPLETED,
                AttemptStatus.FAILED,
                AttemptStatus.UNKNOWN,
            ):
                continue
            running = self.ledger.mark_running(attempt)
            settled = self.ledger.complete(
                running,
                outcome=outcome,
                error_code=error_code,
                retryable=retryable,
                charged_units=(
                    charged if outcome is AttemptOutcome.SUCCESS else None
                ),
            )
            self._settle(index, settled)

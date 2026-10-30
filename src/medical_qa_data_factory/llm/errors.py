"""Stable error classes for real model calls.

Every failure a provider can produce gets a stable code, so the CLI can map it
to an exit status and the attempt ledger can record an outcome without
parsing message text.
"""
from __future__ import annotations

from enum import Enum


class ModelErrorCode(str, Enum):
    """Stable failure codes for a structured model call."""

    CONFIG_ERROR = "model_config_error"
    AUTH_ERROR = "model_auth_error"
    RATE_LIMITED = "model_rate_limited"
    PROVIDER_UNAVAILABLE = "model_provider_unavailable"
    TIMEOUT = "model_timeout"
    NETWORK_ERROR = "model_network_error"
    PARSE_ERROR = "model_parse_error"
    SCHEMA_ERROR = "model_schema_error"
    UNSUPPORTED_PROVIDER = "model_unsupported_provider"
    USAGE_MISSING = "model_usage_missing"
    INTERNAL_ERROR = "model_internal_error"


#: Whether a failure may be retried automatically. A schema error is retried
#: exactly once with a repair prompt; everything else needs a different
#: decision from the caller.
RETRYABLE_CODES: frozenset[str] = frozenset(
    {
        ModelErrorCode.RATE_LIMITED.value,
        ModelErrorCode.PROVIDER_UNAVAILABLE.value,
        ModelErrorCode.TIMEOUT.value,
        ModelErrorCode.NETWORK_ERROR.value,
    }
)


class ModelError(RuntimeError):
    """A model call failed with a stable code."""

    def __init__(
        self,
        code: ModelErrorCode,
        message: str,
        *,
        retryable: bool | None = None,
        status_code: int | None = None,
        provider_request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = (
            retryable if retryable is not None else code.value in RETRYABLE_CODES
        )
        self.status_code = status_code
        self.provider_request_id = provider_request_id

    @property
    def outcome(self):
        """The attempt-ledger outcome for this failure."""
        from ..generation.run_identity import AttemptOutcome

        mapping = {
            ModelErrorCode.AUTH_ERROR: AttemptOutcome.AUTH_ERROR,
            ModelErrorCode.RATE_LIMITED: AttemptOutcome.RATE_LIMITED,
            ModelErrorCode.PROVIDER_UNAVAILABLE: AttemptOutcome.PROVIDER_ERROR,
            ModelErrorCode.TIMEOUT: AttemptOutcome.TIMEOUT,
            ModelErrorCode.PARSE_ERROR: AttemptOutcome.PARSE_ERROR,
            ModelErrorCode.SCHEMA_ERROR: AttemptOutcome.PARSE_ERROR,
            ModelErrorCode.NETWORK_ERROR: AttemptOutcome.PROVIDER_ERROR,
            ModelErrorCode.INTERNAL_ERROR: AttemptOutcome.PROVIDER_ERROR,
            ModelErrorCode.CONFIG_ERROR: AttemptOutcome.POLICY_REJECTED,
            ModelErrorCode.UNSUPPORTED_PROVIDER: AttemptOutcome.POLICY_REJECTED,
            ModelErrorCode.USAGE_MISSING: AttemptOutcome.PROVIDER_ERROR,
        }
        return mapping[self.code]

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
            "status_code": self.status_code,
        }


class ModelConfigurationError(ModelError):
    """The provider configuration is missing or unusable."""

    def __init__(self, message: str) -> None:
        super().__init__(ModelErrorCode.CONFIG_ERROR, message, retryable=False)


class UnsupportedProviderError(ModelError):
    """The provider cannot honour the structured-output contract."""

    def __init__(self, message: str) -> None:
        super().__init__(ModelErrorCode.UNSUPPORTED_PROVIDER, message, retryable=False)

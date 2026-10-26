"""Stable errors for the literature layer."""
from __future__ import annotations

from enum import Enum


class LiteratureErrorCode(str, Enum):
    CONFIG_ERROR = "literature_config_error"
    UNSUPPORTED_PROVIDER = "literature_unsupported_provider"
    CAPABILITY_ERROR = "literature_capability_error"


class LiteratureConfigurationError(RuntimeError):
    """The provider configuration is missing or unusable."""

    code = "literature_config_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = False
"""Model ports, provider adapters, and offline fakes."""
from __future__ import annotations

from .attempted_model import LedgeredModel
from .config import (
    ModelConfig,
    ModelProviderConfig,
    load_model_config,
)
from .contracts import (
    ModelRole,
    SEPARATED_ROLE_PAIRS,
    StructuredGenerationRequest,
    StructuredGenerationResult,
)
from .errors import (
    ModelConfigurationError,
    ModelError,
    ModelErrorCode,
    UnsupportedProviderError,
)
from .fake import FakeStructuredModel
from .ports import LanguageModelPort
from .prompts import (
    PromptTemplate,
    assert_roles_separated,
    prompt_hashes,
    template_for,
)
from .registry import ProviderRegistry, build_model_port

__all__ = [
    "FakeStructuredModel",
    "LanguageModelPort",
    "LedgeredModel",
    "ModelConfig",
    "ModelConfigurationError",
    "ModelError",
    "ModelErrorCode",
    "ModelProviderConfig",
    "ModelRole",
    "PromptTemplate",
    "ProviderRegistry",
    "SEPARATED_ROLE_PAIRS",
    "StructuredGenerationRequest",
    "StructuredGenerationResult",
    "UnsupportedProviderError",
    "assert_roles_separated",
    "build_model_port",
    "load_model_config",
    "prompt_hashes",
    "template_for",
]
"""Real literature retrieval: capabilities, requests, and provider adapters."""
from __future__ import annotations

from .config import LITERATURE_CONFIG_ENV_VAR, LITERATURE_CONFIG_PATH
from .contracts import (
    LiteratureCallStatus,
    LiteratureCapability,
    LiteratureChunkReadRequest,
    LiteratureChunkReadResult,
    LiteratureEvidenceNeed,
    LiteratureRetrievalRequest,
    LiteratureRetrievalResult,
    LiteratureSnapshot,
    RetrievedChunk,
)
from .errors import LiteratureConfigurationError, LiteratureErrorCode
from .registry import (
    LiteratureConfig,
    LiteratureProviderConfig,
    LiteratureRegistry,
    LiteratureRetrievalPort,
    load_literature_config,
)

__all__ = [
    "LITERATURE_CONFIG_ENV_VAR",
    "LITERATURE_CONFIG_PATH",
    "LiteratureCallStatus",
    "LiteratureCapability",
    "LiteratureChunkReadRequest",
    "LiteratureChunkReadResult",
    "LiteratureConfig",
    "LiteratureConfigurationError",
    "LiteratureErrorCode",
    "LiteratureEvidenceNeed",
    "LiteratureProviderConfig",
    "LiteratureRegistry",
    "LiteratureRetrievalPort",
    "LiteratureRetrievalRequest",
    "LiteratureRetrievalResult",
    "LiteratureSnapshot",
    "RetrievedChunk",
    "load_literature_config",
]
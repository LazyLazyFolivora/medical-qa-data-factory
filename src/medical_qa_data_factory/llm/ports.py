"""The single model interface used by claim, writer, and verifier components."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.common import ContractModel
from .contracts import StructuredGenerationRequest, StructuredGenerationResult


@runtime_checkable
class LanguageModelPort(Protocol):
    def generate_structured(
        self,
        request: StructuredGenerationRequest,
        response_model: type[ContractModel],
    ) -> StructuredGenerationResult: ...

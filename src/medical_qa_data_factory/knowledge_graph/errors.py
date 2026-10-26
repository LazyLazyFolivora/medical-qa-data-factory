"""Stable knowledge graph error semantics.

Errors carry a stable ``code`` and an honest ``retryable`` flag so that callers
never need to infer failure class from exception messages.
"""
from __future__ import annotations


class KnowledgeGraphError(Exception):
    code = "kg_error"
    retryable = False

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }


class GraphConfigurationError(KnowledgeGraphError):
    code = "kg_config_error"
    retryable = False


class GraphUnavailableError(KnowledgeGraphError):
    code = "kg_unavailable"
    retryable = True


class GraphQueryTimeoutError(KnowledgeGraphError):
    code = "kg_query_timeout"
    retryable = True


class GraphQueryError(KnowledgeGraphError):
    code = "kg_query_error"
    retryable = True


class GraphDataIntegrityError(KnowledgeGraphError):
    code = "kg_data_integrity"
    retryable = False


class GraphCapabilityError(KnowledgeGraphError):
    code = "kg_capability_error"
    retryable = False


class GraphUnresolvedEntityError(KnowledgeGraphError):
    """The upstream identity matched no graph row. Not retryable."""

    code = "kg_unresolved_entity"
    retryable = False

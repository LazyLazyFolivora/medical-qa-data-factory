"""Provider-neutral knowledge graph contracts, ports, and adapters."""
from __future__ import annotations

from .collector import KnowledgeGraphEvidenceCollector
from .contracts import (
    GraphCapabilities,
    GraphEdge,
    GraphEntity,
    GraphHealth,
    GraphQueryResult,
    GraphQueryTrace,
    GraphSnapshot,
    OrderedPath,
    OrderedPathStep,
    NeighborQuery,
    PathQuery,
    EntityCandidate,
    EntityLookup,
    EntityResolution,
    EntitySearchQuery,
    EntitySearchResult,
)
from .errors import (
    GraphCapabilityError,
    GraphConfigurationError,
    GraphDataIntegrityError,
    GraphQueryError,
    GraphQueryTimeoutError,
    GraphUnavailableError,
    KnowledgeGraphError,
)
from .ports import KnowledgeGraphProvider
from .path_builder import (
    BoundedPathBuilder,
    PathBuildResult,
    assemble_paths,
    bounded_intermediates,
    path_digest,
    traversal_direction,
)
from .registry import (
    KnowledgeGraphProviderRegistry,
    load_provider_config,
    provider_from_config,
)
from .providers.primekg import (
    init_manifest,
    manifest_path_for,
    read_manifest,
    refresh_manifest,
)

__all__ = [
    "EntityCandidate",
    "EntityLookup",
    "EntityResolution",
    "EntitySearchQuery",
    "EntitySearchResult",
    "GraphCapabilities",
    "GraphEdge",
    "GraphEntity",
    "GraphHealth",
    "GraphQueryResult",
    "GraphQueryTrace",
    "GraphSnapshot",
    "KnowledgeGraphEvidenceCollector",
    "KnowledgeGraphError",
    "KnowledgeGraphProvider",
    "KnowledgeGraphProviderRegistry",
    "GraphCapabilityError",
    "GraphConfigurationError",
    "GraphDataIntegrityError",
    "BoundedPathBuilder",
    "PathBuildResult",
    "OrderedPath",
    "OrderedPathStep",
    "assemble_paths",
    "bounded_intermediates",
    "path_digest",
    "traversal_direction",
    "GraphQueryError",
    "GraphQueryTimeoutError",
    "GraphUnavailableError",
    "load_provider_config",
    "OrderedPath",
    "OrderedPathStep",
    "NeighborQuery",
    "PathQuery",
    "provider_from_config",
    "init_manifest",
    "manifest_path_for",
    "read_manifest",
    "refresh_manifest",
]

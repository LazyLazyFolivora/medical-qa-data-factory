"""Evidence enrichment for natural QA candidates."""

from .contracts import EvidenceRecord, EvidenceStore, NormalizedClaim
from .models import (
    CtdEvidenceRecord,
    EnrichmentStatus,
    EvidenceClaim,
    EvidencePolarity,
    EvidenceSupportLevel,
    NormalizedEvidenceClaim,
    PubMedEvidenceRecord,
    QuestionIntent,
)
from .pubmed_evidence import PubMedCorpusManifest, PubMedEvidenceSpan

__all__ = [
    "CtdEvidenceRecord",
    "EvidenceRecord",
    "EvidenceStore",
    "EnrichmentStatus",
    "EvidenceClaim",
    "EvidencePolarity",
    "EvidenceSupportLevel",
    "NormalizedEvidenceClaim",
    "NormalizedClaim",
    "PubMedEvidenceRecord",
    "PubMedCorpusManifest",
    "PubMedEvidenceSpan",
    "QuestionIntent",
]

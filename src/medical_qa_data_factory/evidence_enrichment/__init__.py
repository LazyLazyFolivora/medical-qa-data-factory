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
from .primekg_ctd_identity import (
    PrimeKgCtdChemicalCrosswalk,
    PrimeKgCtdIdentitySnapshot,
)

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
    "PrimeKgCtdChemicalCrosswalk",
    "PrimeKgCtdIdentitySnapshot",
    "QuestionIntent",
]

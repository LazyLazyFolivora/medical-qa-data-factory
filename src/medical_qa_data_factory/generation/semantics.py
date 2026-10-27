"""Versioned relation-semantics policy.

Every relation in the current PrimeKG snapshot is mapped to one of three
semantics. Unknown relations fall back to ``generic_relation``; no model is
ever allowed to upgrade an association into a causal or therapeutic claim.
"""
from __future__ import annotations

from collections.abc import Sequence

from .contracts import RelationSemantics

POLICY_VERSION = "relation-semantics-v1"
STUDY_CLAIM_POLICY_VERSION = "study-claim-semantics-v1"


FORMAL_STUDY_INTENTS: frozenset[str] = frozenset(
    {
        "association_research",
        "biomarker_research",
        "therapeutic_research",
        "risk_increase",
        "risk_decrease",
        "safety",
    }
)

ASSOCIATION_UPGRADE_TERMS: frozenset[str] = frozenset(
    {
        "cause",
        "causes",
        "caused",
        "leads to",
        "results in",
        "prevent",
        "prevents",
        "prevention",
        "treat",
        "treats",
        "treated",
        "cure",
        "cures",
        "effective",
        "efficacy",
        "导致",
        "引起",
        "预防",
        "治疗",
        "治愈",
        "有效",
    }
)

THERAPEUTIC_EFFICACY_TERMS: frozenset[str] = frozenset(
    {
        "effective",
        "efficacy",
        "cure",
        "cures",
        "prevent",
        "prevents",
        "proven treatment",
        "established treatment",
        "治疗有效",
        "能够治疗",
        "可治愈",
        "预防效果",
    }
)


#: Relations that genuinely assert a directed fact. Phrasing may keep
#: "treats" or "contraindicates" because the relation itself carries it.
DIRECTIONAL_FACT: frozenset[str] = frozenset(
    {
        "contraindication",
        "indication",
        "off-label use",
        "drug_effect",
        "anatomy_protein_present",
        "anatomy_protein_absent",
        "disease_phenotype_positive",
        "disease_phenotype_negative",
        "exposure_disease",
    }
)


#: Pure co-occurrence or similarity relations. Never causal or therapeutic.
ASSOCIATION: frozenset[str] = frozenset(
    {
        "anatomy_anatomy",
        "bioprocess_bioprocess",
        "cellcomp_cellcomp",
        "drug_drug",
        "disease_disease",
        "disease_protein",
        "molfunc_molfunc",
        "pathway_pathway",
        "phenotype_phenotype",
        "protein_protein",
    }
)


#: Everything else is a typed association whose wording stays neutral.
GENERIC_RELATION: frozenset[str] = frozenset(
    {
        "bioprocess_protein",
        "cellcomp_protein",
        "drug_protein",
        "exposure_bioprocess",
        "exposure_cellcomp",
        "exposure_exposure",
        "exposure_molfunc",
        "exposure_protein",
        "molfunc_protein",
        "pathway_protein",
        "phenotype_protein",
    }
)


#: Wording that may only be used with a relation that asserts it itself.
FORBIDDEN_FOR_ASSOCIATION: frozenset[str] = frozenset(
    {
        "导致",
        "引起",
        "causes",
        "cause",
        "caused by",
        "leads to",
        "results in",
        "治疗",
        "treats",
        "治疗药物",
        "用于治疗",
        "cures",
        "remedy",
        "mechanism is",
        "机制是",
        "靶点",
        "targets",
    }
)


ALLOWED_TEMPLATES: dict[RelationSemantics, tuple[str, ...]] = {
    RelationSemantics.ASSOCIATION: (
        "Which entity is associated with {focus} through the relation \"{relation}\"?",
        "In the knowledge graph, which entity is linked to {focus} by \"{relation}\"?",
    ),
    RelationSemantics.DIRECTIONAL_FACT: (
        "According to the knowledge graph, which entity is the tail of the "
        "\"{relation}\" edge whose head is {focus}?",
        "Which entity is recorded as the head of the \"{relation}\" edge whose "
        "tail is {focus}?",
    ),
    RelationSemantics.GENERIC_RELATION: (
        "Which entity is recorded as the tail of the \"{relation}\" edge whose "
        "head is {focus}?",
        "Which entity is recorded as the head of the \"{relation}\" edge whose "
        "tail is {focus}?",
    ),
}


class RelationSemanticsPolicy:
    """Maps a relation to semantics plus the wording it permits."""

    version = POLICY_VERSION

    def __init__(self, overrides: dict[str, RelationSemantics] | None = None) -> None:
        self._overrides = dict(overrides or {})

    def classify(self, relation: str) -> RelationSemantics:
        if relation in self._overrides:
            return self._overrides[relation]
        if relation in DIRECTIONAL_FACT:
            return RelationSemantics.DIRECTIONAL_FACT
        if relation in ASSOCIATION:
            return RelationSemantics.ASSOCIATION
        if relation in GENERIC_RELATION:
            return RelationSemantics.GENERIC_RELATION
        # An unrecognised relation must never be escalated into a causal or
        # therapeutic reading.
        return RelationSemantics.GENERIC_RELATION

    def is_known(self, relation: str) -> bool:
        return relation in (DIRECTIONAL_FACT | ASSOCIATION | GENERIC_RELATION)

    def template_for(self, relation: str, endpoint: str) -> str:
        semantics = self.classify(relation)
        templates = ALLOWED_TEMPLATES[semantics]
        index = 0 if endpoint == "tail" else 1
        return templates[index % len(templates)]

    def forbidden_words(self, relation: str) -> frozenset[str]:
        if self.classify(relation) is RelationSemantics.DIRECTIONAL_FACT:
            return frozenset()
        return FORBIDDEN_FOR_ASSOCIATION


def study_claim_rejection_codes(
    *,
    intent: str,
    polarity: str,
    claim_text: str,
    direct_evidence: Sequence[str],
) -> list[str]:
    """Reject unsupported semantic upgrades before a claim can be built."""
    reasons: list[str] = []
    if intent not in FORMAL_STUDY_INTENTS:
        reasons.append("study_intent_not_allowed")
    expected_polarity = {
        "risk_increase": "increase",
        "risk_decrease": "decrease",
    }.get(intent)
    if expected_polarity is not None and polarity != expected_polarity:
        reasons.append("intent_polarity_mismatch")
    if expected_polarity is None and polarity == "mixed":
        reasons.append("mixed_polarity_not_allowed")

    direct = {item.casefold() for item in direct_evidence}
    if intent == "therapeutic_research" and "therapeutic" not in direct:
        reasons.append("ctd_intent_evidence_mismatch")
    if intent == "biomarker_research" and "marker/mechanism" not in direct:
        reasons.append("ctd_intent_evidence_mismatch")

    lowered = claim_text.casefold()
    forbidden = (
        ASSOCIATION_UPGRADE_TERMS
        if intent == "association_research"
        else THERAPEUTIC_EFFICACY_TERMS
        if intent == "therapeutic_research"
        else frozenset()
    )
    if any(term.casefold() in lowered for term in forbidden):
        reasons.append("claim_strength_exaggerated")
    return reasons

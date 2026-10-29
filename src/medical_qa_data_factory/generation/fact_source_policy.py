"""Fact-type to evidence-source routing policy."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FactSourceRoute:
    fact_type: str
    primary_providers: tuple[str, ...]
    verification_providers: tuple[str, ...]
    supporting_providers: tuple[str, ...] = ()

    @property
    def all_providers(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                (
                    *self.primary_providers,
                    *self.verification_providers,
                    *self.supporting_providers,
                )
            )
        )


FACT_SOURCE_ROUTES: dict[str, FactSourceRoute] = {
    "entity_identity": FactSourceRoute(
        "entity_identity",
        ("hgnc",),
        ("ncbi_gene", "ensembl"),
        ("pubmed",),
    ),
    "gene_identity": FactSourceRoute(
        "gene_identity",
        ("hgnc",),
        ("ncbi_gene", "ensembl"),
        ("pubmed",),
    ),
    "gene_location": FactSourceRoute(
        "gene_location",
        ("hgnc",),
        ("ncbi_gene", "ensembl"),
        ("pubmed",),
    ),
    "classification": FactSourceRoute(
        "classification",
        ("hgnc",),
        ("ncbi_gene", "ensembl"),
        ("pubmed",),
    ),
    "protein_function": FactSourceRoute(
        "protein_function",
        ("uniprot",),
        ("ncbi_gene", "ensembl"),
        ("pubmed",),
    ),
    "pathway_membership": FactSourceRoute(
        "pathway_membership",
        ("reactome", "kegg"),
        ("reactome",),
        ("pubmed",),
    ),
    "disease_association": FactSourceRoute(
        "disease_association",
        ("ctd", "opentargets"),
        ("pubmed",),
        ("primekg",),
    ),
    "drug_target": FactSourceRoute(
        "drug_target",
        ("chembl", "opentargets"),
        ("uniprot",),
        ("pubmed",),
    ),
    "observation_association": FactSourceRoute(
        "observation_association",
        ("pubmed", "ctd"),
        ("pubmed",),
        ("primekg",),
    ),
}


def objective_fact_source_route(fact_type: str) -> FactSourceRoute:
    return FACT_SOURCE_ROUTES.get(
        fact_type,
        FactSourceRoute(
            fact_type,
            ("primekg",),
            ("pubmed",),
        ),
    )


def objective_fact_source_routes(
    fact_types: list[str],
) -> dict[str, FactSourceRoute]:
    return {
        fact_type: objective_fact_source_route(fact_type)
        for fact_type in fact_types
    }

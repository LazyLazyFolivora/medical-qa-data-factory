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


def default_objective_fact_types(
    entity_type: str | None,
    *,
    available_providers: list[str] | tuple[str, ...] = (),
) -> list[str]:
    """Choose broad, source-backed defaults instead of gene-only metadata."""
    normalized = (entity_type or "").strip().casefold()
    if normalized in {"gene", "gene/protein", "protein"}:
        candidates = [
            "entity_identity",
            "gene_location",
            "classification",
            "observation_association",
        ]
    elif normalized in {"disease", "phenotype"}:
        candidates = [
            "entity_identity",
            "disease_association",
            "observation_association",
        ]
    elif normalized in {"drug", "chemical", "compound"}:
        candidates = [
            "entity_identity",
            "disease_association",
            "observation_association",
        ]
    else:
        candidates = ["entity_identity", "observation_association"]

    providers = set(available_providers)
    if providers:
        supported = []
        for fact_type in candidates:
            route = objective_fact_source_route(fact_type)
            if (
                providers.intersection(route.primary_providers)
                or providers.intersection(route.verification_providers)
                or providers.intersection(route.supporting_providers)
            ):
                supported.append(fact_type)
        if supported:
            candidates = supported
    return list(dict.fromkeys(candidates))

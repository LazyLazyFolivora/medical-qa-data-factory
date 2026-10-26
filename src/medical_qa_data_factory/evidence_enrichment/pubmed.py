"""Minimal PubMed E-utilities client with read-back provenance."""
from __future__ import annotations

import hashlib
import os
import xml.etree.ElementTree as ET
from datetime import date
from typing import Any, Callable

from ..domain.common import utc_now
from .models import PubMedEvidenceRecord


EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
PubMedTransport = Callable[[str, dict[str, Any]], bytes]


def _requests_transport(url: str, data: dict[str, Any]) -> bytes:
    import requests

    params = dict(data)
    api_key = os.environ.get("NCBI_API_KEY")
    if api_key:
        params["api_key"] = api_key
    response = requests.post(url, data=params, timeout=60)
    response.raise_for_status()
    return response.content


def _text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return "".join(node.itertext()).strip()


class PubMedClient:
    """Fetch titles and abstracts by PMID without hiding failed read-backs."""

    def __init__(
        self,
        *,
        transport: PubMedTransport | None = None,
        base_url: str = EUTILS_BASE,
        snapshot_id: str | None = None,
    ) -> None:
        self._transport = transport or _requests_transport
        self._base_url = base_url.rstrip("/")
        self._snapshot_id = snapshot_id or f"pubmed:{date.today().isoformat()}"

    def search(self, query: str, *, limit: int = 10) -> list[str]:
        raw = self._transport(
            f"{self._base_url}/esearch.fcgi",
            {
                "db": "pubmed",
                "term": query,
                "retmode": "xml",
                "retmax": str(limit),
                "sort": "relevance",
            },
        )
        root = ET.fromstring(raw)
        return [
            _text(node)
            for node in root.findall(".//IdList/Id")
            if _text(node)
        ]

    def fetch(self, pmids: list[str], *, limit: int = 10) -> list[PubMedEvidenceRecord]:
        selected = [item for item in pmids if item.strip()][:limit]
        if not selected:
            return []
        raw = self._transport(
            f"{self._base_url}/efetch.fcgi",
            {
                "db": "pubmed",
                "id": ",".join(selected),
                "retmode": "xml",
            },
        )
        root = ET.fromstring(raw)
        records: list[PubMedEvidenceRecord] = []
        for article in root.findall(".//PubmedArticle"):
            pmid = _text(article.find(".//PMID"))
            if not pmid:
                continue
            title = _text(article.find(".//ArticleTitle"))
            abstract_parts: list[str] = []
            for node in article.findall(".//Abstract/AbstractText"):
                value = _text(node)
                if not value:
                    continue
                label = node.attrib.get("Label")
                abstract_parts.append(f"{label}: {value}" if label else value)
            abstract = "\n".join(abstract_parts)
            doi = None
            for node in article.findall(".//ArticleId"):
                if node.attrib.get("IdType") == "doi":
                    doi = _text(node) or None
                    break
            if not title and not abstract:
                continue
            body = f"{title}\n{abstract}"
            records.append(
                PubMedEvidenceRecord(
                    pmid=pmid,
                    title=title,
                    abstract=abstract,
                    doi=doi,
                    snapshot_id=self._snapshot_id,
                    content_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
                    retrieved_at=utc_now(),
                )
            )
        return records

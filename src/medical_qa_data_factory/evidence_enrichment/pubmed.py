"""Minimal PubMed E-utilities client with read-back provenance."""
from __future__ import annotations

import hashlib
import os
import time
import xml.etree.ElementTree as ET
from datetime import date
from enum import Enum
from typing import Any, Callable

from pydantic import Field

from ..domain.common import ContractModel, utc_now
from .models import PubMedEvidenceRecord


EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
PubMedTransport = Callable[[str, dict[str, Any]], bytes]


class PubMedProviderStatus(str, Enum):
    SUCCESS = "success"
    EMPTY = "empty"
    NO_ABSTRACT = "no_abstract"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    NETWORK_ERROR = "network_error"
    PROVIDER_ERROR = "provider_error"
    MALFORMED_RESPONSE = "malformed_response"


class PubMedProviderError(RuntimeError):
    def __init__(self, status: PubMedProviderStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


class PubMedFetchResult(ContractModel):
    status: PubMedProviderStatus
    snapshot_id: str = Field(min_length=1)
    input_pmids: list[str] = Field(default_factory=list)
    requested_pmids: list[str] = Field(default_factory=list)
    records: list[PubMedEvidenceRecord] = Field(default_factory=list)
    missing_pmids: list[str] = Field(default_factory=list)
    unexpected_pmids: list[str] = Field(default_factory=list)
    no_abstract_pmids: list[str] = Field(default_factory=list)
    truncated: bool = False
    attempt_count: int = Field(default=0, ge=0)
    error_message: str | None = None


class PubMedSearchResult(ContractModel):
    status: PubMedProviderStatus
    snapshot_id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    pmids: list[str] = Field(default_factory=list)
    attempt_count: int = Field(default=0, ge=0)
    error_message: str | None = None


def _requests_transport(url: str, data: dict[str, Any]) -> bytes:
    import requests

    params = dict(data)
    api_key = os.environ.get("NCBI_API_KEY")
    if api_key:
        params["api_key"] = api_key
    try:
        response = requests.post(url, data=params, timeout=60)
        if response.status_code == 429:
            raise PubMedProviderError(
                PubMedProviderStatus.RATE_LIMITED,
                "PubMed rate limit exceeded",
            )
        response.raise_for_status()
        return response.content
    except PubMedProviderError:
        raise
    except requests.Timeout as exc:
        raise PubMedProviderError(
            PubMedProviderStatus.TIMEOUT, str(exc)
        ) from exc
    except requests.ConnectionError as exc:
        raise PubMedProviderError(
            PubMedProviderStatus.NETWORK_ERROR, str(exc)
        ) from exc
    except requests.RequestException as exc:
        raise PubMedProviderError(
            PubMedProviderStatus.PROVIDER_ERROR, str(exc)
        ) from exc


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
        max_attempts: int = 3,
        retry_backoff_seconds: float = 0.25,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._transport = transport or _requests_transport
        self._base_url = base_url.rstrip("/")
        self._snapshot_id = snapshot_id or f"pubmed:{date.today().isoformat()}"
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._max_attempts = max_attempts
        self._retry_backoff_seconds = retry_backoff_seconds
        self._sleep = sleep

    def _request(
        self, url: str, data: dict[str, Any]
    ) -> tuple[bytes | None, PubMedProviderError | None, int]:
        last_error: PubMedProviderError | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                return self._transport(url, data), None, attempt
            except PubMedProviderError as exc:
                last_error = exc
            except TimeoutError as exc:
                last_error = PubMedProviderError(
                    PubMedProviderStatus.TIMEOUT, str(exc)
                )
            except OSError as exc:
                last_error = PubMedProviderError(
                    PubMedProviderStatus.NETWORK_ERROR, str(exc)
                )
            if attempt < self._max_attempts:
                self._sleep(self._retry_backoff_seconds * attempt)
        return None, last_error, self._max_attempts

    def search(self, query: str, *, limit: int = 10) -> list[str]:
        result = self.search_result(query, limit=limit)
        if result.status not in {
            PubMedProviderStatus.SUCCESS,
            PubMedProviderStatus.EMPTY,
        }:
            raise PubMedProviderError(
                result.status, result.error_message or result.status.value
            )
        return result.pmids

    def search_result(
        self, query: str, *, limit: int = 10
    ) -> PubMedSearchResult:
        if limit < 1:
            raise ValueError("PubMed search limit must be positive")
        raw, error, attempts = self._request(
            f"{self._base_url}/esearch.fcgi",
            {
                "db": "pubmed",
                "term": query,
                "retmode": "xml",
                "retmax": str(limit),
                "sort": "relevance",
            },
        )
        if error is not None or raw is None:
            assert error is not None
            return PubMedSearchResult(
                status=error.status,
                snapshot_id=self._snapshot_id,
                query=query,
                attempt_count=attempts,
                error_message=str(error),
            )
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            return PubMedSearchResult(
                status=PubMedProviderStatus.MALFORMED_RESPONSE,
                snapshot_id=self._snapshot_id,
                query=query,
                attempt_count=attempts,
                error_message=str(exc),
            )
        pmids = [
            _text(node)
            for node in root.findall(".//IdList/Id")
            if _text(node)
        ]
        return PubMedSearchResult(
            status=(
                PubMedProviderStatus.SUCCESS
                if pmids
                else PubMedProviderStatus.EMPTY
            ),
            snapshot_id=self._snapshot_id,
            query=query,
            pmids=pmids,
            attempt_count=attempts,
        )

    def fetch(self, pmids: list[str], *, limit: int = 10) -> list[PubMedEvidenceRecord]:
        result = self.fetch_result(pmids, limit=limit)
        if result.status not in {
            PubMedProviderStatus.SUCCESS,
            PubMedProviderStatus.EMPTY,
            PubMedProviderStatus.NO_ABSTRACT,
        }:
            raise PubMedProviderError(
                result.status, result.error_message or result.status.value
            )
        return result.records

    def fetch_result(
        self, pmids: list[str], *, limit: int | None = None
    ) -> PubMedFetchResult:
        unique_pmids = list(
            dict.fromkeys(item.strip() for item in pmids if item.strip())
        )
        selected = unique_pmids[:limit] if limit is not None else unique_pmids
        truncated = len(selected) < len(unique_pmids)
        if not selected:
            return PubMedFetchResult(
                status=PubMedProviderStatus.EMPTY,
                snapshot_id=self._snapshot_id,
                input_pmids=unique_pmids,
                truncated=truncated,
            )
        raw, error, attempts = self._request(
            f"{self._base_url}/efetch.fcgi",
            {
                "db": "pubmed",
                "id": ",".join(selected),
                "retmode": "xml",
            },
        )
        if error is not None or raw is None:
            assert error is not None
            return PubMedFetchResult(
                status=error.status,
                snapshot_id=self._snapshot_id,
                input_pmids=unique_pmids,
                requested_pmids=selected,
                truncated=truncated,
                attempt_count=attempts,
                error_message=str(error),
            )
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            return PubMedFetchResult(
                status=PubMedProviderStatus.MALFORMED_RESPONSE,
                snapshot_id=self._snapshot_id,
                input_pmids=unique_pmids,
                requested_pmids=selected,
                truncated=truncated,
                attempt_count=attempts,
                error_message=str(exc),
            )
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
        returned = {record.pmid for record in records}
        no_abstract = sorted(
            record.pmid for record in records if not record.abstract.strip()
        )
        if not records:
            status = PubMedProviderStatus.EMPTY
        elif len(no_abstract) == len(records):
            status = PubMedProviderStatus.NO_ABSTRACT
        else:
            status = PubMedProviderStatus.SUCCESS
        return PubMedFetchResult(
            status=status,
            snapshot_id=self._snapshot_id,
            input_pmids=unique_pmids,
            requested_pmids=selected,
            records=records,
            missing_pmids=sorted(set(selected) - returned),
            unexpected_pmids=sorted(returned - set(selected)),
            no_abstract_pmids=no_abstract,
            truncated=truncated,
            attempt_count=attempts,
        )

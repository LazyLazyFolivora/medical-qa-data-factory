"""Pinned PubMed corpus manifests and independently verifiable spans."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import Field, model_validator

from ..domain.common import ContractModel, UtcDateTime, utc_now
from .contracts import EvidenceRecord
from .models import PubMedEvidenceRecord


def _hash(payload: object) -> str:
    body = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class PubMedSpanError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PubMedSpanCitation(ContractModel):
    pmid: str = Field(min_length=1)
    text_field: Literal["abstract"] = "abstract"
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: str = Field(min_length=1)

    @model_validator(mode="after")
    def offsets_have_valid_shape(self) -> "PubMedSpanCitation":
        if self.end <= self.start:
            raise ValueError("PubMed citation end must be after start")
        return self


class PubMedEvidenceSpan(ContractModel):
    schema_version: Literal["pubmed-evidence-span-v1"] = (
        "pubmed-evidence-span-v1"
    )
    span_id: str = Field(min_length=1)
    pmid: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    record_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    text_field: Literal["abstract"] = "abstract"
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: str = Field(min_length=1)
    quote_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    verified_at: UtcDateTime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def identity_is_valid(self) -> "PubMedEvidenceSpan":
        if self.end <= self.start:
            raise ValueError("PubMed span end must be after start")
        if hashlib.sha256(self.quote.encode("utf-8")).hexdigest() != self.quote_hash:
            raise ValueError("PubMed span quote hash does not match quote")
        expected = _span_id(
            pmid=self.pmid,
            snapshot_id=self.snapshot_id,
            record_content_hash=self.record_content_hash,
            text_field=self.text_field,
            start=self.start,
            end=self.end,
            quote_hash=self.quote_hash,
        )
        if self.span_id != expected:
            raise ValueError("PubMed span ID does not match provenance")
        return self


def build_span_candidates(
    records: list[PubMedEvidenceRecord],
) -> list[dict[str, object]]:
    """Build exact sentence-like spans that a model can select verbatim.

    Offsets are computed by code over the pinned abstract.  The model still
    decides whether a span supports a claim, but it no longer needs to count
    characters or reproduce source text from memory.
    """
    candidates: list[dict[str, object]] = []
    for record in records:
        text = record.abstract
        boundaries = [0]
        boundaries.extend(
            match.end()
            for match in re.finditer(r"(?<=[.!?])\s+", text)
        )
        boundaries.append(len(text))
        for raw_start, raw_end in zip(boundaries, boundaries[1:]):
            start = raw_start
            end = raw_end
            while start < end and text[start].isspace():
                start += 1
            while end > start and text[end - 1].isspace():
                end -= 1
            if start == end:
                continue
            candidates.append(
                {
                    "candidate_id": (
                        f"pmid:{record.pmid}:abstract:{start}:{end}"
                    ),
                    "pmid": record.pmid,
                    "text_field": "abstract",
                    "start": start,
                    "end": end,
                    "quote": text[start:end],
                }
            )
    return candidates


def _corpus_manifest_id(
    *,
    snapshot_id: str,
    requested_pmids: list[str],
    record_hashes: dict[str, str],
) -> str:
    return "pubmed-corpus." + _hash(
        {
            "snapshot_id": snapshot_id,
            "requested_pmids": sorted(set(requested_pmids)),
            "record_hashes": dict(sorted(record_hashes.items())),
        }
    )[:24]


class PubMedCorpusManifest(ContractModel):
    schema_version: Literal["pubmed-corpus-manifest-v1"] = (
        "pubmed-corpus-manifest-v1"
    )
    manifest_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    requested_pmids: list[str] = Field(default_factory=list)
    record_hashes: dict[str, str] = Field(default_factory=dict)
    built_at: UtcDateTime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def identity_is_valid(self) -> "PubMedCorpusManifest":
        if self.requested_pmids != sorted(set(self.requested_pmids)):
            raise ValueError("PubMed manifest PMIDs must be sorted and unique")
        if any(
            len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in self.record_hashes.values()
        ):
            raise ValueError("PubMed manifest contains an invalid content hash")
        expected = _corpus_manifest_id(
            snapshot_id=self.snapshot_id,
            requested_pmids=self.requested_pmids,
            record_hashes=self.record_hashes,
        )
        if self.manifest_id != expected:
            raise ValueError("PubMed corpus manifest ID does not match content")
        return self


def _span_id(
    *,
    pmid: str,
    snapshot_id: str,
    record_content_hash: str,
    text_field: str,
    start: int,
    end: int,
    quote_hash: str,
) -> str:
    digest = _hash(
        {
            "pmid": pmid,
            "snapshot_id": snapshot_id,
            "record_content_hash": record_content_hash,
            "text_field": text_field,
            "start": start,
            "end": end,
            "quote_hash": quote_hash,
        }
    )
    return f"pubmed-span.{digest[:24]}"


def verify_span_citation(
    citation: PubMedSpanCitation,
    *,
    records: list[PubMedEvidenceRecord],
) -> PubMedEvidenceSpan:
    matches = [record for record in records if record.pmid == citation.pmid]
    if not matches:
        raise PubMedSpanError(
            "unknown_pmid_reference", "citation PMID was not fetched"
        )
    if len(matches) != 1:
        raise PubMedSpanError(
            "ambiguous_pubmed_record", "citation PMID is not unique"
        )
    record = matches[0]
    text = record.abstract
    if citation.end > len(text):
        raise PubMedSpanError(
            "pubmed_span_out_of_bounds", "citation offsets exceed abstract"
        )
    actual_quote = text[citation.start : citation.end]
    if actual_quote != citation.quote:
        # Models are often off by a few characters when counting offsets, in
        # particular around Unicode punctuation.  The quote itself remains
        # the evidence-bearing value, so it is safe to re-anchor only when it
        # occurs verbatim and exactly once in the pinned abstract.  Repeated
        # or modified text still fails closed.
        corrected_start = text.find(citation.quote)
        if corrected_start < 0 or text.find(
            citation.quote, corrected_start + 1
        ) >= 0:
            raise PubMedSpanError(
                "pubmed_quote_mismatch",
                "citation quote does not match abstract",
            )
        start = corrected_start
        end = corrected_start + len(citation.quote)
    else:
        start = citation.start
        end = citation.end
    quote_hash = hashlib.sha256(citation.quote.encode("utf-8")).hexdigest()
    return PubMedEvidenceSpan(
        span_id=_span_id(
            pmid=record.pmid,
            snapshot_id=record.snapshot_id,
            record_content_hash=record.content_hash,
            text_field=citation.text_field,
            start=start,
            end=end,
            quote_hash=quote_hash,
        ),
        pmid=record.pmid,
        snapshot_id=record.snapshot_id,
        record_content_hash=record.content_hash,
        text_field=citation.text_field,
        start=start,
        end=end,
        quote=citation.quote,
        quote_hash=quote_hash,
    )


def read_back_span(
    span: PubMedEvidenceSpan, *, record: PubMedEvidenceRecord
) -> str:
    if record.pmid != span.pmid:
        raise PubMedSpanError("unknown_pmid_reference", "span PMID changed")
    if record.snapshot_id != span.snapshot_id:
        raise PubMedSpanError(
            "pubmed_snapshot_mismatch", "span snapshot changed"
        )
    if record.content_hash != span.record_content_hash:
        raise PubMedSpanError(
            "pubmed_content_hash_mismatch", "PubMed record content changed"
        )
    if span.end > len(record.abstract):
        raise PubMedSpanError(
            "pubmed_span_out_of_bounds", "span offsets exceed abstract"
        )
    quote = record.abstract[span.start : span.end]
    if quote != span.quote:
        raise PubMedSpanError(
            "pubmed_quote_mismatch", "span quote changed during read-back"
        )
    if hashlib.sha256(quote.encode("utf-8")).hexdigest() != span.quote_hash:
        raise PubMedSpanError(
            "pubmed_quote_hash_mismatch", "span quote hash changed"
        )
    return quote


def evidence_from_verified_span(
    span: PubMedEvidenceSpan,
    *,
    record: PubMedEvidenceRecord,
) -> EvidenceRecord:
    quote = read_back_span(span, record=record)
    return EvidenceRecord.create(
        provider_id="pubmed",
        source_kind="literature",
        snapshot_id=span.snapshot_id,
        source_locator=(
            f"pmid:{span.pmid}:{span.text_field}:{span.start}:{span.end}"
        ),
        content_hash=span.quote_hash,
        record_type="document",
        document_text=quote,
        retrieved_at=record.retrieved_at,
        source_metadata={
            "pmid": span.pmid,
            "span_id": span.span_id,
            "parent_content_hash": span.record_content_hash,
            "text_field": span.text_field,
            "start": span.start,
            "end": span.end,
        },
    )


def build_corpus_manifest(
    records: list[PubMedEvidenceRecord],
    *,
    requested_pmids: list[str],
) -> PubMedCorpusManifest:
    snapshots = {record.snapshot_id for record in records}
    if len(snapshots) != 1:
        raise ValueError("a PubMed corpus manifest needs exactly one snapshot")
    record_hashes: dict[str, str] = {}
    for record in records:
        previous = record_hashes.get(record.pmid)
        if previous is not None and previous != record.content_hash:
            raise ValueError("one PMID has conflicting content hashes")
        record_hashes[record.pmid] = record.content_hash
    snapshot_id = next(iter(snapshots))
    normalized_requested = sorted(set(requested_pmids))
    sorted_hashes = dict(sorted(record_hashes.items()))
    manifest_id = _corpus_manifest_id(
        snapshot_id=snapshot_id,
        requested_pmids=normalized_requested,
        record_hashes=sorted_hashes,
    )
    return PubMedCorpusManifest(
        manifest_id=manifest_id,
        snapshot_id=snapshot_id,
        requested_pmids=normalized_requested,
        record_hashes=sorted_hashes,
    )

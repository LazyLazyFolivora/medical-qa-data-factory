from __future__ import annotations

import hashlib

import pytest

from medical_qa_data_factory.evidence_enrichment.pubmed import (
    PubMedClient,
    PubMedProviderError,
    PubMedProviderStatus,
)
from medical_qa_data_factory.evidence_enrichment.pubmed_evidence import (
    PubMedSpanCitation,
    PubMedSpanError,
    build_corpus_manifest,
    evidence_from_verified_span,
    read_back_span,
    verify_span_citation,
)


ABSTRACT = "Aspirin exposure was associated with lower prostate cancer risk."


def _xml(*, abstract: str | None = ABSTRACT) -> bytes:
    abstract_xml = (
        f"<Abstract><AbstractText>{abstract}</AbstractText></Abstract>"
        if abstract is not None
        else ""
    )
    return f"""
    <PubmedArticleSet>
      <PubmedArticle>
        <MedlineCitation>
          <PMID>123</PMID>
          <Article>
            <ArticleTitle>Aspirin and prostate cancer</ArticleTitle>
            {abstract_xml}
          </Article>
        </MedlineCitation>
      </PubmedArticle>
    </PubmedArticleSet>
    """.encode()


def test_fetch_result_distinguishes_missing_and_no_abstract() -> None:
    client = PubMedClient(
        transport=lambda _url, _data: _xml(abstract=None),
        snapshot_id="pubmed:test",
    )

    result = client.fetch_result(["123", "999"])

    assert result.status is PubMedProviderStatus.NO_ABSTRACT
    assert [record.pmid for record in result.records] == ["123"]
    assert result.no_abstract_pmids == ["123"]
    assert result.missing_pmids == ["999"]


@pytest.mark.parametrize(
    "status",
    [
        PubMedProviderStatus.TIMEOUT,
        PubMedProviderStatus.RATE_LIMITED,
        PubMedProviderStatus.NETWORK_ERROR,
        PubMedProviderStatus.PROVIDER_ERROR,
    ],
)
def test_transient_provider_failures_are_classified_and_retried(status) -> None:
    attempts = 0

    def transport(_url, _data):
        nonlocal attempts
        attempts += 1
        raise PubMedProviderError(status, "failed")

    client = PubMedClient(
        transport=transport,
        snapshot_id="pubmed:test",
        max_attempts=3,
        sleep=lambda _seconds: None,
    )

    result = client.fetch_result(["123"])

    assert result.status is status
    assert result.attempt_count == 3
    assert attempts == 3
    assert result.records == []


def test_empty_and_malformed_responses_are_distinct() -> None:
    empty = PubMedClient(
        transport=lambda _url, _data: b"<PubmedArticleSet />",
        snapshot_id="pubmed:test",
    ).fetch_result(["123"])
    malformed = PubMedClient(
        transport=lambda _url, _data: b"not xml",
        snapshot_id="pubmed:test",
    ).fetch_result(["123"])

    assert empty.status is PubMedProviderStatus.EMPTY
    assert malformed.status is PubMedProviderStatus.MALFORMED_RESPONSE


def test_fetch_reports_budget_truncation_without_hiding_input_pmids() -> None:
    result = PubMedClient(
        transport=lambda _url, _data: _xml(),
        snapshot_id="pubmed:test",
    ).fetch_result(["123", "456"], limit=1)

    assert result.input_pmids == ["123", "456"]
    assert result.requested_pmids == ["123"]
    assert result.truncated is True


def test_verified_span_round_trips_against_the_pinned_record() -> None:
    record = PubMedClient(
        transport=lambda _url, _data: _xml(),
        snapshot_id="pubmed:test",
    ).fetch_result(["123"]).records[0]
    quote = "lower prostate cancer risk"
    start = record.abstract.index(quote)
    citation = PubMedSpanCitation(
        pmid="123",
        text_field="abstract",
        start=start,
        end=start + len(quote),
        quote=quote,
    )

    span = verify_span_citation(citation, records=[record])
    evidence = evidence_from_verified_span(span, record=record)

    assert span.quote_hash == hashlib.sha256(quote.encode()).hexdigest()
    assert evidence.document_text == quote
    assert evidence.source_locator.endswith(f":{start}:{start + len(quote)}")
    assert evidence.source_metadata["parent_content_hash"] == record.content_hash


@pytest.mark.parametrize(
    "citation,code",
    [
        (
            PubMedSpanCitation(
                pmid="999",
                start=0,
                end=7,
                quote="Aspirin",
            ),
            "unknown_pmid_reference",
        ),
        (
            PubMedSpanCitation(
                pmid="123",
                start=0,
                end=10_000,
                quote="Aspirin",
            ),
            "pubmed_span_out_of_bounds",
        ),
        (
            PubMedSpanCitation(
                pmid="123",
                start=0,
                end=7,
                quote="Ibuprofen",
            ),
            "pubmed_quote_mismatch",
        ),
    ],
)
def test_invalid_span_citations_fail_closed(citation, code) -> None:
    record = PubMedClient(
        transport=lambda _url, _data: _xml(),
        snapshot_id="pubmed:test",
    ).fetch_result(["123"]).records[0]

    with pytest.raises(PubMedSpanError) as exc_info:
        verify_span_citation(citation, records=[record])

    assert exc_info.value.code == code


def test_corpus_manifest_identity_is_stable_for_same_snapshot_and_text() -> None:
    first = PubMedClient(
        transport=lambda _url, _data: _xml(),
        snapshot_id="pubmed:test",
    ).fetch_result(["123"]).records
    second = PubMedClient(
        transport=lambda _url, _data: _xml(),
        snapshot_id="pubmed:test",
    ).fetch_result(["123"]).records

    first_manifest = build_corpus_manifest(first, requested_pmids=["123"])
    second_manifest = build_corpus_manifest(second, requested_pmids=["123"])

    assert first_manifest.manifest_id == second_manifest.manifest_id
    assert first_manifest.record_hashes == {"123": first[0].content_hash}

    citation = PubMedSpanCitation(
        pmid="123",
        start=0,
        end=7,
        quote="Aspirin",
    )
    first_span = verify_span_citation(citation, records=first)
    second_span = verify_span_citation(citation, records=second)
    assert first_span.span_id == second_span.span_id
    assert (
        evidence_from_verified_span(first_span, record=first[0]).evidence_id
        == evidence_from_verified_span(
            second_span, record=second[0]
        ).evidence_id
    )


def test_span_read_back_rejects_a_different_snapshot() -> None:
    record = PubMedClient(
        transport=lambda _url, _data: _xml(),
        snapshot_id="pubmed:test",
    ).fetch_result(["123"]).records[0]
    span = verify_span_citation(
        PubMedSpanCitation(
            pmid="123", start=0, end=7, quote="Aspirin"
        ),
        records=[record],
    )

    with pytest.raises(PubMedSpanError) as exc_info:
        read_back_span(
            span,
            record=record.model_copy(update={"snapshot_id": "pubmed:other"}),
        )

    assert exc_info.value.code == "pubmed_snapshot_mismatch"


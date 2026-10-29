"""The provider boundary never conflates empty, failed, and unknown results."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from medical_qa_data_factory.generation.evidence_provider import (
    EvidenceProviderCapability,
    EvidenceProviderRequest,
    EvidenceProviderResult,
    ProviderTerminalStatus,
    SideEffectClass,
)


NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)


def test_non_idempotent_write_cannot_claim_safe_automatic_replay() -> None:
    with pytest.raises(ValidationError, match="safe_to_repeat"):
        EvidenceProviderCapability(
            provider_id="unsafe-writer",
            provider_version="v1",
            side_effect_class=SideEffectClass.NON_IDEMPOTENT_WRITE,
            supports_idempotency=False,
            supports_status_lookup=False,
            safe_to_repeat=True,
        )


def test_read_only_provider_is_explicitly_replay_safe() -> None:
    capability = EvidenceProviderCapability.read_only(provider_id="pubmed")
    assert capability.safe_to_repeat is True
    assert capability.side_effect_class is SideEffectClass.READ_ONLY


def test_failed_or_unknown_result_requires_error_category() -> None:
    request = EvidenceProviderRequest(
        request_id="request-1",
        provider_id="pubmed",
        provider_version="v1",
        query={"term": "BRCA1 chromosome"},
        requested_at=NOW,
        material_limit=10,
    )
    with pytest.raises(ValidationError, match="error_category"):
        EvidenceProviderResult(
            request=request,
            terminal_status=ProviderTerminalStatus.FAILED,
            material_ids=[],
            anchor_ids=[],
            error_category=None,
        )


def test_empty_and_failure_are_distinct_terminal_outcomes() -> None:
    request = EvidenceProviderRequest(
        request_id="request-1",
        provider_id="pubmed",
        provider_version="v1",
        query={"term": "no results"},
        requested_at=NOW,
        material_limit=10,
    )
    empty = EvidenceProviderResult(
        request=request,
        terminal_status=ProviderTerminalStatus.EMPTY,
        material_ids=[],
        anchor_ids=[],
        error_category=None,
    )
    assert empty.terminal_status is ProviderTerminalStatus.EMPTY

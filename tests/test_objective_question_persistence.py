"""Immutable material, anchor, entity-resolution and attempt persistence."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from medical_qa_data_factory.generation.evidence_provider import (
    EvidenceProviderAttempt,
    EvidenceProviderRequest,
    EvidenceProviderResult,
    ProviderTerminalStatus,
)
from medical_qa_data_factory.generation.objective_question_contracts import (
    EntityResolution,
    EntityResolutionStatus,
    EvidenceAnchor,
    EvidenceMaterial,
    text_digest,
)
from medical_qa_data_factory.generation.objective_question_persistence import (
    AttemptIdentityConflictError,
    ObjectiveQuestionPersistence,
    immutable_blob_ref,
)
from medical_qa_data_factory.generation.repository import (
    IdempotencyConflictError,
)


NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)
RAW_TEXT = "BRCA1 is located on human chromosome 17."


def _material() -> EvidenceMaterial:
    digest = text_digest(RAW_TEXT)
    return EvidenceMaterial(
        material_id="material-brca1-location",
        provider_id="authority-a",
        provider_version="2026-10",
        source_record_id="gene:BRCA1",
        retrieved_at=NOW,
        content_hash=digest,
        raw_content_ref=immutable_blob_ref(digest),
        material_type="structured_record",
        retrieval_request_id="request-1",
        terminal_status=ProviderTerminalStatus.SUCCEEDED,
    )


def _anchor(**overrides) -> EvidenceAnchor:
    payload = {
        "anchor_id": "anchor-brca1-location",
        "material_id": "material-brca1-location",
        "document_id": "authority-a:gene:BRCA1",
        "section_or_field": "chromosome_location",
        "start_offset": 0,
        "end_offset": 5,
        "quoted_text": "BRCA1",
        "quote_hash": text_digest("BRCA1"),
    }
    payload.update(overrides)
    return EvidenceAnchor(**payload)


def _attempt(
    *,
    attempt_id: str,
    retry_ordinal: int,
    terminal_status: ProviderTerminalStatus,
    error_category: str | None = None,
) -> EvidenceProviderAttempt:
    return EvidenceProviderAttempt(
        attempt_id=attempt_id,
        operation_key="run-1|retrieve|brca1-location",
        retry_ordinal=retry_ordinal,
        provider_id="authority-a",
        provider_version="2026-10",
        started_at=NOW,
        terminal_status=terminal_status,
        error_category=error_category,
    )


def test_material_blob_is_immutable_and_reloadable(tmp_path: Path) -> None:
    store = ObjectiveQuestionPersistence(tmp_path / "workspace")
    material = _material()

    assert store.save_material(material, RAW_TEXT.encode("utf-8")) == "written"
    assert store.save_material(material, RAW_TEXT.encode("utf-8")) == "existing"
    assert store.load_material(material.material_id) == material
    assert store.load_raw_content(material).decode("utf-8") == RAW_TEXT


def test_material_content_hash_must_match_raw_blob(tmp_path: Path) -> None:
    store = ObjectiveQuestionPersistence(tmp_path / "workspace")

    with pytest.raises(ValueError, match="content_hash"):
        store.save_material(_material(), b"different content")


def test_anchor_is_verified_against_immutable_raw_content(tmp_path: Path) -> None:
    store = ObjectiveQuestionPersistence(tmp_path / "workspace")
    store.save_material(_material(), RAW_TEXT.encode("utf-8"))
    anchor = _anchor()

    assert store.save_anchor(anchor) == "written"
    assert store.load_anchor(anchor.anchor_id) == anchor
    with pytest.raises(ValueError, match="raw content"):
        store.save_anchor(
            _anchor(
                anchor_id="anchor-shifted",
                start_offset=1,
                end_offset=6,
            )
        )


def test_active_entity_resolution_is_append_only(tmp_path: Path) -> None:
    store = ObjectiveQuestionPersistence(tmp_path / "workspace")
    resolution = EntityResolution(
        resolution_id="resolution-brca1-1",
        mention="BRCA1",
        namespace="NCBI Gene",
        canonical_id="672",
        canonical_name="BRCA1 DNA repair associated",
        species="Homo sapiens",
        authority="NCBI Gene",
        resolution_status=EntityResolutionStatus.RESOLVED,
    )

    assert store.save_entity_resolution(resolution) == "written"
    assert store.load_entity_resolution(resolution.resolution_id) == resolution
    with pytest.raises(IdempotencyConflictError):
        store.save_entity_resolution(
            resolution.model_copy(update={"canonical_id": "999"})
        )


def test_retry_appends_a_new_attempt_without_overwriting_old_attempt(
    tmp_path: Path,
) -> None:
    store = ObjectiveQuestionPersistence(tmp_path / "workspace")
    failed = _attempt(
        attempt_id="attempt-1",
        retry_ordinal=0,
        terminal_status=ProviderTerminalStatus.FAILED,
        error_category="TRANSIENT_TOOL_FAILURE",
    )
    retried = _attempt(
        attempt_id="attempt-2",
        retry_ordinal=1,
        terminal_status=ProviderTerminalStatus.SUCCEEDED,
    )

    store.save_provider_attempt(failed)
    store.save_provider_attempt(retried)

    attempts = store.list_attempts_by_operation(failed.operation_key)
    assert [item.attempt_id for item in attempts] == ["attempt-1", "attempt-2"]
    assert attempts[0].terminal_status is ProviderTerminalStatus.FAILED
    assert attempts[1].terminal_status is ProviderTerminalStatus.SUCCEEDED


def test_retry_ordinal_cannot_be_reused_by_another_attempt(tmp_path: Path) -> None:
    store = ObjectiveQuestionPersistence(tmp_path / "workspace")
    store.save_provider_attempt(
        _attempt(
            attempt_id="attempt-1",
            retry_ordinal=0,
            terminal_status=ProviderTerminalStatus.SUCCEEDED,
        )
    )

    with pytest.raises(AttemptIdentityConflictError):
        store.save_provider_attempt(
            _attempt(
                attempt_id="attempt-other",
                retry_ordinal=0,
                terminal_status=ProviderTerminalStatus.SUCCEEDED,
            )
        )


def test_provider_result_terminal_status_is_persisted(tmp_path: Path) -> None:
    store = ObjectiveQuestionPersistence(tmp_path / "workspace")
    request = EvidenceProviderRequest(
        request_id="request-1",
        provider_id="authority-a",
        provider_version="2026-10",
        operation_key="run-1|retrieve|brca1-location",
        query={"entity": "BRCA1", "field": "chromosome"},
        requested_at=NOW,
        material_limit=10,
    )
    result = EvidenceProviderResult(
        request=request,
        terminal_status=ProviderTerminalStatus.EMPTY,
        material_ids=[],
        anchor_ids=[],
        error_category=None,
    )

    assert store.save_provider_result(result) == "written"
    assert store.load_provider_result(request.request_id).terminal_status is (
        ProviderTerminalStatus.EMPTY
    )

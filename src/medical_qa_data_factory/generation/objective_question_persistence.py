"""Immutable persistence for objective-question evidence inputs.

Material bytes are content-addressed before any claim can reference them.
Anchors are checked against the stored bytes rather than a remote URL or a
model summary. Provider attempts are append-only and retry identity is explicit.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .evidence_provider import (
    EvidenceProviderAttempt,
    EvidenceProviderResult,
)
from .objective_question_contracts import (
    EntityResolution,
    EvidenceAnchor,
    EvidenceMaterial,
)
from .repository import GenerationRepository


MATERIALS = "objective_materials"
ANCHORS = "objective_evidence_anchors"
ENTITY_RESOLUTIONS = "objective_entity_resolutions"
PROVIDER_ATTEMPTS = "objective_provider_attempts"
PROVIDER_RESULTS = "objective_provider_results"


class AttemptIdentityConflictError(RuntimeError):
    code = "attempt_identity_conflict"


def immutable_blob_ref(content_hash: str) -> str:
    return f"objective_blobs/{content_hash}.bin"


class ObjectiveQuestionPersistence:
    """Content-addressed blob store plus append-only business artifacts."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.repository = GenerationRepository(self.root)
        self.artifacts = self.repository.artifacts

    # ---- immutable raw materials --------------------------------------

    def save_material(
        self,
        material: EvidenceMaterial,
        raw_content: bytes,
    ) -> str:
        digest = hashlib.sha256(raw_content).hexdigest()
        if material.content_hash != digest:
            raise ValueError("material content_hash does not match raw content")
        expected_ref = immutable_blob_ref(digest)
        if material.raw_content_ref != expected_ref:
            raise ValueError(
                "material raw_content_ref does not match content-addressed blob"
            )
        self._save_blob(digest, raw_content, expected_ref)
        return self.repository.save_idempotent(
            MATERIALS,
            material.material_id,
            "v1",
            material,
        )

    def load_material(self, material_id: str) -> EvidenceMaterial:
        material = self.artifacts.load_latest(
            MATERIALS,
            material_id,
            EvidenceMaterial,
        )
        expected_ref = immutable_blob_ref(material.content_hash)
        if material.raw_content_ref != expected_ref:
            raise ValueError("stored material has an invalid raw_content_ref")
        return material

    def load_raw_content(self, material: EvidenceMaterial) -> bytes:
        expected_ref = immutable_blob_ref(material.content_hash)
        if material.raw_content_ref != expected_ref:
            raise ValueError("material raw_content_ref does not match its hash")
        path = self.root / expected_ref
        try:
            payload = path.read_bytes()
        except FileNotFoundError as exc:
            raise ValueError("material raw content is missing") from exc
        if hashlib.sha256(payload).hexdigest() != material.content_hash:
            raise ValueError("material raw content failed hash verification")
        return payload

    # ---- anchors -------------------------------------------------------

    def save_anchor(self, anchor: EvidenceAnchor) -> str:
        material = self.load_material(anchor.material_id)
        raw_content = self.load_raw_content(material)
        try:
            text = raw_content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(
                "text anchors require UTF-8 material content"
            ) from exc
        if anchor.end_offset > len(text):
            raise ValueError("anchor offsets exceed raw content length")
        if text[anchor.start_offset : anchor.end_offset] != anchor.quoted_text:
            raise ValueError("anchor quoted_text does not match raw content")
        return self.repository.save_idempotent(
            ANCHORS,
            anchor.anchor_id,
            "v1",
            anchor,
        )

    def load_anchor(self, anchor_id: str) -> EvidenceAnchor:
        return self.artifacts.load_latest(ANCHORS, anchor_id, EvidenceAnchor)

    # ---- entity resolution --------------------------------------------

    def save_entity_resolution(self, resolution: EntityResolution) -> str:
        return self.repository.save_idempotent(
            ENTITY_RESOLUTIONS,
            resolution.resolution_id,
            "v1",
            resolution,
        )

    def load_entity_resolution(self, resolution_id: str) -> EntityResolution:
        return self.artifacts.load_latest(
            ENTITY_RESOLUTIONS,
            resolution_id,
            EntityResolution,
        )

    # ---- provider attempts and results --------------------------------

    def save_provider_attempt(self, attempt: EvidenceProviderAttempt) -> str:
        for existing in self.list_attempts_by_operation(attempt.operation_key):
            if (
                existing.retry_ordinal == attempt.retry_ordinal
                and existing.attempt_id != attempt.attempt_id
            ):
                raise AttemptIdentityConflictError(
                    "retry_ordinal is already assigned to another attempt"
                )
        return self.repository.save_idempotent(
            PROVIDER_ATTEMPTS,
            attempt.attempt_id,
            "v1",
            attempt,
        )

    def list_attempts_by_operation(
        self, operation_key: str
    ) -> list[EvidenceProviderAttempt]:
        attempts = [
            self.artifacts.load_latest(
                PROVIDER_ATTEMPTS,
                attempt_id,
                EvidenceProviderAttempt,
            )
            for attempt_id in self.artifacts.list_record_ids(PROVIDER_ATTEMPTS)
        ]
        return sorted(
            (
                attempt
                for attempt in attempts
                if attempt.operation_key == operation_key
            ),
            key=lambda item: (item.retry_ordinal, item.attempt_id),
        )

    def save_provider_result(self, result: EvidenceProviderResult) -> str:
        return self.repository.save_idempotent(
            PROVIDER_RESULTS,
            result.request.request_id,
            "v1",
            result,
        )

    def load_provider_result(self, request_id: str) -> EvidenceProviderResult:
        return self.artifacts.load_latest(
            PROVIDER_RESULTS,
            request_id,
            EvidenceProviderResult,
        )

    # ---- private helpers ----------------------------------------------

    def _save_blob(
        self,
        content_hash: str,
        raw_content: bytes,
        relative_ref: str,
    ) -> None:
        path = self.root / relative_ref
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as stream:
                stream.write(raw_content)
        except FileExistsError:
            if path.read_bytes() != raw_content:
                raise ValueError("content-addressed blob collision")
        if hashlib.sha256(path.read_bytes()).hexdigest() != content_hash:
            raise ValueError("stored material blob failed hash verification")

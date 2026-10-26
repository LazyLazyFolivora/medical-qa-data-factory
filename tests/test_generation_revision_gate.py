"""Revision budget: one rewrite only, then hard reject.

Step 0 of ``plans/real-generation-agent-implementation-plan.md`` requires that a
candidate which still needs a revision after the single allowed rewrite is
never carried into the machine-validated or human queue. A first revision still
routes back to the writer, and a blocking rejection never consumes the budget.

Each test runs a real episode over a tiny PrimeKG fixture with the verifier
verdicts scripted. Everything else - graph, repository, audit records - is the
production object.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from medical_qa_data_factory.generation import graph as graph_module
from medical_qa_data_factory.generation import service as service_module
from medical_qa_data_factory.generation.contracts import (
    EntityGenerationRequest,
    QuestionBlueprint,
    QuestionDraft,
    StopReason,
    UpstreamEntitySeed,
    VerificationResult,
)
from medical_qa_data_factory.generation.repository import GenerationRepository
from medical_qa_data_factory.generation.service import EntityGenerationService

TINY_ROWS = [
    (
        "aspirin", "exposure_disease", "migraine",
        "D001241", "B156", "exposure", "disease", "DrugBank",
    ),
    (
        "aspirin", "exposure_disease", "headache",
        "D001241", "B001", "exposure", "disease", "DrugBank",
    ),
    (
        "aspirin", "contraindication", "warfarin",
        "D001241", "D002", "exposure", "exposure", "DrugBank",
    ),
]


@pytest.fixture
def tiny_db(tmp_path: Path) -> Path:
    path = tmp_path / "tiny.sqlite"
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE edges (head TEXT, relation TEXT, tail TEXT, head_id TEXT,"
            " tail_id TEXT, head_type TEXT, tail_type TEXT, source TEXT)"
        )
        connection.executemany("INSERT INTO edges VALUES (?,?,?,?,?,?,?,?)", TINY_ROWS)
        connection.commit()
    finally:
        connection.close()
    return path


def _service(tmp_path: Path, tiny_db: Path, monkeypatch) -> EntityGenerationService:
    config = tmp_path / "knowledge_graphs.json"
    config.write_text(
        json.dumps(
            {
                "default_provider": "primekg",
                "providers": {
                    "primekg": {
                        "driver": "primekg_sqlite",
                        "database_path": tiny_db.as_posix(),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MEDICAL_QA_KG_CONFIG", str(config))
    return EntityGenerationService(workspace=tmp_path / "ws", provider_id="primekg")


def _request(tmp_path: Path, **overrides: Any) -> EntityGenerationRequest:
    payload: dict[str, Any] = {
        "task_id": "aspirin-v1",
        "workspace": str(tmp_path / "ws"),
        "seed": UpstreamEntitySeed(
            provider_id="primekg",
            native_id="D001241",
            entity_type="exposure",
            canonical_name="aspirin",
        ),
        "question_count": 3,
    }
    payload.update(overrides)
    return EntityGenerationRequest(**payload)


class _ScriptedVerifier:
    """Returns one preset verdict per draft id and records every call."""

    def __init__(self, verdict: str) -> None:
        self.verdict = verdict
        self.calls: list[str] = []

    def verify_with_blueprint(
        self,
        draft: QuestionDraft,
        blueprint: QuestionBlueprint,
        claims: list[Any],
        evidence: list[Any],
    ) -> VerificationResult:
        self.calls.append(draft.draft_id)
        # The production node branches on ``blocking`` first and only then on
        # the verdict, so a blocking rejection must say so.
        blocking = self.verdict == "rejected"
        return VerificationResult(
            draft_id=draft.draft_id,
            verdict=self.verdict,  # type: ignore[arg-type]
            reason_codes=[] if self.verdict == "accepted" else ["scripted_reason"],
            message=f"scripted verdict {self.verdict}",
            blocking=blocking,
        )


def _run_with_verdict(
    tmp_path: Path, tiny_db: Path, monkeypatch, verdict: str
) -> tuple[Any, GenerationRepository, _ScriptedVerifier]:
    """Run one full episode with the verifier verdict scripted."""
    scripted = _ScriptedVerifier(verdict)
    service = _service(tmp_path, tiny_db, monkeypatch)
    original = service_module.GenerationContext

    class _Context(original):  # type: ignore[misc,valid-type]
        def __post_init__(self) -> None:
            super().__post_init__()
            # Replace only the verifier verdicts; every other dependency stays
            # exactly as the service assembled it.
            self.verifier = scripted

    monkeypatch.setattr(service_module, "GenerationContext", _Context)
    result = service.run(_request(tmp_path))
    return result, service.episodes, scripted


def test_first_revision_loops_back_once_and_is_not_accepted(
    tmp_path, tiny_db, monkeypatch
) -> None:
    result, episodes, _ = _run_with_verdict(tmp_path, tiny_db, monkeypatch, "needs_revision")
    # One rewrite happened, then the run stopped; nothing was auto-accepted.
    assert result.question_ids == []
    codes = {
        code
        for record in episodes.list_rejections_by_episode(result.episode_id)
        for code in record.reason_codes
    }
    assert "revision_limit_exhausted" in codes
    assert result.stop_reason == StopReason.ALL_CANDIDATES_HARD_REJECTED.value


def test_accepted_verdicts_reach_the_queue(tmp_path, tiny_db, monkeypatch) -> None:
    result, _, _ = _run_with_verdict(tmp_path, tiny_db, monkeypatch, "accepted")
    assert result.question_ids
    assert result.status == "completed"


def test_blocking_rejection_never_consumes_the_revision_budget(
    tmp_path, tiny_db, monkeypatch
) -> None:
    result, episodes, _ = _run_with_verdict(tmp_path, tiny_db, monkeypatch, "rejected")
    assert result.question_ids == []
    codes = {
        code
        for record in episodes.list_rejections_by_episode(result.episode_id)
        for code in record.reason_codes
    }
    assert "revision_limit_exhausted" not in codes
    assert result.stop_reason == StopReason.ALL_CANDIDATES_HARD_REJECTED.value

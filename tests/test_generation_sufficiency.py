"""The probe and the sufficiency judge.

Step 6 of the real-generation-agent plan. The properties under test are the
ones that keep a model from talking its way past the hard facts: a broken
probe, a missing text support, an unresolved conflict and an exhausted budget
all override a model that says "sufficient".
"""
from __future__ import annotations

from typing import Any

import pytest

from medical_qa_data_factory.generation.contracts import EvidencePath, EvidencePathStep
from medical_qa_data_factory.generation.probe import (
    AnswerabilityProbe,
    ProbeStatus,
    SnapshotGraphEvaluator,
    build_probe,
)
from medical_qa_data_factory.generation.run_identity import RequiredEvidenceMode
from medical_qa_data_factory.generation.sufficiency import (
    CoverageState,
    GuardrailCode,
    SufficiencyProposal,
    SufficiencyVerdict,
    SufficiencyJudge,
    coverage_state_from_assessments,
)
from medical_qa_data_factory.domain.common import utc_now


ASPIRIN = "drug:aspirin"
HEADACHE = "disease:headache"
MIGRAINE = "disease:migraine"


class _Entity:
    def __init__(self, canonical_id: str):
        self.canonical_id = canonical_id


class _Edge:
    def __init__(self, head: str, relation: str, tail: str):
        self.head = _Entity(head)
        self.relation = relation
        self.tail = _Entity(tail)


def _path(steps: list[tuple[str, str]] | None = None) -> EvidencePath:
    # An evidence path is multi-hop by contract, so the shortest fixture has
    # two steps.
    pairs = steps or [("treats", HEADACHE), ("indication", MIGRAINE)]
    path_steps = []
    for index, (relation, target) in enumerate(pairs):
        source = ASPIRIN if index == 0 else pairs[index - 1][1]
        path_steps.append(
            EvidencePathStep(
                position=index,
                evidence_id=f"kg.e{index}",
                traversal_direction="forward",
                from_entity_id=source,
                relation=relation,
                to_entity_id=target,
                provider_id="primekg",
                snapshot_id="snap-1",
                query_trace_id=f"q{index}",
            )
        )
    return EvidencePath(
        path_id="opath.1",
        path_version="opath.1.v1",
        episode_id="ep1",
        task_id="t1",
        bundle_id="b1",
        provider_id="primekg",
        snapshot_id="snap-1",
        start_entity_id=ASPIRIN,
        end_entity_id=pairs[-1][1],
        steps=path_steps,
        intermediate_entity_ids=[item[1] for item in pairs[:-1]],
        source_evidence_ids=[f"kg.e{index}" for index in range(len(pairs))],
        path_hash="hash",
        query_trace_ids=[f"q{index}" for index in range(len(pairs))],
    )


CHAIN = [_Edge(ASPIRIN, "treats", HEADACHE), _Edge(HEADACHE, "indication", MIGRAINE)]


# ---- the probe ------------------------------------------------------------


def test_a_probe_mirrors_the_path() -> None:
    probe = build_probe(_path([("treats", HEADACHE), ("indication", MIGRAINE)]))
    assert len(probe.steps) == 2
    assert probe.steps[0].relation == "treats"
    assert probe.steps[1].relation == "indication"
    assert probe.start_entity_id == ASPIRIN


def test_a_probe_carries_no_question_text() -> None:
    probe = build_probe(_path())
    payload = probe.model_dump(mode="json")
    assert "question" not in payload
    assert "answer" not in payload


def test_a_probe_carries_a_dependency_digest() -> None:
    probe = build_probe(_path())
    assert probe.dependency_digest
    assert probe.digest()


def test_two_probes_of_the_same_path_have_the_same_digest() -> None:
    first = build_probe(_path())
    second = build_probe(_path())
    assert first.dependency_digest == second.dependency_digest
    assert first.digest() == second.digest()


def test_a_changed_path_produces_a_different_digest() -> None:
    first = build_probe(_path([("treats", HEADACHE), ("indication", MIGRAINE)]))
    second = build_probe(
        _path([("treats", HEADACHE), ("indication", MIGRAINE), ("symptom", "s:pain")])
    )
    assert first.dependency_digest != second.dependency_digest


# ---- the evaluator --------------------------------------------------------


def test_a_probe_evaluates_to_the_reachable_answer() -> None:
    probe = build_probe(_path([("treats", HEADACHE), ("indication", MIGRAINE)]))
    evaluator = SnapshotGraphEvaluator(CHAIN, snapshot_id="snap-1")
    execution = evaluator.evaluate(probe)
    assert execution.status is ProbeStatus.OK
    assert execution.answer_ids == [MIGRAINE]


def test_a_probe_against_another_snapshot_is_incomplete() -> None:
    probe = build_probe(_path())
    evaluator = SnapshotGraphEvaluator(CHAIN, snapshot_id="snap-2")
    execution = evaluator.evaluate(probe)
    assert execution.status is ProbeStatus.INCOMPLETE
    assert "snapshot" in (execution.failure_reason or "")


def test_a_probe_with_no_answer_says_so() -> None:
    probe = build_probe(_path([("treats", "disease:absent"), ("indication", MIGRAINE)]))
    evaluator = SnapshotGraphEvaluator([], snapshot_id="snap-1")
    execution = evaluator.evaluate(probe)
    assert execution.status is ProbeStatus.NO_ANSWER


def test_an_ambiguous_probe_is_reported() -> None:
    edges = [
        _Edge(ASPIRIN, "treats", HEADACHE),
        _Edge(HEADACHE, "indication", MIGRAINE),
        _Edge(HEADACHE, "indication", "disease:other"),
    ]
    probe = build_probe(_path(), expected_cardinality="one")
    evaluator = SnapshotGraphEvaluator(edges, snapshot_id="snap-1")
    execution = evaluator.evaluate(probe)
    assert execution.status is ProbeStatus.AMBIGUOUS
    assert len(execution.answer_ids) == 2


def test_the_evaluator_searches_the_whole_snapshot_not_only_the_path() -> None:
    """The probe must not be answered by the path it came from."""
    probe = build_probe(_path([("treats", HEADACHE), ("indication", MIGRAINE)]))
    # The snapshot also has a second indication edge that the path did not use.
    edges = [
        _Edge(ASPIRIN, "treats", HEADACHE),
        _Edge(HEADACHE, "indication", MIGRAINE),
        _Edge(HEADACHE, "indication", "disease:other"),
    ]
    evaluator = SnapshotGraphEvaluator(edges, snapshot_id="snap-1")
    execution = evaluator.evaluate(probe)
    assert sorted(execution.answer_ids) == sorted([MIGRAINE, "disease:other"])


def test_a_truncated_answer_set_is_incomplete() -> None:
    edges = [
        _Edge(ASPIRIN, "treats", HEADACHE),
        *[_Edge(HEADACHE, "indication", f"disease:d{i}") for i in range(10)],
    ]
    probe = build_probe(_path())
    evaluator = SnapshotGraphEvaluator(edges, snapshot_id="snap-1", max_answers=3)
    execution = evaluator.evaluate(probe)
    assert execution.status is ProbeStatus.INCOMPLETE
    assert execution.truncated
    assert len(execution.answer_ids) == 3


# ---- the judge ------------------------------------------------------------


def _judge(evaluator: Any = None) -> SufficiencyJudge:
    return SufficiencyJudge(evaluator=evaluator)


def _sufficient_proposal() -> SufficiencyProposal:
    return SufficiencyProposal(
        verdict="sufficient",
        rationale="the evidence looks complete to me",
    )


def test_a_clean_probe_and_complete_coverage_are_sufficient() -> None:
    probe = build_probe(_path([("treats", HEADACHE), ("indication", MIGRAINE)]))
    evaluator = SnapshotGraphEvaluator(CHAIN, snapshot_id="snap-1")
    envelope = _judge(evaluator).judge(
        episode_id="ep1",
        turn=1,
        probe=probe,
        coverage=coverage_state_from_assessments(
            hop_positions=[0, 1],
            supported=[0, 1],
            required_evidence_mode=RequiredEvidenceMode.HYBRID_PER_HOP,
            remaining_tool_calls=5,
        ),
        proposal=_sufficient_proposal(),
    )
    assert envelope.verdict is SufficiencyVerdict.SUFFICIENT
    assert envelope.guardrail_codes == []


def test_a_model_cannot_override_a_missing_text_support() -> None:
    probe = build_probe(_path([("treats", HEADACHE), ("indication", MIGRAINE)]))
    evaluator = SnapshotGraphEvaluator(CHAIN, snapshot_id="snap-1")
    envelope = _judge(evaluator).judge(
        episode_id="ep1",
        turn=1,
        probe=probe,
        coverage=coverage_state_from_assessments(
            hop_positions=[0, 1],
            supported=[0],
            required_evidence_mode=RequiredEvidenceMode.HYBRID_PER_HOP,
            remaining_tool_calls=5,
        ),
        proposal=_sufficient_proposal(),
    )
    assert envelope.verdict is not SufficiencyVerdict.SUFFICIENT
    assert GuardrailCode.MISSING_TEXT_SUPPORT.value in envelope.guardrail_codes


def test_a_model_cannot_override_an_unresolved_conflict() -> None:
    probe = build_probe(_path())
    evaluator = SnapshotGraphEvaluator(CHAIN, snapshot_id="snap-1")
    envelope = _judge(evaluator).judge(
        episode_id="ep1",
        turn=1,
        probe=probe,
        coverage=coverage_state_from_assessments(
            hop_positions=[0, 1],
            supported=[0, 1],
            contradicting=["lit.contradicting"],
            remaining_tool_calls=5,
        ),
        proposal=_sufficient_proposal(),
    )
    assert envelope.verdict is not SufficiencyVerdict.SUFFICIENT
    assert GuardrailCode.UNRESOLVED_CONFLICT.value in envelope.guardrail_codes


def test_a_model_cannot_override_a_broken_probe() -> None:
    probe = build_probe(_path([("treats", "disease:absent"), ("indication", MIGRAINE)]))
    evaluator = SnapshotGraphEvaluator([], snapshot_id="snap-1")
    envelope = _judge(evaluator).judge(
        episode_id="ep1",
        turn=1,
        probe=probe,
        coverage=coverage_state_from_assessments(
            hop_positions=[0, 1],
            supported=[0, 1],
            remaining_tool_calls=5,
        ),
        proposal=_sufficient_proposal(),
    )
    assert envelope.verdict is not SufficiencyVerdict.SUFFICIENT
    assert GuardrailCode.PROBE_NO_ANSWER.value in envelope.guardrail_codes


def test_a_missing_probe_is_not_sufficient() -> None:
    envelope = _judge().judge(
        episode_id="ep1",
        turn=1,
        probe=None,
        coverage=coverage_state_from_assessments(
            hop_positions=[0, 1], supported=[0, 1], remaining_tool_calls=5
        ),
        proposal=_sufficient_proposal(),
    )
    assert envelope.verdict is not SufficiencyVerdict.SUFFICIENT
    assert GuardrailCode.NO_PROBE.value in envelope.guardrail_codes


def test_gaps_with_budget_left_continue() -> None:
    probe = build_probe(_path())
    envelope = _judge(SnapshotGraphEvaluator(CHAIN, snapshot_id="snap-1")).judge(
        episode_id="ep1",
        turn=1,
        probe=probe,
        coverage=coverage_state_from_assessments(
            hop_positions=[0, 1],
            supported=[],
            required_evidence_mode=RequiredEvidenceMode.HYBRID_PER_HOP,
            remaining_tool_calls=5,
        ),
    )
    assert envelope.verdict is SufficiencyVerdict.CONTINUE
    assert envelope.evidence_gaps


def test_gaps_without_budget_end_the_run() -> None:
    probe = build_probe(_path())
    envelope = _judge(SnapshotGraphEvaluator(CHAIN, snapshot_id="snap-1")).judge(
        episode_id="ep1",
        turn=1,
        probe=probe,
        coverage=coverage_state_from_assessments(
            hop_positions=[0, 1],
            supported=[],
            required_evidence_mode=RequiredEvidenceMode.HYBRID_PER_HOP,
            remaining_tool_calls=0,
        ),
    )
    assert envelope.verdict is SufficiencyVerdict.INSUFFICIENT_TERMINAL
    assert envelope.stop_reason == "no_valid_action"
    assert GuardrailCode.NO_BUDGET.value in envelope.guardrail_codes


def test_the_envelope_records_the_probe_and_its_digest() -> None:
    probe = build_probe(_path())
    envelope = _judge(SnapshotGraphEvaluator(CHAIN, snapshot_id="snap-1")).judge(
        episode_id="ep1",
        turn=1,
        probe=probe,
        coverage=coverage_state_from_assessments(
            hop_positions=[0, 1], supported=[0, 1], remaining_tool_calls=5
        ),
    )
    assert envelope.probe_id == probe.probe_id
    assert envelope.probe_digest == probe.dependency_digest
    assert envelope.probe_status == "ok"
    assert envelope.answer_ids == [MIGRAINE]


def test_the_model_proposal_is_recorded_but_not_decisive() -> None:
    probe = build_probe(_path())
    envelope = _judge(SnapshotGraphEvaluator(CHAIN, snapshot_id="snap-1")).judge(
        episode_id="ep1",
        turn=1,
        probe=probe,
        coverage=coverage_state_from_assessments(
            hop_positions=[0, 1], supported=[0, 1], remaining_tool_calls=5
        ),
        proposal=SufficiencyProposal(
            verdict="insufficient",
            evidence_gaps=["the model wants more"],
            rationale="not sure",
        ),
    )
    # The model asked for more, the facts said otherwise.
    assert envelope.verdict is SufficiencyVerdict.SUFFICIENT
    # But the model's gap is still recorded.
    assert "the model wants more" in envelope.evidence_gaps


def test_kg_only_mode_does_not_require_text_support() -> None:
    """Text support is only mandatory for the per-hop hybrid mode."""
    probe = build_probe(_path([("treats", HEADACHE), ("indication", MIGRAINE)]))
    evaluator = SnapshotGraphEvaluator(CHAIN, snapshot_id="snap-1")
    envelope = _judge(evaluator).judge(
        episode_id="ep1",
        turn=1,
        probe=probe,
        coverage=coverage_state_from_assessments(
            hop_positions=[0, 1],
            supported=[0],
            required_evidence_mode=RequiredEvidenceMode.KG_ONLY,
            remaining_tool_calls=5,
        ),
    )
    assert envelope.verdict is SufficiencyVerdict.SUFFICIENT
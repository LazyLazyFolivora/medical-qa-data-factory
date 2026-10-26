"""The logical query, the shortcut check and the blind verifier.

Step 7 of the real-generation-agent plan. The properties under test are that
a query cannot drift from the probe that justified it, that a question whose
answer is reachable in fewer hops is caught, and that a verifier never sees
the candidate answer.
"""
from __future__ import annotations

from typing import Any

import pytest

from medical_qa_data_factory.generation.blind_verification import (
    BlindCheckCode,
    BlindVerdict,
    DeterministicBlindVerifier,
    VerificationRequest,
    build_verification_request,
)
from medical_qa_data_factory.generation.composition import (
    CompositionVerdict,
    RelationCompositionPolicy,
)
from medical_qa_data_factory.generation.contracts import (
    EvidencePath,
    EvidencePathStep,
    RelationSemantics,
)
from medical_qa_data_factory.generation.logical_query import (
    Aggregation,
    LogicalDirection,
    LogicalQueryError,
    SnapshotQueryEvaluator,
    promote_probe_to_query,
)
from medical_qa_data_factory.generation.probe import build_probe
from medical_qa_data_factory.generation.shortcuts import (
    ShortcutKind,
    check_shortcuts,
)
from medical_qa_data_factory.domain.common import utc_now


ASPIRIN = "drug:aspirin"
HEADACHE = "disease:headache"
MIGRAINE = "disease:migraine"
PAIN = "symptom:pain"


class _Entity:
    def __init__(self, canonical_id: str, entity_type: str | None = None):
        self.canonical_id = canonical_id
        self.entity_type = entity_type


class _Edge:
    def __init__(
        self,
        head: str,
        relation: str,
        tail: str,
        head_type: str | None = None,
        tail_type: str | None = None,
    ):
        self.head = _Entity(head, head_type)
        self.relation = relation
        self.tail = _Entity(tail, tail_type)


CHAIN = [
    _Edge(ASPIRIN, "treats", HEADACHE, "drug", "disease"),
    _Edge(HEADACHE, "indication", MIGRAINE, "disease", "disease"),
    _Edge(MIGRAINE, "symptom", PAIN, "disease", "symptom"),
]


def _path(relations: list[tuple[str, str]]) -> EvidencePath:
    steps = []
    for index, (relation, target) in enumerate(relations):
        source = ASPIRIN if index == 0 else relations[index - 1][1]
        steps.append(
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
        end_entity_id=relations[-1][1],
        steps=steps,
        intermediate_entity_ids=[item[1] for item in relations[:-1]],
        source_evidence_ids=[f"kg.e{index}" for index in range(len(relations))],
        path_hash="hash",
        query_trace_ids=[f"q{index}" for index in range(len(relations))],
    )


TWO_HOPS = [("treats", HEADACHE), ("indication", MIGRAINE)]
THREE_HOPS = TWO_HOPS + [("symptom", PAIN)]


# ---- the logical query ---------------------------------------------------


def test_a_probe_is_promoted_into_a_typed_query() -> None:
    probe = build_probe(_path(TWO_HOPS))
    query = promote_probe_to_query(probe, query_id="query.1")
    assert len(query.steps) == 2
    assert query.steps[0].relation == "treats"
    assert query.steps[0].direction is LogicalDirection.FORWARD
    assert query.start_entity_id == ASPIRIN
    assert query.answer_variable == "answer"


def test_the_query_keeps_the_probe_digest() -> None:
    """The query can always be traced back to the probe that justified it."""
    probe = build_probe(_path(TWO_HOPS))
    query = promote_probe_to_query(probe, query_id="query.1")
    assert query.probe_digest == probe.dependency_digest
    # The query's own digest covers its own content, which may constrain the
    # answer further than the probe did.
    assert query.dependency_digest == query.digest()
    assert query.dependency_digest != probe.dependency_digest


def test_the_query_digest_is_stable() -> None:
    probe = build_probe(_path(TWO_HOPS))
    first = promote_probe_to_query(probe, query_id="query.1")
    second = promote_probe_to_query(probe, query_id="query.1")
    assert first.digest() == second.digest()


def test_a_query_with_a_broken_probe_is_refused() -> None:
    probe = build_probe(_path(TWO_HOPS))
    broken = probe.model_copy(update={"dependency_digest": "wrong"})
    with pytest.raises(LogicalQueryError):
        promote_probe_to_query(broken, query_id="query.1")


def test_the_query_steps_must_be_contiguous() -> None:
    """A query with a gap in its positions is not a query."""
    from medical_qa_data_factory.generation.logical_query import LogicalQuery

    probe = build_probe(_path(TWO_HOPS))
    query = promote_probe_to_query(probe, query_id="query.1")
    payload = query.model_dump()
    payload["steps"][0]["position"] = 3
    with pytest.raises(ValueError):
        LogicalQuery.model_validate(payload)


# ---- the query evaluator -------------------------------------------------


def test_the_query_resolves_to_the_reachable_answer() -> None:
    probe = build_probe(_path(THREE_HOPS))
    query = promote_probe_to_query(probe, query_id="query.1")
    evaluator = SnapshotQueryEvaluator(CHAIN, snapshot_id="snap-1")
    answer = evaluator.answer(query)
    assert answer.status == "ok"
    assert answer.answer_ids == [PAIN]


def test_the_query_evaluates_against_the_whole_snapshot() -> None:
    probe = build_probe(_path(TWO_HOPS))
    query = promote_probe_to_query(probe, query_id="query.1")
    # The snapshot also has a second indication edge the path did not use.
    edges = [*CHAIN, _Edge(HEADACHE, "indication", "disease:other")]
    evaluator = SnapshotQueryEvaluator(edges, snapshot_id="snap-1")
    answer = evaluator.answer(query)
    assert sorted(answer.answer_ids) == sorted([MIGRAINE, "disease:other"])


def test_a_query_against_another_snapshot_is_incomplete() -> None:
    probe = build_probe(_path(TWO_HOPS))
    query = promote_probe_to_query(probe, query_id="query.1")
    evaluator = SnapshotQueryEvaluator(CHAIN, snapshot_id="snap-2")
    answer = evaluator.answer(query)
    assert answer.status == "incomplete"


def test_a_count_aggregation_produces_a_number() -> None:
    probe = build_probe(_path(TWO_HOPS))
    query = promote_probe_to_query(
        probe, query_id="query.1", aggregation=Aggregation.COUNT
    )
    evaluator = SnapshotQueryEvaluator(CHAIN, snapshot_id="snap-1")
    answer = evaluator.answer(query)
    assert answer.aggregate_value == 1


# ---- shortcuts -----------------------------------------------------------


def test_a_three_hop_chain_with_no_shortcut_is_clean() -> None:
    probe = build_probe(_path(THREE_HOPS))
    query = promote_probe_to_query(probe, query_id="query.1")
    evaluator = SnapshotQueryEvaluator(CHAIN, snapshot_id="snap-1")
    report = check_shortcuts(query, edges=CHAIN, evaluator=evaluator)
    assert not report.has_shortcut
    assert report.shortest_hop_count == 3


def test_a_shorter_path_is_detected() -> None:
    """The answer is reachable in one hop, so the three-hop chain is a fake."""
    edges = [
        _Edge(ASPIRIN, "treats", HEADACHE),
        _Edge(HEADACHE, "indication", MIGRAINE),
        _Edge(MIGRAINE, "symptom", PAIN),
        # A direct one-hop edge to the same answer.
        _Edge(ASPIRIN, "symptom", PAIN),
    ]
    probe = build_probe(_path(THREE_HOPS))
    query = promote_probe_to_query(probe, query_id="query.1")
    evaluator = SnapshotQueryEvaluator(edges, snapshot_id="snap-1")
    report = check_shortcuts(query, edges=edges, evaluator=evaluator)
    assert report.has_shortcut
    assert any(
        item.kind is ShortcutKind.SHORTER_PATH for item in report.findings
    )
    assert report.shortest_hop_count == 1


def test_a_redundant_hop_is_detected_by_leaving_it_out() -> None:
    """The first hop already reaches the answer, so the second is redundant."""
    edges = [
        _Edge(ASPIRIN, "treats", HEADACHE),
        _Edge(ASPIRIN, "indication", MIGRAINE),
        _Edge(HEADACHE, "indication", MIGRAINE),
    ]
    probe = build_probe(_path(TWO_HOPS))
    query = promote_probe_to_query(probe, query_id="query.1")
    evaluator = SnapshotQueryEvaluator(edges, snapshot_id="snap-1")
    answer = evaluator.answer(query)
    assert answer.answer_ids == [MIGRAINE]
    report = check_shortcuts(
        query, edges=edges, evaluator=evaluator, answer=answer
    )
    # Dropping the first hop still reaches migraine, so the chain is longer
    # than it needs to be.
    assert report.has_shortcut
    assert any(
        item.kind is ShortcutKind.LEAVE_ONE_HOP_OUT for item in report.findings
    )


def test_a_query_without_an_answer_has_no_shortcut() -> None:
    probe = build_probe(_path([("treats", "disease:absent"), ("indication", MIGRAINE)]))
    query = promote_probe_to_query(probe, query_id="query.1")
    evaluator = SnapshotQueryEvaluator([], snapshot_id="snap-1")
    report = check_shortcuts(query, edges=[], evaluator=evaluator)
    assert not report.has_shortcut


# ---- the composition policy ---------------------------------------------


def test_a_single_relation_is_allowed() -> None:
    policy = RelationCompositionPolicy()
    assert policy.verdict(["treats"]) is CompositionVerdict.ALLOW


def test_a_directional_chain_neutral_wording() -> None:
    policy = RelationCompositionPolicy()
    verdict = policy.verdict(["treats", "protein_protein"])
    assert verdict is CompositionVerdict.NEUTRALIZE
    assert policy.requires_neutral_wording(["treats", "protein_protein"])


def test_an_unknown_chain_is_neutralized_not_escalated() -> None:
    policy = RelationCompositionPolicy()
    verdict = policy.verdict(["some_unknown_relation", "another_unknown"])
    assert verdict is CompositionVerdict.NEUTRALIZE


def test_an_empty_chain_asserts_nothing() -> None:
    policy = RelationCompositionPolicy()
    assert policy.verdict([]) is CompositionVerdict.NEUTRALIZE
    assert "empty" in policy.rationale([])


def test_the_policy_explains_its_verdict() -> None:
    policy = RelationCompositionPolicy()
    rationale = policy.rationale(["treats", "protein_protein"])
    assert "treats" in rationale
    assert "protein_protein" in rationale


# ---- the blind verifier ---------------------------------------------------


def _query() -> LogicalQuery:
    probe = build_probe(_path(TWO_HOPS))
    return promote_probe_to_query(probe, query_id="query.1")


def _answer(answer_ids: list[str]):
    from medical_qa_data_factory.generation.logical_query import QueryAnswer

    return QueryAnswer(
        query_id="query.1", answer_ids=answer_ids, evaluated_at=utc_now()
    )


def test_a_clean_question_is_accepted() -> None:
    request = build_verification_request(
        request_id="v1",
        question_text="Which disease is indicated for the condition treated by aspirin?",
        query=_query(),
        answer=_answer([MIGRAINE]),
        answer_names=["migraine"],
        intermediate_names=["headache"],
    )
    result = DeterministicBlindVerifier().verify(request)
    assert result.verdict is BlindVerdict.ACCEPTED
    assert result.reason_codes == []


def test_a_question_that_names_the_answer_is_rejected() -> None:
    request = build_verification_request(
        request_id="v1",
        question_text="Does aspirin treat migraine?",
        query=_query(),
        answer=_answer([MIGRAINE]),
        answer_names=["migraine"],
    )
    result = DeterministicBlindVerifier().verify(request)
    assert result.verdict is BlindVerdict.HARD_REJECTED
    assert BlindCheckCode.ANSWER_LEAKED.value in result.reason_codes
    assert result.blocking


def test_a_question_that_names_an_intermediate_entity_is_flagged() -> None:
    request = build_verification_request(
        request_id="v1",
        question_text="Which disease is indicated for the condition that treats headache?",
        query=_query(),
        answer=_answer([MIGRAINE]),
        answer_names=["migraine"],
        intermediate_names=["headache"],
    )
    result = DeterministicBlindVerifier().verify(request)
    assert BlindCheckCode.INTERMEDIATE_ENTITY_LEAKED.value in result.reason_codes


def test_an_ambiguous_answer_is_rejected() -> None:
    query = _query()
    ambiguous = query.model_copy(update={"expected_cardinality": "one"})
    request = VerificationRequest(
        request_id="v1",
        question_text="Which disease?",
        query=ambiguous,
        answer_ids=[MIGRAINE, "disease:other"],
        forbidden_names=[],
    )
    result = DeterministicBlindVerifier().verify(request)
    assert BlindCheckCode.AMBIGUOUS_ANSWER.value in result.reason_codes
    assert result.verdict is BlindVerdict.HARD_REJECTED


def test_the_verification_request_carries_no_candidate_answer() -> None:
    """The verifier may see the answer set, never a proposed answer."""
    request = build_verification_request(
        request_id="v1",
        question_text="Which disease?",
        query=_query(),
        answer=_answer([MIGRAINE]),
        answer_names=["migraine"],
    )
    payload = request.model_dump(mode="json")
    assert "candidate_answer" not in payload
    assert "gold" not in payload
    # The authoritative answer set is there, because the check is about
    # cardinality and uniqueness, not about a proposed answer.
    assert payload["answer_ids"] == [MIGRAINE]
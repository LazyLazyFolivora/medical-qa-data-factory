"""The objective writer can change wording but never the frozen contract."""
from __future__ import annotations

import json

import pytest

from medical_qa_data_factory.generation.objective_question_contracts import (
    AnswerCardinality,
    FrozenQuestionContract,
    SubagentTask,
)
from medical_qa_data_factory.generation.objective_question_realization import (
    ObjectiveQuestionDraft,
    ObjectiveQuestionRealizationSubagent,
    QuestionSemanticReadback,
    objective_question_realization_capability,
)
from medical_qa_data_factory.generation.subagent_contracts import (
    RoleContractViolation,
    SubagentRole,
    build_objective_subagent_task,
)
from medical_qa_data_factory.llm.fake import FakeStructuredModel


def _contract() -> FrozenQuestionContract:
    return FrozenQuestionContract(
        contract_id="contract-brca1",
        question_intent="On which human chromosome is BRCA1 located?",
        public_scope="Homo sapiens reference genome",
        answer_type="entity",
        answer_values=["chromosome 17"],
        answer_cardinality=AnswerCardinality.EXACTLY_ONE,
        claim_strength="directional_fact",
        required_public_conditions=["Homo sapiens reference genome"],
        allowed_wording_constraints=[
            "preserve answer cardinality",
            "preserve relation strength",
        ],
        forbidden_semantic_upgrades=[
            "causal",
            "treatment",
            "diagnosis",
            "prognosis",
        ],
        resolution_id="resolution-brca1",
    )


def _task() -> SubagentTask:
    return build_objective_subagent_task(
        task_id="subtask-realize-brca1",
        role=SubagentRole.QUESTION_REALIZATION,
        capability=objective_question_realization_capability(),
        goal={"frozen_question_contract": _contract().model_dump(mode="json")},
        input_artifact_ids=["contract-brca1", "resolution-brca1"],
    )


def _draft(question_text: str = "On which human chromosome is BRCA1 located?") -> ObjectiveQuestionDraft:
    contract = _contract()
    return ObjectiveQuestionDraft(
        draft_id="question-brca1",
        contract_id=contract.contract_id,
        question_text=question_text,
        answer_values=contract.answer_values,
        concise_explanation=(
            "BRCA1 is located on human chromosome 17."
        ),
        semantic_readback=QuestionSemanticReadback(
            question_intent=contract.question_intent,
            public_scope=contract.public_scope,
            answer_type=contract.answer_type,
            answer_values=contract.answer_values,
            answer_cardinality=contract.answer_cardinality,
            claim_strength=contract.claim_strength,
            required_public_conditions=contract.required_public_conditions,
        ),
    )


def test_writer_task_cannot_see_candidate_claims_or_proposed_answers() -> None:
    with pytest.raises(RoleContractViolation, match="candidate_claims"):
        build_objective_subagent_task(
            task_id="leaky-writer",
            role=SubagentRole.QUESTION_REALIZATION,
            capability=objective_question_realization_capability(),
            goal={
                "frozen_question_contract": _contract().model_dump(mode="json"),
                "candidate_claims": ["claim-brca1"],
                "proposed_answer_values": ["chromosome 17"],
            },
            input_artifact_ids=["contract-brca1"],
        )


def test_writer_receives_only_the_frozen_contract() -> None:
    model = FakeStructuredModel(
        responses={"objective_question_realization": _draft()}
    )
    outcome = ObjectiveQuestionRealizationSubagent(model).run(
        _task(),
        contract=_contract(),
    )

    assert outcome.draft.question_text == _draft().question_text
    assert outcome.draft.draft_id == "draft.contract-brca1"
    payload = model.payloads_for("objective_question_realization")[0]
    assert payload["frozen_question_contract"]["contract_id"] == "contract-brca1"
    serialized = json.dumps(payload)
    assert "candidate_claims" not in serialized
    assert "discovery_anchor_ids" not in serialized
    assert "proposed_answer_values" not in serialized


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"answer_values": ["chromosome 13"]}, "answer_values"),
        (
            {
                "semantic_readback": QuestionSemanticReadback(
                    question_intent=(
                        "On which human chromosome is BRCA1 located?"
                    ),
                    public_scope="Homo sapiens reference genome",
                    answer_type="entity",
                    answer_values=["chromosome 17"],
                    answer_cardinality=AnswerCardinality.EXACTLY_ONE,
                    claim_strength="causal",
                    required_public_conditions=["Homo sapiens reference genome"],
                )
            },
            "claim_strength",
        ),
        (
            {
                "semantic_readback": QuestionSemanticReadback(
                    question_intent=(
                        "On which human chromosome is BRCA1 located?"
                    ),
                    public_scope="adults only",
                    answer_type="entity",
                    answer_values=["chromosome 17"],
                    answer_cardinality=AnswerCardinality.EXACTLY_ONE,
                    claim_strength="directional_fact",
                    required_public_conditions=["adults only"],
                )
            },
            "public_scope",
        ),
    ],
)
def test_writer_cannot_change_answer_scope_or_strength(
    override: dict[str, object],
    message: str,
) -> None:
    drifted = _draft().model_copy(update=override)
    model = FakeStructuredModel(
        responses={"objective_question_realization": drifted}
    )

    with pytest.raises(RoleContractViolation, match=message):
        ObjectiveQuestionRealizationSubagent(model).run(
            _task(),
            contract=_contract(),
        )


@pytest.mark.parametrize(
    ("question_text", "reason"),
    [
        (
            "According to PrimeKG, on which chromosome is BRCA1?",
            "SOURCE_RELATIVE_WORDING",
        ),
        (
            "Does current research support that BRCA1 is on chromosome 17?",
            "META_EVIDENCE_WORDING",
        ),
        (
            "Which chromosome does BRCA1 cause disease on?",
            "SEMANTIC_UPGRADE",
        ),
    ],
)
def test_writer_rejects_source_relative_meta_evidence_and_causal_wording(
    question_text: str,
    reason: str,
) -> None:
    model = FakeStructuredModel(
        responses={
            "objective_question_realization": _draft(question_text)
        }
    )

    with pytest.raises(RoleContractViolation, match=reason):
        ObjectiveQuestionRealizationSubagent(model).run(
            _task(),
            contract=_contract(),
        )

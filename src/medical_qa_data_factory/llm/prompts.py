"""Prompt templates, versioned per role.

Every role has its own template and its own version. The writer and the
verifier must never share a template, because the writer must not see the
verifier's reasoning and the verifier must never receive the writer's rationale
as a source of fact.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class PromptTemplate:
    """One role's template and its version."""

    role: str
    version: str
    body: str

    def hash(self) -> str:
        return hashlib.sha256(
            f"{self.role}:{self.version}:{self.body}".encode("utf-8")
        ).hexdigest()


#: The templates. Each one states the role's single job and what it must not
#: do. They contain no facts about the data: the payload carries those.
TEMPLATES: dict[str, PromptTemplate] = {
    "exploration_planner": PromptTemplate(
        role="exploration_planner",
        version="explorer-v2",
        body=(
            "You choose the single next tool action for a knowledge-graph or "
            "literature search. You may not invent entity ids, observation ids "
            "or budgets. You state what the action should reveal and which "
            "evidence gap it addresses. For every neighbour or path request, "
            "read max_results_per_query and remaining_edge_budget from the "
            "payload and set max_results or max_paths no higher than either "
            "number. Never request all edges. When recent_policy_rejections is "
            "non-empty, correct the rejected parameters instead of repeating "
            "the same oversized request."
        ),
    ),
    "evidence_curator": PromptTemplate(
        role="evidence_curator",
        version="curator-v1",
        body=(
            "You judge whether the supplied evidence supports, contradicts or "
            "merely contextualises a claim. You may not introduce facts that "
            "are not in the supplied evidence."
        ),
    ),
    "sufficiency_judge": PromptTemplate(
        role="sufficiency_judge",
        version="sufficiency-v1",
        body=(
            "You judge whether the current evidence can carry a question. You "
            "must name the specific remaining gap. You may not answer the "
            "question yourself."
        ),
    ),
    "claim_builder": PromptTemplate(
        role="claim_builder",
        version="claim-v1",
        body=(
            "You turn supplied evidence into candidate claims. Every claim must "
            "name the evidence that supports it. You may not add a claim that "
            "the supplied evidence does not support."
        ),
    ),
    "question_planner": PromptTemplate(
        role="question_planner",
        version="planner-v1",
        body=(
            "You design the structure of a question: its theme, answer form, "
            "reasoning structure and the evidence it requires. You may not "
            "choose an answer the evidence does not determine."
        ),
    ),
    "question_writer": PromptTemplate(
        role="question_writer",
        version="writer-v1",
        body=(
            "You write the question text from an approved structure. You may "
            "not change the answer, the required evidence, the entity set or "
            "the question's logical structure, and you may not state the "
            "answer or a necessary intermediate entity in the question."
        ),
    ),
    "style_judge": PromptTemplate(
        role="style_judge",
        version="style-v1",
        body=(
            "You evaluate whether a question sounds like the requested real-user "
            "style and still preserves the supplied intent constraints. You may "
            "not infer, mention or reward knowledge of the candidate answer. "
            "Reject unsupported medical details, answer leakage, technical "
            "implementation jargon and ambiguity."
        ),
    ),
    "answer_verifier": PromptTemplate(
        role="answer_verifier",
        version="verifier-v1",
        body=(
            "You check a written question against its structure and the "
            "supplied evidence, without being shown any candidate answer. You "
            "report whether the question is entailed, ambiguous or leaking. "
            "You may not suggest what the answer is. For an evidence-enriched "
            "question, verify that the wording preserves the declared question "
            "intent and that the frozen evidence supports the same unique "
            "answer entity under the existing answer contract."
        ),
    ),
}


#: Pairs that must never share a template version.
SEPARATED_ROLE_PAIRS: tuple[tuple[str, str], ...] = (
    ("question_writer", "answer_verifier"),
)


def template_for(role: str) -> PromptTemplate:
    try:
        return TEMPLATES[role]
    except KeyError as exc:
        raise KeyError(
            f"no prompt template for role {role!r}; known roles: "
            f"{sorted(TEMPLATES)}"
        ) from exc


def prompt_hashes() -> dict[str, str]:
    """Every role's template hash, for the run manifest."""
    return {role: item.hash() for role, item in TEMPLATES.items()}


def assert_roles_separated() -> None:
    """Fail if two roles that must stay apart share a template."""
    for left, right in SEPARATED_ROLE_PAIRS:
        first = template_for(left)
        second = template_for(right)
        if first.hash() == second.hash():
            raise ValueError(
                f"roles {left!r} and {right!r} must not share a prompt template"
            )

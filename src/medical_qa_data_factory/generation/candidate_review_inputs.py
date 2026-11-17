"""The exact inputs shared by candidate review and controller availability."""
import hashlib
import json

from ..llm.objective_skills import load_objective_skill
from ..llm.prompts import TEMPLATES
from .question_reader import QUESTION_REVIEW_VERSION
from .source_question_inputs import candidate_research_context


def candidate_review_inputs(snapshot, hypothesis_id, model):
    hypothesis = snapshot.artifacts[hypothesis_id].payload
    claim_ids = set(hypothesis["candidate_claim_ids"])
    claims = [a.payload for a in snapshot.artifacts.values() if a.artifact_id in claim_ids]
    anchor_ids = {aid for c in claims for aid in c["supporting_anchor_ids"]}
    anchor_ids.update(hypothesis.get("discovery_anchor_ids", []))
    originals = [a.payload for a in snapshot.artifacts.values() if a.artifact_id in anchor_ids]
    saved_context = candidate_research_context(snapshot, hypothesis_id) or {}
    source_context = {"source_passages": saved_context.get("source_passages", []),
                      "section_context": saved_context.get("section_context", [])}
    skill = load_objective_skill("objective_candidate_review", {"stage": "review_only"})
    config = getattr(model, "config", None)
    inputs = {"hypothesis": hypothesis, "claims": claims, "originals": originals,
              "source_context": source_context}
    digest = hashlib.sha256(json.dumps({
        **inputs, "goal": snapshot.goal.model_dump(mode="json"),
        "skill_version": skill[1] if skill else None,
        "experimental_skill_version": (model.skill_version_for("objective_candidate_review")
            if callable(getattr(model, "skill_version_for", None)) else None),
        "candidate_review_mode": getattr(model, "candidate_review_mode", "reader_only"),
        "reader_version": QUESTION_REVIEW_VERSION,
        "review_prompt": TEMPLATES["objective_candidate_review"].hash(),
        "model_config": config.config_hash() if config is not None else type(model).__qualname__,
        "contract_version": "independent-review-v1",
    }, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return {**inputs, "review_input_hash": digest}

"""Deterministic, role-scoped instruction bundles for objective generation."""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[3] / "skills"
ROLE_SKILLS = {
    "objective_research_agent": ("objective-research-agent", ()),
    "objective_question_discovery": ("objective-research-question-discovery", ()),
    "objective_evidence_synthesis": ("objective-evidence-synthesis", ()),
    "objective_difficulty_assessment": ("objective-difficulty-assessment", ()),
    "objective_react_agent": ("objective-react-agent", ()),
    "objective_workflow_planner": ("objective-workflow-planner", ()),
    "objective_candidate_review": ("objective-candidate-review", ()),
    "objective_main_agent": ("objective-main-agent", ()),
    "material_retrieval": (
        "objective-material-retrieval",
        ("entity-directions.md", "search-strategies.md"),
    ),
    "fact_understanding": ("objective-fact-understanding", ("extraction-examples.md",)),
    "objective_question_design": ("objective-question-design", ()),
    "objective_answer_verification": (
        "objective-answer-verification", ("verification-cases.md",),
    ),
    "objective_question_realization": (
        "objective-question-realization", ("writing-examples.md",),
    ),
}

ROLE_COMPANION_SKILLS = {}


def load_objective_skill(role: str, payload: dict, *, root: Path = SKILL_ROOT):
    """Return a bundle, or None for roles with their own instruction lifecycle.

    Release review retains its existing loader and audited skill identity.
    Paths are selected only by this allowlist, never by material contents.
    """
    specification = ROLE_SKILLS.get(role)
    if specification is None:
        return None
    skill_id, references = specification
    if role == "objective_question_design":
        from ..generation.question_strategies import question_strategy
        strategy = question_strategy(payload.get("task", {}))
        targets = [strategy.name] if strategy.name != "free" else ["easy", "medium", "hard"]
        if any(level not in {"easy", "medium", "hard"} for level in targets):
            raise ValueError("unsupported objective question difficulty targets")
        selected = [level for level in ("easy", "medium", "hard") if level in targets]
        if targets and not selected:
            raise ValueError("unsupported objective question difficulty targets")
        references = ("fact-selection.md", *(
            f"{level}.md" for level in (selected or ["easy", "medium", "hard"])
        ))
    if role == "objective_answer_verification" and payload.get("stage") in {"final_blind", "final_review"}:
        skill_id, references = "objective-final-verification", ()
    folder = root / skill_id
    paths = [Path("SKILL.md"), *(Path("references") / name for name in references)]
    parts = []
    for relative in paths:
        content = (folder / relative).read_text(encoding="utf-8")
        if not content.strip():
            raise ValueError(f"empty skill resource: {skill_id}/{relative}")
        parts.append(f"Resource: {relative.as_posix()}\n{content}")
    for companion in ROLE_COMPANION_SKILLS.get(role, ()):
        content = (root / companion / "SKILL.md").read_text(encoding="utf-8")
        if not content.strip():
            raise ValueError(f"empty companion skill: {companion}")
        parts.append(f"Companion skill: {companion}/SKILL.md\n{content}")
    instruction = "\n\n".join(parts)
    stage = str(payload.get("stage", "default"))
    instruction += (
        f"\n\nRuntime stage: {stage}. Follow only this stage's assigned scope. "
        "The caller's response schema controls output. Repository migration notes "
        "describe limitations, not permission to modify code or invent fields. "
        "Do not claim checks or tool operations absent from the supplied evidence."
    )
    digest = sha256(instruction.encode("utf-8")).hexdigest()
    return skill_id, f"sha256:{digest}", instruction

import pytest

from medical_qa_data_factory.llm.contracts import StructuredGenerationRequest
from medical_qa_data_factory.llm.objective_skills import (
    ROLE_SKILLS, SKILL_ROOT, load_objective_skill,
)


def request(role="objective_question_design", payload=None, **kwargs):
    return StructuredGenerationRequest(
        invocation_id="test", role=role, system_prompt_version="test-v1",
        payload=payload or {}, idempotency_key="operation", **kwargs,
    )


@pytest.mark.parametrize("role", ROLE_SKILLS)
def test_objective_roles_load_their_own_instructions(role):
    result = request(role)
    assert result.skill_id == ROLE_SKILLS[role][0]
    assert result.skill_instruction
    assert result.skill_version in result.idempotency_key


def test_difficulty_selects_only_needed_method():
    result = request(payload={"task": {"difficulty_targets": ["hard"]}})
    assert "Resource: references/hard.md" in result.skill_instruction
    assert "Resource: references/easy.md" not in result.skill_instruction
    assert "Resource: references/fact-selection.md" in result.skill_instruction


def test_production_design_and_writer_load_natural_question_method():
    design = request(payload={"task": {"difficulty_targets": ["easy"]}})
    assert "question_intent 必须直接是候选题的自然问句" in design.skill_instruction
    assert "信息增量" in design.skill_instruction
    assert "RGS20 位于人类哪条染色体的哪个区带？" in design.skill_instruction
    writer = request("objective_question_realization")
    assert "RGS20 属于哪个蛋白家族？" in writer.skill_instruction
    assert "不追加" in writer.skill_instruction


def test_explicit_release_skill_and_other_roles_are_preserved():
    result = request("objective_release_review", skill_id="review", skill_version="v1",
                     skill_instruction="public-only")
    assert result.skill_instruction == "public-only"
    assert result.idempotency_key == "operation"
    assert request("question_writer").skill_id is None


def test_missing_resource_fails_instead_of_silent_fallback(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_objective_skill("fact_understanding", {}, root=tmp_path)


def test_reference_changes_invalidate_bundle_version(tmp_path):
    import shutil
    shutil.copytree(SKILL_ROOT / "objective-question-design", tmp_path / "objective-question-design")
    payload = {"task": {"difficulty_targets": ["hard"]}}
    before = load_objective_skill("objective_question_design", payload, root=tmp_path)
    reference = tmp_path / "objective-question-design/references/hard.md"
    reference.write_text(reference.read_text(encoding="utf-8") + "\nRevision", encoding="utf-8")
    after = load_objective_skill("objective_question_design", payload, root=tmp_path)
    assert before[1] != after[1]


def test_blind_payload_is_not_enriched_with_candidate_answers():
    payload = {"stage": "blind_solve", "blind_answer_query": {"question_intent": "test"}}
    result = request("objective_answer_verification", payload)
    assert result.payload == payload
    assert "proposed_answer_values" not in result.payload

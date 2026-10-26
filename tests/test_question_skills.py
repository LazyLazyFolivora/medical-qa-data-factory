from pathlib import Path

import pytest

from medical_qa_data_factory.generation.question_skills import (
    available_question_skills,
    load_question_skill,
    load_question_skills,
    profile_plan,
)


def test_all_default_question_style_skills_load() -> None:
    skills = available_question_skills()
    assert {item.skill_id for item in skills} == {
        "layperson-search-v1",
        "patient-caregiver-v1",
        "medical-learner-v1",
    }
    assert all(item.prompt and item.prompt_hash() for item in skills)
    assert all(item.examples for item in skills)
    assert all(item.hard_negatives for item in skills)


def test_requested_skills_preserve_order() -> None:
    skills = load_question_skills(
        ["medical-learner-v1", "layperson-search-v1"]
    )
    assert [item.skill_id for item in skills] == [
        "medical-learner-v1",
        "layperson-search-v1",
    ]


def test_unknown_or_traversing_skill_id_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        load_question_skill("../models")
    with pytest.raises(ValueError):
        load_question_skill("absent", root=tmp_path)


def test_profile_plan_is_stable_and_distinct() -> None:
    skill = load_question_skill("layperson-search-v1")
    profiles = profile_plan(skill, 3)
    assert profiles == skill.manifest.style_profiles[:3]
    assert len(set(profiles)) == 3

"""Prompt templates are per role, versioned, and never shared when they must not be."""
from __future__ import annotations

import pytest

from medical_qa_data_factory.llm.contracts import SEPARATED_ROLE_PAIRS
from medical_qa_data_factory.llm.prompts import (
    TEMPLATES,
    assert_roles_separated,
    prompt_hashes,
    template_for,
)


def test_every_model_role_has_a_template() -> None:
    from medical_qa_data_factory.llm.contracts import ModelRole
    from typing import get_args

    for role in get_args(ModelRole):
        assert role in TEMPLATES


def test_the_writer_and_the_verifier_do_not_share_a_template() -> None:
    assert_roles_separated()
    writer = template_for("question_writer")
    verifier = template_for("answer_verifier")
    assert writer.hash() != verifier.hash()
    assert writer.version != verifier.version


def test_every_template_has_its_own_hash() -> None:
    hashes = list(prompt_hashes().values())
    assert len(hashes) == len(set(hashes))


def test_the_prompt_hashes_cover_every_role() -> None:
    hashes = prompt_hashes()
    assert set(hashes) == set(TEMPLATES)


def test_an_unknown_role_is_rejected() -> None:
    with pytest.raises(KeyError):
        template_for("not_a_role")


def test_the_verifier_template_forbids_suggesting_an_answer() -> None:
    body = template_for("answer_verifier").body.lower()
    assert "may not suggest what the answer is" in body


def test_the_writer_template_forbids_stating_the_answer() -> None:
    body = template_for("question_writer").body.lower()
    assert "may not state the" in body
    assert "answer" in body


def test_the_separated_role_pairs_are_declared() -> None:
    assert ("question_writer", "answer_verifier") in SEPARATED_ROLE_PAIRS
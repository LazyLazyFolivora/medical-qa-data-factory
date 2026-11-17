from types import SimpleNamespace

import pytest

from medical_qa_data_factory.generation import agent_loop, research_agent
from medical_qa_data_factory.generation.research_agent import _bounded_recent_read_sections


@pytest.mark.parametrize(
    "has_candidate,selected,expected,selection",
    [
        (False, {}, {"paper": ["p1", "p2"]}, "recent_read_fallback"),
        (False, {"paper": ["p1"]}, {"paper": ["p1"]}, "explicit"),
        (False, {"paper": ["unread"], "unknown": ["p1"]}, {"paper": ["p1", "p2"]}, "recent_read_fallback"),
        (True, {}, {}, "none"),
    ],
)
def test_returned_research_hands_off_read_originals_if_exploration_selection_is_empty(
    monkeypatch, has_candidate, selected, expected, selection
):
    params = {"information_need": "Explore the measured outcome"}
    if has_candidate:
        params["hypothesis_id"] = "hypothesis.1"
    session = SimpleNamespace(
        decision=SimpleNamespace(decision_id="decision.1", action_parameters=params),
        data={"turn": {"source_sections": selected}, "history": [], "artifacts": {}},
        read_views=lambda: [{"material_id": "paper", "returned_paragraph_ids": ["p1", "p2"]}],
        load=lambda: None,
        path="checkpoint.json",
    )

    def passages(_session, _snapshot, _material_id, ids, **_kwargs):
        return {"content": "\n\n".join(ids), "excerpts": [{"paragraph_id": pid, "text": pid} for pid in ids]}

    monkeypatch.setattr(research_agent, "material_passages", passages)
    monkeypatch.setattr(agent_loop, "_artifact", lambda payload, *_args: payload)
    report = research_agent.ResearchSession.report(session, "returned")

    assert report["source_sections"] == expected
    assert report["source_selection"] == selection
    assert ("source_selection_reason" in report) == ("unknown" in selected)


def test_exhausted_research_hands_off_recent_read_paragraphs_within_budget():
    reads = [
        {"material_id": "old-paper", "returned_paragraph_ids": ["p1", "p2"]},
        {"material_id": "recent-paper", "returned_paragraph_ids": ["p3", "p3", "p4"]},
    ]
    sizes = {
        ("old-paper", "p1"): 4,
        ("old-paper", "p2"): 4,
        ("recent-paper", "p3"): 6,
        ("recent-paper", "p4"): 5,
    }

    selected = _bounded_recent_read_sections(
        reads, lambda material_id, paragraph_id: sizes[(material_id, paragraph_id)], max_chars=10
    )

    assert selected == {
        "old-paper": ["p1"],
        "recent-paper": ["p3"],
    }


def test_exhausted_research_limits_handoff_to_six_recent_sources():
    reads = [
        {"material_id": f"paper-{index}", "returned_paragraph_ids": ["p1"]}
        for index in range(8)
    ]

    selected = _bounded_recent_read_sections(reads, lambda _material_id, _paragraph_id: 1)

    assert list(selected) == [f"paper-{index}" for index in range(2, 8)]


def test_exhausted_research_budgets_the_blank_line_between_passages():
    reads = [
        {"material_id": "older-paper", "returned_paragraph_ids": ["summary"]},
        {"material_id": "newer-paper", "returned_paragraph_ids": ["methods", "results"]},
    ]

    selected = _bounded_recent_read_sections(
        reads, lambda _material_id, _paragraph_id: 5, max_chars=10
    )

    assert selected == {"older-paper": ["summary"], "newer-paper": ["methods"]}


def test_exhausted_research_combines_multiple_reads_of_the_same_material():
    reads = [
        {"material_id": "long-paper", "returned_paragraph_ids": ["methods", "result-1"]},
        {"material_id": "other-paper", "returned_paragraph_ids": ["abstract"]},
        {"material_id": "long-paper", "returned_paragraph_ids": ["result-1", "discussion"]},
    ]

    selected = _bounded_recent_read_sections(reads, lambda _material_id, _paragraph_id: 1)

    assert selected == {
        "other-paper": ["abstract"],
        "long-paper": ["result-1", "discussion", "methods"],
    }

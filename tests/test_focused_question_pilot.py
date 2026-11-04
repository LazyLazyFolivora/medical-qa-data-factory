"""The writer receives the chosen source, not the previous batch context."""
from medical_qa_data_factory.focused_question_pilot import (
    Assignment, literature_cards, writing_payload,
)


def test_handoff_keeps_original_selected_source_and_excludes_other_opportunities():
    selected = {"pmid": "1", "abstract": "Original relation and conditions."}
    unrelated = {"pmid": "2", "abstract": "Unrelated subject."}
    materials = {
        "exploration_results": [{"material": {"fetch": {"records": [selected, unrelated]}}}],
        "opportunities": {"previous_guess": "Unsupported mechanism"},
        "graph": {"neighbors": ["unrelated graph edge"]},
    }
    assignment = Assignment(
        information_need="Identify partner and function", source_pmid="1",
        relation={"fact": "relation", "source": "1", "quote": "Original relation"},
        lookup_entity="R", lookup_property="function", connection_value="relevant function",
        question_outline="Which partner?",
    )
    lookup = {"source_url": "https://example.test/R", "record": {"function": "observed"}}
    payload = writing_payload(assignment, literature_cards(materials), lookup)
    assert payload["relation_source"] == selected
    assert payload["targeted_lookup"] == lookup
    assert set(payload) == {"assignment", "relation_source", "targeted_lookup"}
    assert len(materials["exploration_results"][0]["material"]["fetch"]["records"]) == 2

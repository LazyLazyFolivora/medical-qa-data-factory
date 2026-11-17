from types import SimpleNamespace

import pytest

from medical_qa_data_factory.generation import question_discovery
from medical_qa_data_factory.generation.objective_question_contracts import (
    EvidenceMaterial, MainAgentDecision, text_digest,
)
from medical_qa_data_factory.generation.objective_question_persistence import (
    ObjectiveQuestionPersistence, immutable_blob_ref,
)
from medical_qa_data_factory.generation.objective_question_runtime import ArtifactEnvelope
from medical_qa_data_factory.generation.react_main_agent import (
    _discovery_handoffs, _unused_discovery_opportunities, candidate_choice_model, validate_react_decision,
)
from medical_qa_data_factory.generation.react_tools import execute_react_tool
from medical_qa_data_factory.generation.research_tools import design_research_context, material_passages
from medical_qa_data_factory.generation.subagent_contracts import RoleContractViolation
from medical_qa_data_factory.llm.contracts import StructuredGenerationRequest
from medical_qa_data_factory.llm.fake import FakeStructuredModel


REPORT_ID = "research.fixture"
SOURCES = {"material.paper-a": ["p1", "p0"], "material.paper-b": ["p0"]}


def _decision(ids, parameters=None):
    return MainAgentDecision(
        decision_id="decision.fixture", action="discover_questions", input_artifact_ids=ids,
        state_digest="a" * 64, expected_information_gain=0.5, action_parameters=parameters or {},
    )


def _observation(artifact_id, payload):
    return ArtifactEnvelope(artifact_id=artifact_id, artifact_type="react_observation", payload=payload)


def test_alternative_uses_original_offsets_and_earns_its_own_acceptance():
    question = "Which effect was observed in adults with the condition?"
    quote = "Adults with the condition responded."
    original = {"material_id": "selected", "excerpts": [{"offset": 100, "text": quote}]}
    anchor = SimpleNamespace(material_id="selected", start_offset=100)
    alternative = {"question": question, "information_need": "Find the observed effect",
                   "quote": quote, "start_in_anchor": 0, "end_in_anchor": len(quote)}
    def phrase_alternative(request):
        assert request.payload["unresolved_relationships"] == ["Mechanism not established"]
        assert [item["span_id"] for item in request.payload["observations"]] == [2]
        return {"alternative": {"span_id": 2, "question": question, "information_need": "Find the observed effect"}}

    model = FakeStructuredModel(responses={
        "objective_question_discovery": phrase_alternative,
        "objective_question_form_reader": {"judgments": [{"index": 0, "task_in_question": "Find the effect",
            "missing_required_context": [], "task_kind": "domain_knowledge", "verdict": "ACCEPT", "reason": "Standalone"}]},
        "objective_question_reader": {"judgments": [{"index": 0, "premises": [], "verdict": "ACCEPT", "reason": "Supported"}]},
    })
    kwargs = {"anchor": anchor, "research_context": {"source_passages": [original], "research_id": "prior"},
              "goal": {}, "operation_id": "alternative"}
    reading = {"missing_relationships": ["Mechanism not established"], "observations": [
        {"span_id": 2, "relevance": "direct", **alternative},
        {"span_id": 3, "relevance": "direct", "relationship": "proposed_explanation", **alternative},
    ]}
    selected, _ = question_discovery.formulate_alternative_question(
        model, reading=reading, original_question="How does it work?", goal={}, operation_id="alternative",
    )
    result, _ = question_discovery.review_alternative_question(model, alternative=selected, **kwargs)
    assert result["approved_question"] == question
    assert result["source_passages"] == [original]
    assert result["parent_research_id"] == "prior"
    assert result["opportunity"]["primary_evidence"]["start"] == 100
    invalid, _ = question_discovery.review_alternative_question(model, alternative={**alternative, "quote": "Invented"}, **kwargs)
    assert invalid is None
    assert len(model.invocations) == 3


def test_discovery_reader_checks_the_sources_that_will_reach_answer_design():
    original = "How does the intervention affect patients?"
    revised = "How does the intervention affect adults with the condition?"

    def plan(request):
        assert "focus" not in request.payload and "research_notebook" not in request.payload
        assert request.payload["materials"][0]["excerpts"][0]["sentences"] == [
            {"span_id": 1, "text": "Adults with the condition responded."},
        ]
        return {"opportunities": [{
            "source_ids": [], "supported_observations": ["Adults with the condition responded."],
            "primary_span_id": 1,
            "information_need": "Effect of the intervention", "value": "Find its effect",
            "evidence_scope": {"population_or_genotype": "adults with the condition", "setting": None,
                               "comparator": None, "time_period": None},
            "readiness": "ready_for_design", "next_information_target": "",
        }]}

    def discover(request):
        assert "focus" not in request.payload and "research_notebook" not in request.payload
        question = revised if request.payload["revision_questions"] else original
        assert request.payload["plans"][0]["required_question_scope"] == ["adults with the condition"]
        assert request.payload["plans"][0]["primary_evidence"]["start"] == 100
        assert [item["material_id"] for item in request.payload["materials"]] == ["selected"]
        return {"questions": [{"plan_id": 1, "question": question}]}

    def form(request):
        assert "materials" not in request.payload and "question_source_ids" not in request.payload
        assert "question_required_scope" not in request.payload
        return {"judgments": [{"index": 0, "task_in_question": "Find the effect", "missing_required_context": [],
                              "task_kind": "domain_knowledge", "verdict": "ACCEPT", "reason": "Standalone"}]}

    def premises(request):
        assert [m["material_id"] for m in request.payload["materials"]] == ["selected"]
        assert request.payload["question_source_ids"] == {0: ["selected"]}
        assert request.payload["question_required_scope"] == {0: ["adults with the condition"]}
        is_revised = request.payload["questions"][0]["question"] == revised
        return {"judgments": [{"index": 0, "premises": [], "verdict": "ACCEPT" if is_revised else "REVISE",
                              "reason": "Only adults with the condition are covered"}]}

    model = FakeStructuredModel(responses={"objective_question_opportunity": plan,
                                           "objective_question_discovery": discover,
                                           "objective_question_form_reader": form,
                                           "objective_question_reader": premises})
    discovery, audits, _ = question_discovery.formulate_questions(model, StructuredGenerationRequest(
        invocation_id="discovery", role="objective_question_discovery", system_prompt_version="discovery-v1",
        idempotency_key="discovery", payload={"goal": {}, "focus": "Explain unproven mechanisms",
                                               "research_notebook": "Controller search notes", "materials": [
            {"material_id": "selected", "excerpts": [{"offset": 98, "text": "  Adults with the condition responded."}]},
            {"material_id": "unselected", "excerpts": [{"offset": 0, "text": "Other patients were enrolled."}]},
        ]},
    ))
    assert [item.question for item in discovery.opportunities] == [revised]
    assert discovery.opportunities[0].source_ids == ["selected"]
    assert discovery.opportunities[0].primary_evidence["text"] == "Adults with the condition responded."
    assert len(model.calls_for("objective_question_opportunity")) == 1
    assert len(audits) == 2


@pytest.fixture
def handoff(tmp_path):
    committed = []
    loop = SimpleNamespace(
        persistence=ObjectiveQuestionPersistence(tmp_path), providers={}, model=None,
        _commit_child=lambda _snapshot, _decision, _slot, _result, artifacts: committed.extend(artifacts),
    )
    snapshot = SimpleNamespace(artifacts={}, hypotheses={}, run_id="fixture",
                               goal=SimpleNamespace(model_dump=lambda **_: {"target_entity": "G6PD"}))
    texts = {
        "material.paper-a": "[Results / Enzyme]\nThe first selected paragraph reports enzyme activity in the cohort.\n\n"
                            "The second selected paragraph describes the study population.\n\n"
                            "Unread adjacent material should stay outside the report.",
        "material.paper-b": "The other source compares two variant classification schemes.",
    }
    for index, (material_id, content) in enumerate(texts.items(), 1):
        digest = text_digest(content)
        material = EvidenceMaterial(
            material_id=material_id, provider_id="pubmed", provider_version="v1",
            source_record_id=f"pmid:{index}", retrieved_at="2026-11-16T00:00:00Z",
            content_hash=digest, raw_content_ref=immutable_blob_ref(digest), material_type="literature_full_text",
            retrieval_request_id=f"request.{index}", terminal_status="SUCCEEDED",
        )
        loop.persistence.save_material(material, content.encode("utf-8"))
        snapshot.artifacts[material_id] = ArtifactEnvelope(
            artifact_id=material_id, artifact_type="evidence_material", payload=material.model_dump(mode="json"),
        )
    passages = [{"material_id": aid, "excerpts": material_passages(loop, snapshot, aid, ids)["excerpts"]}
                for aid, ids in SOURCES.items()]
    snapshot.artifacts[REPORT_ID] = _observation(REPORT_ID, {
        "status": "research_guidance_not_verified_evidence", "information_need": "Survey distinct findings",
        "summary": "Model summary must not become evidence",
        "source_sections": dict(reversed(SOURCES.items())), "source_passages": passages,
    })
    return loop, snapshot, committed


@pytest.mark.parametrize("use_report", [True, False])
def test_discovery_dispatch_preserves_report_selection_and_legacy_material_path(handoff, monkeypatch, use_report):
    loop, snapshot, committed = handoff
    material_ids = [REPORT_ID] if use_report else list(SOURCES)
    parameters = {} if use_report else {"source_sections": SOURCES}
    received = []
    primary = {"span_id": 1, "material_id": "material.paper-a", "text": "Enzyme activity"}

    def formulate(_model, request):
        received.append(request.payload)
        opportunity = question_discovery.QuestionOpportunity(
            question="What did the study report about enzyme activity?", information_need="Enzyme activity",
            value="Compare study results", source_ids=["material.paper-a"],
            supported_observations=["Reports activity in a cohort"], readiness="ready_for_design",
            primary_evidence=primary,
            next_information_target="",
        )
        return question_discovery.QuestionDiscovery(opportunities=[opportunity]), [{"retained_verdicts": ["ACCEPT"]}], {}

    monkeypatch.setattr(question_discovery, "formulate_questions", formulate)
    execute_react_tool(loop, snapshot, _decision(material_ids, parameters))

    assert len(received) == 1
    payload = received[0]
    assert "Model summary must not become evidence" not in str(payload)
    summary, opportunity = committed
    assert summary.payload["source_ids"] == list(SOURCES)
    assert opportunity.payload["source_ids"] == ["material.paper-a"]
    snapshot.artifacts[opportunity.artifact_id] = opportunity
    context = design_research_context(snapshot, opportunity.artifact_id)
    assert context["information_need"] == "Enzyme activity"
    assert context["supported_observations"] == ["Reports activity in a cohort"]
    assert context["primary_evidence"] == primary
    assert context["source_passages"] == opportunity.payload["source_passages"]
    if use_report:
        saved = snapshot.artifacts[REPORT_ID].payload["source_passages"]
        assert [item["excerpts"] for item in payload["materials"]] == [item["excerpts"] for item in saved]
        assert "Unread adjacent material" not in str(payload)
        assert payload["evidence_research_id"] == REPORT_ID
        for artifact in committed:
            assert artifact.payload["evidence_research_id"] == REPORT_ID
            assert artifact.metadata["evidence_research_id"] == REPORT_ID
    else:
        assert "Unread adjacent material" in str(payload["materials"][0])  # Existing named-section expansion.
        assert "evidence_research_id" not in payload
        assert all("evidence_research_id" not in artifact.payload for artifact in committed)


@pytest.mark.parametrize("ids, parameters", [
    ([REPORT_ID, "material.paper-a"], {}),
    ([REPORT_ID, REPORT_ID], {}),
    ([REPORT_ID], {"source_sections": SOURCES}),
])
def test_report_selector_does_not_mix_with_explicit_selection(handoff, ids, parameters):
    _, snapshot, _ = handoff
    with pytest.raises(RoleContractViolation, match="one research report ID alone"):
        validate_react_decision(snapshot, _decision(ids, parameters))


def test_controller_lists_recent_unused_exploration_reports_with_sources(handoff):
    _, snapshot, _ = handoff
    payload = snapshot.artifacts[REPORT_ID].payload
    snapshot.artifacts["research.empty"] = _observation("research.empty", {**payload, "source_passages": []})
    snapshot.artifacts["research.verification"] = _observation("research.verification", payload)
    steps = [
        {"action": "research_evidence", "result_ids": [REPORT_ID]},
        {"action": "research_evidence", "result_ids": ["research.empty"]},
        {"action": "research_evidence", "parameters": {"hypothesis_id": "hypothesis.1"},
         "result_ids": ["research.verification"]},
    ]
    assert _discovery_handoffs(snapshot, steps) == [{
        "report_id": REPORT_ID, "information_need": "Survey distinct findings",
        "sources": [{"material_id": aid, "record_id": f"pmid:{index}", "material_type": "literature_full_text"}
                    for index, aid in enumerate(SOURCES, 1)],
    }]
    with pytest.raises(RoleContractViolation, match="selected original passages"):
        validate_react_decision(snapshot, _decision(["research.empty"]))

    snapshot.artifacts["discovery.fixture"] = _observation("discovery.fixture", {
        "status": "discovery_advice_not_verified_evidence", "evidence_research_id": REPORT_ID,
    })
    assert _discovery_handoffs(snapshot, steps) == []
    validate_react_decision(snapshot, _decision([REPORT_ID]))  # Explicit reruns remain available.


def test_controller_schema_points_research_and_discovery_to_saved_sources(handoff):
    def input_options(snapshot, aliases=None):
        choice_model, _ = candidate_choice_model(snapshot, None, aliases)
        return {p["action"]["const"]: p["input_artifact_ids"]
                for branch in choice_model.model_json_schema()["anyOf"] if (p := branch["properties"])}

    empty = input_options(SimpleNamespace(artifacts={}, hypotheses={}))
    assert empty["research_evidence"]["maxItems"] == 0
    assert "discover_questions" not in empty
    assert "design_questions" not in empty

    _, snapshot, _ = handoff
    snapshot.artifacts["opportunity"] = _observation("opportunity", {"status": "model_assessment_not_verified_fact"})
    options = input_options(snapshot, {"M0001": "material.paper-a"})
    assert options["research_evidence"]["items"]["enum"] == ["M0001", "material.paper-b"]
    materials, report = options["discover_questions"]["anyOf"]
    assert materials["items"]["enum"] == ["M0001", "material.paper-b"]
    assert report["items"]["enum"] == [REPORT_ID] and report["maxItems"] == 1
    schema, _ = candidate_choice_model(snapshot, None, design_ids=("opportunity",))
    design = next(branch["properties"]["parameters"] for branch in schema.model_json_schema()["anyOf"]
                  if branch["properties"]["action"]["const"] == "design_questions")
    assert design["properties"]["research_id"]["enum"] == ["opportunity"]

    handoff_schema, _ = candidate_choice_model(snapshot, None, fresh_handoff=True)
    research = next(branch["properties"]["parameters"] for branch in handoff_schema.model_json_schema()["anyOf"]
                    if branch["properties"]["action"]["const"] == "research_evidence")
    assert "hypothesis_id" in research["required"]
    with pytest.raises(ValueError, match="unused original passages"):
        handoff_schema.model_validate({
            "action": "research_evidence", "rationale": "Search again",
            "parameters": {"information_need": "Broad exploration"},
        })


def test_controller_surfaces_unused_question_directions_after_a_draft_attempt(handoff):
    from medical_qa_data_factory.generation.question_reader import QUESTION_REVIEW_VERSION

    _, snapshot, _ = handoff
    base = {"status": "model_assessment_not_verified_fact", "question_review": "ACCEPT",
            "question_review_version": QUESTION_REVIEW_VERSION,
            "assessment": {"ready_for_design": True}, "source_passages": ["saved selection"]}
    for aid, question, overrides in [
        ("attempted", "Prior question?", {}),
        ("available", "An independent question?", {}),
        ("needs_research", "A question needing sources?", {"assessment": {"ready_for_design": False}}),
        ("duplicate", "Current question?", {}),
    ]:
        snapshot.artifacts[aid] = _observation(aid, {**base, "approved_question": question, **overrides})
    snapshot.artifacts["hypothesis.current"] = ArtifactEnvelope(
        artifact_id="hypothesis.current", artifact_type="question_hypothesis",
        payload={"question_intent": "Current question?"},
    )
    snapshot.artifacts["original"] = _observation("original", {"research_question": "What mechanism explains it?"})
    snapshot.artifacts["alternative"] = _observation("alternative", {
        **base, "approved_question": "Which effect was observed?", "origin": "answer_reading_alternative",
        "parent_research_id": "original", "opportunity": {"primary_evidence": {"text": "An effect was observed."}},
    }).model_copy(update={"parent_decision_id": "decision.original"})
    snapshot.artifacts["notes"] = ArtifactEnvelope(
        artifact_id="notes", artifact_type="question_design_notes", parent_decision_id="decision.original",
        payload={"answer_source_reading": {"unestablished_links": ["The mechanism was not established."]}},
    )
    steps = [{"action": "design_questions", "parameters": {"research_id": "attempted"}}]
    assert _unused_discovery_opportunities(snapshot, steps) == [
        {"research_id": "alternative", "question": "Which effect was observed?",
         "original_question": "What mechanism explains it?", "original_question_gap": "The mechanism was not established.",
         "supporting_observation": "An effect was observed."},
        {"research_id": "available", "question": "An independent question?"},
    ]


def test_report_does_not_send_mismatched_excerpts_to_discovery(handoff):
    loop, snapshot, committed = handoff
    original = snapshot.artifacts[REPORT_ID].payload
    first, *others = original["source_passages"]
    excerpt, *rest = first["excerpts"]
    snapshot.artifacts[REPORT_ID] = _observation(REPORT_ID, {
        **original, "source_passages": [{**first, "excerpts": [{**excerpt, "text": "Changed text"}, *rest]}, *others],
    })
    with pytest.raises(RoleContractViolation, match="do not match their stored source text"):
        execute_react_tool(loop, snapshot, _decision([REPORT_ID]))
    assert not committed


def test_report_validation_rejects_malformed_saved_excerpts(handoff):
    _, snapshot, _ = handoff
    original = snapshot.artifacts[REPORT_ID].payload
    first, *others = original["source_passages"]
    snapshot.artifacts[REPORT_ID] = _observation(REPORT_ID, {
        **original, "source_passages": [{**first, "excerpts": [None]}, *others],
    })
    with pytest.raises(RoleContractViolation, match="invalid saved excerpts"):
        validate_react_decision(snapshot, _decision([REPORT_ID]))

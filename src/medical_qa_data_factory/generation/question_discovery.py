"""Independent question discovery over selected, persisted source passages."""
from typing import Literal

from pydantic import Field, create_model

from ..domain.common import ContractModel
from ..domain.source_spans import sentence_spans
from ..llm.contracts import StructuredGenerationRequest
from .research_tools import add_section_context, material_passages
from .subagent_contracts import RoleContractViolation, SubagentRole
from .source_question_inputs import SELECTED_EVIDENCE_CHAR_BUDGET, completed_research_evidence
from .question_reader import QUESTION_REVIEW_VERSION, review_questions


class QuestionOpportunityPlan(ContractModel):
    source_ids: list[str] = Field(min_length=1, max_length=6)
    supported_observations: list[str] = Field(
        min_length=1, max_length=6,
        description="Record the source observations first, with their scope and uncertainty. Each necessary part of the planned question needs an observation.",
    )
    information_need: str = Field(min_length=1, max_length=800,
                                 description="One central information need the observations can answer, or the specific need for further research.")
    value: str = Field(min_length=1, max_length=600)
    answer_boundaries: list[str] = Field(default_factory=list, max_length=6)
    missing_evidence: list[str] = Field(default_factory=list, max_length=6)
    readiness: Literal["ready_for_design", "needs_evidence"]
    next_information_target: str = Field(max_length=800)


class QuestionOpportunity(QuestionOpportunityPlan):
    required_question_scope: list[str] = Field(default_factory=list, max_length=6)
    primary_evidence: dict | None = None
    question: str = Field(min_length=1, max_length=800,
                          description="The actual standalone public question, without an answer.")


class EvidenceScope(ContractModel):
    population_or_genotype: str | None = Field(description="Actual population, species or genotype that changes the answer.")
    setting: str | None = Field(description="Actual experimental model, setting or intervention condition that changes the answer.")
    comparator: str | None = Field(description="Actual comparison group, treatment or reference condition that changes the answer.")
    time_period: str | None = Field(description="Actual time period or evidence version that changes the answer.")


class QuestionDiscovery(ContractModel):
    opportunities: list[QuestionOpportunity] = Field(max_length=4)
    material_limitations: list[str] = Field(default_factory=list, max_length=6)
    no_opportunity_reason: str = Field(default="", max_length=1000)


class QuestionOpportunityPlans(ContractModel):
    opportunities: list[QuestionOpportunityPlan] = Field(max_length=4)
    material_limitations: list[str] = Field(default_factory=list, max_length=6)
    no_opportunity_reason: str = Field(default="", max_length=1000)


class PlannedQuestionWording(ContractModel):
    plan_id: int = Field(ge=1, le=4)
    question: str = Field(min_length=1, max_length=800)


class PlannedQuestionWordings(ContractModel):
    questions: list[PlannedQuestionWording] = Field(max_length=4)


def discovery_schema(materials, evidence_ids):
    """Expose the actual source choices before generation, not after reader calls."""
    ids = tuple(dict.fromkeys(m["material_id"] for m in materials))
    if not ids or not evidence_ids:
        raise RoleContractViolation("Question discovery requires selected original passages")
    source_id = Literal[ids]
    opportunity = create_model("SourceQuestionOpportunityPlan", __base__=QuestionOpportunityPlan,
        source_ids=(list[source_id], Field(max_length=5, description="Additional sources needed for the same relationship.")),
        evidence_scope=(EvidenceScope, Field(description="Limits supported by the source. Use null where no limit is needed.")),
        primary_span_id=(Literal[tuple(evidence_ids)], Field(description=(
            "Select the sentence supporting the core relationship for the named group, intervention and outcome."
        ))))
    return create_model("SourceQuestionOpportunityPlans", __base__=QuestionOpportunityPlans,
        opportunities=(list[opportunity], Field(max_length=4)))


def _index_planning_passages(materials):
    """Expose selectable original sentences and keep their source offsets."""
    structured, spans = [], {}
    for material in materials:
        passages = []
        for excerpt in material["excerpts"]:
            text = excerpt["text"]
            passage = {key: value for key, value in excerpt.items() if key != "text"}
            passage["sentences"] = []
            for start, end in sentence_spans(text):
                span_id = len(spans) + 1
                sentence = {"span_id": span_id, "text": text[start:end]}
                passage["sentences"].append(sentence)
                spans[span_id] = {
                    **sentence, "material_id": material["material_id"],
                    "paragraph_id": excerpt.get("paragraph_id"),
                    "start": excerpt["offset"] + start, "end": excerpt["offset"] + end,
                }
            passages.append(passage)
        structured.append({**material, "excerpts": passages})
    return structured, spans


def formulate_questions(model, request):
    """Review question usefulness and source-supported premises before answers."""
    audits, usage = [], {}
    retained, retained_verdicts = [], []
    revisions = []
    # The controller focus describes why the material was retrieved; the goal
    # carries the requested scope. Plan from the observations actually found.
    grounding = {key: request.payload[key] for key in ("goal", "materials", "existing_questions")
                 if key in request.payload}
    planning_materials, spans = _index_planning_passages(grounding["materials"])
    response_schema = discovery_schema(planning_materials, spans)
    plan_response = model.generate_structured(StructuredGenerationRequest(
        invocation_id=f"{request.invocation_id}.plan", role="objective_question_opportunity",
        system_prompt_version="question-opportunity-v3",
        idempotency_key=f"{request.idempotency_key}|plan:v3",
        payload={**grounding, "materials": planning_materials, "instruction": (
            request.payload.get("instruction", "") + " Choose distinct information needs and pin the original observations and answer-changing "
            "conditions before formulating any wording. Compare the actual groups or interventions named "
            "by the source. Start with findings that support a useful question today. "
            "For an entity run, each selected observation must support a question about the target entity's role "
            "or a condition involving it. A similarly named gene or a related entity from the search results "
            "can be a comparator, but a finding solely about that other entity does not meet this goal. "
            "Compare each proposed information need with the existing questions and the other plans in this "
            "batch. If one would answer the other as a subquestion, keep one and use the remaining slot "
            "for a different relationship or result."
            " If a source supports a bounded observation but not its mechanism, plan that observation first and keep the mechanism as "
            "a separate lead. Choose one primary_span_id that actually links the named subject, group and outcome. "
            "A neighboring sentence about a different group or intervention can suggest a separate question. "
            "Record source-supported answer-changing limits in evidence_scope. Identify exact genotype or zygosity, "
            "model and comparator when applicable. A topic, answer category or proposed result is not a scope limit. "
            "Do not merge unrelated outcomes just because they are reported together."
        )},
    ), response_schema)
    for key, value in plan_response.usage.items():
        if isinstance(value, (int, float)):
            usage[key] = value
    planning = response_schema.model_validate(plan_response.parsed)
    plans = {}
    for index, plan in enumerate(planning.opportunities, 1):
        primary = spans[plan.primary_span_id]
        sources = list(dict.fromkeys([primary["material_id"], *plan.source_ids]))
        scope = plan.evidence_scope.model_dump(exclude_none=True)
        plans[index] = {**plan.model_dump(exclude={"primary_span_id", "evidence_scope"}),
                        "source_ids": sources, "required_question_scope": list(scope.values()),
                        "primary_evidence": primary}
    discovery = QuestionDiscovery(opportunities=[], material_limitations=planning.material_limitations,
                                  no_opportunity_reason=planning.no_opportunity_reason)
    if not plans:
        return discovery, audits, usage
    available_ids = list(plans)

    def finish(discovery):
        if audits:
            audits[-1]["retained_verdicts"] = retained_verdicts
            audits[-1]["retained_questions"] = [o.question for o in retained]
        return discovery.model_copy(update={"opportunities": retained,
            "no_opportunity_reason": "" if retained else discovery.no_opportunity_reason or
            "Questions did not pass reader review; use question_reviews for specific formulation gaps."}), audits, usage

    for attempt in range(2):
        visible_sources = {source for index in available_ids for source in plans[index]["source_ids"]}
        current = request.model_copy(update={
            "invocation_id": f"{request.invocation_id}.form{attempt}",
            "idempotency_key": f"{request.idempotency_key}|form:{attempt}",
            "payload": {**grounding,
                        "materials": [m for m in request.payload["materials"] if m["material_id"] in visible_sources],
                        "plans": [{"plan_id": index, **plans[index]}
                                  for index in available_ids],
                        "instruction": (
                            "Write one natural standalone question per supplied plan. Return only the plan ID "
                            "and question. Keep its information need and explicitly name its required question scope. "
                            "Use the named subject, group and outcome of primary_evidence for the core question. "
                            "Do not assign findings about a neighboring intervention or group to that subject. "
                            "Use the actual genotype or zygosity when provided; keep a gene and its variant distinct. "
                            "Do not add another requested fact, outcome, mechanism or stronger claim. If a plan "
                            "cannot form a meaningful supported standalone question, omit it."
                        ),
                        "question_feedback": audits[-1:],
                        "retained_questions": [o.question for o in retained],
                        "revision_questions": revisions,
                        "revision_instruction": (
                            f"Keep the retained questions unchanged; do not return them again. "
                            f"Return at most {4 - len(retained)} revised opportunities addressing the specific reader feedback. "
                            "Preserve the meaningful information need and fix only the identified defect using the supplied sources. "
                            "Do not add paper names, unsupported premises, or expected answers to the question. "
                            "Return none if no suitable revision is supported."
                            if revisions else "")}})
        response = model.generate_structured(current, PlannedQuestionWordings)
        for key, value in response.usage.items():
            if isinstance(value, (int, float)):
                usage[key] = usage.get(key, 0) + value
        wordings = PlannedQuestionWordings.model_validate(response.parsed)
        by_plan = {}
        for wording in wordings.questions:
            if wording.plan_id in available_ids:
                by_plan.setdefault(wording.plan_id, wording.question)
        drafted_ids = list(by_plan)
        discovery = discovery.model_copy(update={"opportunities": [
            QuestionOpportunity(question=by_plan[index], **plans[index]) for index in drafted_ids
        ]})
        if not discovery.opportunities:
            return finish(discovery)
        review, reader_views, reader_usage = review_questions(model,
            operation_id=f"{request.idempotency_key}|reader:{attempt}", goal=request.payload["goal"],
            materials=request.payload["materials"],
            questions=[{"index": i, "question": o.question} for i, o in enumerate(discovery.opportunities)],
            question_source_ids={i: o.source_ids for i, o in enumerate(discovery.opportunities)},
            question_required_scope={i: o.required_question_scope for i, o in enumerate(discovery.opportunities)})
        for key, value in reader_usage.items():
            if isinstance(value, (int, float)):
                usage[key] = usage.get(key, 0) + value
        audits.append({"questions": [o.question for o in discovery.opportunities], "plan_ids": drafted_ids,
                       "plans": [{"plan_id": i, **plan} for i, plan in plans.items()],
                       "review": review.model_dump(mode="json"), **reader_views})
        revisions = []
        by_index = {j.index: j for j in review.judgments}
        for index, opportunity in enumerate(discovery.opportunities):
            judgment = by_index[index]
            if opportunity.question in {o.question for o in retained}:
                continue
            if judgment.verdict == "REVISE":
                revisions.append({"plan_id": drafted_ids[index], "question": opportunity.question,
                                  "reason": judgment.reason})
            if len(retained) >= 4:
                continue
            if judgment.verdict == "ACCEPT":
                retained.append(opportunity)
                retained_verdicts.append(judgment.verdict)
            elif judgment.verdict == "NEEDS_EVIDENCE":
                gaps = list(opportunity.missing_evidence)
                if len(gaps) < 6:
                    gaps.append(judgment.reason)
                else:
                    gaps[-1] += "\n题干前提待核实：" + judgment.reason
                retained.append(opportunity.model_copy(update={
                    "readiness": "needs_evidence", "missing_evidence": gaps,
                    "next_information_target": "先核实 missing_evidence 中的题干前提，再评估是否足以回答原问题。"}))
                retained_verdicts.append(judgment.verdict)
        if not revisions or len(retained) >= 4:
            return finish(discovery)
        available_ids = [item["plan_id"] for item in revisions]
    return finish(discovery)


def is_research_report(artifact):
    return (artifact is not None and artifact.artifact_type == "react_observation"
            and artifact.payload.get("status") == "research_guidance_not_verified_evidence")


class AlternativeQuestion(ContractModel):
    span_id: int = Field(ge=1)
    question: str = Field(min_length=1, max_length=800)
    information_need: str = Field(min_length=1, max_length=500)


class AlternativeResult(ContractModel):
    alternative: AlternativeQuestion | None


def formulate_alternative_question(model, *, reading, original_question, goal, operation_id):
    if not (reading.get("missing_relationships") or reading.get("unestablished_links")):
        return None, {}
    observations = {item["span_id"]: item for item in reading["observations"]
                    if item["relevance"] == "direct" and item.get("relationship") != "proposed_explanation"}
    if not observations:
        return None, {}
    request = StructuredGenerationRequest(
        invocation_id=f"{operation_id}.wording", role="objective_question_discovery",
        system_prompt_version="discovery-alternative-v2", idempotency_key=f"{operation_id}|wording:v2",
        payload={"goal": goal, "original_question": original_question,
                 "observations": list(observations.values()),
                 "unresolved_relationships": [*reading.get("unestablished_links", []),
                                              *reading.get("missing_relationships", [])],
                 "instruction": "Formulate one new standalone question about one directly observed relationship. "
                                "Select one span_id and ground the question in its verbatim quote. Keep the "
                                "actual subject, conditions and measured outcome. Ask what was observed or which "
                                "entity was involved. None of unresolved_relationships may be a premise or a required "
                                "part of the new answer. When a pronoun leaves ownership of a detail unclear, ask "
                                "about a named entity or unambiguous relationship instead. Keep genes distinct from "
                                "their variants and preserve actual zygosity and experimental setting. Do not name the "
                                "paper. Return null only if no distinct useful question is warranted."},
    )
    response = model.generate_structured(request, AlternativeResult)
    result = AlternativeResult.model_validate(response.parsed).alternative
    if result and result.span_id in observations:
        return {**observations[result.span_id], **result.model_dump(mode="json")}, response.usage
    return None, response.usage


def review_alternative_question(model, *, alternative, anchor, research_context, goal, operation_id):
    """Check a new question over the same saved passage without inheriting acceptance."""
    if not alternative or anchor is None:
        return None, {}
    start = anchor.start_offset + alternative["start_in_anchor"]
    end = anchor.start_offset + alternative["end_in_anchor"]
    for original in research_context.get("source_passages", []):
        if original["material_id"] != anchor.material_id:
            continue
        for excerpt in original["excerpts"]:
            offset = excerpt["offset"]
            if start >= offset and end <= offset + len(excerpt["text"]) and (
                excerpt["text"][start - offset:end - offset] == alternative["quote"]
            ):
                question = alternative["question"]
                # Keep saved neighboring passages for context such as table legends.
                source = original
                review, audit, usage = review_questions(
                    model, questions=[{"index": 0, "question": question}], materials=[source], goal=goal,
                    operation_id=operation_id, question_source_ids={0: [anchor.material_id]},
                )
                judgment = review.judgments[0]
                accepted = judgment.verdict == "ACCEPT"
                return {
                    "artifact_id": operation_id, "status": "model_assessment_not_verified_fact",
                    "origin": "answer_reading_alternative", "parent_research_id": research_context.get("research_id"),
                    "source_ids": [anchor.material_id], "source_passages": [source],
                    "research_question": question, "approved_question": question if accepted else None,
                    "question_review": judgment.verdict, "question_review_version": QUESTION_REVIEW_VERSION,
                    "alternative_review": {"judgment": judgment.model_dump(mode="json"), **audit},
                    "opportunity": {
                        "information_need": alternative["information_need"],
                        "primary_evidence": {"material_id": anchor.material_id, "start": start, "end": end,
                                             "paragraph_id": excerpt.get("paragraph_id"), "text": alternative["quote"]},
                    },
                    "assessment": {"question_focus": question, "ready_for_design": accepted,
                                   "remaining_gaps": [] if accepted else [judgment.reason]},
                }, usage
    return None, {}


def research_report_sections(snapshot, report):
    evidence = completed_research_evidence(snapshot, report.artifact_id)
    sources = evidence["source_passages"]
    sections = report.payload.get("source_sections", {})
    if (not isinstance(sections, dict) or not isinstance(sources, list)
            or not 1 <= len(sources) <= 6 or len(sources) != len(sections)):
        raise RoleContractViolation("Research report has no usable saved original paragraph selection")
    selected_sections = {}
    for source in sources:
        if not isinstance(source, dict) or not isinstance(source.get("material_id"), str):
            raise RoleContractViolation("Research report contains an invalid saved source")
        material_id = source["material_id"]
        if material_id in selected_sections:
            raise RoleContractViolation(f"Research report repeats source {material_id}")
        artifact = snapshot.artifacts.get(material_id)
        if artifact is None or artifact.artifact_type != "evidence_material":
            raise RoleContractViolation(f"Research report source {material_id} is not a saved evidence_material")
        ids = sections.get(material_id)
        excerpts = source.get("excerpts")
        if (not isinstance(excerpts, list) or not excerpts or any(
                not isinstance(excerpt, dict) or not isinstance(excerpt.get("offset"), int)
                or not isinstance(excerpt.get("text"), str) or not excerpt["text"] for excerpt in excerpts)):
            raise RoleContractViolation(f"Research report source {material_id} has invalid saved excerpts")
        if (not isinstance(ids, list) or not ids or any(not isinstance(pid, str) for pid in ids)
                or list(dict.fromkeys(ids)) != [excerpt.get("paragraph_id") for excerpt in excerpts]):
            raise RoleContractViolation(f"Research report source {material_id} has inconsistent saved paragraphs")
        selected_sections[material_id] = ids
    return selected_sections


def discovery_report_selection(snapshot, decision):
    reports = [artifact for aid in decision.input_artifact_ids
               if is_research_report(artifact := snapshot.artifacts.get(aid))]
    if not reports:
        return None
    if len(decision.input_artifact_ids) != 1 or decision.action_parameters.get("source_sections"):
        raise RoleContractViolation("Select one research report ID alone, or explicit material IDs and source_sections")
    report = reports[0]
    return report, research_report_sections(snapshot, report)


def _check_handoff_passages(report, materials):
    def locators(excerpts):
        return [(item["paragraph_id"], item["offset"], item["text"]) for item in excerpts]

    originals = {source["material_id"]: locators(source["excerpts"])
                 for source in report.payload["source_passages"]}
    if any(locators(view["excerpts"]) != originals[view["material_id"]] for view in materials):
        raise RoleContractViolation("Research report passages do not match their stored source text")
    if sum(len(view["content"]) for view in materials) > SELECTED_EVIDENCE_CHAR_BUDGET:
        raise RoleContractViolation("Research report exceeds the discovery evidence budget; select explicit passages")


def discover_questions(loop, snapshot, decision):
    from .agent_loop import _artifact, _success_result
    from .react_observations import ObservationView

    selection = discovery_report_selection(snapshot, decision)
    if selection:
        report, sections = selection
        ids = list(sections)
    else:
        ids = decision.input_artifact_ids
        sections = decision.action_parameters.get("source_sections", {})
    # Assembly of selected evidence is not an interactive reading page.
    materials = [material_passages(loop, snapshot, aid, sections.get(aid),
                                  max_paragraphs=None, max_chars=SELECTED_EVIDENCE_CHAR_BUDGET)
                 for aid in ids]
    # Choosing a stored source should carry forward the paragraphs the agents
    # actually read. Never substitute summaries, previews or unread text.
    reused_reads = []
    provenance = {}
    if selection:
        _check_handoff_passages(report, materials)
        provenance = {"evidence_research_id": report.artifact_id}
    else:
        read_sections = {item["material_id"]: item["returned_paragraph_ids"]
                         for item in ObservationView(snapshot).reading_ledger()["materials"]}
        for index, item in enumerate(materials):
            aid = item["material_id"]
            if not item["excerpts"] and aid not in sections and read_sections.get(aid):
                selected = material_passages(loop, snapshot, aid, read_sections[aid],
                    max_paragraphs=None, max_chars=SELECTED_EVIDENCE_CHAR_BUDGET)
                selected["excerpts"].sort(key=lambda excerpt: excerpt["offset"])
                selected["content"] = "\n\n".join(excerpt["text"] for excerpt in selected["excerpts"])
                selected["returned_paragraph_ids"] = [excerpt["paragraph_id"] for excerpt in selected["excerpts"]]
                materials[index] = selected
                reused_reads.append(aid)
        if any(not item["excerpts"] for item in materials):
            missing = [item["material_id"] for item in materials if not item["excerpts"]]
            raise RoleContractViolation(
                f"Discovery NOT EXECUTED: no original paragraphs selected/read for {missing}. "
                "Use read_artifacts to read their indexes and select source_sections={material_id:[paragraph_ids]}. "
                "Previously read paragraphs of other selected materials remain available.")
        materials = add_section_context(loop, snapshot, materials, max_chars=SELECTED_EVIDENCE_CHAR_BUDGET)
    questions = [{"hypothesis_id": aid, "question": snapshot.artifacts[aid].payload.get("question_intent"),
                  "status": status.value} for aid, status in snapshot.hypotheses.items()
                 if aid in snapshot.artifacts]
    payload = {
        "goal": snapshot.goal.model_dump(mode="json"),
        "materials": [{"material_id": item["material_id"], "source_record_id": item["source_record_id"],
                       "material_type": item["material_type"], "excerpts": item["excerpts"],
                       "section_context": item["section_context"],
                       "content_complete": item["content_complete"],
                       "coverage_meaning": item["coverage_meaning"]} for item in materials],
        "existing_questions": questions,
        "instruction": "从所给原文中挑选值得了解的内容，规划最多四个有边界且不同的检索目标。"
                       "先选primary_span_id和必要的补充source_ids，再写supported_observations，保留原文对象、条件和不确定性。"
                       "从这些观察确定一个中心information_need，在evidence_scope记录会改变答案的限定。单篇论文的发现可以独立出题。"
                       "逐项对照信息需求。如果原文支持差异却没有变异特异机制，只规划差异。"
                       "如果原文只支持蛋白稳定性，不能把它自动扩写成已经观察到的下游结果。"
                       "当机制本身是有价值的搜索方向但证据仍缺，单独列为needs_evidence并明确缺什么。"
                       "保留必要对象与条件，不默认追求分子级机制。"
                       "已有材料足以回答核心信息需求就ready_for_design，不为完善非关键细节补证。"
                       "使用提供的material IDs，保留适用范围和不确定性，不写公开题干或答案。",

    }
    if provenance:
        payload.update(provenance)
        payload["instruction"] += (
            "逐个查看报告选中的来源。面向整个实体出题时，先从不同来源找互不重复的发现，再考虑同一来源的第二个问题。"
            "同一个结论的总问和子问合成一个信息需求。聚焦任务以用户指定的范围为准。"
        )
    discovery, question_reviews, usage = formulate_questions(loop.model, StructuredGenerationRequest(
        role="objective_question_discovery", system_prompt_version="discovery-v1",
        invocation_id=f"discovery.{decision.decision_id}",
        idempotency_key=f"{snapshot.run_id}|discovery|{decision.decision_id}", payload=payload))
    artifacts, opportunities = [], []
    for index, opportunity in enumerate(discovery.opportunities, 1):
        if not set(opportunity.source_ids) <= set(ids):
            raise RoleContractViolation("Discovery source_ids must refer only to supplied materials")
        research_id = f"discovery.{decision.decision_id}.opportunity.{index}"
        data = opportunity.model_dump(mode="json")
        verdict = question_reviews[-1]["retained_verdicts"][index - 1]
        opportunities.append({"research_id": research_id, "question_review": verdict, **data})
        # Reuse the existing evidence-preserving design handoff. These assessments
        # remain model guidance; the original passages are the evidence.
        artifacts.append(_artifact({
            "artifact_id": research_id, "source_ids": list(dict.fromkeys(opportunity.source_ids)),
            "research_question": opportunity.question,
            "approved_question": opportunity.question if verdict == "ACCEPT" else None,
            "question_review": verdict,
            "question_review_version": QUESTION_REVIEW_VERSION,
            "source_passages": [{"material_id": item["material_id"], "excerpts": item["excerpts"],
                                 "section_context": item["section_context"]}
                                for item in materials if item["material_id"] in opportunity.source_ids],
            "assessment": {"question_focus": opportunity.question,
                           "answer_boundaries": opportunity.answer_boundaries,
                           "remaining_gaps": opportunity.missing_evidence,
                           "ready_for_design": opportunity.readiness == "ready_for_design"},
            "opportunity": data, "status": "model_assessment_not_verified_fact", **provenance,
        }, "react_observation", decision, SubagentRole.QUESTION_DISCOVERY, metadata=provenance))
    summary = _artifact({
        "artifact_id": f"discovery.{decision.decision_id}", "source_ids": ids,
        "opportunities": opportunities, "material_limitations": discovery.material_limitations,
        "no_opportunity_reason": discovery.no_opportunity_reason,
        "question_reviews": question_reviews,
        "reused_read_material_ids": reused_reads,
        "status": "discovery_advice_not_verified_evidence", **provenance,
    }, "react_observation", decision, SubagentRole.QUESTION_DISCOVERY, metadata=provenance)
    artifacts.insert(0, summary)
    result = _success_result(summary.artifact_id, [a.artifact_id for a in artifacts], "questions_discovered")
    result = result.model_copy(update={"usage": usage})
    loop._commit_child(snapshot, decision, result.subtask_id, result, artifacts)

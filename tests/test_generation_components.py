"""Rule-based components must produce a verifiable draft with no model or network."""
from __future__ import annotations

from medical_qa_data_factory.domain import (
    Claim,
    ClaimStatus,
    ClaimType,
    EntityRef,
    EvidenceDirection,
    KGEvidence,
    LinkStatus,
)
from medical_qa_data_factory.generation.claims import RuleBasedClaimBuilder
from medical_qa_data_factory.generation.contracts import (
    GenerationSpec,
    RelationSemantics,
    QuestionBlueprint,
    QuestionDraft,
    VerificationResult,
)
from medical_qa_data_factory.generation.curation import curate_evidence
from medical_qa_data_factory.generation.planning import QuestionPlanner
from medical_qa_data_factory.generation.semantics import RelationSemanticsPolicy
from medical_qa_data_factory.generation.question_skills import load_question_skill
from medical_qa_data_factory.generation.style_evaluation import (
    StructuredStyleJudge,
    deterministic_style_issues,
)
from medical_qa_data_factory.generation.verification import (
    RuleBasedAnswerVerifier,
    check_relation_semantics_safety,
)
from medical_qa_data_factory.generation.writing import RuleBasedQuestionWriter
from medical_qa_data_factory.generation.writing import StructuredQuestionWriter
from medical_qa_data_factory.llm import FakeStructuredModel, LanguageModelPort
from medical_qa_data_factory.llm.contracts import StructuredGenerationRequest

NOW = "2026-10-17T00:00:00Z"


def _ref(name: str, canonical_id: str, entity_type: str) -> EntityRef:
    return EntityRef(
        entity_ref_id=f"entity-{canonical_id}",
        raw_mention=name,
        canonical_name=name,
        canonical_id=canonical_id,
        namespace="primekg",
        entity_type=entity_type,
        aliases=[],
        link_status=LinkStatus.EXACT,
        link_score=1.0,
        source="primekg",
    )


ASPIRIN = _ref("aspirin", "primekg:exposure:D001241", "exposure")
HEADACHE = _ref("migraine", "primekg:disease:B156", "disease")
IBUPROFEN = _ref("ibuprofen", "primekg:exposure:D00521", "exposure")
BRAP = _ref("BRAP", "primekg:gene:8315", "gene")


def _edge(
    head: EntityRef,
    relation: str,
    tail: EntityRef,
    edge_id: str,
    direction: EvidenceDirection = EvidenceDirection.FORWARD,
) -> KGEvidence:
    return KGEvidence(
        evidence_id=f"kg.{edge_id[:8]}",
        head=head,
        relation=relation,
        tail=tail,
        direction=direction,
        source_database="primekg",
        source_record_id=edge_id,
        confidence=1.0,
        retrieval_action={"provider_id": "primekg"},
        retrieved_at=NOW,
    )


def _entities() -> dict[str, EntityRef]:
    return {
        item.canonical_id or "": item
        for item in (ASPIRIN, HEADACHE, IBUPROFEN, BRAP)
    }


def _pipeline(evidence: list[KGEvidence]):
    report = curate_evidence(
        evidence, seed_canonical_id=ASPIRIN.canonical_id or ""
    )
    claims = RuleBasedClaimBuilder().build_from_curated(report.curated, len(report.curated))
    blueprints = QuestionPlanner().plan(
        report.curated,
        seed_canonical_id=ASPIRIN.canonical_id or "",
        question_count=5,
    )
    drafts = RuleBasedQuestionWriter().write(
        blueprints, evidence=evidence, entities=_entities(), episode_id="ep1"
    )
    return report, claims, blueprints, drafts


def test_single_evidence_yields_at_least_one_draft() -> None:
    evidence = [_edge(ASPIRIN, "exposure_disease", HEADACHE, "e1")]
    report, claims, blueprints, drafts = _pipeline(evidence)
    assert len(report.curated) == 1
    assert len(claims) == 1
    assert len(blueprints) == 1
    assert len(drafts) == 1
    assert drafts[0].question_text
    assert drafts[0].candidate_answer.short_answer == "migraine"


def test_outgoing_edge_answer_is_the_tail_not_the_seed() -> None:
    evidence = [_edge(ASPIRIN, "exposure_disease", HEADACHE, "e1")]
    _, _, blueprints, drafts = _pipeline(evidence)
    assert blueprints[0].answer_endpoint == "tail"
    assert drafts[0].candidate_answer.short_answer == "migraine"
    assert drafts[0].candidate_answer.values[0]["canonical_id"] != ASPIRIN.canonical_id


def test_incoming_edge_answer_is_the_head_not_the_seed() -> None:
    evidence = [
        _edge(
            IBUPROFEN,
            "exposure_disease",
            ASPIRIN,
            "e2",
            direction=EvidenceDirection.BACKWARD,
        )
    ]
    report = curate_evidence(evidence, seed_canonical_id=ASPIRIN.canonical_id or "")
    assert report.curated[0].answer_endpoint == "head"
    blueprints = QuestionPlanner().plan(
        report.curated, seed_canonical_id=ASPIRIN.canonical_id or "", question_count=1
    )
    drafts = RuleBasedQuestionWriter().write(
        blueprints, evidence=evidence, entities=_entities(), episode_id="ep1"
    )
    assert drafts[0].candidate_answer.short_answer == "ibuprofen"
    assert blueprints[0].answer_endpoint == "head"


def test_evidence_not_involving_the_seed_is_dropped() -> None:
    evidence = [_edge(BRAP, "interacts_with", IBUPROFEN, "e9")]
    report = curate_evidence(evidence, seed_canonical_id=ASPIRIN.canonical_id or "")
    assert report.curated == []
    assert report.dropped_identity_mismatch == 1


def test_duplicate_stable_edges_collapse() -> None:
    evidence = [
        _edge(ASPIRIN, "exposure_disease", HEADACHE, "e1"),
        _edge(ASPIRIN, "exposure_disease", HEADACHE, "e1"),
    ]
    report = curate_evidence(evidence, seed_canonical_id=ASPIRIN.canonical_id or "")
    assert len(report.curated) == 1
    assert report.dropped_duplicate == 1


def test_curation_order_is_stable_across_input_permutations() -> None:
    first = _edge(ASPIRIN, "exposure_disease", HEADACHE, "e1")
    second = _edge(ASPIRIN, "drug_effect", IBUPROFEN, "e2")
    third = _edge(ASPIRIN, "contraindication", BRAP, "e3")
    seed = ASPIRIN.canonical_id or ""
    forward = curate_evidence([first, second, third], seed_canonical_id=seed)
    backward = curate_evidence([third, second, first], seed_canonical_id=seed)
    assert [item.stable_edge_id for item in forward.curated] == [
        item.stable_edge_id for item in backward.curated
    ]


def test_one_relation_cannot_fill_every_slot() -> None:
    edges = [
        _edge(ASPIRIN, "drug_effect", IBUPROFEN, f"e{i:02d}") for i in range(5)
    ]
    report = curate_evidence(
        edges, seed_canonical_id=ASPIRIN.canonical_id or "", max_per_relation=2
    )
    assert len(report.curated) == 2


def test_claim_predicate_keeps_raw_relation_and_direction() -> None:
    evidence = [_edge(ASPIRIN, "exposure_disease", HEADACHE, "e1")]
    _, claims, _, _ = _pipeline(evidence)
    claim = claims[0]
    assert claim.predicate == "exposure_disease"
    assert claim.subject.canonical_id == ASPIRIN.canonical_id
    assert claim.object.canonical_id == HEADACHE.canonical_id
    assert claim.claim_type in (ClaimType.FACT, ClaimType.ASSOCIATION)
    assert claim.status is ClaimStatus.SUPPORTED
    # A graph edge is not a calibrated medical confidence.
    assert claim.confidence is None
    assert "e1" in claim.rationale


def test_claim_only_references_existing_evidence() -> None:
    evidence = [_edge(ASPIRIN, "exposure_disease", HEADACHE, "e1")]
    _, claims, _, drafts = _pipeline(evidence)
    known = {item.evidence_id for item in evidence}
    assert set(claims[0].supporting_evidence_ids) <= known
    assert set(drafts[0].evidence_ids) <= known


def test_question_text_never_contains_the_answer() -> None:
    evidence = [_edge(ASPIRIN, "exposure_disease", HEADACHE, "e1")]
    _, _, _, drafts = _pipeline(evidence)
    assert "migraine" not in drafts[0].question_text.lower()


def test_candidate_answer_has_no_confirmed_gold_fields() -> None:
    evidence = [_edge(ASPIRIN, "exposure_disease", HEADACHE, "e1")]
    _, _, _, drafts = _pipeline(evidence)
    answer = drafts[0].candidate_answer
    assert answer.confidence is None
    assert answer.explanation is None
    assert answer.acceptable_variants == []


def test_verifier_accepts_a_consistent_draft() -> None:
    evidence = [_edge(ASPIRIN, "exposure_disease", HEADACHE, "e1")]
    _, claims, _, drafts = _pipeline(evidence)
    result = RuleBasedAnswerVerifier().verify(drafts[0], claims, evidence)
    assert result.verdict == "accepted"
    assert result.blocking is False


def test_verifier_accepts_incoming_draft_when_focus_is_stated() -> None:
    """An incoming edge keeps the seed as its head; that is not answer_is_seed."""
    evidence = [
        _edge(
            IBUPROFEN,
            "exposure_disease",
            ASPIRIN,
            "e2",
            direction=EvidenceDirection.BACKWARD,
        )
    ]
    report = curate_evidence(evidence, seed_canonical_id=ASPIRIN.canonical_id or "")
    blueprints = QuestionPlanner().plan(
        report.curated,
        seed_canonical_id=ASPIRIN.canonical_id or "",
        question_count=1,
    )
    claims = RuleBasedClaimBuilder().build_from_curated(report.curated, 1)
    drafts = RuleBasedQuestionWriter().write(
        blueprints, evidence=evidence, entities=_entities(), episode_id="ep1"
    )
    assert drafts[0].candidate_answer.short_answer == "ibuprofen"
    result = RuleBasedAnswerVerifier().verify_with_blueprint(
        drafts[0], blueprints[0], claims, evidence
    )
    assert result.verdict == "accepted"
    assert result.blocking is False
    assert "answer_is_seed" not in result.reason_codes


def test_verifier_still_rejects_an_answer_that_really_is_the_seed() -> None:
    evidence = [_edge(ASPIRIN, "exposure_disease", HEADACHE, "e1")]
    report = curate_evidence(evidence, seed_canonical_id=ASPIRIN.canonical_id or "")
    blueprints = QuestionPlanner().plan(
        report.curated,
        seed_canonical_id=ASPIRIN.canonical_id or "",
        question_count=1,
    )
    claims = RuleBasedClaimBuilder().build_from_curated(report.curated, 1)
    drafts = RuleBasedQuestionWriter().write(
        blueprints, evidence=evidence, entities=_entities(), episode_id="ep1"
    )
    leaked = drafts[0].model_copy(
        update={
            "candidate_answer": drafts[0].candidate_answer.model_copy(
                update={
                    "values": [{"canonical_id": ASPIRIN.canonical_id}],
                    "short_answer": ASPIRIN.canonical_name or "",
                }
            )
        }
    )
    # The blueprint endpoint check fires first for a single-hop answer; the
    # seed check itself is exercised through the base verify call.
    result = RuleBasedAnswerVerifier().verify_with_blueprint(
        leaked, blueprints[0], claims, evidence
    )
    assert result.blocking is True
    assert "answer_endpoint_mismatch" in result.reason_codes

    base = RuleBasedAnswerVerifier().verify(
        leaked, claims, evidence, focus_entity_id=ASPIRIN.canonical_id
    )
    assert base.blocking is True
    assert "answer_is_seed" in base.reason_codes

    # Without an explicit focus the answer cannot be judged against the seed.
    undecidable = RuleBasedAnswerVerifier().verify(leaked, claims, evidence)
    assert "answer_is_seed" not in undecidable.reason_codes


def test_verifier_rejects_answer_is_seed_for_outgoing_direction() -> None:
    """A wrong answer pointing back at the seed must be rejected either way."""
    evidence = [_edge(ASPIRIN, "exposure_disease", HEADACHE, "e1")]
    _, claims, blueprints, drafts = _pipeline(evidence)
    assert blueprints[0].answer_endpoint == "tail"
    result = RuleBasedAnswerVerifier().verify_with_blueprint(
        drafts[0], blueprints[0], claims, evidence
    )
    assert "answer_is_seed" not in result.reason_codes


def test_verifier_rejects_answer_leakage() -> None:
    evidence = [_edge(ASPIRIN, "exposure_disease", HEADACHE, "e1")]
    _, claims, _, drafts = _pipeline(evidence)
    leaked = drafts[0].model_copy(
        update={"question_text": drafts[0].question_text + " Answer: migraine?"}
    )
    result = RuleBasedAnswerVerifier().verify(leaked, claims, evidence)
    assert result.verdict == "rejected"
    assert result.blocking is True
    assert "answer_leakage" in result.reason_codes


def test_verifier_rejects_unknown_claim_reference() -> None:
    evidence = [_edge(ASPIRIN, "exposure_disease", HEADACHE, "e1")]
    _, claims, _, drafts = _pipeline(evidence)
    broken = drafts[0].model_copy(update={"claim_ids": ["claim.missing"]})
    result = RuleBasedAnswerVerifier().verify(broken, claims, evidence)
    assert result.blocking is True
    assert result.reason_codes == ["unknown_claim_reference"]


def test_verifier_rejects_unknown_evidence_reference() -> None:
    evidence = [_edge(ASPIRIN, "exposure_disease", HEADACHE, "e1")]
    _, claims, _, drafts = _pipeline(evidence)
    broken = drafts[0].model_copy(update={"evidence_ids": ["kg.missing"]})
    result = RuleBasedAnswerVerifier().verify(broken, claims, evidence)
    assert result.blocking is True
    assert result.reason_codes == ["unknown_evidence_reference"]


def test_relation_semantics_classification() -> None:
    policy = RelationSemanticsPolicy()
    assert policy.classify("exposure_disease") is RelationSemantics.DIRECTIONAL_FACT
    assert policy.classify("disease_protein") is RelationSemantics.ASSOCIATION
    assert policy.classify("exposure_protein") is RelationSemantics.GENERIC_RELATION


def test_unknown_relation_falls_back_to_generic() -> None:
    policy = RelationSemanticsPolicy()
    assert policy.is_known("totally_new_relation") is False
    assert policy.classify("totally_new_relation") is RelationSemantics.GENERIC_RELATION


def test_every_primekg_snapshot_relation_is_covered() -> None:
    from medical_qa_data_factory.generation.semantics import (
        ASSOCIATION,
        DIRECTIONAL_FACT,
        GENERIC_RELATION,
    )

    relations = {
        "anatomy_anatomy", "anatomy_protein_absent", "anatomy_protein_present",
        "bioprocess_bioprocess", "bioprocess_protein", "cellcomp_cellcomp",
        "cellcomp_protein", "contraindication", "disease_disease",
        "disease_phenotype_negative", "disease_phenotype_positive",
        "disease_protein", "drug_drug", "drug_effect", "drug_protein",
        "exposure_bioprocess", "exposure_cellcomp", "exposure_disease",
        "exposure_exposure", "exposure_molfunc", "exposure_protein",
        "indication", "molfunc_molfunc", "molfunc_protein", "off-label use",
        "pathway_pathway", "pathway_protein", "phenotype_phenotype",
        "phenotype_protein", "protein_protein",
    }
    covered = ASSOCIATION | DIRECTIONAL_FACT | GENERIC_RELATION
    assert relations <= covered
    assert not (ASSOCIATION & DIRECTIONAL_FACT)
    assert not (ASSOCIATION & GENERIC_RELATION)
    assert not (DIRECTIONAL_FACT & GENERIC_RELATION)


def test_association_with_causal_wording_is_blocking() -> None:
    policy = RelationSemanticsPolicy()
    code, message = check_relation_semantics_safety(
        "Which protein does disease_protein cause for aspirin?", "disease_protein", policy
    )
    assert code == "relation_semantics_violation"
    assert message


def test_association_with_treatment_wording_is_blocking() -> None:
    policy = RelationSemanticsPolicy()
    code, _ = check_relation_semantics_safety(
        "Which drug treats this disease?", "disease_protein", policy
    )
    assert code == "relation_semantics_violation"


def test_directional_relation_keeps_its_own_wording() -> None:
    policy = RelationSemanticsPolicy()
    code, _ = check_relation_semantics_safety(
        "Which disease does aspirin treat?", "indication", policy
    )
    assert code is None


def test_structured_writer_generates_three_style_variants() -> None:
    evidence = [_edge(ASPIRIN, "exposure_disease", HEADACHE, "e1")]
    report = curate_evidence(
        evidence, seed_canonical_id=ASPIRIN.canonical_id or ""
    )
    blueprints = QuestionPlanner().plan(
        report.curated,
        seed_canonical_id=ASPIRIN.canonical_id or "",
        question_count=1,
    )
    model = FakeStructuredModel()

    def writer(request):
        payload = request.payload
        profile = payload["style"]["profile"]
        questions = {
            "short_search": "阿司匹林和哪种疾病有关？",
            "conversational": "我想知道阿司匹林和哪种疾病有关。",
            "contextual": "我在查看阿司匹林资料，想确认它和哪种疾病有关。",
        }
        return {
            "question_text": questions[profile],
            "answer_entity_id": payload["candidate_answer"][
                "canonical_entity_id"
            ],
            "kg_evidence_ids": payload["blueprint"]["required_evidence_ids"],
            "literature_evidence_ids": [],
            "question_type": "single_fact",
            "language": "zh-CN",
        }

    model.set_response("question_writer", writer)
    drafts = StructuredQuestionWriter(model).write(
        blueprints,
        evidence=evidence,
        entities=_entities(),
        episode_id="ep-style",
        generation_spec=GenerationSpec(
            question_count=1,
            question_style_ids=["layperson-search-v1"],
            variants_per_blueprint=3,
        ),
    )
    assert len(drafts) == 3
    assert {item.style_skill_id for item in drafts} == {
        "layperson-search-v1"
    }
    assert len({item.style_profile for item in drafts}) == 3
    requests = model.calls_for("question_writer")
    assert all(item.skill_instruction for item in requests)
    assert all(
        "Layperson Search Query" in (item.skill_instruction or "")
        for item in requests
    )


def test_style_validator_blocks_answer_and_technical_leakage() -> None:
    evidence = [_edge(ASPIRIN, "exposure_disease", HEADACHE, "e1")]
    report = curate_evidence(
        evidence, seed_canonical_id=ASPIRIN.canonical_id or ""
    )
    blueprint = QuestionPlanner().plan(
        report.curated,
        seed_canonical_id=ASPIRIN.canonical_id or "",
        question_count=1,
    )[0]
    draft = QuestionDraft(
        draft_id="draft.leak",
        blueprint_id=blueprint.blueprint_id,
        question_text="PrimeKG 中 migraine 的 exposure_disease 是什么？",
        candidate_answer={
            "answer_mode": "entity",
            "values": [
                {
                    "canonical_id": HEADACHE.canonical_id,
                    "canonical_name": "migraine",
                }
            ],
            "short_answer": "migraine",
        },
        claim_ids=list(blueprint.claim_ids),
        evidence_ids=list(blueprint.required_evidence_ids),
        generation_episode_id="ep1",
    )
    reasons, penalty = deterministic_style_issues(
        draft,
        blueprint,
        load_question_skill("layperson-search-v1"),
    )
    assert "technical_jargon" in reasons
    assert "answer_leakage" in reasons
    assert penalty > 0


def test_style_validator_blocks_node_type_phrasing() -> None:
    evidence = [_edge(ASPIRIN, "exposure_disease", HEADACHE, "e1")]
    report = curate_evidence(
        evidence, seed_canonical_id=ASPIRIN.canonical_id or ""
    )
    blueprint = QuestionPlanner().plan(
        report.curated,
        seed_canonical_id=ASPIRIN.canonical_id or "",
        question_count=1,
    )[0]
    draft = QuestionDraft(
        draft_id="draft.node-type",
        blueprint_id=blueprint.blueprint_id,
        question_text="阿司匹林这种暴露因素与哪种疾病有关？",
        candidate_answer={
            "answer_mode": "entity",
            "values": [
                {
                    "canonical_id": HEADACHE.canonical_id,
                    "canonical_name": "migraine",
                }
            ],
            "short_answer": "migraine",
        },
        claim_ids=list(blueprint.claim_ids),
        evidence_ids=list(blueprint.required_evidence_ids),
        generation_episode_id="ep1",
    )
    reasons, _ = deterministic_style_issues(
        draft,
        blueprint,
        load_question_skill("layperson-search-v1"),
    )
    assert "technical_jargon" in reasons


def test_style_judge_payload_does_not_receive_the_answer() -> None:
    evidence = [_edge(ASPIRIN, "exposure_disease", HEADACHE, "e1")]
    report = curate_evidence(
        evidence, seed_canonical_id=ASPIRIN.canonical_id or ""
    )
    blueprint = QuestionPlanner().plan(
        report.curated,
        seed_canonical_id=ASPIRIN.canonical_id or "",
        question_count=1,
    )[0]
    draft = QuestionDraft(
        draft_id="draft.style",
        blueprint_id=blueprint.blueprint_id,
        question_text="阿司匹林暴露和哪种疾病有关？",
        candidate_answer={
            "answer_mode": "entity",
            "values": [
                {
                    "canonical_id": HEADACHE.canonical_id,
                    "canonical_name": "migraine",
                }
            ],
            "short_answer": "migraine",
        },
        claim_ids=list(blueprint.claim_ids),
        evidence_ids=list(blueprint.required_evidence_ids),
        generation_episode_id="ep1",
        style_skill_id="layperson-search-v1",
        style_skill_version="1.2.0",
        style_profile="short_search",
    )
    model = FakeStructuredModel()
    model.set_response(
        "style_judge",
        {
            "naturalness_score": 0.9,
            "persona_fit_score": 0.9,
            "intent_preserved": True,
            "unsupported_details": False,
            "technical_jargon": False,
            "answer_leaked": False,
            "reason_codes": [],
            "feedback": "ok",
        },
    )

    result = StructuredStyleJudge(model).assess(draft, blueprint)

    assert result.score > 0.8
    payload = model.payloads_for("style_judge")[0]
    serialized = repr(payload)
    assert "migraine" not in serialized
    assert HEADACHE.canonical_id not in serialized


def test_unknown_relation_with_neutral_wording_passes() -> None:
    policy = RelationSemanticsPolicy()
    code, _ = check_relation_semantics_safety(
        'Which entity is recorded as the tail of the "brand_new" edge whose head is aspirin?',
        "brand_new",
        policy,
    )
    assert code is None


def test_planner_respects_question_count() -> None:
    edges = [
        _edge(ASPIRIN, "contraindication", HEADACHE, "a0"),
        _edge(ASPIRIN, "drug_effect", IBUPROFEN, "b0"),
        _edge(ASPIRIN, "exposure_protein", BRAP, "c0"),
    ]
    report = curate_evidence(edges, seed_canonical_id=ASPIRIN.canonical_id or "")
    blueprints = QuestionPlanner().plan(
        report.curated, seed_canonical_id=ASPIRIN.canonical_id or "", question_count=2
    )
    assert len(blueprints) == 2
    assert len({item.claim_ids[0] for item in blueprints}) == 2


def test_planner_rotates_relations() -> None:
    edges = [
        _edge(ASPIRIN, "contraindication", HEADACHE, f"a{i}") for i in range(3)
    ] + [_edge(ASPIRIN, "drug_effect", IBUPROFEN, f"b{i}") for i in range(3)]
    report = curate_evidence(edges, seed_canonical_id=ASPIRIN.canonical_id or "")
    blueprints = QuestionPlanner().plan(
        report.curated, seed_canonical_id=ASPIRIN.canonical_id or "", question_count=4
    )
    assert len(blueprints) == 4


def test_default_generation_is_single_fact_only() -> None:
    evidence = [_edge(ASPIRIN, "exposure_disease", HEADACHE, "e1")]
    _, _, blueprints, _ = _pipeline(evidence)
    assert all(item.question_type == "single_fact" for item in blueprints)
    assert all(item.expected_route == "kg" for item in blueprints)


def test_fake_model_is_shared_but_role_isolated() -> None:
    """Writer and verifier share one model instance, different roles/contexts."""
    model = FakeStructuredModel()
    assert isinstance(model, LanguageModelPort)
    writer_request = StructuredGenerationRequest(
        invocation_id="inv-w",
        role="question_writer",
        system_prompt_version="writer-v1",
        payload={"blueprint_id": "bp1"},
        idempotency_key="ep1|write|bp1|1",
    )
    verifier_request = StructuredGenerationRequest(
        invocation_id="inv-v",
        role="answer_verifier",
        system_prompt_version="verifier-v1",
        payload={"draft_id": "d1"},
        idempotency_key="ep1|verify|d1|1",
    )
    model.set_response("question_writer", QuestionDraft(
        draft_id="d1",
        blueprint_id="bp1",
        question_text="q",
        candidate_answer={"answer_mode": "entity", "values": [], "short_answer": "a"},
        claim_ids=["c1"],
        evidence_ids=["e1"],
        generation_episode_id="ep1",
    ))
    model.set_response("answer_verifier", {"draft_id": "d1", "verdict": "accepted"})

    model.generate_structured(writer_request, QuestionDraft)
    model.generate_structured(verifier_request, VerificationResult)

    assert len(model.calls_for("question_writer")) == 1
    assert len(model.calls_for("answer_verifier")) == 1
    assert model.payloads_for("question_writer") != model.payloads_for("answer_verifier")
    assert len(model.usage_records) == 2


def test_fake_model_refuses_unknown_role() -> None:
    from medical_qa_data_factory.llm.contracts import StructuredGenerationRequest as Req

    model = FakeStructuredModel()
    request = Req(
        invocation_id="inv1",
        role="claim_builder",
        system_prompt_version="v1",
        payload={},
        idempotency_key="k",
    )
    try:
        model.generate_structured(request, Claim)
    except KeyError as exc:
        assert "claim_builder" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("unknown role must not silently succeed")

import json
from hashlib import sha256

from scripts.summarize_multientity_quality import build_manifest, summarize_run


def _artifact(artifact_id, kind, **payload):
    return {"artifact_id": artifact_id, "artifact_type": kind, "payload": payload}


def _saved_run(run, *, audit_id="review.new", missing_release=False):
    question = "Why is testing useful in this setting?"
    raw = b"Source title\nEvidence"
    artifacts = [
        _artifact("hyp.old", "question_hypothesis", question_intent=question),
        _artifact("hyp.new", "question_hypothesis", question_intent=question,
                  proposed_answer_values=["Unverified proposal"]),
        _artifact("draft", "objective_question_draft", draft_id="draft", contract_id="contract",
                  question_text=question, answer_values=["Published answer"]),
        _artifact("contract", "frozen_question_contract", resolution_id="resolution"),
        _artifact("resolution", "answer_resolution", hypothesis_id="hyp.new",
                  supporting_anchor_ids=["anchor"]),
        _artifact("review.old", "final_question_review", hypothesis_id="hyp.old", question=question,
                  review={"recommended_action": "revise"}),
        _artifact("review.new", "final_question_review", hypothesis_id="hyp.new", question=question,
                  review={"recommended_action": "accept", "support_directly_entails_answer": True}),
        _artifact("anchor", "evidence_anchor", anchor_id="anchor", document_id="pubmed:123",
                  material_id="source", start_offset=13, end_offset=21, quoted_text="Evidence",
                  quote_hash=sha256(b"Evidence").hexdigest()),
        _artifact("source", "evidence_material", material_id="source", provider_id="pubmed",
                  source_record_id="pmid:123", raw_content_ref="raw.txt", content_hash=sha256(raw).hexdigest()),
        _artifact("proposal", "react_publication_proposal", hypothesis_id="hyp.old"),
    ]
    if not missing_release:
        artifacts.append(_artifact(
            "report", "objective_validation_report", report_id="report", question_id="draft",
            verdict="PASS", release_allowed=True, answer_audit_id=audit_id,
            evidence_used=["pubmed:123"], input_digest="published-digest",
        ))
    state = {
        "run_id": "run", "goal": {"target_entity": "entity", "question_count_upper_bound": 3},
        "hypotheses": {"hyp.old": "CLOSED", "hyp.new": "PUBLISHED"},
        "artifacts": {artifact["artifact_id"]: artifact for artifact in artifacts},
    }
    (run / "after.json").write_text(json.dumps(state), encoding="utf-8")
    (run / "provenance.json").write_text('{"replay":false,"checkpoint":"old/after.json"}', encoding="utf-8")
    (run / "raw.txt").write_bytes(raw)


def test_summary_links_published_text_to_its_exact_review_and_sources(tmp_path):
    _saved_run(tmp_path)
    run = summarize_run(tmp_path, {"run|hyp.new": {"label": "basic_usable", "note": "old"}})
    item, = run["questions"]

    assert run["published_question_count"] == 1
    assert run["target_question_count"] == 3
    assert run["candidate_version_count"] == 2
    assert run["unique_candidate_question_text_count"] == 1
    assert run["mode"] == "replay"
    assert item["answer"] == ["Published answer"]
    assert item["proposal_answer_differs"] is True
    assert [review["artifact_id"] for review in item["final_reviews"]] == ["review.new"]
    assert item["sources"][0]["source_record_id"] == "pmid:123"
    assert item["traceability_gaps"] == []
    assert item["manual_assessment"] is None
    assert item["unbound_assessment"]["note"] == "old"
    assert build_manifest([run])["assessment_counts"] == {
        "question": {"unassessed": 1}, "answer": {"unassessed": 1},
    }


def test_summary_records_stale_audit_and_counts_bound_labels_separately(tmp_path):
    _saved_run(tmp_path, audit_id="review.old")
    review = {
        "publication_input_digest": "published-digest",
        "question": {"label": "basic_usable"},
        "answer": {"label": "not_usable"},
    }
    run = summarize_run(tmp_path, {"run|hyp.new": review})
    item, = run["questions"]

    assert item["final_reviews"] == []
    assert item["traceability_gaps"] == ["Missing or mismatched final review review.old"]
    assert build_manifest([run])["assessment_counts"] == {
        "question": {"basic_usable": 1}, "answer": {"not_usable": 1},
    }
    assert build_manifest([run])["pair_quality_by_run_mode"] == {
        "replay": {"runs": 1, "published": 1, "not_usable": 1},
    }


def test_published_state_without_committed_report_is_a_gap(tmp_path):
    _saved_run(tmp_path, missing_release=True)
    run = summarize_run(tmp_path, {})

    assert run["published_question_count"] == 0
    assert run["hypothesis_state_counts"]["PUBLISHED"] == 1
    assert run["traceability_gaps"] == ["PUBLISHED hypothesis hyp.new has 0 linked release reports"]


def test_replay_does_not_count_an_inherited_release_as_new(tmp_path):
    _saved_run(tmp_path)
    (tmp_path / "step-01-before.json").write_bytes((tmp_path / "after.json").read_bytes())
    run = summarize_run(tmp_path, {})
    assert run["published_question_count"] == 1
    assert run["newly_published_question_count"] == 0
    assert run["questions"][0]["inherited_from_checkpoint"] is True
    assert build_manifest([run])["published_question_count"] == 0
    assert build_manifest([run])["pair_quality_by_run_mode"] == {"replay": {"runs": 1}}


def test_source_drift_is_reported_even_when_the_file_and_citation_are_present(tmp_path):
    _saved_run(tmp_path)
    (tmp_path / "raw.txt").write_bytes(b"Source title\nReplaced")
    item, = summarize_run(tmp_path, {})["questions"]
    assert item["traceability_gaps"] == [
        "Local source content hash does not match source",
        "Source text does not match published evidence anchor anchor",
    ]

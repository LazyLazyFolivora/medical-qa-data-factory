"""Resolve selected original passages for direct question design."""
from .objective_question_contracts import EvidenceAnchor, canonical_digest, text_digest
from .research_tools import add_section_context, material_passages
from .subagent_contracts import RoleContractViolation

SELECTED_EVIDENCE_CHAR_BUDGET = 32000


def completed_research_evidence(snapshot, report_id):
    """Select a research handoff's originals, never its model-written summary."""
    artifact = snapshot.artifacts.get(report_id)
    if (artifact is None or artifact.artifact_type != "react_observation"
            or artifact.payload.get("status") != "research_guidance_not_verified_evidence"
            or not artifact.payload.get("source_passages")):
        raise RoleContractViolation("Select a research_evidence report with selected original passages, not a discovery opportunity or an empty report")
    return {"evidence_research_id": report_id, "source_passages": artifact.payload["source_passages"]}


def _design_anchor(snapshot, material_id, document_id, excerpt):
    """A passage has one identity whether read whole or selected by paragraph."""
    quote, offset = excerpt["text"], excerpt["offset"]
    anchor_id = "anchor.design." + canonical_digest([material_id, offset, quote])[:24]
    anchor = EvidenceAnchor(
        anchor_id=anchor_id, material_id=material_id, document_id=document_id,
        section_or_field=f"design passage at {offset}",
        start_offset=offset, end_offset=offset + len(quote), quoted_text=quote,
        quote_hash=text_digest(quote))
    existing = snapshot.artifacts.get(anchor_id)
    if existing is not None:
        saved = EvidenceAnchor.model_validate(existing.payload)
        if saved.model_dump(exclude={"section_or_field"}) != anchor.model_dump(exclude={"section_or_field"}):
            raise RoleContractViolation("Saved design anchor conflicts with the selected original passage")
        # Preserve old display labels when resuming existing runs. Paragraph IDs
        # are navigation metadata; offsets and quote bytes determine identity.
        return saved
    return anchor


def research_source_anchors(loop, snapshot, context):
    """Resolve saved synthesis excerpts against original bytes, never model notes."""
    from .objective_question_contracts import EvidenceMaterial
    anchors = []
    total = 0
    for source in context["source_passages"]:
        aid = source["material_id"]
        material = EvidenceMaterial.model_validate(snapshot.artifacts[aid].payload)
        raw = loop.persistence.load_raw_content(material).decode("utf-8")
        original = next((a for a in snapshot.artifacts.values()
                         if a.artifact_type == "evidence_anchor" and a.payload["material_id"] == aid), None)
        if original is None or not source.get("excerpts"):
            raise RoleContractViolation("Research has no original evidence; read source paragraphs and synthesize again")
        for excerpt in source["excerpts"]:
            quote, offset = excerpt["text"], excerpt["offset"]
            if not quote or raw[offset:offset + len(quote)] != quote:
                raise RoleContractViolation("Research excerpt no longer matches persisted source; read and synthesize again")
            total += len(quote)
            if total > SELECTED_EVIDENCE_CHAR_BUDGET:
                raise RoleContractViolation(f"Research passages total {total} characters, exceeding {SELECTED_EVIDENCE_CHAR_BUDGET}; select fewer passages")
            anchors.append(_design_anchor(snapshot, aid, original.payload["document_id"], excerpt))
    if not anchors:
        raise RoleContractViolation("Research has no passages; read original text and synthesize again")
    return anchors


def candidate_research_context(snapshot, hypothesis_id):
    """Host-saved context belongs to this exact candidate, not the latest research."""
    artifact = snapshot.artifacts.get(f"candidate_context.{hypothesis_id}")
    return artifact.payload.get("research_context") if artifact else None


def design_source_anchors(loop, snapshot, sections, *, section_context_out=None):
    anchors = []
    total = 0
    views = []
    for material_id, paragraph_ids in sections.items():
        # Assembly may combine several reading pages; apply the shared evidence
        # budget rather than reapplying the interactive reading-page limit.
        unique_ids = list(dict.fromkeys(paragraph_ids or []))
        view = material_passages(loop, snapshot, material_id,
            None if paragraph_ids is None else unique_ids,
            max_paragraphs=None, max_chars=SELECTED_EVIDENCE_CHAR_BUDGET)
        if not view["excerpts"]:
            raise RoleContractViolation("Read the material index and select original paragraph IDs first")
        views.append(view)
    views = add_section_context(loop, snapshot, views, max_chars=SELECTED_EVIDENCE_CHAR_BUDGET)
    if section_context_out is not None:
        section_context_out.extend({"material_id": view["material_id"], **view["section_context"]}
                                  for view in views)
    for view in views:
        material_id, excerpts = view["material_id"], view["excerpts"]
        original = next((a for a in snapshot.artifacts.values()
                         if a.artifact_type == "evidence_anchor" and a.payload["material_id"] == material_id), None)
        if original is None:
            raise RoleContractViolation("Material has no persisted source identity")
        for excerpt in excerpts:
            quote, offset = excerpt["text"], excerpt["offset"]
            total += len(quote)
            if total > SELECTED_EVIDENCE_CHAR_BUDGET:
                raise RoleContractViolation(f"Selected design passages total {total} characters, exceeding {SELECTED_EVIDENCE_CHAR_BUDGET}; select fewer passages")
            anchors.append(_design_anchor(snapshot, material_id, original.payload["document_id"], excerpt))
    return anchors

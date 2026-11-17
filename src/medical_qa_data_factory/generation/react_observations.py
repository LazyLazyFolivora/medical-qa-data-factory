"""Readable projections of stored tool results, without selecting research routes."""
import json


class ObservationView:
    def __init__(self, snapshot):
        self.artifacts = snapshot.artifacts
        self.anchors = {}
        self.fulltexts = {}
        self.fulltext_attempts = {}
        sequences = {a.parent_decision_id: a.payload.get("sequence", 0)
                     for a in snapshot.artifacts.values() if a.artifact_type == "react_step"
                     and hasattr(a, "parent_decision_id")}
        for artifact in self.artifacts.values():
            if artifact.artifact_type == "evidence_anchor":
                self.anchors.setdefault(artifact.payload.get("material_id"), []).append(artifact.payload)
            if (artifact.artifact_type == "evidence_material" and artifact.payload.get("provider_id") == "pubmed"
                    and artifact.payload.get("material_type") == "literature_full_text"):
                self.fulltexts.setdefault(artifact.payload.get("source_record_id"), []).append(artifact.artifact_id)
            if artifact.artifact_type == "react_observation":
                sequence = sequences.get(getattr(artifact, "parent_decision_id", None), artifact.payload.get("sequence", 0))
                for article in artifact.payload.get("articles", []):
                    sid = article.get("source_record_id")
                    if sid and sequence >= self.fulltext_attempts.get(sid, (-1, None))[0]:
                        self.fulltext_attempts[sid] = (sequence, article.get("acquisition_status"))

    def paper_access(self, material_id):
        """Navigation is host-owned metadata, outside truncated source previews."""
        material = self.artifacts.get(material_id)
        if material is None or material.artifact_type != "evidence_material":
            return {}
        p = material.payload
        if p.get("provider_id") != "pubmed" or p.get("material_type") != "literature_abstract":
            return {}
        saved = self.fulltexts.get(p.get("source_record_id"), [])
        attempt = self.fulltext_attempts.get(p.get("source_record_id"))
        status = "saved" if saved else "not_acquired_on_last_attempt" if attempt and attempt[1] == "not_acquired" else "not_confirmed"
        tool = "read_artifacts" if saved else "read_source_details"
        return {"paper_access": {
            "current_content": "abstract_only", "full_text_status": status,
            "saved_full_text_ids": saved,
            "next_tool": tool, "input_artifact_ids": saved[:1] if saved else [material_id],
            "guidance": ("Full text is saved, not necessarily read. Inspect its index and select relevant paragraphs."
                if saved else "Full-text availability is not guaranteed. If this paper is relevant but the abstract omits "
                "needed methods, comparators, endpoints or mechanism evidence, call read_source_details with these IDs "
                "and parameters.evidence_need describing that gap. After acquisition, read relevant paragraphs. "
                "If a previous attempt failed, inspect its diagnostics before retrying. An explicit abstract fact may suffice.")}}

    def content(self, artifact):
        """Put evidence and actual outputs before storage identifiers in previews."""
        p, kind = artifact.payload, artifact.artifact_type
        if kind == "react_observation" and "source_views" in p:
            # Stored tool output stays complete. Send each original passage once.
            result = {k: v for k, v in p.items() if k != "artifact_id"}
            result["source_views"] = []
            for source in p["source_views"]:
                view = dict(source)
                view.update(self.paper_access(source.get("material_id")))
                if source.get("content"):
                    view["excerpts"] = [{k: v for k, v in excerpt.items() if k != "text"}
                                        for excerpt in source.get("excerpts", [])]
                    returned = set(source.get("returned_paragraph_ids", []))
                    view["paragraph_index"] = [
                        {k: v for k, v in entry.items() if k != "preview"}
                        if source.get("content_complete") or entry.get("paragraph_id") in returned else dict(entry)
                        for entry in source.get("paragraph_index", [])]
                    view["text_location"] = "Verbatim text appears once in content; excerpts retain location metadata only."
                result["source_views"].append(view)
            return result
        if kind == "evidence_material":
            return {"evidence_excerpt": "\n".join(
                a.get("quoted_text", "") for a in self.anchors.get(artifact.artifact_id, [])[:3])[:900],
                "provider": p.get("provider_id"), "source_record": p.get("source_record_id"),
                "material_type": p.get("material_type"), "purpose": artifact.metadata.get("purpose", "discovery")}
        if kind == "evidence_anchor":
            return {"evidence_excerpt": p.get("quoted_text", "")[:900],
                    "section": p.get("section_or_field"), "material_id": p.get("material_id")}
        if kind == "candidate_claim":
            return {key: p.get(key) for key in ("subject", "predicate", "object_or_value", "qualifiers",
                    "claim_strength", "supporting_anchor_ids")}
        if kind == "question_hypothesis":
            return {key: p.get(key) for key in ("question_intent", "proposed_answer_values",
                    "proposed_public_scope", "risk_flags", "candidate_claim_ids")}
        if kind == "provider_result":
            ids = p.get("material_ids", [])
            return {"status": p.get("terminal_status"), "query": p.get("request", {}).get("query"),
                    "returned_material_count": len(ids), "selection_status": p.get("usage", {}).get("selection_status"),
                    "error_category": p.get("error_category"),
                    "search_trace": p.get("usage", {}).get("search_trace", []),
                    "detail_trace": p.get("usage", {}).get("detail_trace", []),
                    "materials": [self.card(self.artifacts[aid], limit=450)
                                  for aid in ids[:12] if aid in self.artifacts],
                    "omitted_material_count": max(0, len(ids) - 12),
                    "instruction": "These are returned hits, not verified facts. The sample is in provider order, not a relevance ranking. "
                        "Read this provider_result by ID for all material IDs. Reading a literature_abstract material returns the abstract, "
                        "NOT the full paper. Each PubMed abstract's paper_access shows the separate full-text acquisition/reading route; "
                        "choose it when needed to resolve a concrete evidence gap."}
        return {key: value for key, value in p.items()
                if key not in {"artifact_id", "schema_version", "content_hash", "quote_hash", "raw_content_ref"}}

    def card(self, artifact, *, limit=360):
        raw = json.dumps(self.content(artifact), ensure_ascii=False)
        return {"id": artifact.artifact_id, "type": artifact.artifact_type,
                **self.source_identity(artifact),
                **self.paper_access(artifact.artifact_id),
                "preview": raw[:limit], "truncated": len(raw) > limit}

    def source_identity(self, artifact):
        """Source identity is structural metadata, never part of a truncated preview."""
        material = artifact
        if artifact.artifact_type == "evidence_anchor":
            material = self.artifacts.get(artifact.payload.get("material_id"))
        if material is None or material.artifact_type != "evidence_material":
            return {}
        return {"material_id": material.artifact_id,
                **{key: material.payload.get(key) for key in
                   ("provider_id", "source_record_id", "material_type")}}

    def reading_ledger(self):
        """Report stored tool-returned text, not comprehension or model-written progress."""
        ledger = {a.artifact_id: {**self.source_identity(a), "stored": True,
                  "returned_paragraph_ids": [], "whole_stored_content_returned": False}
                  for a in self.artifacts.values() if a.artifact_type == "evidence_material"}
        for artifact in self.artifacts.values():
            if artifact.artifact_type != "react_observation":
                continue
            for view in artifact.payload.get("source_views", []):
                entry = ledger.get(view.get("material_id"))
                if entry is None or view.get("read_error") or not view.get("content"):
                    continue
                ids = view.get("returned_paragraph_ids", [e["paragraph_id"] for e in
                    view.get("excerpts", []) if e.get("paragraph_id")])
                entry["returned_paragraph_ids"] = sorted(set(entry["returned_paragraph_ids"]) | set(ids))
                entry["whole_stored_content_returned"] |= bool(view.get("content_complete"))
                total = view.get("total_paragraphs")
                if isinstance(total, int) and total > 0:
                    entry["total_paragraphs"] = total
                    entry["whole_stored_content_returned"] |= len(entry["returned_paragraph_ids"]) == total
        return {"origin": "host", "materials": list(ledger.values()),
                "meaning": "Stored read-tool output only, not proof of comprehension or current context visibility. "
                "Stored does not mean read. Whole abstract is not whole paper. Index previews are not read paragraphs. "
                "Unlisted paragraphs have no recorded passage return; legacy coverage may be incomplete."}

    def recent(self, steps, *, max_chars=None):
        """Keep recent payloads within a character budget and retain older references."""
        observations, remaining = [], max_chars
        seen = set()
        for step in reversed(steps):
            step_artifact = self.artifacts.get(step.get("artifact_id"))
            ids = [aid for aid in step["result_ids"] if aid in self.artifacts]
            has_search = any(self.artifacts[aid].artifact_type == "provider_result" for aid in ids)
            for aid in sorted(ids, key=lambda aid: self.artifacts[aid].artifact_type != "provider_result"):
                if aid in seen:
                    continue
                seen.add(aid)
                artifact = self.artifacts[aid]
                if has_search and artifact.artifact_type in {"evidence_material", "evidence_anchor"}:
                    continue  # Already represented by the search result's readable sample.
                direct = step_artifact is None or artifact.parent_decision_id == step_artifact.parent_decision_id
                newest_direct = step is steps[-1] and direct
                payload = self.content(artifact) if remaining != 0 or newest_direct else None
                size = len(json.dumps(payload, ensure_ascii=False)) if remaining is not None and payload is not None else 0
                include = payload is not None and (remaining is None or newest_direct or size <= remaining)
                observations.append({"id": aid, "type": artifact.artifact_type, "step_sequence": step["sequence"],
                    "action": step.get("action"), "step_id": step.get("artifact_id"),
                    "direct_action_result": direct,
                    **self.source_identity(artifact),
                    **({"payload": payload} if include else
                       {"content_omitted": True, "omission_reason": "older_observation_budget",
                        "instruction": "Older output omitted from current context, not unread. "
                            "Consult reading_ledger; reread only for a specific evidence gap."})})
                if include and remaining is not None:
                    remaining = max(0, remaining - size)
        return observations

    def latest_step(self, steps):
        if not steps:
            return None
        step = steps[-1]
        ids = [aid for aid in step["result_ids"] if aid in self.artifacts]
        ids.sort(key=lambda aid: self.artifacts[aid].artifact_type != "provider_result")
        return {"sequence": step["sequence"], "action": step["action"], "step_id": step.get("artifact_id"),
                "result_count": len(ids), "omitted_result_count": max(0, len(ids) - 8),
                "results": [{"id": aid, "type": self.artifacts[aid].artifact_type} for aid in ids[:8]],
                "instruction": "Compare these actual results with your previous research note before choosing the next action. Planned revisions are not completed revisions; a returned result is not an exhausted search space."}

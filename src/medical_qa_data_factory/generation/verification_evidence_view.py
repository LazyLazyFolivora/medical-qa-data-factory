"""Read-only, request-scoped access to the evidence visible to a verifier."""
import json
from typing import Generic, TypeVar

from pydantic import Field

from ..domain.common import ContractModel
from .objective_question_contracts import canonical_digest


class ReadEvidence(ContractModel):
    read_refs: list[str] = Field(min_length=1, max_length=3)


class CompareExons(ContractModel):
    transcript_list_ref: str
    first_transcript_id: str
    second_transcript_id: str


Verdict = TypeVar("Verdict", bound=ContractModel)
MAX_VIEW_CALLS = 4


class EvidenceTurn(ContractModel, Generic[Verdict]):
    step: ReadEvidence | CompareExons | Verdict


class EvidenceView:
    def __init__(self):
        self.pages = {}

    def reference(self, anchor, path, value, offset=0):
        ref = f"{anchor}#{path}@{offset}"
        self.pages[ref] = (anchor, path, value, offset)
        return {"read_ref": ref, "size": len(value)}

    def page(self, anchor, path, value, offset=0):
        if isinstance(value, str):
            end = min(offset + 1000, len(value))
            page = {"text": value[offset:end], "offset": offset}
        else:
            limit = 20 if isinstance(value, dict) else 8
            end = min(offset + limit, len(value))
            keys = list(value)[offset:end] if isinstance(value, dict) else range(offset, end)
            fields = {}
            for key in keys:
                item = value[key]
                child = path + "/" + str(key).replace("~", "~0").replace("/", "~1")
                if (isinstance(item, (list, dict)) and item) or (isinstance(item, str) and len(item) > 200):
                    fields[str(key)] = self.reference(anchor, child, item)
                    if isinstance(item, dict) and isinstance(item.get("id"), str):
                        fields[str(key)]["id"] = item["id"][:128]
                else:
                    fields[str(key)] = item
            page = {"fields": fields}
        if end < len(value):
            page["next"] = self.reference(anchor, path, value, end)
        return page

    def shared_exons(self, query):
        ref = self.pages.get(query.transcript_list_ref)
        ids = [query.first_transcript_id, query.second_transcript_id]
        if not ref or not isinstance(ref[2], list) or ids[0] == ids[1]:
            return {"gap": "Select a visible transcript list and two distinct transcript IDs."}
        indexes = []
        for tid in ids:
            matches = [row for row in ref[2] if isinstance(row, dict) and row.get("id") == tid]
            exons = matches[0].get("Exon") if len(matches) == 1 else None
            if not isinstance(exons, list) or not exons or not all(
                isinstance(e, dict) and isinstance(e.get("id"), str) and e["id"] for e in exons
            ):
                return {"gap": "Both transcripts must have annotated exon IDs to compare."}
            indexes.append({e["id"]: e for e in exons})
        shared = sorted(indexes[0].keys() & indexes[1].keys())
        rows = [{"id": eid, "first": indexes[0][eid], "second": indexes[1][eid]} for eid in shared]
        anchor = ref[0]
        return {"anchor_id": anchor, "transcripts": ids, "criterion": "identical annotated exon ID",
                "distinct_exon_counts": [len(index) for index in indexes], "shared_count": len(shared),
                "shared_rows": self.page(anchor, "/shared-" + canonical_digest(ids)[:16], rows)}

    def initial(self, anchors):
        visible = []
        remaining = 10000
        for anchor in anchors:
            text = anchor.get("quoted_text", "")
            try:
                record = json.loads(text) if text.lstrip().startswith(("{", "[")) else None
            except ValueError:
                record = None
            # A transcript/row collection belongs behind a reference even on small records.
            structured_list = isinstance(record, list) and any(isinstance(row, (dict, list)) for row in record)
            nested_record = isinstance(record, dict) and any(isinstance(v, (dict, list)) and v for v in record.values())
            if len(text) <= min(3000, remaining) and not (structured_list or nested_record):
                visible.append(anchor)
                remaining -= len(text)
                continue
            aid = anchor["anchor_id"]
            item = {k: anchor[k] for k in ("anchor_id", "document_id", "section_or_field") if k in anchor}
            value = record if isinstance(record, (dict, list)) else text
            if remaining > 2000 and not structured_list:
                item["excerpt"] = self.page(aid, "", value)
                remaining -= len(json.dumps(item["excerpt"], ensure_ascii=False))
            else:
                item["available"] = self.reference(aid, "", value)
            visible.append(item)
        return visible


def generate_with_evidence(model, request, schema, field):
    """Answer immediately when possible, or open visible evidence on demand."""
    source = request.payload[field]
    nested = isinstance(source, dict)
    anchors = source["anchors"] if nested else source
    view = EvidenceView()
    visible = view.initial(anchors)
    payload = {**request.payload, field: {**source, "anchors": visible} if nested else visible}
    current = request.model_copy(update={"payload": payload})
    if not view.pages:
        return model.generate_structured(current, schema)
    opened = {}
    note = ("Decide from visible evidence values when sufficient. A read_ref identifies unseen evidence; "
            "its existence does not support or contradict an answer. To inspect a relevant field or next page, "
            "return read_refs in step. Otherwise return the requested assessment in step. "
            "To compare two named Ensembl transcripts by exon ID, supply transcript_list_ref, "
            "first_transcript_id and second_transcript_id in step. "
            "For missing evidence state the specific gap. Never assume omitted pages are empty or exhaustive.")
    usage = {"input_tokens": 0, "output_tokens": 0}
    stop_reading = False
    for turn in range(MAX_VIEW_CALLS):
        final = stop_reading or turn == MAX_VIEW_CALLS - 1
        next_payload = {**payload, "evidence_pages": opened.copy(),
                        "read_instruction": "Assess only visible values; report any remaining gap."
                                            if final else note}
        key = canonical_digest(next_payload)[:20]
        current = request.model_copy(update={"payload": next_payload,
            "invocation_id": f"{request.invocation_id}.view{turn}",
            "idempotency_key": f"{request.idempotency_key}|view|{key}"})
        response = model.generate_structured(current, schema if final else EvidenceTurn[schema])
        for name in usage:
            usage[name] += response.usage.get(name) or 0
        step = schema.model_validate(response.parsed) if final else EvidenceTurn[schema].model_validate(response.parsed).step
        if isinstance(step, CompareExons):
            name = "exon_comparison_" + canonical_digest(step.model_dump())[:16]
            stop_reading = name in opened
            opened[name] = view.shared_exons(step)
            continue
        if not isinstance(step, ReadEvidence):
            return response.model_copy(update={"parsed": step.model_dump(mode="json"),
                "usage": {**response.usage, **usage, "model_calls": turn + 1}})
        fresh = [ref for ref in dict.fromkeys(step.read_refs) if ref in view.pages and ref not in opened]
        stop_reading = not fresh
        for ref in fresh:
            opened[ref] = view.page(*view.pages[ref])

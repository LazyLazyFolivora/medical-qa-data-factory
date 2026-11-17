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

    def scalar_fields(self, record, budget):
        """Expose bounded exact scalar values through object wrappers, never arrays."""
        fields, visited = {}, 0
        def walk(value, path, depth):
            nonlocal visited
            if depth > 8 or visited >= 128:
                return
            for key, item in value.items():
                if visited >= 128:
                    break
                visited += 1
                child = path + "/" + str(key).replace("~", "~0").replace("/", "~1")
                if isinstance(item, dict):
                    walk(item, child, depth + 1)
                elif item is None or isinstance(item, (bool, int, float, str)):
                    # Narrative database summaries are scalar evidence too. The
                    # shared character budget bounds them; a per-string cutoff
                    # hid biological content while exposing only short metadata.
                    candidate = {**fields, child: item}
                    if len(json.dumps(candidate, ensure_ascii=False)) <= budget:
                        fields[child] = item
        walk(record, "", 0)
        return fields

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

    def collection_refs(self, anchor, record, budget):
        """Index collections, including exact values only for tiny flat tables."""
        refs, visited = {}, 0

        def scalar(value):
            return value is None or isinstance(value, (str, bool, int, float))

        def walk(value, path, depth):
            nonlocal visited
            if depth > 8:
                return
            for key, item in value.items():
                if visited >= 128 or len(refs) >= 16:
                    return
                visited += 1
                child = path + "/" + str(key).replace("~", "~0").replace("/", "~1")
                if isinstance(item, dict):
                    walk(item, child, depth + 1)
                elif isinstance(item, list) and item:
                    ref = {"read_ref": f"{anchor}#{child}@0", "size": len(item)}
                    if len(item) <= 8 and all(
                        scalar(row) or (isinstance(row, dict) and len(row) <= 20
                                        and all(scalar(v) for v in row.values()))
                        for row in item
                    ) and len(json.dumps(item, ensure_ascii=False)) <= 500:
                        ref["complete_value"] = item
                    if len(json.dumps({**refs, child: ref}, ensure_ascii=False)) > budget:
                        ref.pop("complete_value", None)
                    if len(json.dumps({**refs, child: ref}, ensure_ascii=False)) <= budget:
                        self.reference(anchor, child, item)
                        refs[child] = ref

        walk(record, "", 0)
        return refs

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

    def initial(self, anchors, *, verbatim_text_budget=0):
        visible = []
        remaining = 10000
        inline_selected_text = (verbatim_text_budget > 0 and
                                sum(len(a.get("quoted_text", "")) for a in anchors) <= verbatim_text_budget)
        for anchor in anchors:
            text = anchor.get("quoted_text", "")
            try:
                record = json.loads(text) if text.lstrip().startswith(("{", "[")) else None
            except ValueError:
                record = None
            # Already selected paper passages are a bounded evidence packet,
            # not a whole database record. Keep writer and verifier views equal.
            # Structured records still use the field/exon navigation below.
            if inline_selected_text and record is None:
                visible.append(anchor)
                continue
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
            if isinstance(record, dict) and remaining > 1000:
                fields = self.scalar_fields(record, min(4000, remaining - 1000))
                if fields:
                    item["visible_scalar_fields"] = fields
                    item["scalar_coverage"] = "Selected exact values at JSON Pointer paths, not exhaustive. Use read_refs for omitted fields and arrays."
                    remaining -= len(json.dumps(fields, ensure_ascii=False))
                refs = self.collection_refs(aid, record, max(0, min(2000, remaining - 1000)))
                if refs:
                    item["collection_refs"] = refs
                    remaining -= len(json.dumps(refs, ensure_ascii=False))
            if remaining > 2000 and not structured_list:
                item["excerpt"] = self.page(aid, "", value)
                remaining -= len(json.dumps(item["excerpt"], ensure_ascii=False))
            else:
                item["available"] = self.reference(aid, "", value)
            visible.append(item)
        return visible


def generate_with_evidence(model, request, schema, field, *, verbatim_text_budget=0):
    """Answer immediately when possible, or open visible evidence on demand."""
    source = request.payload[field]
    nested = isinstance(source, dict)
    anchors = source["anchors"] if nested else source
    view = EvidenceView()
    visible = view.initial(anchors, verbatim_text_budget=verbatim_text_budget)
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

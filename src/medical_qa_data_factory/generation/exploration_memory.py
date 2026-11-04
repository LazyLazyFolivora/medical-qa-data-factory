"""Private, incremental handoff notes for the outer search planner."""
import json
from pathlib import Path
from tempfile import NamedTemporaryFile

from pydantic import Field

from ..domain.common import ContractModel
from ..llm.contracts import StructuredGenerationRequest
from .objective_question_contracts import canonical_digest


HISTORY_FIELDS = ("prior_searches", "search_results", "facts", "candidate_reviews")


class HistoryNote(ContractModel):
    note: str = Field(min_length=1, max_length=1600)


def _save(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False, mode="w", encoding="utf-8") as handle:
        temporary = Path(handle.name)
        try:
            json.dump(state, handle, ensure_ascii=False)
        except BaseException:
            handle.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_exploration_history(model, request, schema, root, run_id):
    exceeds = getattr(model, "needs_history_compaction", None)
    if exceeds is None or request.payload.get("stage") != "exploration":
        return request
    path = Path(root) / "objective_runtime" / "exploration_memory" / (canonical_digest(run_id) + ".json")
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        saved = {}

    records = {canonical_digest([field, value]): (field, value)
               for field in HISTORY_FIELDS for value in request.payload.get(field, [])}
    covered = set(saved.get("covered", []))
    if not covered.issubset(records):
        saved, covered = {}, set()
    note = saved.get("note", "")
    # Remember first observation order across snapshots (snapshot keys may be hash-sorted).
    order = dict.fromkeys(key for key in saved.get("order", []) if key in records)
    order.update(dict.fromkeys(records))
    pending = [key for key in order if key not in covered]

    def planner_request():
        if not note:
            return request
        payload = {**request.payload, **{field: [] for field in HISTORY_FIELDS}, "exploration_history_note": note}
        for key in pending:
            field, value = records[key]
            payload[field].append(value)
        return request.model_copy(update={"payload": payload})

    def summary_request(batch, previous_note):
        data = {"previous_note": previous_note, "records": batch}
        digest = canonical_digest(data)
        return StructuredGenerationRequest(role="objective_history_summary", system_prompt_version="history-v1",
            invocation_id=f"{request.invocation_id}.summary.{digest[:16]}",
            idempotency_key=f"{run_id}|history-v1|{digest}", payload=data)

    def checkpoint():
        state = {"note": note, "covered": sorted(covered), "order": list(order)}
        if state != saved:
            _save(path, state)
            saved.update(state)

    active = planner_request()
    while pending and exceeds(active, schema):
        # Take the oldest prefix that fits a summary call, leaving newer items verbatim.
        prior = set(saved.get("order", []))
        older = sum(key in prior for key in pending)
        low, high = 0, min(older or len(pending), 32)
        while low < high:
            size = (low + high + 1) // 2
            batch = [records[key] for key in pending[:size]]
            if exceeds(summary_request(batch, note), HistoryNote):
                high = size - 1
            else:
                low = size
        keys = pending[:low or 1]
        if low:
            summary = summary_request([records[key] for key in keys], note)
            note = HistoryNote.model_validate(model.generate_structured(summary, HistoryNote).parsed).note
        else:
            # Split only the oversized historical record. Keep the original until every part succeeds.
            field, value = records[keys[0]]
            raw = json.dumps(value, ensure_ascii=False)
            offset, partial = 0, note
            while offset < len(raw):
                left, right = 0, len(raw) - offset
                while left < right:
                    width = (left + right + 1) // 2
                    fragment = [field, {"record": keys[0], "excerpt": raw[offset:offset + width]}]
                    if exceeds(summary_request([fragment], partial), HistoryNote):
                        right = width - 1
                    else:
                        left = width
                if not left:
                    return active
                fragment = [field, {"record": keys[0], "excerpt": raw[offset:offset + left]}]
                summary = summary_request([fragment], partial)
                partial = HistoryNote.model_validate(model.generate_structured(summary, HistoryNote).parsed).note
                offset += left
            note = partial
        covered.update(keys)
        del pending[:len(keys)]
        checkpoint()
        active = planner_request()
    checkpoint()
    return active

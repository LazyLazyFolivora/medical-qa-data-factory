"""Private, incremental handoff notes for the outer search planner."""
import json
from pathlib import Path
from tempfile import NamedTemporaryFile

from pydantic import Field

from ..domain.common import ContractModel
from ..llm.contracts import StructuredGenerationRequest
from .objective_question_contracts import canonical_digest


HISTORY_FIELDS = ("prior_searches", "search_results", "facts", "artifact_index", "candidate_reviews")


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
    react = request.payload.get("control_mode") == "objective-react-v1"
    # Current review feedback is actionable state, not expendable conversation.
    fields = ("latest_observations", "facts", "research_history") if react else HISTORY_FIELDS
    # Old mixed execution/research notes remain on disk for audit, never seed the new memory.
    version = "conversation-v6" if react else "history-v1"
    path = Path(root) / "objective_runtime" / "exploration_memory" / (canonical_digest(run_id) + (".conversation-v6" if react else "") + ".json")
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        saved = {}

    observations = request.payload.get("latest_observations", []) if react else []
    latest_sequence = max((v.get("step_sequence", 0) for v in observations), default=0)
    # A delegated research action may contain dozens of completed child calls.
    # Preserve its returned handoff, not every intermediate call as an indivisible
    # "latest" turn. Child originals remain stored and accessible by reference.
    protected = [v for v in observations if v.get("step_sequence", 0) == latest_sequence
                 and v.get("direct_action_result", True)]
    records = {}
    for field in fields:
        values = request.payload.get(field, [])
        if field == "latest_observations":
            values = sorted((v for v in values if v not in protected),
                            key=lambda v: v.get("step_sequence", 0))
        for value in values:
            records[canonical_digest([field, value])] = (field, value)
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
        payload = {**request.payload, **{field: [] for field in fields}, "exploration_history_note": note,
                   "history_note_status": "MODEL RESEARCH SUMMARY ONLY; not execution history or original evidence. "
                       "Host execution_state, reading_ledger and source identity take precedence. Re-read sources to check claims."}
        for key in pending:
            field, value = records[key]
            payload[field].append(value)
        if react:
            payload["latest_observations"].extend(protected)
            payload["latest_observations"].sort(key=lambda v: v.get("step_sequence", 0), reverse=True)
            payload["visible_source_text_ids"] = sorted({source["material_id"]
                for observation in payload["latest_observations"]
                for source in observation.get("payload", {}).get("source_views", [])
                if source.get("content") and source.get("material_id")})
            payload["compacted_observation_ids"] = [records[key][1]["id"] for key in order
                if key in covered and records[key][0] == "latest_observations"]
        return request.model_copy(update={"payload": payload})

    def summary_request(batch, previous_note):
        data = {"previous_note": previous_note, "records": batch}
        if react:
            data["current_candidate_reviews"] = request.payload.get("candidate_reviews", [])
            data["instruction"] = (
                "Write a navigation note: search directions already tried, source IDs and topics worth rereading, "
                "access failures, and remaining information needs. Copy source IDs exactly. "
                "Do not restate scientific findings, candidate IDs, approval states, or claims that a gap is resolved. "
                "Those are available in original sources and current reviews, not established by this note. "
                "Use current_candidate_reviews only to identify remaining information needs. Missing evidence "
                "is not evidence of absence. An old model interpretation is only a lead to check, not a fact.")
        digest = canonical_digest(data)
        return StructuredGenerationRequest(role="objective_history_summary", system_prompt_version="history-v1",
            invocation_id=f"{request.invocation_id}.summary.{digest[:16]}",
            idempotency_key=f"{run_id}|{version}|{digest}", payload=data)

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

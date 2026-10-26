"""Graph state.

Only JSON-serializable data lives here. Provider, repository, and model
objects are injected through the graph builder closure instead.
"""
from __future__ import annotations

from typing import TypedDict


class EntityGenerationState(TypedDict, total=False):
    request: dict
    episode_id: str
    task_id: str
    current_node: str
    seed_ref: dict | None
    seed_entity: dict | None
    bundle_id: str | None
    bundle_version: str | None
    evidence_ids: list[str]
    claim_ids: list[str]
    blueprints: list[dict]
    drafts: dict[str, dict]
    question_blueprints: dict[str, dict]
    blueprint_by_question_id: dict[str, dict]
    draft_question_ids: list[str]
    accepted_question_ids: list[str]
    rejected_question_ids: list[str]
    qa_item_ids: list[str]
    planner_trajectory_id: str | None
    target_difficulties_satisfied: bool
    rejection_record_ids: list[str]
    revision_counts: dict[str, int]
    edges: list[dict]
    literature_evidence: list[dict]
    tool_calls: int
    edges_observed: int
    steps: int
    sequence: int
    attempt: dict[str, int]
    stop_reason: str | None
    errors: list[dict]
    # ---- ReAct loop state ----
    #: The turn counter of the exploration loop.
    react_turn: int
    #: The server-assigned policy for the current run.
    react_policy: dict
    #: The observations the loop has produced so far.
    react_observations: list[dict]
    #: The entities the loop has discovered.
    react_known_entity_ids: list[str]
    #: The action hashes the loop already executed.
    react_action_hashes: list[str]
    #: The evidence needs that are still open.
    react_open_need_ids: list[str]
    #: The paths the loop produced.
    react_path_ids: list[str]
    #: The last sufficiency envelope, for the audit trail.
    react_sufficiency: dict | None
    #: The sealed actions, for the audit trail.
    react_sealed_actions: list[dict]
    #: The policy rejections, for the audit trail.
    react_rejections: list[dict]
    #: Consecutive proposals refused without an accepted action between them.
    react_rejection_streak: int
    #: Requirements-v2 planner steps, one per model proposal.
    react_planner_steps: list[dict]
    #: Consecutive executed turns that produced no new evidence or path.
    react_stagnation_count: int
    # ---- per-turn scratch state ----
    #: The proposal the planner produced this turn.
    _proposal: dict | None
    #: The error from a failed planner call.
    _proposal_error: str | None
    #: The sealed action the gate approved this turn.
    _sealed: dict | None
    #: The rejection the gate issued this turn.
    _rejection: dict | None
    #: What the execution produced this turn.
    _outcome: dict | None
    #: The planner-visible state captured before the selected action.
    _planner_state: dict | None
    _policy_before: dict | None

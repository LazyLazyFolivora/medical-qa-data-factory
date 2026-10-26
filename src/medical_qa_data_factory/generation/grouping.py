"""The entity-relation review group.

Step 9 of the real-generation-agent plan. Candidates are grouped by the
structure of the question they ask, so a reviewer can compare the ones that
genuinely compete with each other.

The grouping key is built from structured identity, never from display names
or free text:

    seed entity  +  ordered relation chain  +  ordered traversal directions
                 +  answer entity type

Two important properties:

* A group is not a verdict. Candidates in the same group may be entirely
  different questions, and the reviewer decides.
* The key is recomputable. A stored key that cannot be recomputed from the
  question's structure is an error, and the integrity check catches it.

Intermediate entity types are deliberately not part of the default key. They
are a display and filter aid, not an identity.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Sequence

from pydantic import Field

from ..domain.common import ContractModel, UtcDateTime


#: The grouping rules version. A change to the key must bump it.
REVIEW_GROUP_VERSION = "review-group-v1"


class GroupStep(ContractModel):
    """One hop of the grouped question."""

    position: int = Field(ge=0)
    relation: str = Field(min_length=1)
    direction: str = Field(pattern="^(forward|reverse)$")
    #: The entity type the hop arrives at. It is not part of the default key.
    target_entity_type: str | None = None


class GroupPayload(ContractModel):
    """The structured identity of a review group."""

    seed_provider_id: str = Field(min_length=1)
    seed_entity_id: str = Field(min_length=1)
    seed_entity_type: str | None = None
    steps: list[GroupStep] = Field(default_factory=list)
    answer_entity_type: str | None = None

    def canonical_json(self) -> str:
        """The canonical form the key is hashed from."""
        payload = {
            "seed_provider_id": self.seed_provider_id,
            "seed_entity_id": self.seed_entity_id,
            "steps": [
                {
                    "position": step.position,
                    "relation": step.relation,
                    "direction": step.direction,
                }
                for step in self.steps
            ],
            "answer_entity_type": self.answer_entity_type,
        }
        return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def group_key(payload: GroupPayload) -> str:
    """The stable key of a group."""
    digest = hashlib.sha256(payload.canonical_json().encode("utf-8")).hexdigest()
    return f"group.{digest[:24]}"


def group_key_for_single_hop(
    *,
    seed_provider_id: str,
    seed_entity_id: str,
    relation: str,
    direction: str,
    answer_entity_type: str | None = None,
) -> str:
    """The key of a single-hop group."""
    return group_key(
        GroupPayload(
            seed_provider_id=seed_provider_id,
            seed_entity_id=seed_entity_id,
            steps=[
                GroupStep(position=0, relation=relation, direction=direction)
            ],
            answer_entity_type=answer_entity_type,
        )
    )


def group_key_for_path(
    *,
    seed_provider_id: str,
    seed_entity_id: str,
    steps: Sequence[GroupStep],
    answer_entity_type: str | None = None,
) -> str:
    """The key of a multi-hop group.

    The whole ordered chain is part of the key. A three-hop question and a
    two-hop question that happen to end at the same entity are different
    questions and belong to different groups.
    """
    return group_key(
        GroupPayload(
            seed_provider_id=seed_provider_id,
            seed_entity_id=seed_entity_id,
            steps=list(steps),
            answer_entity_type=answer_entity_type,
        )
    )


def group_key_for_multi_paths(
    *,
    seed_provider_id: str,
    seed_entity_id: str,
    path_signatures: Sequence[Sequence[GroupStep]],
    answer_entity_type: str | None = None,
) -> str:
    """The key of a question that may be reached by several paths.

    The path signatures are sorted, so the key does not depend on the order
    the paths were discovered in.
    """
    sorted_signatures = sorted(
        (
            [
                {
                    "position": step.position,
                    "relation": step.relation,
                    "direction": step.direction,
                }
                for step in signature
            ]
            for signature in path_signatures
        ),
        key=lambda item: json.dumps(item, sort_keys=True),
    )
    payload = {
        "seed_provider_id": seed_provider_id,
        "seed_entity_id": seed_entity_id,
        "paths": sorted_signatures,
        "answer_entity_type": answer_entity_type,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return f"group.{digest[:24]}"


def is_recomputable(payload: GroupPayload, stored_key: str | None) -> bool:
    """Whether a stored key matches the one the structure produces."""
    if not stored_key:
        return False
    return stored_key == group_key(payload)


def intermediate_types(payload: GroupPayload) -> list[str | None]:
    """The entity types of the intermediate hops, for display and filtering.

    These are not part of the group key. They are shown so a reviewer can
    narrow the group, and they never split a group.
    """
    return [step.target_entity_type for step in payload.steps[:-1]]


@dataclass
class ReviewGroup:
    """A group of candidates that ask the same structural question."""

    review_group_key: str
    review_group_version: str = REVIEW_GROUP_VERSION
    payload: GroupPayload | None = None
    #: The question ids in the group.
    question_ids: list[str] = field(default_factory=list)
    created_at: UtcDateTime | None = None


def build_groups(
    candidates: Sequence[tuple[str, GroupPayload]],
) -> dict[str, ReviewGroup]:
    """Group the candidates by their structural identity."""
    from ..domain.common import utc_now

    groups: dict[str, ReviewGroup] = {}
    for question_id, payload in candidates:
        key = group_key(payload)
        group = groups.get(key)
        if group is None:
            group = ReviewGroup(
                review_group_key=key,
                payload=payload,
                created_at=utc_now(),
            )
            groups[key] = group
        group.question_ids.append(question_id)
    return groups
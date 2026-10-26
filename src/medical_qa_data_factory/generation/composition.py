"""Relation composition policy for multi-hop chains.

Step 7 of the real-generation-agent plan. A chain of relations does not
automatically mean what a reader assumes it means. "Aspirin treats headache,
headache indicates migraine" is a path in a graph, not a claim that aspirin
treats migraine.

The policy returns one of three verdicts for a chain:

* ``allow`` - the composition may be stated as the question intends.
* ``neutralize`` - the chain may be used, but the wording must not carry the
  relation of the first hop onto the last one.
* ``deny`` - the chain may not be used for a question at all.

The default for an unknown combination is ``neutralize``: a chain of
relations is never silently escalated into a causal or therapeutic reading.
"""
from __future__ import annotations

from enum import Enum
from typing import Sequence

from .contracts import RelationSemantics
from .semantics import RelationSemanticsPolicy


class CompositionVerdict(str, Enum):
    """What may be done with a relation chain."""

    ALLOW = "allow"
    NEUTRALIZE = "neutralize"
    DENY = "deny"


#: The policy version. A change to the rules must bump it.
COMPOSITION_POLICY_VERSION = "relation-composition-v1"


#: Chains whose head relation asserts a therapeutic or causal reading and whose
#: tail relation does not. These may be used, but only with neutral wording.
#:
#: The key is (first relation semantics, last relation semantics).
NEUTRALIZE_COMPOSITIONS: frozenset[tuple[RelationSemantics, RelationSemantics]] = (
    frozenset(
        {
            (RelationSemantics.DIRECTIONAL_FACT, RelationSemantics.ASSOCIATION),
            (RelationSemantics.DIRECTIONAL_FACT, RelationSemantics.GENERIC_RELATION),
            (RelationSemantics.ASSOCIATION, RelationSemantics.DIRECTIONAL_FACT),
            (RelationSemantics.ASSOCIATION, RelationSemantics.GENERIC_RELATION),
            (RelationSemantics.GENERIC_RELATION, RelationSemantics.DIRECTIONAL_FACT),
            (RelationSemantics.GENERIC_RELATION, RelationSemantics.ASSOCIATION),
        }
    )
)

#: Chains that are never usable for a question.
DENY_COMPOSITIONS: frozenset[tuple[RelationSemantics, RelationSemantics]] = (
    frozenset()
)


class RelationCompositionPolicy:
    """Decides what a chain of relations may assert."""

    version = COMPOSITION_POLICY_VERSION

    def __init__(
        self,
        semantics_policy: RelationSemanticsPolicy | None = None,
        *,
        overrides: dict[tuple[str, str], CompositionVerdict] | None = None,
    ) -> None:
        self.semantics = semantics_policy or RelationSemanticsPolicy()
        self._overrides = dict(overrides or {})

    def verdict(
        self, relations: Sequence[str]
    ) -> CompositionVerdict:
        """The verdict for a chain of relations."""
        if not relations:
            return CompositionVerdict.NEUTRALIZE
        key = (relations[0], relations[-1])
        if key in self._overrides:
            return self._overrides[key]
        if len(relations) == 1:
            # A single relation asserts only itself.
            return CompositionVerdict.ALLOW
        first = self.semantics.classify(relations[0])
        last = self.semantics.classify(relations[-1])
        if (first, last) in DENY_COMPOSITIONS:
            return CompositionVerdict.DENY
        if (first, last) in NEUTRALIZE_COMPOSITIONS:
            return CompositionVerdict.NEUTRALIZE
        # Two directional facts in a row may be stated, but the second must not
        # be read as a property of the first's subject.
        if first is RelationSemantics.DIRECTIONAL_FACT and last is (
            RelationSemantics.DIRECTIONAL_FACT
        ):
            return CompositionVerdict.NEUTRALIZE
        return CompositionVerdict.NEUTRALIZE

    def is_deny(self, relations: Sequence[str]) -> bool:
        return self.verdict(relations) is CompositionVerdict.DENY

    def requires_neutral_wording(self, relations: Sequence[str]) -> bool:
        return self.verdict(relations) is CompositionVerdict.NEUTRALIZE

    def rationale(self, relations: Sequence[str]) -> str:
        """Why the chain gets its verdict."""
        if not relations:
            return "an empty chain asserts nothing"
        if len(relations) == 1:
            return "a single relation asserts only itself"
        verdict = self.verdict(relations)
        first = self.semantics.classify(relations[0])
        last = self.semantics.classify(relations[-1])
        if verdict is CompositionVerdict.DENY:
            return (
                f"the combination of {relations[0]!r} and {relations[-1]!r} is "
                "not usable for a question"
            )
        return (
            f"the chain starts with {relations[0]!r} ({first.value}) and ends "
            f"with {relations[-1]!r} ({last.value}); the wording must not carry "
            "the first relation onto the last one"
        )
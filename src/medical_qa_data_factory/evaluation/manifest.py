"""The fixed evaluation manifest.

Step 11 of the real-generation-agent plan. An acceptance run is only
reproducible if everything it depends on is pinned: the seeds, the graph
snapshot, the model, the prompts and the policy.

The manifest is a file, not a run-time guess. It records what was pinned, and
the runner refuses to start when a pinned thing does not match what it finds.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import Field

from ..domain.common import ContractModel, UtcDateTime


#: The manifest schema version.
EVAL_MANIFEST_VERSION = "eval-manifest-v1"


class EvalSeed(ContractModel):
    """One pinned seed entity."""

    seed_id: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    native_id: str = Field(min_length=1)
    entity_type: str = Field(min_length=1)
    canonical_name: str = Field(min_length=1)
    #: The number of hops the run may explore for this seed.
    max_hops: int = Field(default=3, ge=1, le=4)
    #: The number of questions to aim for.
    target_question_count: int = Field(default=3, ge=1, le=20)
    #: The evidence mode the run must satisfy for this seed.
    required_evidence_mode: str = Field(default="kg_only")


class EvalGraphSnapshot(ContractModel):
    """The pinned graph snapshot."""

    provider_id: str = Field(min_length=1)
    #: The digest of the database or the snapshot file.
    sha256: str = Field(min_length=1)
    #: The manifest the provider reports.
    data_snapshot_id: str | None = None
    size_bytes: int | None = None


class EvalModelConfig(ContractModel):
    """The pinned model configuration."""

    provider_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    #: The hash of the model configuration, without the secret.
    config_hash: str = Field(min_length=1)
    #: The prompt version per role.
    prompt_hashes: dict[str, str] = Field(default_factory=dict)


class EvalRagSnapshot(ContractModel):
    """The pinned literature corpus.

    A provider that cannot pin a snapshot, or cannot read a chunk back, may
    not enter a formal acceptance run. The field records that decision
    rather than leaving it implicit.
    """

    provider_id: str = Field(min_length=1)
    snapshot_id: str | None = None
    pinned: bool = False
    supports_read_chunk: bool = False
    knowledge_base_ids: list[str] = Field(default_factory=list)
    #: Why this provider is not in the formal acceptance set.
    exclusion_reason: str | None = None


class EvalManifest(ContractModel):
    """Everything a reproducible acceptance run depends on."""

    schema_version: str = EVAL_MANIFEST_VERSION
    manifest_id: str = Field(min_length=1)
    #: The policy version the run uses.
    policy_version: str = Field(min_length=1)
    seeds: list[EvalSeed] = Field(min_length=1)
    graph: EvalGraphSnapshot
    model: EvalModelConfig
    rag: EvalRagSnapshot
    #: The versions of the code the run was made with.
    component_versions: dict[str, str] = Field(default_factory=dict)
    #: The number of questions the run aims for in total.
    target_question_count: int = Field(default=20, ge=1, le=200)
    notes: str = ""
    created_at: UtcDateTime

    def manifest_hash(self) -> str:
        """A stable digest of the manifest, for the acceptance report."""
        payload = self.model_dump(mode="json", exclude={"created_at"})
        text = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def formal_rag_eligible(self) -> bool:
        """Whether the literature provider may be used for formal acceptance."""
        return bool(
            self.rag.pinned
            and self.rag.snapshot_id
            and self.rag.supports_read_chunk
        )


def load_eval_manifest(path: str | Path) -> EvalManifest:
    """Load and validate a manifest."""
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"eval manifest not found: {target}")
    payload = json.loads(target.read_text(encoding="utf-8"))
    return EvalManifest.model_validate(payload)


def write_eval_manifest(manifest: EvalManifest, path: str | Path) -> Path:
    """Write a manifest as canonical JSON."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            manifest.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return target


def verify_against_environment(
    manifest: EvalManifest,
    *,
    graph_snapshot_id: str | None = None,
    model_config_id: str | None = None,
) -> list[str]:
    """The mismatches between the manifest and the current environment.

    An empty list means the environment matches the manifest. A non-empty
    list means the run is not the run the manifest describes, and the report
    has to say so.
    """
    problems: list[str] = []
    if graph_snapshot_id and manifest.graph.data_snapshot_id:
        if graph_snapshot_id != manifest.graph.data_snapshot_id:
            problems.append(
                f"the graph snapshot is {graph_snapshot_id!r}, the manifest "
                f"pins {manifest.graph.data_snapshot_id!r}"
            )
    if model_config_id and model_config_id != manifest.model.config_hash:
        problems.append(
            f"the model configuration is {model_config_id!r}, the manifest "
            f"pins {manifest.model.config_hash!r}"
        )
    return problems
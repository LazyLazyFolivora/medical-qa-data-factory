"""Loadable, versioned question-style skills for user-like question writing."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import Field, model_validator

from ..domain.common import ContractModel


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SKILL_ROOT = PROJECT_ROOT / "skills" / "question-styles"
DEFAULT_QUESTION_STYLE_ID = "layperson-search-v1"
_SKILL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class QuestionStyleManifest(ContractModel):
    skill_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    language: str = Field(min_length=2)
    question_types: list[str] = Field(min_length=1)
    style_profiles: list[str] = Field(min_length=1)
    profile_guides: dict[str, str] = Field(default_factory=dict)
    variants_per_blueprint: int = Field(default=3, ge=1, le=3)
    min_length: int = Field(default=4, ge=1)
    max_length: int = Field(default=80, ge=1)
    forbidden_terms: list[str] = Field(default_factory=list)
    forbidden_phrases: list[str] = Field(default_factory=list)
    validator_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def bounded_lengths(self) -> "QuestionStyleManifest":
        if self.min_length > self.max_length:
            raise ValueError("style minimum length exceeds maximum length")
        if len(set(self.style_profiles)) != len(self.style_profiles):
            raise ValueError("style profiles must be unique")
        if self.profile_guides and set(self.profile_guides) != set(
            self.style_profiles
        ):
            raise ValueError(
                "profile_guides must define every style profile exactly once"
            )
        return self


@dataclass(frozen=True)
class QuestionStyleSkill:
    manifest: QuestionStyleManifest
    prompt: str
    examples: tuple[dict[str, Any], ...] = ()
    hard_negatives: tuple[dict[str, Any], ...] = ()

    @property
    def skill_id(self) -> str:
        return self.manifest.skill_id

    @property
    def version(self) -> str:
        return self.manifest.version

    @property
    def display_name(self) -> str:
        return self.manifest.display_name

    def prompt_hash(self) -> str:
        examples = json.dumps(
            [self.prompt, self.examples, self.hard_negatives],
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        return hashlib.sha256(
            f"{self.skill_id}:{self.version}:{examples}".encode("utf-8")
        ).hexdigest()

    def model_instruction(self) -> str:
        """Prompt plus bounded examples actually sent to the model."""
        payload = [
            self.prompt,
            "Positive style examples:",
            *[
                json.dumps(item, ensure_ascii=False, sort_keys=True)
                for item in self.examples[:3]
            ],
            "Hard negatives to avoid:",
            *[
                json.dumps(item, ensure_ascii=False, sort_keys=True)
                for item in self.hard_negatives[:2]
            ],
        ]
        return "\n".join(payload)


def _read_jsonl(path: Path) -> tuple[dict[str, Any], ...]:
    if not path.exists():
        return ()
    records: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL at {path}:{number}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"JSONL record at {path}:{number} is not an object")
        records.append(payload)
    return tuple(records)


def load_question_skill(
    skill_id: str = DEFAULT_QUESTION_STYLE_ID,
    *,
    root: str | Path | None = None,
) -> QuestionStyleSkill:
    if not _SKILL_ID.fullmatch(skill_id):
        raise ValueError(f"invalid question style skill id: {skill_id!r}")
    base = Path(root) if root is not None else DEFAULT_SKILL_ROOT
    directory = base / skill_id
    manifest_path = directory / "skill.json"
    prompt_path = directory / "SKILL.md"
    if not manifest_path.is_file() or not prompt_path.is_file():
        raise ValueError(f"question style skill {skill_id!r} does not exist")
    try:
        manifest = QuestionStyleManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise ValueError(
            f"invalid question style manifest for {skill_id!r}: {exc}"
        ) from exc
    if manifest.skill_id != skill_id or directory.name != skill_id:
        raise ValueError(
            f"question style id mismatch for {skill_id!r}: {manifest.skill_id!r}"
        )
    prompt = prompt_path.read_text(encoding="utf-8").strip()
    if not prompt:
        raise ValueError(f"question style prompt is empty: {skill_id!r}")
    return QuestionStyleSkill(
        manifest=manifest,
        prompt=prompt,
        examples=_read_jsonl(directory / "examples.jsonl"),
        hard_negatives=_read_jsonl(directory / "hard-negatives.jsonl"),
    )


def load_question_skills(
    skill_ids: list[str] | tuple[str, ...] | None = None,
    *,
    root: str | Path | None = None,
) -> list[QuestionStyleSkill]:
    selected = list(skill_ids or [DEFAULT_QUESTION_STYLE_ID])
    if len(set(selected)) != len(selected):
        raise ValueError("question style skill ids must be unique")
    return [load_question_skill(item, root=root) for item in selected]


def available_question_skills(
    *, root: str | Path | None = None
) -> list[QuestionStyleSkill]:
    base = Path(root) if root is not None else DEFAULT_SKILL_ROOT
    if not base.is_dir():
        return []
    skills: list[QuestionStyleSkill] = []
    for directory in sorted(base.iterdir()):
        if directory.is_dir():
            skills.append(load_question_skill(directory.name, root=base))
    return skills


def profile_plan(skill: QuestionStyleSkill, count: int) -> list[str]:
    """Choose stable, distinct style profiles for the requested variants."""
    profiles = skill.manifest.style_profiles
    return [profiles[index % len(profiles)] for index in range(count)]

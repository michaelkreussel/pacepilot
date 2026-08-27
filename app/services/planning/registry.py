import hashlib
import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Annotated, Any, Protocol

import yaml
from pydantic import AfterValidator, BaseModel, WithJsonSchema
from yaml.resolver import BaseResolver

from app.services.planning.registry_models import (
    SUPPORTED_CONSTRAINT_IMPLEMENTATIONS,
    ConstraintRule,
    ConstraintSet,
    EvidenceEntry,
    EvidenceIndex,
    WorkoutTemplate,
)

KNOWLEDGE_ROOT = Path(__file__).parents[3] / "knowledge"
_LEGACY_WORKOUT_FILE_ORDER = (
    "easy_run.yaml",
    "recovery_run.yaml",
    "long_run.yaml",
    "strides.yaml",
    "threshold_cruise.yaml",
    "vo2_intervals.yaml",
)
CONSTRAINT_FILES = (
    "safety.yaml",
    "progression.yaml",
    "quality_density.yaml",
    "daily_adaptation.yaml",
)


class KnowledgeRegistryError(RuntimeError):
    pass


class RegistryItem(Protocol):
    id: str


class UniqueKeySafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: UniqueKeySafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[object, object]:
    output: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in output:
            raise KnowledgeRegistryError(f"Duplicate YAML key: {key}")
        output[key] = loader.construct_object(value_node, deep=deep)
    return output


UniqueKeySafeLoader.add_constructor(
    BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True)
class KnowledgeRegistry:
    version: str
    evidence: dict[str, EvidenceEntry]
    workouts: dict[str, WorkoutTemplate]
    constraints: dict[str, ConstraintRule]


def _load_yaml[T: BaseModel](path: Path, model: type[T]) -> T:
    try:
        with path.open(encoding="utf-8") as handle:
            value = yaml.load(handle, Loader=UniqueKeySafeLoader)
        return model.model_validate(value)
    except Exception as exc:
        raise KnowledgeRegistryError(f"Invalid knowledge document: {path}") from exc


def _unique_by_id[T: RegistryItem](items: list[T], kind: str) -> dict[str, T]:
    output: dict[str, T] = {}
    for item in items:
        item_id = item.id
        if item_id in output:
            raise KnowledgeRegistryError(f"Duplicate {kind} id: {item_id}")
        output[item_id] = item
    return output


def load_knowledge_registry(root: Path = KNOWLEDGE_ROOT) -> KnowledgeRegistry:
    evidence_index = _load_yaml(root / "evidence" / "index.yaml", EvidenceIndex)
    workouts = [
        _load_yaml(path, WorkoutTemplate)
        for path in sorted(
            (root / "workouts").glob("*.yaml"),
            key=lambda path: (
                _LEGACY_WORKOUT_FILE_ORDER.index(path.name)
                if path.name in _LEGACY_WORKOUT_FILE_ORDER
                else len(_LEGACY_WORKOUT_FILE_ORDER),
                path.name,
            ),
        )
    ]
    constraint_sets = [
        _load_yaml(root / "constraints" / filename, ConstraintSet) for filename in CONSTRAINT_FILES
    ]
    evidence_by_id = _unique_by_id(evidence_index.entries, "evidence")
    workout_by_id = _unique_by_id(workouts, "workout template")
    constraint_by_id = _unique_by_id(
        [rule for constraint_set in constraint_sets for rule in constraint_set.rules],
        "constraint",
    )
    evidence_ids = set(evidence_by_id)
    for artifact in [*workouts, *constraint_by_id.values()]:
        missing = set(artifact.evidence_refs) - evidence_ids
        if missing:
            raise KnowledgeRegistryError(
                f"Unknown evidence references for {artifact.id}: {', '.join(sorted(missing))}"
            )
    unsupported = {
        rule.implementation for rule in constraint_by_id.values() if rule.status == "active"
    } - SUPPORTED_CONSTRAINT_IMPLEMENTATIONS
    if unsupported:
        raise KnowledgeRegistryError(
            f"Unsupported active constraint implementations: {', '.join(sorted(unsupported))}"
        )

    canonical: dict[str, Any] = {
        "evidence": evidence_index.model_dump(mode="json"),
        "workouts": [item.model_dump(mode="json") for item in workouts],
        "constraints": [item.model_dump(mode="json") for item in constraint_sets],
    }
    digest = hashlib.sha256(
        json.dumps(
            canonical,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    return KnowledgeRegistry(
        version=f"{evidence_index.version}+sha256:{digest}",
        evidence=evidence_by_id,
        workouts=workout_by_id,
        constraints=constraint_by_id,
    )


@cache
def get_knowledge_registry() -> KnowledgeRegistry:
    return load_knowledge_registry()


def registered_workout_formats() -> tuple[WorkoutTemplate, ...]:
    return tuple(get_knowledge_registry().workouts.values())


WORKOUT_FORMAT_IDS = tuple(item.id for item in registered_workout_formats())


def _validate_workout_format_id(value: str) -> str:
    if value not in WORKOUT_FORMAT_IDS:
        raise ValueError(f"Unsupported workout format: {value}")
    return value


WorkoutFormatId = Annotated[
    str,
    AfterValidator(_validate_workout_format_id),
    WithJsonSchema({"type": "string", "enum": list(WORKOUT_FORMAT_IDS)}),
]

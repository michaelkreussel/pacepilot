from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import isfinite
from typing import Annotated, Any, Literal, overload
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field


class TimeEnd(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["time"]
    seconds: float


class DistanceEnd(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["distance"]
    meters: float


type EndCondition = Annotated[TimeEnd | DistanceEnd, Field(discriminator="type")]


class NoTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["none"]


class PaceRangeTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["pace_range"]
    fastest_seconds_per_km: float
    slowest_seconds_per_km: float


class HeartRateRangeTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["heart_rate_range"]
    lower_bpm: int
    upper_bpm: int


class HeartRateZoneTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["heart_rate_zone"]
    zone: int


type WorkoutTarget = Annotated[
    NoTarget | PaceRangeTarget | HeartRateRangeTarget | HeartRateZoneTarget,
    Field(discriminator="type"),
]


class RpeRangeTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["rpe_range"]
    lower_rpe: int = Field(ge=1, le=10)
    upper_rpe: int = Field(ge=1, le=10)


type WorkoutTargetV2 = Annotated[
    NoTarget | PaceRangeTarget | HeartRateRangeTarget | HeartRateZoneTarget | RpeRangeTarget,
    Field(discriminator="type"),
]

type StepInstruction = Annotated[str, Field(min_length=1, max_length=300)]


class StepBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Literal["step"]
    step_type: Literal["warmup", "interval", "recovery", "cooldown"]
    end: EndCondition
    target: WorkoutTarget


class RepeatBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Literal["repeat"]
    iterations: int
    children: list[WorkoutBlock]


type WorkoutBlock = Annotated[StepBlock | RepeatBlock, Field(discriminator="kind")]
RepeatBlock.model_rebuild()


class WorkoutDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    blocks: list[WorkoutBlock]


class StepBlockV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Literal["step"]
    step_type: Literal["warmup", "interval", "recovery", "cooldown"]
    end: EndCondition
    target: WorkoutTargetV2
    instructions: list[StepInstruction] = Field(default_factory=list, max_length=5)


class RepeatBlockV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Literal["repeat"]
    iterations: int
    children: list[WorkoutBlockV2]


type WorkoutBlockV2 = Annotated[StepBlockV2 | RepeatBlockV2, Field(discriminator="kind")]
RepeatBlockV2.model_rebuild()


class WorkoutDefinitionV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    blocks: list[WorkoutBlockV2]


type WorkoutDefinitionModel = WorkoutDefinition | WorkoutDefinitionV2
type AnyWorkoutBlock = WorkoutBlock | WorkoutBlockV2
type AnyStepBlock = StepBlock | StepBlockV2
type AnyRepeatBlock = RepeatBlock | RepeatBlockV2


class DefinitionValidationError(ValueError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class WorkoutMetrics:
    step_count: int = 0
    expanded_step_count: int = 0
    duration_seconds: float = 0
    distance_meters: float = 0
    duration_complete: bool = True
    distance_complete: bool = True


def new_id() -> str:
    return str(uuid4())


def default_definition() -> WorkoutDefinition:
    return WorkoutDefinition(
        blocks=[
            _new_step("warmup", TimeEnd(type="time", seconds=600)),
            _new_step("interval", TimeEnd(type="time", seconds=1200)),
            _new_step("cooldown", TimeEnd(type="time", seconds=600)),
        ]
    )


def _new_step(
    step_type: Literal["warmup", "interval", "recovery", "cooldown"], end: EndCondition
) -> StepBlock:
    return StepBlock(
        id=new_id(),
        kind="step",
        step_type=step_type,
        end=end,
        target=NoTarget(type="none"),
    )


def definition_to_json(definition: WorkoutDefinitionModel) -> dict[str, Any]:
    return definition.model_dump(mode="json")


@overload
def parse_definition(value: object, definition_version: Literal[1] = 1) -> WorkoutDefinition: ...


@overload
def parse_definition(value: object, definition_version: Literal[2]) -> WorkoutDefinitionV2: ...


@overload
def parse_definition(value: object, definition_version: int) -> WorkoutDefinitionModel: ...


def parse_definition(value: object, definition_version: int = 1) -> WorkoutDefinitionModel:
    if definition_version == 1:
        return WorkoutDefinition.model_validate(value)
    if definition_version == 2:
        return WorkoutDefinitionV2.model_validate(value)
    raise DefinitionValidationError(
        "Diese Workout-Formatversion wird nicht unterstützt.",
        code="definition.version_unsupported",
    )


def validate_definition(definition: WorkoutDefinitionModel, sport: str) -> None:
    if not definition.blocks:
        raise DefinitionValidationError(
            "Mindestens ein Trainingsschritt ist erforderlich.",
            code="definition.blocks_required",
        )

    seen_ids: set[str] = set()

    def validate_blocks(blocks: Sequence[AnyWorkoutBlock], depth: int) -> None:
        for block in blocks:
            if not block.id or block.id in seen_ids:
                raise DefinitionValidationError(
                    "Jeder Trainingsblock braucht eine eindeutige ID.",
                    code="definition.block_id_invalid",
                )
            seen_ids.add(block.id)
            if isinstance(block, (RepeatBlock, RepeatBlockV2)):
                if depth >= 1:
                    raise DefinitionValidationError(
                        "Verschachtelte Wiederholungen werden noch nicht unterstützt.",
                        code="definition.nested_repeat_unsupported",
                    )
                if not 2 <= block.iterations <= 50:
                    raise DefinitionValidationError(
                        "Wiederholungen müssen zwischen 2 und 50 liegen.",
                        code="definition.repeat_iterations_invalid",
                    )
                if not block.children:
                    raise DefinitionValidationError(
                        "Eine Wiederholung muss mindestens einen Schritt enthalten.",
                        code="definition.repeat_children_required",
                    )
                validate_blocks(block.children, depth + 1)
                continue

            value = block.end.seconds if isinstance(block.end, TimeEnd) else block.end.meters
            if not isfinite(value) or value <= 0:
                raise DefinitionValidationError(
                    "Zeit und Distanz müssen größer als null sein.",
                    code="definition.end_value_invalid",
                )
            target = block.target
            if isinstance(target, PaceRangeTarget):
                if sport != "running":
                    raise DefinitionValidationError(
                        "Pace-Ziele sind derzeit nur beim Laufen möglich.",
                        code="definition.pace_running_only",
                    )
                if not _valid_positive(target.fastest_seconds_per_km) or not _valid_positive(
                    target.slowest_seconds_per_km
                ):
                    raise DefinitionValidationError(
                        "Pace-Grenzen müssen größer als null sein.",
                        code="definition.pace_value_invalid",
                    )
                if target.fastest_seconds_per_km > target.slowest_seconds_per_km:
                    raise DefinitionValidationError(
                        "Die schnelle Pace-Grenze darf nicht langsamer als die langsame sein.",
                        code="definition.pace_order_invalid",
                    )
            elif isinstance(target, HeartRateRangeTarget):
                if not 30 <= target.lower_bpm < target.upper_bpm <= 250:
                    raise DefinitionValidationError(
                        "Der Herzfrequenzbereich muss zwischen 30 und 250 bpm liegen.",
                        code="definition.heart_rate_range_invalid",
                    )
            elif isinstance(target, HeartRateZoneTarget) and not 1 <= target.zone <= 5:
                raise DefinitionValidationError(
                    "Die Herzfrequenzzone muss zwischen 1 und 5 liegen.",
                    code="definition.heart_rate_zone_invalid",
                )
            elif isinstance(target, RpeRangeTarget) and target.lower_rpe > target.upper_rpe:
                raise DefinitionValidationError(
                    "Die untere RPE-Grenze darf nicht höher als die obere sein.",
                    code="definition.rpe_order_invalid",
                )

    validate_blocks(definition.blocks, 0)


def _valid_positive(value: float) -> bool:
    return isfinite(value) and value > 0


def workout_metrics(definition: WorkoutDefinitionModel) -> WorkoutMetrics:
    def collect(blocks: Sequence[AnyWorkoutBlock], multiplier: int = 1) -> WorkoutMetrics:
        result = WorkoutMetrics()
        for block in blocks:
            if isinstance(block, (RepeatBlock, RepeatBlockV2)):
                child = collect(block.children, multiplier * block.iterations)
                result = _add_metrics(result, child)
                continue
            duration = block.end.seconds * multiplier if isinstance(block.end, TimeEnd) else 0
            distance = block.end.meters * multiplier if isinstance(block.end, DistanceEnd) else 0
            result = _add_metrics(
                result,
                WorkoutMetrics(
                    step_count=1,
                    expanded_step_count=multiplier,
                    duration_seconds=duration,
                    distance_meters=distance,
                    duration_complete=isinstance(block.end, TimeEnd),
                    distance_complete=isinstance(block.end, DistanceEnd),
                ),
            )
        return result

    return collect(definition.blocks)


def _add_metrics(left: WorkoutMetrics, right: WorkoutMetrics) -> WorkoutMetrics:
    return WorkoutMetrics(
        step_count=left.step_count + right.step_count,
        expanded_step_count=left.expanded_step_count + right.expanded_step_count,
        duration_seconds=left.duration_seconds + right.duration_seconds,
        distance_meters=left.distance_meters + right.distance_meters,
        duration_complete=left.duration_complete and right.duration_complete,
        distance_complete=left.distance_complete and right.distance_complete,
    )


def legacy_definition(workout_id: int, steps: list[Any]) -> WorkoutDefinition:
    blocks: list[WorkoutBlock] = []
    index = 0
    while index < len(steps):
        step = steps[index]
        repetitions = step.repeat_count or 1
        next_step = steps[index + 1] if index + 1 < len(steps) else None
        if (
            step.step_type == "interval"
            and repetitions > 1
            and next_step is not None
            and next_step.step_type == "recovery"
            and (next_step.repeat_count or 1) == repetitions
        ):
            blocks.append(
                RepeatBlock(
                    id=_legacy_id(workout_id, "pair", step.id, next_step.id),
                    kind="repeat",
                    iterations=repetitions,
                    children=[
                        _legacy_step(workout_id, step),
                        _legacy_step(workout_id, next_step),
                    ],
                )
            )
            index += 2
            continue
        converted = _legacy_step(workout_id, step)
        if repetitions > 1:
            blocks.append(
                RepeatBlock(
                    id=_legacy_id(workout_id, "single", step.id),
                    kind="repeat",
                    iterations=repetitions,
                    children=[converted],
                )
            )
        else:
            blocks.append(converted)
        index += 1
    return WorkoutDefinition(blocks=blocks)


def _legacy_step(workout_id: int, step: Any) -> StepBlock:
    end: EndCondition
    if step.duration_type == "time":
        end = TimeEnd(type="time", seconds=float(step.duration_value or 0))
    else:
        end = DistanceEnd(type="distance", meters=float(step.duration_value or 0))
    target: WorkoutTarget
    if step.target_type == "pace":
        target = PaceRangeTarget(
            type="pace_range",
            fastest_seconds_per_km=float(step.target_min or 0),
            slowest_seconds_per_km=float(step.target_max or 0),
        )
    else:
        target = NoTarget(type="none")
    return StepBlock(
        id=_legacy_id(workout_id, "step", step.id),
        kind="step",
        step_type=step.step_type,
        end=end,
        target=target,
    )


def _legacy_id(workout_id: int, kind: str, *parts: object) -> str:
    suffix = ":".join(str(part) for part in parts)
    return str(uuid5(NAMESPACE_URL, f"pacepilot:workout:{workout_id}:{kind}:{suffix}"))

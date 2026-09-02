import json
from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field

from app.services.planning.load_estimate import IntensityDomainTime, LoadEstimate
from app.services.planning.registry import KnowledgeRegistry, get_knowledge_registry
from app.services.planning.registry_models import (
    ContinuousStructure,
    IntervalStructure,
    NumericRange,
    RpeRange,
    StridesStructure,
    WorkoutTemplate,
)
from app.services.planning.workout_definition import (
    NoTarget,
    RepeatBlockV2,
    RpeRangeTarget,
    StepBlockV2,
    TimeEnd,
    WorkoutDefinitionV2,
    definition_to_json,
)

GENERATOR_VERSION = "workout-template-expander-v2"


class TemplateExpansionError(ValueError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class TemplateParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    duration_minutes: int | None = Field(default=None, ge=1)
    repetitions: int | None = Field(default=None, ge=1)


class TemplateEligibilityContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consistent_running_weeks: int = Field(ge=0)
    runs_per_week: int = Field(ge=0)
    available_minutes: int = Field(ge=1)
    safety_stop: bool = False
    facts: set[str] = Field(default_factory=set)
    active_contraindications: set[str] = Field(default_factory=set)


@dataclass(frozen=True)
class ExpandedWorkoutTemplate:
    template_id: str
    template_version: str
    name: str
    generator_version: str
    knowledge_base_version: str
    purpose: str
    definition_version: int
    definition: WorkoutDefinitionV2
    guidance: dict[str, object]
    load_estimate: LoadEstimate
    evidence_refs: tuple[str, ...]

    def canonical_json(self) -> str:
        return json.dumps(
            {
                "template_id": self.template_id,
                "template_version": self.template_version,
                "name": self.name,
                "generator_version": self.generator_version,
                "knowledge_base_version": self.knowledge_base_version,
                "purpose": self.purpose,
                "definition_version": self.definition_version,
                "definition": definition_to_json(self.definition),
                "guidance": self.guidance,
                "load_estimate": self.load_estimate.model_dump(mode="json"),
                "evidence_refs": list(self.evidence_refs),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )


def _bounded(value: int | None, bounds: NumericRange, field: str) -> int:
    selected = bounds.default if value is None else value
    if not bounds.minimum <= selected <= bounds.maximum:
        raise TemplateExpansionError(
            f"{field} muss zwischen {bounds.minimum} und {bounds.maximum} liegen.",
            code="template.parameter_out_of_range",
        )
    return selected


def _stable_id(template: WorkoutTemplate, parameters: TemplateParameters, role: str) -> str:
    key = parameters.model_dump_json(exclude_none=True)
    return str(uuid5(NAMESPACE_URL, f"pacepilot:{template.id}:{template.version}:{key}:{role}"))


def _rpe_target(value: RpeRange) -> RpeRangeTarget:
    return RpeRangeTarget(
        type="rpe_range",
        lower_rpe=value.minimum,
        upper_rpe=value.maximum,
    )


def _continuous(
    template: WorkoutTemplate,
    structure: ContinuousStructure,
    parameters: TemplateParameters,
) -> tuple[WorkoutDefinitionV2, LoadEstimate]:
    if parameters.repetitions is not None:
        raise TemplateExpansionError(
            "Dieses Template besitzt keine Wiederholungen.",
            code="template.parameter_unsupported",
        )
    duration = _bounded(parameters.duration_minutes, structure.duration_minutes, "Dauer")
    seconds = duration * 60
    definition = WorkoutDefinitionV2(
        blocks=[
            StepBlockV2(
                id=_stable_id(template, parameters, "main"),
                kind="step",
                step_type="interval",
                end=TimeEnd(type="time", seconds=seconds),
                target=_rpe_target(structure.rpe),
                instructions=structure.instructions,
            )
        ]
    )
    estimate = LoadEstimate(
        duration_seconds=seconds,
        distance_meters=None,
        time_by_intensity_domain_seconds=IntensityDomainTime(low=seconds, moderate=0, high=0),
        mechanical_load="low" if template.id != "long_run" else "moderate",
        session_rpe=structure.session_rpe,
        confidence="moderate",
        uncertainty=[
            "distance_unknown_for_time_based_workout",
            "individual_response_requires_baseline_validation",
        ],
    )
    return definition, estimate


def _strides(
    template: WorkoutTemplate,
    structure: StridesStructure,
    parameters: TemplateParameters,
) -> tuple[WorkoutDefinitionV2, LoadEstimate]:
    duration = _bounded(parameters.duration_minutes, structure.easy_duration_minutes, "Dauer")
    repetitions = _bounded(parameters.repetitions, structure.repetitions, "Wiederholungen")
    stride_seconds = structure.stride_duration_seconds.default
    recovery_seconds = structure.recovery_duration_seconds.default
    easy_seconds = duration * 60
    definition = WorkoutDefinitionV2(
        blocks=[
            StepBlockV2(
                id=_stable_id(template, parameters, "easy"),
                kind="step",
                step_type="warmup",
                end=TimeEnd(type="time", seconds=easy_seconds),
                target=_rpe_target(structure.easy_rpe),
                instructions=["Laufe locker und in vollständigen Sätzen."],
            ),
            RepeatBlockV2(
                id=_stable_id(template, parameters, "repeat"),
                kind="repeat",
                iterations=repetitions,
                children=[
                    StepBlockV2(
                        id=_stable_id(template, parameters, "stride"),
                        kind="step",
                        step_type="interval",
                        end=TimeEnd(type="time", seconds=stride_seconds),
                        target=_rpe_target(structure.stride_rpe),
                        instructions=[structure.instructions[0]],
                    ),
                    StepBlockV2(
                        id=_stable_id(template, parameters, "recovery"),
                        kind="step",
                        step_type="recovery",
                        end=TimeEnd(type="time", seconds=recovery_seconds),
                        target=NoTarget(type="none"),
                        instructions=[structure.instructions[1]],
                    ),
                ],
            ),
        ]
    )
    repeated_seconds = repetitions * (stride_seconds + recovery_seconds)
    estimate = LoadEstimate(
        duration_seconds=easy_seconds + repeated_seconds,
        distance_meters=None,
        time_by_intensity_domain_seconds=IntensityDomainTime(
            low=easy_seconds + repetitions * recovery_seconds,
            moderate=0,
            high=repetitions * stride_seconds,
        ),
        mechanical_load="moderate",
        session_rpe=structure.session_rpe,
        confidence="low",
        uncertainty=[
            "distance_unknown_for_time_based_workout",
            "short_stride_intensity_has_high_individual_variance",
        ],
    )
    return definition, estimate


def _intervals(
    template: WorkoutTemplate,
    structure: IntervalStructure,
    parameters: TemplateParameters,
) -> tuple[WorkoutDefinitionV2, LoadEstimate]:
    if parameters.duration_minutes is not None:
        raise TemplateExpansionError(
            "Die Gesamtdauer eines Intervall-Templates wird aus seinen Blöcken berechnet.",
            code="template.parameter_unsupported",
        )
    repetitions = _bounded(parameters.repetitions, structure.repetitions, "Wiederholungen")
    work_minutes = structure.work_minutes.default
    total_work_minutes = repetitions * work_minutes
    if (
        not structure.total_work_minutes.minimum
        <= total_work_minutes
        <= structure.total_work_minutes.maximum
    ):
        raise TemplateExpansionError(
            "Die gewählte Wiederholungszahl liegt außerhalb der erlaubten Gesamtbelastung.",
            code="template.total_work_out_of_range",
        )
    warmup_seconds = structure.warmup_minutes.default * 60
    work_seconds = work_minutes * 60
    recovery_seconds = structure.recovery_minutes.default * 60
    cooldown_seconds = structure.cooldown_minutes.default * 60
    definition = WorkoutDefinitionV2(
        blocks=[
            StepBlockV2(
                id=_stable_id(template, parameters, "warmup"),
                kind="step",
                step_type="warmup",
                end=TimeEnd(type="time", seconds=warmup_seconds),
                target=NoTarget(type="none"),
                instructions=["Laufe dich locker ein und bereite dich kontrolliert vor."],
            ),
            RepeatBlockV2(
                id=_stable_id(template, parameters, "repeat"),
                kind="repeat",
                iterations=repetitions,
                children=[
                    StepBlockV2(
                        id=_stable_id(template, parameters, "work"),
                        kind="step",
                        step_type="interval",
                        end=TimeEnd(type="time", seconds=work_seconds),
                        target=_rpe_target(structure.work_rpe),
                        instructions=structure.instructions,
                    ),
                    StepBlockV2(
                        id=_stable_id(template, parameters, "recovery"),
                        kind="step",
                        step_type="recovery",
                        end=TimeEnd(type="time", seconds=recovery_seconds),
                        target=NoTarget(type="none"),
                        instructions=["Trabe oder gehe locker bis zur nächsten Wiederholung."],
                    ),
                ],
            ),
            StepBlockV2(
                id=_stable_id(template, parameters, "cooldown"),
                kind="step",
                step_type="cooldown",
                end=TimeEnd(type="time", seconds=cooldown_seconds),
                target=NoTarget(type="none"),
                instructions=["Laufe anschließend bewusst locker aus."],
            ),
        ]
    )
    recovery_total_seconds = repetitions * recovery_seconds
    work_total_seconds = repetitions * work_seconds
    low_seconds = warmup_seconds + recovery_total_seconds + cooldown_seconds
    estimate = LoadEstimate(
        duration_seconds=low_seconds + work_total_seconds,
        distance_meters=None,
        time_by_intensity_domain_seconds=IntensityDomainTime(
            low=low_seconds,
            moderate=work_total_seconds if template.id == "threshold_cruise" else 0,
            high=work_total_seconds if template.id == "vo2_intervals" else 0,
        ),
        mechanical_load="high" if template.id == "vo2_intervals" else "moderate",
        session_rpe=structure.session_rpe,
        confidence="low",
        uncertainty=[
            "distance_unknown_for_time_based_workout",
            "deferred_quality_template_development_override",
            "individual_response_requires_baseline_validation",
        ],
    )
    return definition, estimate


def expand_workout_template(
    template_id: str,
    parameters: TemplateParameters | None = None,
    *,
    eligibility: TemplateEligibilityContext,
    registry: KnowledgeRegistry | None = None,
) -> ExpandedWorkoutTemplate:
    knowledge = registry or get_knowledge_registry()
    template = knowledge.workouts.get(template_id)
    if template is None:
        raise TemplateExpansionError("Workout-Template nicht gefunden.", code="template.not_found")
    selected = parameters or TemplateParameters()
    if isinstance(template.structure, ContinuousStructure):
        definition, estimate = _continuous(template, template.structure, selected)
    elif isinstance(template.structure, StridesStructure):
        definition, estimate = _strides(template, template.structure, selected)
    elif isinstance(template.structure, IntervalStructure):
        definition, estimate = _intervals(template, template.structure, selected)
    else:
        raise TemplateExpansionError(
            "Diese Template-Struktur ist noch nicht freigegeben.",
            code="template.structure_not_active",
        )
    if estimate.duration_seconds > eligibility.available_minutes * 60:
        raise TemplateExpansionError(
            "Das Workout überschreitet die verfügbare Zeit.",
            code="template.available_time_exceeded",
        )
    return ExpandedWorkoutTemplate(
        template_id=template.id,
        template_version=template.version,
        name=template.name,
        generator_version=GENERATOR_VERSION,
        knowledge_base_version=knowledge.version,
        purpose=template.purpose,
        definition_version=2,
        definition=definition,
        guidance={
            "instructions": template.structure.instructions,
            "fallback_targets": template.fallback_targets,
            "contraindications": template.contraindications,
        },
        load_estimate=estimate,
        evidence_refs=tuple(template.evidence_refs),
    )

from datetime import date
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RegistryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class EvidenceLevel(StrEnum):
    A = "A"
    B = "B"
    C = "C"
    D = "D"


class RuleStrength(StrEnum):
    HARD_CONSTRAINT = "hard_constraint"
    SOFT_CONSTRAINT = "soft_constraint"
    HEURISTIC = "heuristic"
    EXPLANATORY_ONLY = "explanatory_only"


SUPPORTED_CONSTRAINT_IMPLEMENTATIONS = {
    "adaptation.no_load_increase",
    "constraint.no_time_available",
    "progression.change_one_axis",
    "progression.no_catchup_stacking",
    "progression.single_session_distance_spike",
    "quality.minimum_spacing",
    "readiness.subjective_strain",
    "recovery.difficult_session",
    "safety.cardiopulmonary_warning",
    "safety.fever_or_systemic_illness",
    "safety.illness_unclear",
    "safety.mild_illness",
    "safety.pain_alters_gait",
    "safety.pain_unclear",
    "safety.pain_warning",
}


class EvidenceSource(RegistryModel):
    title: str = Field(min_length=1)
    year: int = Field(ge=1900, le=2100)
    citation: str = Field(min_length=1)
    doi: str | None = None
    url: str | None = None


class EvidencePopulation(RegistryModel):
    description: str = Field(min_length=1)
    sample_size: int | None = Field(default=None, ge=1)


class EvidenceEntry(RegistryModel):
    id: str = Field(pattern=r"^E-[A-Z0-9-]+$")
    claim: str = Field(min_length=1)
    source: EvidenceSource
    evidence_level: EvidenceLevel
    study_design: str = Field(min_length=1)
    population: EvidencePopulation
    limitations: list[str] = Field(min_length=1)
    allowed_uses: list[str] = Field(min_length=1)
    forbidden_uses: list[str] = Field(min_length=1)
    last_reviewed: date


class EvidenceIndex(RegistryModel):
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    entries: list[EvidenceEntry] = Field(min_length=1)


class NumericRange(RegistryModel):
    minimum: int = Field(ge=1)
    default: int = Field(ge=1)
    maximum: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_order(self) -> "NumericRange":
        if not self.minimum <= self.default <= self.maximum:
            raise ValueError("range must satisfy minimum <= default <= maximum")
        return self


class RpeRange(RegistryModel):
    minimum: int = Field(ge=1, le=10)
    maximum: int = Field(ge=1, le=10)

    @model_validator(mode="after")
    def validate_order(self) -> "RpeRange":
        if self.minimum > self.maximum:
            raise ValueError("RPE range minimum must not exceed maximum")
        return self


class Eligibility(RegistryModel):
    min_consistent_running_weeks: int = Field(default=0, ge=0)
    min_runs_per_week: int = Field(default=0, ge=0)
    requirements: list[str] = Field(default_factory=list)


class ProductHeuristic(RegistryModel):
    product_default: Literal[True]
    rationale: str = Field(min_length=1)
    status: Literal["initial_product_default", "professionally_reviewed"]
    reviewed_on: date


class GarminCapabilities(RegistryModel):
    time_steps: bool
    repeat_blocks: bool
    rpe_target: bool
    local_instructions: bool
    degradation: list[str] = Field(default_factory=list)


class ContinuousStructure(RegistryModel):
    kind: Literal["continuous"]
    duration_minutes: NumericRange
    rpe: RpeRange
    session_rpe: RpeRange
    instructions: list[str] = Field(min_length=1)


class StridesStructure(RegistryModel):
    kind: Literal["strides"]
    easy_duration_minutes: NumericRange
    easy_rpe: RpeRange
    repetitions: NumericRange
    stride_duration_seconds: NumericRange
    stride_rpe: RpeRange
    recovery_duration_seconds: NumericRange
    session_rpe: RpeRange
    instructions: list[str] = Field(min_length=1)


class IntervalStructure(RegistryModel):
    kind: Literal["intervals"]
    warmup_minutes: NumericRange
    repetitions: NumericRange
    work_minutes: NumericRange
    work_rpe: RpeRange
    recovery_minutes: NumericRange
    cooldown_minutes: NumericRange
    total_work_minutes: NumericRange
    session_rpe: RpeRange
    instructions: list[str] = Field(min_length=1)


type TemplateStructure = Annotated[
    ContinuousStructure | StridesStructure | IntervalStructure,
    Field(discriminator="kind"),
]


class WorkoutTemplate(RegistryModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]+$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    status: Literal["active", "deferred"]
    name: str = Field(min_length=1)
    sport: Literal["running"]
    purpose: str = Field(min_length=1)
    intensity_domain: Literal["low", "moderate", "high", "mixed"]
    rule_strength: RuleStrength
    eligibility: Eligibility
    contraindications: list[str] = Field(min_length=1)
    structure: TemplateStructure
    progression_axes: list[str] = Field(min_length=1)
    weekly_frequency_cap: int = Field(ge=1, le=14)
    fallback_targets: list[str] = Field(min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)
    product_heuristic: ProductHeuristic | None = None
    garmin_capabilities: GarminCapabilities

    @model_validator(mode="after")
    def validate_grounding(self) -> "WorkoutTemplate":
        if not self.evidence_refs and self.product_heuristic is None:
            raise ValueError("template requires evidence references or product heuristic metadata")
        return self


type FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
type ConstraintParameter = int | FiniteFloat | bool | str | list[str]


class ConstraintRule(RegistryModel):
    id: str = Field(pattern=r"^[A-Z][A-Z0-9-]+$")
    status: Literal["active", "deferred"]
    enforcement: RuleStrength
    implementation: str = Field(pattern=r"^[a-z][a-z0-9_.]+$")
    description: str = Field(min_length=1)
    parameters: dict[str, ConstraintParameter] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
    product_heuristic: ProductHeuristic | None = None

    @model_validator(mode="after")
    def validate_grounding(self) -> "ConstraintRule":
        if self.status == "active" and not self.evidence_refs and self.product_heuristic is None:
            raise ValueError("active constraint requires evidence or product heuristic metadata")
        return self


class ConstraintSet(RegistryModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]+$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    rules: list[ConstraintRule] = Field(min_length=1)

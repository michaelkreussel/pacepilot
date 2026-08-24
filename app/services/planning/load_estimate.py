from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.planning.registry_models import RpeRange


class IntensityDomainTime(BaseModel):
    model_config = ConfigDict(extra="forbid")

    low: int = Field(ge=0)
    moderate: int = Field(ge=0)
    high: int = Field(ge=0)


class LoadEstimate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    duration_seconds: int = Field(ge=1)
    distance_meters: float | None = Field(default=None, ge=0)
    time_by_intensity_domain_seconds: IntensityDomainTime
    mechanical_load: Literal["low", "moderate", "high"]
    session_rpe: RpeRange | None
    confidence: Literal["low", "moderate", "high"]
    uncertainty: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_domain_time(self) -> "LoadEstimate":
        domain_time = self.time_by_intensity_domain_seconds
        if domain_time.low + domain_time.moderate + domain_time.high != self.duration_seconds:
            raise ValueError("intensity-domain time must equal total duration")
        return self

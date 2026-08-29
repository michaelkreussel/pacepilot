from datetime import date
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AthleteAvailability,
    AthleteGoal,
    AthletePlanningProfile,
    PerformanceAnchor,
    TrainingCycle,
    User,
)
from app.models.planning import ANCHOR_KINDS, EXPERIENCE_LEVELS, GOAL_EVENT_TYPES
from app.services.planning.planning_queries import (
    AvailabilityFact,
    GoalFact,
    PerformanceAnchorFact,
    PlanningProfileFact,
)


class PlanningCommandInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class GoalCreateInput(PlanningCommandInput):
    event_type: str
    event_name: str | None = Field(default=None, max_length=200)
    target_date: date | None = None

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, value: str) -> str:
        if value not in GOAL_EVENT_TYPES:
            raise ValueError("unsupported goal event type")
        return value


class GoalUpdateInput(PlanningCommandInput):
    event_type: str | None = None
    event_name: str | None = Field(default=None, max_length=200)
    target_date: date | None = None

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, value: str | None) -> str | None:
        if value is not None and value not in GOAL_EVENT_TYPES:
            raise ValueError("unsupported goal event type")
        return value

    @model_validator(mode="after")
    def require_change(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("at least one goal field is required")
        if "event_type" in self.model_fields_set and self.event_type is None:
            raise ValueError("goal event type cannot be null")
        return self


class PlanningProfileUpdateInput(PlanningCommandInput):
    experience_level: str | None = None
    preferred_long_run_weekday: int | None = Field(default=None, ge=0, le=6)
    self_declared_reentry: bool | None = None
    constraint_note: str | None = Field(default=None, max_length=2000)

    @field_validator("experience_level")
    @classmethod
    def validate_experience_level(cls, value: str | None) -> str | None:
        if value is not None and value not in EXPERIENCE_LEVELS:
            raise ValueError("unsupported experience level")
        return value

    @model_validator(mode="after")
    def require_change(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("at least one profile field is required")
        if "self_declared_reentry" in self.model_fields_set and self.self_declared_reentry is None:
            raise ValueError("self-declared re-entry cannot be null")
        return self


class AvailabilityInput(PlanningCommandInput):
    weekday: int = Field(ge=0, le=6)
    available: bool
    available_minutes: int | None = Field(default=None, ge=1, le=1440)

    @model_validator(mode="after")
    def validate_minutes(self) -> Self:
        if self.available and self.available_minutes is None:
            raise ValueError("available minutes are required for an available day")
        if not self.available and self.available_minutes is not None:
            raise ValueError("available minutes are unavailable for an unavailable day")
        return self


class PerformanceAnchorCreateInput(PlanningCommandInput):
    kind: str
    distance_m: float = Field(gt=0)
    duration_s: float = Field(gt=0)
    achieved_on: date
    reliable: bool = True
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("kind")
    @classmethod
    def validate_kind(cls, value: str) -> str:
        if value not in ANCHOR_KINDS:
            raise ValueError("unsupported performance anchor kind")
        return value


class PerformanceAnchorUpdateInput(PlanningCommandInput):
    kind: str | None = None
    distance_m: float | None = Field(default=None, gt=0)
    duration_s: float | None = Field(default=None, gt=0)
    achieved_on: date | None = None
    reliable: bool | None = None
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("kind")
    @classmethod
    def validate_kind(cls, value: str | None) -> str | None:
        if value is not None and value not in ANCHOR_KINDS:
            raise ValueError("unsupported performance anchor kind")
        return value

    @model_validator(mode="after")
    def require_change(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("at least one performance anchor field is required")
        required_fields = {"kind", "distance_m", "duration_s", "achieved_on", "reliable"}
        if any(
            field_name in self.model_fields_set and getattr(self, field_name) is None
            for field_name in required_fields
        ):
            raise ValueError("performance anchor required fields cannot be null")
        return self


class AcceptedCycleReference(PlanningCommandInput):
    cycle_id: int = Field(gt=0)
    accepted_revision_id: int = Field(gt=0)


class ReferencedGoalChangeConfirmation(PlanningCommandInput):
    goal_id: int = Field(gt=0)
    operation: Literal["update", "deactivate"]
    accepted_cycles: tuple[AcceptedCycleReference, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_unique_cycles(self) -> Self:
        cycle_ids = [reference.cycle_id for reference in self.accepted_cycles]
        if len(cycle_ids) != len(set(cycle_ids)):
            raise ValueError("accepted cycle references must be unique")
        return self


class PlanningInputCommandError(ValueError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def _goal_fact(goal: AthleteGoal) -> GoalFact:
    return GoalFact(
        id=goal.id,
        event_type=goal.event_type,
        event_name=goal.event_name,
        target_date=goal.target_date,
        status=goal.status,
    )


def _profile_fact(profile: AthletePlanningProfile) -> PlanningProfileFact:
    return PlanningProfileFact(
        experience_level=profile.experience_level,
        preferred_long_run_weekday=profile.preferred_long_run_weekday,
        self_declared_reentry=profile.self_declared_reentry,
        constraint_note=profile.constraint_note,
    )


def _availability_fact(availability: AthleteAvailability) -> AvailabilityFact:
    return AvailabilityFact(
        weekday=availability.weekday,
        available=availability.available,
        available_minutes=availability.available_minutes,
    )


def _performance_anchor_fact(anchor: PerformanceAnchor) -> PerformanceAnchorFact:
    return PerformanceAnchorFact(
        id=anchor.id,
        kind=anchor.kind,
        distance_m=anchor.distance_m,
        duration_s=anchor.duration_s,
        achieved_on=anchor.achieved_on,
        reliable=anchor.reliable,
        notes=anchor.notes,
    )


class PlanningInputCommands:
    """User-scoped mutations; each successful command owns commit and rollback."""

    def __init__(self, session: Session, user: User, *, as_of: date | None = None) -> None:
        self.session = session
        self.user_id = user.id
        self.as_of = as_of or date.today()

    def create_goal(self, data: GoalCreateInput) -> GoalFact:
        self._validate_goal_target(data.event_type, data.target_date, reject_past=True)
        goal = AthleteGoal(
            user_id=self.user_id,
            event_type=data.event_type,
            event_name=data.event_name or None,
            target_date=data.target_date,
        )
        self.session.add(goal)
        self._commit()
        return _goal_fact(goal)

    def update_goal(
        self,
        goal_id: int,
        data: GoalUpdateInput,
        *,
        confirmation: ReferencedGoalChangeConfirmation | None = None,
    ) -> GoalFact:
        goal = self._goal(goal_id)
        event_type = data.event_type if data.event_type is not None else goal.event_type
        target_date = (
            data.target_date if "target_date" in data.model_fields_set else goal.target_date
        )
        self._validate_goal_target(
            event_type,
            target_date,
            reject_past="target_date" in data.model_fields_set,
        )
        self._require_goal_confirmation(goal.id, confirmation, operation="update")
        for field_name in data.model_fields_set:
            value = getattr(data, field_name)
            if field_name == "event_name":
                value = value or None
            setattr(goal, field_name, value)
        self._commit()
        return _goal_fact(goal)

    def deactivate_goal(
        self,
        goal_id: int,
        *,
        confirmation: ReferencedGoalChangeConfirmation | None = None,
    ) -> GoalFact:
        goal = self._goal(goal_id)
        if goal.status != "archived":
            self._require_goal_confirmation(goal.id, confirmation, operation="deactivate")
            goal.status = "archived"
            self._commit()
        return _goal_fact(goal)

    def update_profile(self, data: PlanningProfileUpdateInput) -> PlanningProfileFact:
        profile = self.session.get(AthletePlanningProfile, self.user_id)
        if profile is None:
            profile = AthletePlanningProfile(user_id=self.user_id)
            self.session.add(profile)
        for field_name in data.model_fields_set:
            value = getattr(data, field_name)
            if field_name == "constraint_note":
                value = value or None
            setattr(profile, field_name, value)
        self._commit()
        return _profile_fact(profile)

    def set_availability(self, data: AvailabilityInput) -> AvailabilityFact:
        availability = self.session.scalar(
            select(AthleteAvailability).where(
                AthleteAvailability.user_id == self.user_id,
                AthleteAvailability.weekday == data.weekday,
            )
        )
        if availability is None:
            availability = AthleteAvailability(user_id=self.user_id, weekday=data.weekday)
            self.session.add(availability)
        availability.available = data.available
        availability.available_minutes = data.available_minutes
        self._commit()
        return _availability_fact(availability)

    def deactivate_availability(self, *, weekday: int) -> AvailabilityFact:
        availability = self.session.scalar(
            select(AthleteAvailability).where(
                AthleteAvailability.user_id == self.user_id,
                AthleteAvailability.weekday == weekday,
            )
        )
        if availability is None:
            raise PlanningInputCommandError(
                "Verfügbarkeit nicht gefunden.", code="planning.availability_not_found"
            )
        if availability.available or availability.available_minutes is not None:
            availability.available = False
            availability.available_minutes = None
            self._commit()
        return _availability_fact(availability)

    def create_performance_anchor(
        self, data: PerformanceAnchorCreateInput
    ) -> PerformanceAnchorFact:
        self._validate_anchor_date(data.achieved_on)
        anchor = PerformanceAnchor(
            user_id=self.user_id,
            kind=data.kind,
            distance_m=data.distance_m,
            duration_s=data.duration_s,
            achieved_on=data.achieved_on,
            reliable=data.reliable,
            notes=data.notes or None,
        )
        self.session.add(anchor)
        self._commit()
        return _performance_anchor_fact(anchor)

    def update_performance_anchor(
        self,
        anchor_id: int,
        data: PerformanceAnchorUpdateInput,
    ) -> PerformanceAnchorFact:
        anchor = self._performance_anchor(anchor_id)
        if "achieved_on" in data.model_fields_set:
            if data.achieved_on is None:
                raise PlanningInputCommandError(
                    "Das Leistungsdatum muss angegeben werden.",
                    code="planning.anchor_date_invalid",
                )
            self._validate_anchor_date(data.achieved_on)
        for field_name in data.model_fields_set:
            value = getattr(data, field_name)
            if field_name == "notes":
                value = value or None
            setattr(anchor, field_name, value)
        self._commit()
        return _performance_anchor_fact(anchor)

    def deactivate_performance_anchor(self, anchor_id: int) -> PerformanceAnchorFact:
        anchor = self._performance_anchor(anchor_id)
        if anchor.reliable:
            anchor.reliable = False
            self._commit()
        return _performance_anchor_fact(anchor)

    def _goal(self, goal_id: int) -> AthleteGoal:
        goal = self.session.scalar(
            select(AthleteGoal).where(
                AthleteGoal.id == goal_id,
                AthleteGoal.user_id == self.user_id,
            )
        )
        if goal is None:
            raise PlanningInputCommandError("Ziel nicht gefunden.", code="planning.goal_not_found")
        return goal

    def _validate_goal_target(
        self,
        event_type: str,
        target_date: date | None,
        *,
        reject_past: bool,
    ) -> None:
        if event_type != "general_fitness" and target_date is None:
            raise PlanningInputCommandError(
                "Für dieses Ziel muss ein Zieldatum angegeben werden.",
                code="planning.goal_target_date_required",
            )
        if reject_past and target_date is not None and target_date < self.as_of:
            raise PlanningInputCommandError(
                "Das Zieldatum darf nicht in der Vergangenheit liegen.",
                code="planning.goal_target_date_invalid",
            )

    def _performance_anchor(self, anchor_id: int) -> PerformanceAnchor:
        anchor = self.session.scalar(
            select(PerformanceAnchor).where(
                PerformanceAnchor.id == anchor_id,
                PerformanceAnchor.user_id == self.user_id,
            )
        )
        if anchor is None:
            raise PlanningInputCommandError(
                "Leistungsanker nicht gefunden.", code="planning.anchor_not_found"
            )
        return anchor

    def _validate_anchor_date(self, achieved_on: date) -> None:
        if achieved_on > self.as_of:
            raise PlanningInputCommandError(
                "Das Leistungsdatum darf nicht in der Zukunft liegen.",
                code="planning.anchor_date_invalid",
            )

    def _require_goal_confirmation(
        self,
        goal_id: int,
        confirmation: ReferencedGoalChangeConfirmation | None,
        *,
        operation: Literal["update", "deactivate"],
    ) -> None:
        references = tuple(
            self.session.execute(
                select(TrainingCycle.id, TrainingCycle.accepted_revision_id)
                .where(
                    TrainingCycle.user_id == self.user_id,
                    TrainingCycle.goal_id == goal_id,
                    TrainingCycle.accepted_revision_id.is_not(None),
                )
                .order_by(TrainingCycle.id)
            ).all()
        )
        if not references:
            return
        if confirmation is None:
            raise PlanningInputCommandError(
                "Die Änderung dieses verwendeten Ziels muss ausdrücklich bestätigt werden.",
                code="planning.goal_confirmation_required",
            )
        confirmed = tuple(
            sorted(
                (reference.cycle_id, reference.accepted_revision_id)
                for reference in confirmation.accepted_cycles
            )
        )
        expected = tuple((cycle_id, revision_id) for cycle_id, revision_id in references)
        if (
            confirmation.goal_id != goal_id
            or confirmation.operation != operation
            or confirmed != expected
        ):
            raise PlanningInputCommandError(
                "Die Bestätigung passt nicht zum verwendeten Ziel und Zyklus.",
                code="planning.goal_confirmation_mismatch",
            )

    def _commit(self) -> None:
        try:
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise

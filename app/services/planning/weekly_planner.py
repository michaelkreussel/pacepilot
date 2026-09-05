import hashlib
import json
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta

from sqlalchemy.orm import Session

from app.models import User
from app.repositories.activities import activities_between
from app.services.analytics.activity_semantics import is_running_sport
from app.services.analytics.athlete_data import AthleteDataService
from app.services.planning.planning_queries import get_planning_inputs
from app.services.planning.registry import get_knowledge_registry
from app.services.planning.registry_models import ContinuousStructure
from app.services.planning.safety_triage import build_proposal_safety_context
from app.services.planning.training_fit import assess_training_fit
from app.services.planning.workout_templates import (
    ExpandedWorkoutTemplate,
    TemplateEligibilityContext,
    TemplateExpansionError,
    TemplateParameters,
    expand_workout_template,
)

WEEKLY_PLANNER_VERSION = "weekly-shadow-planner-v1"
PLANNER_SCHEMA_VERSION = "weekly_plan_candidate.v1"
MIN_TYPICAL_WEEKLY_RUNS = 2
CONSISTENT_WEEKS_WINDOW_DAYS = 56
RUNS_PER_CONSISTENT_WEEK = 2
MAX_CONSISTENT_WEEKS = 8
LONG_RUN_REQUIRED_CONSISTENT_WEEKS = 4
CONSERVATIVE_FREQUENCY_CAP = 3
LONG_RUN_HISTORY_RATIO = 1.1
QUALITY_SPACING_HOURS = 48
STRIDES_ADJACENCY_TO_LONG_RUN_WARNING = "planner.strides_adjacent_to_long_run"
DURATION_GRID_MINUTES = 5
MAX_PLAN_DAYS = 6


class WeeklyPlannerError(ValueError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DayAvailability:
    weekday: int
    available_minutes: int


@dataclass(frozen=True)
class GoalSummary:
    event_type: str
    status: str
    target_date: date | None


@dataclass(frozen=True)
class WeeklyPlannerSnapshot:
    week_start: date
    as_of: date
    availability: tuple[DayAvailability, ...]
    preferred_long_run_weekday: int | None
    experience_level: str | None
    effective_reentry: bool
    goals: tuple[GoalSummary, ...]
    baseline_confidence: str
    typical_weekly_runs_median: float | None
    observed_runs_per_week: float
    consistent_running_weeks: int
    longest_run_28d_seconds: float | None
    typical_longest_run_seconds: float | None
    median_run_seconds: float | None
    hard_runs_28d: int
    intensity_mode: str
    intensity_confidence: str
    baseline_fingerprint: str
    intensity_fingerprint: str
    knowledge_base_version: str
    safety_outcome: str = "allow"


@dataclass(frozen=True)
class PlannedSessionCandidate:
    scheduled_for: date
    weekday: int
    template_id: str
    template_version: str
    name: str
    planned_minutes: int
    intensity_domain: str
    role: str
    rationale: str
    warnings: tuple[str, ...]
    load_estimate_json: dict[str, object]


@dataclass(frozen=True)
class SkippedDay:
    weekday: int
    reason_code: str


@dataclass(frozen=True)
class LongRunDecision:
    minutes: int | None
    skip_reason: str | None
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class WeeklyPlanCandidate:
    week_start: date
    week_end: date
    target_days: int
    sessions: tuple[PlannedSessionCandidate, ...]
    skipped_days: tuple[SkippedDay, ...]
    validation_report: dict[str, object]
    generation_context: dict[str, object]
    input_fingerprint: str
    planner_version: str
    knowledge_base_version: str


def _floor_grid(minutes: float) -> int:
    return int(minutes) // DURATION_GRID_MINUTES * DURATION_GRID_MINUTES


def _continuous_bounds(template_id: str) -> tuple[int, int, int]:
    structure = get_knowledge_registry().workouts[template_id].structure
    assert isinstance(structure, ContinuousStructure)
    return (
        structure.duration_minutes.minimum,
        structure.duration_minutes.default,
        structure.duration_minutes.maximum,
    )


def _easy_minutes(snapshot: WeeklyPlannerSnapshot) -> int:
    minimum, default, maximum = _continuous_bounds("easy_run")
    if snapshot.median_run_seconds is None:
        return default
    minutes = _floor_grid(snapshot.median_run_seconds / 60)
    return max(minimum, min(maximum, minutes))


def _long_run_decision(snapshot: WeeklyPlannerSnapshot) -> LongRunDecision:
    longest = snapshot.longest_run_28d_seconds
    if longest is None:
        return LongRunDecision(None, "planner.no_measurable_long_run_history", ())
    minimum, _, maximum = _continuous_bounds("long_run")
    bound_minutes = _floor_grid(longest * LONG_RUN_HISTORY_RATIO / 60)
    if bound_minutes < minimum:
        return LongRunDecision(
            None,
            "planner.long_run_below_template_minimum_after_history_bound",
            (),
        )
    minutes = min(bound_minutes, maximum)
    warnings: list[str] = []
    typical = snapshot.typical_longest_run_seconds
    if typical is not None and minutes * 60 > typical * LONG_RUN_HISTORY_RATIO:
        warnings.append("planner.long_run_above_typical_weekly_longest")
    return LongRunDecision(minutes, None, tuple(warnings))


def _strides_eligible(snapshot: WeeklyPlannerSnapshot, consistent_running_weeks: int) -> bool:
    return (
        consistent_running_weeks >= LONG_RUN_REQUIRED_CONSISTENT_WEEKS
        and snapshot.hard_runs_28d > 0
    )


def _composition(target_days: int, long_ok: bool, strides_ok: bool) -> list[str]:
    roles: list[str] = []
    if target_days >= 2 and long_ok:
        roles.append("long_run")
    if target_days >= 4 and strides_ok:
        roles.append("strides")
    while len(roles) < target_days:
        roles.append("easy_run")
    return roles[:target_days]


def _spacing_hours(first_weekday: int, second_weekday: int) -> float:
    return abs(first_weekday - second_weekday) * 24.0


def _assign_strides_day(
    availability: tuple[DayAvailability, ...],
    required_minutes: int,
    taken: set[int],
) -> tuple[int | None, dict[int, str]]:
    skips: dict[int, str] = {}
    for day in sorted(availability, key=lambda item: item.weekday):
        if day.weekday in taken:
            continue
        if day.available_minutes < required_minutes:
            skips[day.weekday] = "planner.budget_below_quality_requirement"
            continue
        if any(_spacing_hours(day.weekday, other) < QUALITY_SPACING_HOURS for other in taken):
            skips[day.weekday] = "planner.quality_spacing_violation"
            continue
        return day.weekday, skips
    return None, skips


def _weekly_history_advisory(snapshot: WeeklyPlannerSnapshot) -> dict[str, object]:
    warnings: list[str] = []
    evidence: list[dict[str, object]] = []
    typical = snapshot.typical_weekly_runs_median
    if typical is None or typical < MIN_TYPICAL_WEEKLY_RUNS:
        warnings.append("planner.weekly_frequency_low")
        evidence.append(
            {
                "code": "planner.weekly_frequency_low",
                "source": "running.frequency",
                "observed_on": snapshot.as_of.isoformat(),
                "value": typical,
                "unit": "runs_per_week",
                "severe": False,
            }
        )
    if snapshot.baseline_confidence == "insufficient":
        warnings.append("planner.baseline_confidence_insufficient")
        evidence.append(
            {
                "code": "planner.baseline_confidence_insufficient",
                "source": "running.history",
                "observed_on": snapshot.as_of.isoformat(),
                "value": snapshot.baseline_confidence,
                "unit": "confidence",
                "severe": False,
            }
        )
    elif snapshot.baseline_confidence == "low":
        warnings.append("planner.baseline_confidence_low")
        evidence.append(
            {
                "code": "planner.baseline_confidence_low",
                "source": "running.history",
                "observed_on": snapshot.as_of.isoformat(),
                "value": snapshot.baseline_confidence,
                "unit": "confidence",
                "severe": False,
            }
        )
    if snapshot.effective_reentry:
        warnings.append("planner.reentry_conservative")
        evidence.append(
            {
                "code": "planner.reentry_conservative",
                "source": "running.reentry",
                "observed_on": snapshot.as_of.isoformat(),
                "value": True,
                "unit": "reentry",
                "severe": False,
            }
        )
    if snapshot.consistent_running_weeks < LONG_RUN_REQUIRED_CONSISTENT_WEEKS:
        warnings.append("planner.consistent_weeks_sparse")
        evidence.append(
            {
                "code": "planner.consistent_weeks_sparse",
                "source": "running.consistency",
                "observed_on": snapshot.as_of.isoformat(),
                "value": snapshot.consistent_running_weeks,
                "unit": "weeks",
                "severe": False,
            }
        )
    if (
        typical is None
        or typical < MIN_TYPICAL_WEEKLY_RUNS
        or snapshot.baseline_confidence in {"insufficient", "low"}
        or snapshot.effective_reentry
    ):
        confidence = "low"
    else:
        confidence = snapshot.baseline_confidence
    coverage: list[dict[str, object]] = [
        {
            "metric": "running_history_28_days",
            "current_day": snapshot.as_of.isoformat(),
            "baseline_sample_count": snapshot.consistent_running_weeks,
            "minimum_baseline_samples": LONG_RUN_REQUIRED_CONSISTENT_WEEKS,
            "sufficient_for_elevation": False,
        },
        {
            "metric": "weekly_frequency",
            "current_day": snapshot.as_of.isoformat(),
            "baseline_sample_count": int(typical) if typical is not None else 0,
            "minimum_baseline_samples": MIN_TYPICAL_WEEKLY_RUNS,
            "sufficient_for_elevation": False,
        },
    ]
    if warnings:
        recommendation = (
            "Der angeforderte Wochenentwurf bleibt verfügbar; prüfe vor der Annahme die "
            "konservativere Alternative."
        )
    else:
        recommendation = "Der angeforderte Wochenentwurf passt zu den aktuell verfügbaren Daten."
    return {
        "confidence": confidence,
        "warnings": warnings,
        "evidence": evidence,
        "coverage": coverage,
        "recommendation": recommendation,
        "alternative": {"type": "conservative_week", "code": "reduced_frequency"},
    }


def compose_week(
    snapshot: WeeklyPlannerSnapshot,
    *,
    enforce_history_gates: bool = True,
    enable_deferred_quality: bool = False,
) -> WeeklyPlanCandidate:
    # Advisory mode: sparse history, low frequency, and re-entry are warnings,
    # never refusals. The flag stays for caller compatibility.
    _ = enforce_history_gates
    registry = get_knowledge_registry()
    available_days = snapshot.availability
    if not available_days:
        raise WeeklyPlannerError(
            "Für diese Woche sind keine verfügbaren Lauftage erfasst.",
            code="planner.no_available_days",
        )
    typical = snapshot.typical_weekly_runs_median
    advisory = _weekly_history_advisory(snapshot)

    effective_consistent_weeks = snapshot.consistent_running_weeks
    effective_runs_per_week = max(round(snapshot.observed_runs_per_week), 1)
    if enable_deferred_quality:
        frequency_cap = MAX_PLAN_DAYS
        effective_consistent_weeks = max(effective_consistent_weeks, 8)
        effective_runs_per_week = max(effective_runs_per_week, 3)
    else:
        if typical is None:
            frequency_cap = MIN_TYPICAL_WEEKLY_RUNS
        else:
            frequency_cap = max(int(typical), MIN_TYPICAL_WEEKLY_RUNS)
        if snapshot.baseline_confidence in {"insufficient", "low"} or snapshot.effective_reentry:
            frequency_cap = min(frequency_cap, CONSERVATIVE_FREQUENCY_CAP)
    target_days = min(frequency_cap, len(available_days), MAX_PLAN_DAYS)

    long_decision = _long_run_decision(snapshot)
    long_ok = (
        long_decision.minutes is not None
        and effective_consistent_weeks >= LONG_RUN_REQUIRED_CONSISTENT_WEEKS
    )
    strides_ok = target_days >= 4 and _strides_eligible(snapshot, effective_consistent_weeks)
    roles = _composition(target_days, long_ok, strides_ok)

    placements: list[tuple[str, DayAvailability, int]] = []
    taken: set[int] = set()
    skips: dict[int | None, str] = {}
    warnings_by_day: dict[int, list[str]] = {}

    if long_ok and "long_run" in roles:
        assert long_decision.minutes is not None
        fitting = [day for day in available_days if day.available_minutes >= long_decision.minutes]
        chosen: DayAvailability | None = None
        if fitting:
            preferred_match = (
                next(
                    (day for day in fitting if day.weekday == snapshot.preferred_long_run_weekday),
                    None,
                )
                if snapshot.preferred_long_run_weekday is not None
                else None
            )
            chosen = (
                preferred_match
                or sorted(fitting, key=lambda day: (-day.available_minutes, -day.weekday))[0]
            )
        if chosen is None:
            roles.remove("long_run")
            roles.append("easy_run")
            skips[None] = long_decision.skip_reason or "planner.long_run_not_placeable"
        else:
            placements.append(("long_run", chosen, long_decision.minutes))
            taken.add(chosen.weekday)
            for warning in long_decision.warnings:
                warnings_by_day.setdefault(chosen.weekday, []).append(warning)
    elif not long_ok:
        skips[None] = (
            long_decision.skip_reason or "planner.long_run_requires_consistent_running_weeks"
        )

    strides_template: ExpandedWorkoutTemplate | None = None
    if "strides" in roles:
        try:
            strides_template = expand_workout_template(
                "strides",
                None,
                eligibility=TemplateEligibilityContext(
                    consistent_running_weeks=effective_consistent_weeks,
                    runs_per_week=effective_runs_per_week,
                    available_minutes=10_080,
                    facts={"familiar_with_relaxed_fast_running"},
                ),
            )
        except TemplateExpansionError:
            strides_template = None
        if strides_template is None:
            roles.remove("strides")
            roles.append("easy_run")
            skips[None] = "planner.strides_not_eligible"
        else:
            required_minutes = -(-int(strides_template.load_estimate.duration_seconds) // 60)
            strides_day, stride_skips = _assign_strides_day(available_days, required_minutes, taken)
            for skip_weekday, skip_reason in stride_skips.items():
                skips[skip_weekday] = skip_reason
            if strides_day is None:
                roles.remove("strides")
                roles.append("easy_run")
            else:
                strides_choice = next(day for day in available_days if day.weekday == strides_day)
                placements.append(("strides", strides_choice, required_minutes))
                taken.add(strides_day)

    easy_minimum, _, _ = _continuous_bounds("easy_run")
    easy_minutes = _easy_minutes(snapshot)
    easy_slots_needed = sum(1 for role in roles if role == "easy_run")
    easy_assignments: list[tuple[DayAvailability, int]] = []
    for day in sorted(available_days, key=lambda item: item.weekday):
        if len(easy_assignments) >= easy_slots_needed:
            break
        if day.weekday in taken:
            skips.pop(day.weekday, None)
            continue
        if day.available_minutes < easy_minimum:
            skips[day.weekday] = "planner.budget_below_easy_minimum"
            continue
        easy_assignments.append((day, min(easy_minutes, day.available_minutes)))
    if len(easy_assignments) < easy_slots_needed:
        target_days = len(taken) + len(easy_assignments)
        roles = [role for role in roles if role != "easy_run"] + ["easy_run"] * len(
            easy_assignments
        )
    for day, minutes in easy_assignments:
        placements.append(("easy_run", day, minutes))
        taken.add(day.weekday)
        skips.pop(day.weekday, None)

    session_candidates: list[PlannedSessionCandidate] = []
    for role, day, minutes in placements:
        facts: set[str] = set()
        if role == "long_run":
            facts.add("sufficient_recent_long_run_baseline")
        template_id = role
        if role == "strides":
            facts.add("familiar_with_relaxed_fast_running")
            assert strides_template is not None
            expanded = strides_template
            template_id = expanded.template_id
        else:
            try:
                expanded = expand_workout_template(
                    role,
                    TemplateParameters(duration_minutes=minutes),
                    eligibility=TemplateEligibilityContext(
                        consistent_running_weeks=effective_consistent_weeks,
                        runs_per_week=effective_runs_per_week,
                        available_minutes=day.available_minutes,
                        safety_stop=False,
                        facts=facts,
                    ),
                )
            except TemplateExpansionError as exc:
                raise WeeklyPlannerError(
                    str(exc), code="planner.template_expansion_failed"
                ) from exc
        scheduled_for = snapshot.week_start + timedelta(days=day.weekday)
        session_warnings = list(warnings_by_day.get(day.weekday, ()))
        if role == "strides" and any(
            placement_role == "long_run" and _spacing_hours(day.weekday, placed_day.weekday) < 24
            for placement_role, placed_day, _ in placements
        ):
            session_warnings.append(STRIDES_ADJACENCY_TO_LONG_RUN_WARNING)
        domain = get_knowledge_registry().workouts[template_id].intensity_domain
        session_candidates.append(
            PlannedSessionCandidate(
                scheduled_for=scheduled_for,
                weekday=day.weekday,
                template_id=expanded.template_id,
                template_version=expanded.template_version,
                name=expanded.name,
                planned_minutes=minutes,
                intensity_domain=domain,
                role=role,
                rationale=_rationale(role),
                warnings=tuple(session_warnings),
                load_estimate_json=expanded.load_estimate.model_dump(mode="json"),
            )
        )

    skipped_tuple = tuple(
        SkippedDay(weekday=weekday if weekday is not None else -1, reason_code=reason)
        for weekday, reason in sorted(skips.items(), key=lambda item: (item[0] is None, item[0]))
    )
    quality_weekdays = sorted(
        session.weekday for session in session_candidates if session.intensity_domain != "low"
    )
    spacing_ok = all(
        _spacing_hours(first, second) >= QUALITY_SPACING_HOURS
        for index, first in enumerate(quality_weekdays)
        for second in quality_weekdays[index + 1 :]
    )
    placed_roles = {session.role for session in session_candidates}
    validation_report: dict[str, object] = {
        "valid": spacing_ok,
        "rule_set_version": f"{WEEKLY_PLANNER_VERSION}+{registry.version}",
        "checks": [
            {"code": "planner.templates.active_only", "result": "pass"},
            {"code": "planner.availability.respected", "result": "pass"},
            {"code": "planner.budget.respected", "result": "pass"},
            {
                "code": "planner.history_gates",
                "result": "advisory" if advisory["warnings"] else "pass",
            },
            {"code": "planner.quality_spacing", "result": "pass" if spacing_ok else "fail"},
            {
                "code": "planner.longrun.history_bound",
                "result": "pass" if "long_run" in placed_roles else "not_applicable",
            },
            {"code": "planner.no_catchup", "result": "pass"},
        ],
    }
    generation_context = _generation_context(
        snapshot,
        target_days,
        advisory=advisory,
        effective_consistent_weeks=effective_consistent_weeks,
        effective_runs_per_week=effective_runs_per_week,
    )
    fingerprint = _fingerprint_candidate(
        generation_context, tuple(session_candidates), snapshot.knowledge_base_version
    )
    return WeeklyPlanCandidate(
        week_start=snapshot.week_start,
        week_end=snapshot.week_start + timedelta(days=6),
        target_days=target_days,
        sessions=tuple(session_candidates),
        skipped_days=skipped_tuple,
        validation_report=validation_report,
        generation_context=generation_context,
        input_fingerprint=fingerprint,
        planner_version=WEEKLY_PLANNER_VERSION,
        knowledge_base_version=snapshot.knowledge_base_version,
    )


def _rationale(role: str) -> str:
    rationales = {
        "long_run": (
            "Der lange lockere Lauf baut die aerobe Ausdauer auf und bleibt bewusst im Rahmen "
            "deiner jüngsten Long-Run-Historie."
        ),
        "strides": (
            "Steigerungen nach einem lockeren Lauf erhalten Laufökonomie und neuromuskuläre "
            "Qualität, ohne einen harten Intervalltag zu erzeugen."
        ),
        "easy_run": (
            "Ein lockerer Lauf hält Frequenz und aerobe Basis; Dauer und RPE folgen deinem "
            "üblichen Umfang."
        ),
    }
    return rationales[role]


def _fingerprint_candidate(
    generation_context: dict[str, object],
    sessions: tuple[PlannedSessionCandidate, ...],
    knowledge_base_version: str,
) -> str:
    fingerprint_input = {
        **json.loads(json.dumps(generation_context, sort_keys=True, separators=(",", ":"))),
        "planner_version": WEEKLY_PLANNER_VERSION,
        "knowledge_base_version": knowledge_base_version,
        "sessions": [
            {
                "scheduled_for": session.scheduled_for.isoformat(),
                "role": session.role,
                "template_version": session.template_version,
                "planned_minutes": session.planned_minutes,
                "warnings": list(session.warnings),
            }
            for session in sessions
        ],
    }
    encoded = json.dumps(
        fingerprint_input, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _generation_context(
    snapshot: WeeklyPlannerSnapshot,
    target_days: int,
    *,
    advisory: dict[str, object],
    effective_consistent_weeks: int,
    effective_runs_per_week: int,
) -> dict[str, object]:
    return {
        "schema_version": PLANNER_SCHEMA_VERSION,
        "as_of": snapshot.as_of.isoformat(),
        "week_start": snapshot.week_start.isoformat(),
        "week_end": (snapshot.week_start + timedelta(days=6)).isoformat(),
        "target_days": target_days,
        "history_gates": {
            "enabled": False,
            "mode": "advisory",
            "effective_consistent_running_weeks": effective_consistent_weeks,
            "effective_runs_per_week": effective_runs_per_week,
        },
        "advisory": advisory,
        "safety": {"outcome": snapshot.safety_outcome},
        "availability": [
            {"weekday": day.weekday, "available_minutes": day.available_minutes}
            for day in snapshot.availability
        ],
        "profile": {
            "experience_level": snapshot.experience_level,
            "preferred_long_run_weekday": snapshot.preferred_long_run_weekday,
            "effective_reentry": snapshot.effective_reentry,
        },
        "goals": [
            {
                "event_type": goal.event_type,
                "status": goal.status,
                "target_date": goal.target_date.isoformat() if goal.target_date else None,
            }
            for goal in snapshot.goals
        ],
        "baseline": {
            "confidence": snapshot.baseline_confidence,
            "typical_weekly_runs_median": snapshot.typical_weekly_runs_median,
            "observed_runs_per_week": snapshot.observed_runs_per_week,
            "consistent_running_weeks": snapshot.consistent_running_weeks,
            "longest_run_28d_seconds": snapshot.longest_run_28d_seconds,
            "typical_longest_run_seconds": snapshot.typical_longest_run_seconds,
            "median_run_seconds": snapshot.median_run_seconds,
            "hard_runs_28d": snapshot.hard_runs_28d,
            "input_fingerprint": snapshot.baseline_fingerprint,
        },
        "intensity": {
            "mode": snapshot.intensity_mode,
            "confidence": snapshot.intensity_confidence,
            "input_fingerprint": snapshot.intensity_fingerprint,
        },
        "units": {"duration": "seconds", "duration_grid": "minutes"},
    }


def plan_shadow_week(
    session: Session,
    user: User,
    *,
    week_start: date,
    as_of: date,
    availability: tuple[DayAvailability, ...] | list[DayAvailability] | None = None,
) -> WeeklyPlanCandidate:
    if week_start.weekday() != 0:
        raise WeeklyPlannerError(
            "Die Woche muss an einem Montag beginnen.",
            code="planner.week_start_invalid",
        )
    safety = build_proposal_safety_context(session, user.id, now=datetime.combine(as_of, time.max))
    planning_inputs = get_planning_inputs(session, user.id, as_of=as_of)
    profile = planning_inputs.profile
    shadow = AthleteDataService(session, user.id, as_of=as_of).get_running_shadow_analysis(
        performance_anchors=planning_inputs.performance_anchors
    )
    window28 = shadow.baseline.window(28)
    window56 = shadow.baseline.window(56)
    resolved_availability = (
        tuple(availability)
        if availability is not None
        else tuple(
            DayAvailability(weekday=row.weekday, available_minutes=int(row.available_minutes or 0))
            for row in planning_inputs.availability
        )
    )
    if not resolved_availability:
        raise WeeklyPlannerError(
            "Für diese Woche sind keine verfügbaren Lauftage erfasst.",
            code="planner.no_available_days",
        )
    if any(
        day.weekday < 0 or day.weekday > 6 or day.available_minutes <= 0
        for day in resolved_availability
    ):
        raise WeeklyPlannerError(
            "Die angegebene Verfügbarkeit ist ungültig.",
            code="planner.availability_invalid",
        )
    snapshot = WeeklyPlannerSnapshot(
        week_start=week_start,
        as_of=as_of,
        availability=resolved_availability,
        preferred_long_run_weekday=(
            profile.preferred_long_run_weekday if profile is not None else None
        ),
        experience_level=profile.experience_level if profile is not None else None,
        effective_reentry=bool(
            shadow.baseline.reentry.active
            or (profile is not None and profile.self_declared_reentry)
        ),
        goals=tuple(
            GoalSummary(
                event_type=goal.event_type,
                status=goal.status,
                target_date=goal.target_date,
            )
            for goal in planning_inputs.goals
        ),
        baseline_confidence=window56.quality.confidence,
        typical_weekly_runs_median=(
            float(window28.weekly_runs.median) if window28.weekly_runs.median is not None else None
        ),
        observed_runs_per_week=window28.frequency_per_week,
        consistent_running_weeks=_count_consistent_weeks(session, user.id, as_of),
        longest_run_28d_seconds=(
            float(window28.longest_duration.value)
            if window28.longest_duration.value is not None
            else None
        ),
        typical_longest_run_seconds=(
            float(window28.weekly_longest_duration_s.median)
            if window28.weekly_longest_duration_s.median is not None
            else None
        ),
        median_run_seconds=(
            float(window28.per_run_duration_s.median)
            if window28.per_run_duration_s.median is not None
            else None
        ),
        hard_runs_28d=window28.hard_runs,
        intensity_mode=shadow.intensity.mode,
        intensity_confidence=shadow.intensity.confidence,
        baseline_fingerprint=shadow.baseline.input_fingerprint,
        intensity_fingerprint=shadow.intensity.input_fingerprint,
        knowledge_base_version=get_knowledge_registry().version,
        safety_outcome=safety.report.outcome.value,
    )
    candidate = compose_week(snapshot)
    assessment = assess_training_fit(
        session,
        user.id,
        effective_workout_date=as_of,
        revision_fingerprint=f"weekly-plan:{week_start.isoformat()}:{as_of.isoformat()}",
        evaluated_at=datetime.combine(as_of, time.max),
    )
    context = dict(candidate.generation_context)
    raw_advisory = context.get("advisory")
    advisory = dict(raw_advisory) if isinstance(raw_advisory, dict) else {}
    raw_warnings = advisory.get("warnings", [])
    raw_evidence = advisory.get("evidence", [])
    raw_coverage = advisory.get("coverage", [])
    warnings = list(raw_warnings) if isinstance(raw_warnings, list) else []
    evidence = list(raw_evidence) if isinstance(raw_evidence, list) else []
    coverage = list(raw_coverage) if isinstance(raw_coverage, list) else []
    for code in assessment.warning_codes:
        if code not in warnings:
            warnings.append(code)
    for item in assessment.evidence:
        evidence.append(
            {
                "code": item.code,
                "source": item.source,
                "observed_on": item.observed_on.isoformat(),
                "value": item.value,
                "unit": item.unit,
                "severe": item.severe,
            }
        )
    for item in assessment.coverage:
        coverage.append(
            {
                "metric": item.metric,
                "current_day": item.current_day.isoformat() if item.current_day else None,
                "baseline_sample_count": item.baseline_sample_count,
                "minimum_baseline_samples": item.minimum_baseline_samples,
                "sufficient_for_elevation": item.sufficient_for_elevation,
            }
        )
    confidence = str(advisory.get("confidence", "medium"))
    if assessment.outcome.value == "elevated":
        confidence = "low"
    elif assessment.outcome.value == "caution" and confidence in {"high", "medium"}:
        confidence = "low" if warnings else confidence
    if warnings:
        recommendation = (
            "Der angeforderte Wochenentwurf bleibt verfügbar; prüfe vor der Annahme die "
            "konservativere Alternative."
        )
    else:
        recommendation = "Der angeforderte Wochenentwurf passt zu den aktuell verfügbaren Daten."
    alternative = advisory.get("alternative")
    if not isinstance(alternative, dict):
        alternative = {"type": "conservative_week", "code": "reduced_frequency"}
    advisory = {
        **advisory,
        "confidence": confidence,
        "warnings": warnings,
        "evidence": evidence,
        "coverage": coverage,
        "recommendation": recommendation,
        "alternative": alternative,
        "training_fit": {
            "outcome": assessment.outcome.value,
            "policy_version": assessment.policy_version,
            "authoritative_input_fingerprint": assessment.authoritative_input_fingerprint,
        },
    }
    context["advisory"] = advisory
    fingerprint = _fingerprint_candidate(
        context, candidate.sessions, candidate.knowledge_base_version
    )

    return replace(candidate, generation_context=context, input_fingerprint=fingerprint)


def _count_consistent_weeks(session: Session, user_id: int, as_of: date) -> int:
    start = as_of - timedelta(days=CONSISTENT_WEEKS_WINDOW_DAYS - 1)
    rows = activities_between(
        session,
        user_id,
        datetime.combine(start, time.min),
        datetime.combine(as_of + timedelta(days=1), time.min),
    )
    run_days = [
        row.started_at.date()
        for row in rows
        if is_running_sport(row.activity_type) and row.started_at.date() <= as_of
    ]
    weeks = 0
    for offset in range(MAX_CONSISTENT_WEEKS):
        end = as_of - timedelta(days=7 * offset)
        window_start = end - timedelta(days=6)
        runs_in_week = sum(1 for day in run_days if window_start <= day <= end)
        if runs_in_week < RUNS_PER_CONSISTENT_WEEK:
            break
        weeks += 1
    return weeks

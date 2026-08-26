import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AthleteAvailability,
    AthleteGoal,
    AthletePlanningProfile,
    PerformanceAnchor,
    User,
)
from app.repositories.activities import activities_between
from app.services.analytics.activity_semantics import is_running_sport
from app.services.analytics.athlete_data import AthleteDataService
from app.services.analytics.running_intensity import PerformanceAnchorInput
from app.services.planning.registry import get_knowledge_registry
from app.services.planning.registry_models import ContinuousStructure
from app.services.planning.safety_triage import build_proposal_safety_context
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
CONSISTENT_WEEKS_WINDOW_DAYS = 28
RUNS_PER_CONSISTENT_WEEK = 2
MAX_CONSISTENT_WEEKS = 4
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


def _strides_eligible(snapshot: WeeklyPlannerSnapshot) -> bool:
    return (
        snapshot.consistent_running_weeks >= LONG_RUN_REQUIRED_CONSISTENT_WEEKS
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


def compose_week(snapshot: WeeklyPlannerSnapshot) -> WeeklyPlanCandidate:
    registry = get_knowledge_registry()
    available_days = snapshot.availability
    if not available_days:
        raise WeeklyPlannerError(
            "Für diese Woche sind keine verfügbaren Lauftage erfasst.",
            code="planner.no_available_days",
        )
    typical = snapshot.typical_weekly_runs_median
    if typical is None or typical < MIN_TYPICAL_WEEKLY_RUNS:
        raise WeeklyPlannerError(
            "Für eine Wochenplanung fehlt eine beobachtete wöchentliche Laufroutine "
            "(weniger als zwei Läufe pro Woche in den letzten 28 Tagen).",
            code="planner.insufficient_frequency_history",
        )
    if snapshot.baseline_confidence == "insufficient":
        raise WeeklyPlannerError(
            "Die Datenqualität deiner Baseline reicht für eine Wochenplanung noch nicht aus.",
            code="planner.insufficient_data",
        )

    frequency_cap = max(int(typical), MIN_TYPICAL_WEEKLY_RUNS)
    if snapshot.baseline_confidence == "low" or snapshot.effective_reentry:
        frequency_cap = min(frequency_cap, CONSERVATIVE_FREQUENCY_CAP)
    target_days = min(frequency_cap, len(available_days), MAX_PLAN_DAYS)

    long_decision = _long_run_decision(snapshot)
    long_ok = (
        long_decision.minutes is not None
        and snapshot.consistent_running_weeks >= LONG_RUN_REQUIRED_CONSISTENT_WEEKS
    )
    strides_ok = target_days >= 4 and _strides_eligible(snapshot)
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
                    consistent_running_weeks=snapshot.consistent_running_weeks,
                    runs_per_week=max(round(snapshot.observed_runs_per_week), 1),
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
                        consistent_running_weeks=snapshot.consistent_running_weeks,
                        runs_per_week=max(round(snapshot.observed_runs_per_week), 1),
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
            {"code": "planner.quality_spacing", "result": "pass" if spacing_ok else "fail"},
            {
                "code": "planner.longrun.history_bound",
                "result": "pass" if "long_run" in placed_roles else "not_applicable",
            },
            {"code": "planner.no_catchup", "result": "pass"},
        ],
    }
    generation_context = _generation_context(snapshot, target_days)
    fingerprint_input = {
        **json.loads(json.dumps(generation_context, sort_keys=True, separators=(",", ":"))),
        "planner_version": WEEKLY_PLANNER_VERSION,
        "knowledge_base_version": snapshot.knowledge_base_version,
        "sessions": [
            {
                "scheduled_for": session.scheduled_for.isoformat(),
                "role": session.role,
                "template_version": session.template_version,
                "planned_minutes": session.planned_minutes,
                "warnings": list(session.warnings),
            }
            for session in session_candidates
        ],
    }
    encoded = json.dumps(
        fingerprint_input, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str
    ).encode()
    return WeeklyPlanCandidate(
        week_start=snapshot.week_start,
        week_end=snapshot.week_start + timedelta(days=6),
        target_days=target_days,
        sessions=tuple(session_candidates),
        skipped_days=skipped_tuple,
        validation_report=validation_report,
        generation_context=generation_context,
        input_fingerprint=hashlib.sha256(encoded).hexdigest(),
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


def _generation_context(snapshot: WeeklyPlannerSnapshot, target_days: int) -> dict[str, object]:
    return {
        "schema_version": PLANNER_SCHEMA_VERSION,
        "as_of": snapshot.as_of.isoformat(),
        "week_start": snapshot.week_start.isoformat(),
        "week_end": (snapshot.week_start + timedelta(days=6)).isoformat(),
        "target_days": target_days,
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


def plan_shadow_week(session: Session, user: User, *, week_start: date) -> WeeklyPlanCandidate:
    if week_start.weekday() != 0:
        raise WeeklyPlannerError(
            "Die Woche muss an einem Montag beginnen.",
            code="planner.week_start_invalid",
        )
    safety = build_proposal_safety_context(session, user.id)
    if not safety.report.valid:
        raise WeeklyPlannerError(
            "Ein aktueller Sicherheitshinweis blockiert die Wochenplanung.",
            code="planner.safety_blocked",
        )
    profile = session.get(AthletePlanningProfile, user.id)
    goals = session.scalars(
        select(AthleteGoal)
        .where(AthleteGoal.user_id == user.id, AthleteGoal.status == "active")
        .order_by(AthleteGoal.target_date, AthleteGoal.id)
    ).all()
    availability_rows = session.scalars(
        select(AthleteAvailability)
        .where(
            AthleteAvailability.user_id == user.id,
            AthleteAvailability.available.is_(True),
        )
        .order_by(AthleteAvailability.weekday)
    ).all()
    anchors = session.scalars(
        select(PerformanceAnchor)
        .where(PerformanceAnchor.user_id == user.id)
        .order_by(PerformanceAnchor.achieved_on, PerformanceAnchor.id)
    ).all()
    anchor_inputs = tuple(
        PerformanceAnchorInput(
            kind=anchor.kind,
            achieved_on=anchor.achieved_on,
            distance_m=anchor.distance_m,
            duration_s=anchor.duration_s,
            reliable=anchor.reliable,
        )
        for anchor in anchors
    )
    as_of = date.today()
    shadow = AthleteDataService(session, user.id, as_of=as_of).get_running_shadow_analysis(
        performance_anchors=anchor_inputs
    )
    window28 = shadow.baseline.window(28)
    window56 = shadow.baseline.window(56)
    snapshot = WeeklyPlannerSnapshot(
        week_start=week_start,
        as_of=as_of,
        availability=tuple(
            DayAvailability(weekday=row.weekday, available_minutes=int(row.available_minutes or 0))
            for row in availability_rows
        ),
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
            for goal in goals
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
    )
    return compose_week(snapshot)


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

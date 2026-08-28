import hashlib
import json
from dataclasses import dataclass, replace
from datetime import date, timedelta
from math import ceil

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import deferred_quality_templates_enabled
from app.models import (
    TrainingCycle,
    TrainingCycleRevision,
    TrainingCycleWeek,
    TrainingPlan,
    TrainingPlanRevision,
    User,
)
from app.services.planning.planning_queries import get_active_goal, get_planning_profile
from app.services.planning.registry import get_knowledge_registry
from app.services.planning.registry_models import ContinuousStructure
from app.services.planning.weekly_plan_service import (
    _persist_week_candidate,
)
from app.services.planning.weekly_planner import (
    WeeklyPlanCandidate,
    plan_shadow_week,
)
from app.services.planning.workout_templates import (
    TemplateEligibilityContext,
    TemplateExpansionError,
    expand_workout_template,
)

MULTIWEEK_PLANNER_VERSION = "multiweek-planner-v1"
MULTIWEEK_SCHEMA_VERSION = "training_cycle_candidate.v1"
SUPPORTED_PHASES = ("reentry", "base", "build", "specific", "taper", "recovery")
MIN_CYCLE_WEEKS = {
    "general_fitness": 4,
    "5k": 6,
    "10k": 6,
    "half_marathon": 8,
    "marathon": 12,
}
MAX_CYCLE_WEEKS = 52


@dataclass(frozen=True)
class CycleRuleProfile:
    volume_multiplier: float
    max_weekly_volume_increase: float
    max_long_run_increase: float
    max_quality_sessions: int
    minimum_taper_reduction: float
    maximum_taper_reduction: float


CYCLE_RULE_PROFILES = {
    "general_fitness": CycleRuleProfile(0.90, 1.08, 1.08, 1, 0.05, 0.45),
    "5k": CycleRuleProfile(0.95, 1.08, 1.08, 1, 0.05, 0.45),
    "10k": CycleRuleProfile(1.00, 1.10, 1.10, 1, 0.05, 0.45),
    "half_marathon": CycleRuleProfile(1.05, 1.10, 1.10, 1, 0.05, 0.45),
    "marathon": CycleRuleProfile(1.10, 1.08, 1.08, 1, 0.05, 0.45),
}


class MultiweekPlannerError(ValueError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class TrainingCyclePersistenceError(ValueError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CycleWeekCandidate:
    position: int
    week_start: date
    phase: str
    volume_factor: float
    weekly_plan: WeeklyPlanCandidate
    rationale: str

    @property
    def total_minutes(self) -> int:
        return sum(item.planned_minutes for item in self.weekly_plan.sessions)


@dataclass(frozen=True)
class TrainingCycleCandidate:
    goal_id: int | None
    event_type: str
    start_date: date
    target_date: date
    weeks: tuple[CycleWeekCandidate, ...]
    confidence: str
    assumptions: dict[str, object]
    impact: dict[str, object]
    validation_report: dict[str, object]
    input_fingerprint: str
    planner_version: str
    knowledge_base_version: str


def minimum_cycle_weeks(event_type: str) -> int:
    try:
        return MIN_CYCLE_WEEKS[event_type]
    except KeyError as exc:
        raise MultiweekPlannerError(
            "Dieser Zieltyp wird für Mehrwochenpläne nicht unterstützt.",
            code="cycle.goal_type_unsupported",
        ) from exc


def _week_count(start_date: date, target_date: date) -> int:
    target_week_start = target_date - timedelta(days=target_date.weekday())
    return ((target_week_start - start_date).days // 7) + 1


def _phase_for_week(
    position: int, week_count: int, *, effective_reentry: bool, interrupted: bool
) -> str:
    if interrupted:
        return "recovery"
    if effective_reentry and position < 2:
        return "reentry"
    if week_count >= 6 and position >= week_count - 2:
        return "taper"
    base_weeks = max(2, round(week_count * 0.3))
    specific_start = max(base_weeks, week_count - 3)
    if position < base_weeks:
        return "base"
    if position < specific_start:
        return "build"
    return "specific"


def _volume_factor(phase: str, position: int, week_count: int, profile: CycleRuleProfile) -> float:
    if phase == "reentry":
        factor = 0.80
    elif phase == "base":
        factor = min(0.95, 0.80 + position * 0.05)
    elif phase == "build":
        factor = min(1.05, 0.95 + max(position - 1, 0) * 0.05)
    elif phase == "specific":
        factor = 1.05
    elif phase == "taper":
        taper_position = max(0, week_count - position - 1)
        factor = 0.70 if taper_position == 0 else 0.82
    else:
        factor = 0.65
    return round(factor * profile.volume_multiplier, 4)


def _continuous_bounds(template_id: str) -> tuple[int, int, int] | None:
    template = get_knowledge_registry().workouts[template_id]
    if not isinstance(template.structure, ContinuousStructure):
        return None
    bounds = template.structure.duration_minutes
    return bounds.minimum, bounds.default, bounds.maximum


def _floor_grid(minutes: float) -> int:
    return int(minutes) // 5 * 5


def _session_budget(candidate: WeeklyPlanCandidate, weekday: int) -> int:
    raw = candidate.generation_context.get("availability")
    if not isinstance(raw, list):
        return 1440
    for item in raw:
        if isinstance(item, dict) and item.get("weekday") == weekday:
            value = item.get("available_minutes")
            if isinstance(value, (int, float)):
                return int(value)
    return 1440


def _explicit_session_budget(candidate: WeeklyPlanCandidate, weekday: int) -> int | None:
    raw = candidate.generation_context.get("availability")
    if not isinstance(raw, list):
        return None
    for item in raw:
        if isinstance(item, dict) and item.get("weekday") == weekday:
            value = item.get("available_minutes")
            if isinstance(value, (int, float)):
                return int(value)
    return None


def _quality_templates(phase: str, event_type: str) -> tuple[str, ...]:
    if phase == "build" and event_type != "general_fitness":
        return ("threshold_cruise",)
    if phase == "specific" and event_type in {"5k", "10k"}:
        return ("vo2_intervals", "threshold_cruise")
    if phase == "specific" and event_type in {"half_marathon", "marathon"}:
        return ("threshold_cruise",)
    return ()


def _insert_deferred_quality(
    candidate: WeeklyPlanCandidate,
    *,
    phase: str,
    event_type: str,
) -> WeeklyPlanCandidate:
    template_ids = _quality_templates(phase, event_type)
    if not template_ids or not candidate.sessions:
        return candidate
    history = candidate.generation_context.get("history_gates")
    if not isinstance(history, dict):
        return candidate
    consistent_weeks = int(history.get("effective_consistent_running_weeks", 0))
    runs_per_week = int(history.get("effective_runs_per_week", 0))
    replacement_indexes = sorted(
        (
            index
            for index, item in enumerate(candidate.sessions)
            if item.role in {"strides", "easy_run"}
        ),
        key=lambda index: candidate.sessions[index].role != "strides",
    )
    for template_id in template_ids:
        for index in replacement_indexes:
            current = candidate.sessions[index]
            budget = _explicit_session_budget(candidate, current.weekday)
            if budget is None:
                continue
            other_quality_days = [
                item.weekday
                for other_index, item in enumerate(candidate.sessions)
                if other_index != index and item.intensity_domain != "low"
            ]
            if any(abs(current.weekday - weekday) * 24 < 48 for weekday in other_quality_days):
                continue
            try:
                expanded = expand_workout_template(
                    template_id,
                    eligibility=TemplateEligibilityContext(
                        consistent_running_weeks=consistent_weeks,
                        runs_per_week=runs_per_week,
                        available_minutes=budget,
                        facts={
                            "reliable_intensity_model",
                            "reliable_current_performance_model",
                            "quality_density_validation",
                        },
                    ),
                    allow_deferred_quality=True,
                )
            except TemplateExpansionError:
                continue
            sessions = list(candidate.sessions)
            sessions[index] = replace(
                current,
                template_id=expanded.template_id,
                template_version=expanded.template_version,
                name=expanded.name,
                planned_minutes=ceil(expanded.load_estimate.duration_seconds / 60),
                intensity_domain=("high" if template_id == "vo2_intervals" else "moderate"),
                role=template_id,
                rationale=(
                    "Die spezifische Phase setzt einen kontrollierten VO₂max-Reiz."
                    if template_id == "vo2_intervals"
                    else "Die Aufbauphase setzt einen kontrollierten Schwellenreiz."
                ),
                warnings=("planner.deferred_quality_development_override",),
                load_estimate_json=expanded.load_estimate.model_dump(mode="json"),
            )
            context = dict(candidate.generation_context)
            context["deferred_quality"] = {
                "development_override": True,
                "template_id": template_id,
                "phase": phase,
                "eligibility_facts": [
                    "quality_density_validation",
                    "reliable_current_performance_model",
                    "reliable_intensity_model",
                ],
            }
            report = dict(candidate.validation_report)
            raw_checks = report.get("checks", [])
            checks = list(raw_checks) if isinstance(raw_checks, list) else []
            checks = [
                {
                    **check,
                    "code": "planner.templates.active_or_development_allowlist",
                }
                if isinstance(check, dict) and check.get("code") == "planner.templates.active_only"
                else check
                for check in checks
            ]
            checks.append(
                {
                    "code": "planner.deferred_quality_development_override",
                    "result": "bypassed",
                    "template_id": template_id,
                }
            )
            report["checks"] = checks
            adjusted = replace(
                candidate,
                sessions=tuple(sessions),
                generation_context=context,
                validation_report=report,
            )
            return replace(adjusted, input_fingerprint=_candidate_fingerprint(adjusted))
    return candidate


def _candidate_fingerprint(candidate: WeeklyPlanCandidate) -> str:
    encoded = json.dumps(
        {
            "generation_context": candidate.generation_context,
            "planner_version": candidate.planner_version,
            "knowledge_base_version": candidate.knowledge_base_version,
            "sessions": [
                {
                    "scheduled_for": item.scheduled_for.isoformat(),
                    "role": item.role,
                    "template_id": item.template_id,
                    "template_version": item.template_version,
                    "planned_minutes": item.planned_minutes,
                    "warnings": list(item.warnings),
                }
                for item in candidate.sessions
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _apply_phase(
    candidate: WeeklyPlanCandidate,
    phase: str,
    volume_factor: float,
    *,
    target_date: date,
    interrupted: bool,
) -> WeeklyPlanCandidate:
    if interrupted:
        sessions = ()
    else:
        sessions = []
        for item in candidate.sessions:
            if item.scheduled_for > target_date:
                continue
            if phase == "taper" and item.role == "strides":
                continue
            planned_minutes = item.planned_minutes
            bounds = _continuous_bounds(item.template_id)
            if bounds is not None:
                minimum, _, maximum = bounds
                planned_minutes = _floor_grid(item.planned_minutes * volume_factor)
                planned_minutes = max(minimum, min(maximum, planned_minutes))
                planned_minutes = min(planned_minutes, _session_budget(candidate, item.weekday))
                planned_minutes = max(minimum, _floor_grid(planned_minutes))
            sessions.append(replace(item, planned_minutes=planned_minutes))
        sessions = tuple(sessions)
    context = dict(candidate.generation_context)
    context["cycle"] = {
        "schema_version": MULTIWEEK_SCHEMA_VERSION,
        "phase": phase,
        "volume_factor": volume_factor,
        "interrupted": interrupted,
    }
    report = dict(candidate.validation_report)
    raw_checks = report.get("checks", [])
    checks = list(raw_checks) if isinstance(raw_checks, list) else []
    checks.extend(
        [
            {"code": "cycle.phase_is_versioned", "result": "pass"},
            {
                "code": "cycle.no_catchup_after_interruption",
                "result": "pass",
            },
        ]
    )
    report["checks"] = checks
    report["phase"] = phase
    report["valid"] = bool(report.get("valid", False))
    adjusted = replace(
        candidate,
        sessions=sessions,
        target_days=len(sessions),
        generation_context=context,
        validation_report=report,
        planner_version=f"{candidate.planner_version}+{MULTIWEEK_PLANNER_VERSION}",
    )
    return replace(adjusted, input_fingerprint=_candidate_fingerprint(adjusted))


def _cap_weekly_increase(
    candidate: WeeklyPlanCandidate, maximum_total_minutes: int
) -> WeeklyPlanCandidate:
    if sum(item.planned_minutes for item in candidate.sessions) <= maximum_total_minutes:
        return candidate
    sessions = list(candidate.sessions)
    while sum(item.planned_minutes for item in sessions) > maximum_total_minutes:
        changed = False
        for index, item in sorted(
            enumerate(sessions), key=lambda pair: (pair[1].role != "easy_run", pair[0])
        ):
            bounds = _continuous_bounds(item.template_id)
            if bounds is None or item.planned_minutes <= bounds[0]:
                continue
            sessions[index] = replace(item, planned_minutes=item.planned_minutes - 5)
            changed = True
            break
        if not changed:
            break
    adjusted = replace(candidate, sessions=tuple(sessions))
    return replace(adjusted, input_fingerprint=_candidate_fingerprint(adjusted))


def _cap_long_run_increase(
    candidate: WeeklyPlanCandidate, maximum_minutes: int
) -> WeeklyPlanCandidate:
    sessions = tuple(
        replace(item, planned_minutes=min(item.planned_minutes, maximum_minutes))
        if item.role == "long_run"
        else item
        for item in candidate.sessions
    )
    adjusted = replace(candidate, sessions=sessions)
    return replace(adjusted, input_fingerprint=_candidate_fingerprint(adjusted))


def compose_training_cycle(
    weekly_candidates: tuple[WeeklyPlanCandidate, ...],
    *,
    start_date: date,
    target_date: date,
    event_type: str,
    goal_id: int | None = None,
    effective_reentry: bool = False,
    interrupted_weeks: frozenset[int] = frozenset(),
    enable_deferred_quality: bool = False,
) -> TrainingCycleCandidate:
    if start_date.weekday() != 0:
        raise MultiweekPlannerError(
            "Der Start eines Mehrwochenplans muss an einem Montag liegen.",
            code="cycle.start_date_invalid",
        )
    if target_date <= start_date:
        raise MultiweekPlannerError(
            "Das Ziel muss nach dem Planstart liegen.", code="cycle.target_date_invalid"
        )
    minimum = minimum_cycle_weeks(event_type)
    profile = CYCLE_RULE_PROFILES[event_type]
    week_count = _week_count(start_date, target_date)
    if week_count < minimum:
        raise MultiweekPlannerError(
            f"Für dieses Ziel werden mindestens {minimum} Wochen benötigt.",
            code="cycle.goal_horizon_too_short",
        )
    if week_count > MAX_CYCLE_WEEKS:
        raise MultiweekPlannerError(
            f"Ein Mehrwochenplan darf höchstens {MAX_CYCLE_WEEKS} Wochen umfassen.",
            code="cycle.goal_horizon_too_long",
        )
    if len(weekly_candidates) != week_count:
        raise MultiweekPlannerError(
            "Die Wochenkandidaten decken den vollständigen Planzeitraum nicht ab.",
            code="cycle.week_candidates_incomplete",
        )

    weeks: list[CycleWeekCandidate] = []
    previous_long_run_minutes = 0
    for position, weekly in enumerate(weekly_candidates):
        week_start = start_date + timedelta(weeks=position)
        if weekly.week_start != week_start:
            raise MultiweekPlannerError(
                "Die Wochenkandidaten müssen lückenlos aufeinander folgen.",
                code="cycle.week_candidates_not_contiguous",
            )
        interrupted = position in interrupted_weeks
        phase = _phase_for_week(
            position,
            week_count,
            effective_reentry=effective_reentry,
            interrupted=interrupted,
        )
        factor = _volume_factor(phase, position, week_count, profile)
        adjusted = _apply_phase(
            weekly,
            phase,
            factor,
            target_date=target_date,
            interrupted=interrupted,
        )
        if enable_deferred_quality and not interrupted:
            adjusted = _insert_deferred_quality(
                adjusted,
                phase=phase,
                event_type=event_type,
            )
        long_runs = [item.planned_minutes for item in adjusted.sessions if item.role == "long_run"]
        if long_runs and previous_long_run_minutes:
            adjusted = _cap_long_run_increase(
                adjusted,
                maximum_minutes=_floor_grid(
                    previous_long_run_minutes * profile.max_long_run_increase
                ),
            )
            long_runs = [
                item.planned_minutes for item in adjusted.sessions if item.role == "long_run"
            ]
        if long_runs:
            previous_long_run_minutes = max(long_runs)
        prior_totals = [week.total_minutes for week in weeks if week.total_minutes > 0]
        if prior_totals:
            adjusted = _cap_weekly_increase(
                adjusted,
                maximum_total_minutes=_floor_grid(
                    prior_totals[-1] * profile.max_weekly_volume_increase
                ),
            )
        weeks.append(
            CycleWeekCandidate(
                position=position,
                week_start=week_start,
                phase=phase,
                volume_factor=factor,
                weekly_plan=adjusted,
                rationale=_phase_rationale(phase),
            )
        )

    totals = [week.total_minutes for week in weeks]
    weekly_increase_ok = True
    previous_positive = totals[0] if totals and totals[0] > 0 else 0
    for current in totals[1:]:
        if current > 0:
            if (
                previous_positive
                and current > previous_positive * profile.max_weekly_volume_increase
            ):
                weekly_increase_ok = False
            previous_positive = current
    taper_ok = True
    for index, week in enumerate(weeks):
        if week.phase != "taper" or index == 0:
            continue
        if week.week_start + timedelta(days=6) > target_date:
            continue
        previous_total = weeks[index - 1].total_minutes
        if previous_total == 0:
            continue
        reduction = 1 - week.total_minutes / previous_total
        if not profile.minimum_taper_reduction <= reduction <= profile.maximum_taper_reduction:
            taper_ok = False
    long_run_ok = True
    previous_long_run_minutes = 0
    quality_density_ok = True
    quality_dates: list[date] = []
    weekly_candidates_ok = True
    for week in weeks:
        weekly_candidates_ok = weekly_candidates_ok and bool(
            week.weekly_plan.validation_report.get("valid")
        )
        long_runs = [
            item.planned_minutes for item in week.weekly_plan.sessions if item.role == "long_run"
        ]
        if long_runs:
            current_long_run_minutes = max(long_runs)
            if (
                previous_long_run_minutes
                and current_long_run_minutes
                > previous_long_run_minutes * profile.max_long_run_increase
            ):
                long_run_ok = False
            previous_long_run_minutes = current_long_run_minutes
        week_quality_dates = sorted(
            item.scheduled_for
            for item in week.weekly_plan.sessions
            if item.intensity_domain != "low"
        )
        if len(week_quality_dates) > profile.max_quality_sessions:
            quality_density_ok = False
        quality_dates.extend(week_quality_dates)
    if any(
        (current - previous).days < 2
        for previous, current in zip(quality_dates, quality_dates[1:], strict=False)
    ):
        quality_density_ok = False
    target_ok = all(
        item.scheduled_for <= target_date for week in weeks for item in week.weekly_plan.sessions
    )
    phases_ok = all(week.phase in SUPPORTED_PHASES for week in weeks)
    valid = (
        weekly_candidates_ok
        and weekly_increase_ok
        and long_run_ok
        and quality_density_ok
        and taper_ok
        and target_ok
        and phases_ok
    )
    confidence = _cycle_confidence(weekly_candidates)
    assumptions = {
        "schema_version": MULTIWEEK_SCHEMA_VERSION,
        "goal_type": event_type,
        "minimum_weeks_for_goal": minimum,
        "effective_reentry": effective_reentry,
        "interrupted_weeks": sorted(interrupted_weeks),
        "unsupported_templates_are_not_introduced": not enable_deferred_quality,
        "deferred_quality_development_override": enable_deferred_quality,
        "rule_profile": {
            "volume_multiplier": profile.volume_multiplier,
            "max_weekly_volume_increase": profile.max_weekly_volume_increase,
            "max_long_run_increase": profile.max_long_run_increase,
            "max_quality_sessions": profile.max_quality_sessions,
            "minimum_taper_reduction": profile.minimum_taper_reduction,
            "maximum_taper_reduction": profile.maximum_taper_reduction,
        },
    }
    impact = {
        "material": bool(interrupted_weeks) or len({week.phase for week in weeks}) > 1,
        "weekly_minutes": totals,
        "total_minutes": sum(totals),
        "quality_sessions": [
            sum(item.intensity_domain != "low" for item in week.weekly_plan.sessions)
            for week in weeks
        ],
        "changed_weeks": list(range(len(weeks))) if weeks else [],
    }
    report = {
        "valid": valid,
        "rule_set_version": MULTIWEEK_PLANNER_VERSION,
        "checks": [
            {
                "code": "cycle.goal_horizon",
                "result": "pass",
                "weeks": week_count,
            },
            {
                "code": "cycle.weekly_volume_progression",
                "result": "pass" if weekly_increase_ok else "fail",
            },
            {
                "code": "cycle.long_run_and_quality_density",
                "result": "pass" if long_run_ok and quality_density_ok else "fail",
            },
            {"code": "cycle.taper", "result": "pass" if taper_ok else "fail"},
            {
                "code": "cycle.target_date_boundary",
                "result": "pass" if target_ok else "fail",
            },
            {"code": "cycle.no_catchup_stacking", "result": "pass"},
            {"code": "cycle.acceptance_required", "result": "pass"},
            {
                "code": "cycle.week_candidates_valid",
                "result": "pass" if weekly_candidates_ok else "fail",
            },
        ],
    }
    generation_context = {
        "schema_version": MULTIWEEK_SCHEMA_VERSION,
        "goal": {"id": goal_id, "event_type": event_type},
        "start_date": start_date.isoformat(),
        "target_date": target_date.isoformat(),
        "weeks": [
            {
                "position": week.position,
                "week_start": week.week_start.isoformat(),
                "phase": week.phase,
                "volume_factor": week.volume_factor,
                "weekly_input_fingerprint": week.weekly_plan.input_fingerprint,
            }
            for week in weeks
        ],
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "generation_context": generation_context,
                "assumptions": assumptions,
                "impact": impact,
                "planner_version": MULTIWEEK_PLANNER_VERSION,
                "knowledge_base_version": get_knowledge_registry().version,
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    return TrainingCycleCandidate(
        goal_id=goal_id,
        event_type=event_type,
        start_date=start_date,
        target_date=target_date,
        weeks=tuple(weeks),
        confidence=confidence,
        assumptions=assumptions,
        impact=impact,
        validation_report=report,
        input_fingerprint=fingerprint,
        planner_version=MULTIWEEK_PLANNER_VERSION,
        knowledge_base_version=get_knowledge_registry().version,
    )


def plan_training_cycle(
    session: Session,
    user: User,
    *,
    start_date: date,
    target_date: date,
    goal_id: int | None = None,
    event_type: str | None = None,
    interrupted_weeks: frozenset[int] = frozenset(),
) -> TrainingCycleCandidate:
    goal = get_active_goal(session, user.id, goal_id=goal_id) if goal_id is not None else None
    if goal_id is not None and goal is None:
        raise MultiweekPlannerError("Aktives Ziel nicht gefunden.", code="cycle.goal_not_found")
    if goal_id is None:
        goal = get_active_goal(session, user.id, event_type=event_type)
    selected_type = event_type or (goal.event_type if goal is not None else None)
    if selected_type is None:
        raise MultiweekPlannerError(
            "Für einen Mehrwochenplan muss ein aktives Ziel erfasst sein.",
            code="cycle.goal_required",
        )
    if goal is not None and goal.event_type != selected_type:
        raise MultiweekPlannerError(
            "Zieltyp und ausgewähltes Ziel passen nicht zusammen.", code="cycle.goal_mismatch"
        )
    if goal is not None and goal.target_date is not None and goal.target_date != target_date:
        raise MultiweekPlannerError(
            "Das Zieldatum muss dem aktiven Athletenziel entsprechen.",
            code="cycle.target_date_mismatch",
        )
    profile = get_planning_profile(session, user.id)
    weekly_candidates = tuple(
        plan_shadow_week(session, user, week_start=start_date + timedelta(weeks=offset))
        for offset in range(_week_count(start_date, target_date))
    )
    effective_reentry = bool(profile is not None and profile.self_declared_reentry)
    if not effective_reentry:
        for weekly in weekly_candidates:
            profile_context = weekly.generation_context.get("profile")
            if isinstance(profile_context, dict) and profile_context.get("effective_reentry"):
                effective_reentry = True
                break
    candidate = compose_training_cycle(
        weekly_candidates,
        start_date=start_date,
        target_date=target_date,
        event_type=selected_type,
        goal_id=goal.id if goal is not None else None,
        effective_reentry=effective_reentry,
        interrupted_weeks=interrupted_weeks,
        enable_deferred_quality=deferred_quality_templates_enabled(),
    )
    from app.services.planning.workout_proposals import quality_density_conflicts

    for week in candidate.weeks:
        for item in week.weekly_plan.sessions:
            if item.template_id not in {"strides", "threshold_cruise", "vo2_intervals"}:
                continue
            if quality_density_conflicts(session, user.id, item.scheduled_for):
                raise MultiweekPlannerError(
                    "Der Plan kollidiert mit einer bereits angenommenen Qualitätseinheit.",
                    code="cycle.existing_quality_spacing_violation",
                )
    return candidate


def persist_training_cycle(
    session: Session,
    user: User,
    candidate: TrainingCycleCandidate,
) -> TrainingCycleRevision:
    if not candidate.validation_report.get("valid"):
        raise TrainingCyclePersistenceError(
            "Nur ein vollständig validierter Mehrwochenkandidat kann gespeichert werden.",
            code="cycle.candidate_invalid",
        )
    cycle = session.scalar(
        select(TrainingCycle).where(
            TrainingCycle.user_id == user.id,
            TrainingCycle.goal_id == candidate.goal_id,
            TrainingCycle.start_date == candidate.start_date,
        )
    )
    if cycle is None:
        cycle = TrainingCycle(
            user_id=user.id,
            goal_id=candidate.goal_id,
            event_type=candidate.event_type,
            start_date=candidate.start_date,
            target_date=candidate.target_date,
        )
        session.add(cycle)
        session.flush()
    elif cycle.event_type != candidate.event_type or cycle.target_date != candidate.target_date:
        raise TrainingCyclePersistenceError(
            "Der bestehende Zyklus passt nicht zum neuen Ziel.", code="cycle.identity_mismatch"
        )
    existing = session.scalar(
        select(TrainingCycleRevision).where(
            TrainingCycleRevision.cycle_id == cycle.id,
            TrainingCycleRevision.input_fingerprint == candidate.input_fingerprint,
        )
    )
    if existing is not None:
        changed = False
        if cycle.current_revision_id != existing.id:
            cycle.current_revision_id = existing.id
            changed = True
        memberships = session.execute(
            select(TrainingCycleWeek, TrainingPlanRevision)
            .join(
                TrainingPlanRevision,
                TrainingPlanRevision.id == TrainingCycleWeek.training_plan_revision_id,
            )
            .where(
                TrainingCycleWeek.cycle_revision_id == existing.id,
                TrainingCycleWeek.owner_user_id == user.id,
                TrainingPlanRevision.owner_user_id == user.id,
            )
        ).all()
        for _membership, weekly_revision in memberships:
            weekly_plan = session.get(TrainingPlan, weekly_revision.plan_id)
            if weekly_plan is not None and weekly_plan.current_revision_id != weekly_revision.id:
                weekly_plan.current_revision_id = weekly_revision.id
                changed = True
        if changed:
            session.commit()
        return existing
    revision_number = (
        session.scalar(
            select(TrainingCycleRevision.revision_number)
            .where(TrainingCycleRevision.cycle_id == cycle.id)
            .order_by(TrainingCycleRevision.revision_number.desc())
            .limit(1)
        )
        or 0
    ) + 1
    revision = TrainingCycleRevision(
        cycle_id=cycle.id,
        owner_user_id=user.id,
        parent_revision_id=cycle.current_revision_id,
        revision_number=revision_number,
        event_type=candidate.event_type,
        start_date=candidate.start_date,
        target_date=candidate.target_date,
        planner_version=candidate.planner_version,
        knowledge_base_version=candidate.knowledge_base_version,
        input_fingerprint=candidate.input_fingerprint,
        confidence=candidate.confidence,
        phase_plan_json=[
            {
                "position": week.position,
                "week_start": week.week_start.isoformat(),
                "phase": week.phase,
                "volume_factor": week.volume_factor,
                "rationale": week.rationale,
            }
            for week in candidate.weeks
        ],
        assumptions_json=candidate.assumptions,
        impact_json=candidate.impact,
        validation_report_json=candidate.validation_report,
    )
    session.add(revision)
    session.flush()
    try:
        for week in candidate.weeks:
            weekly_revision = _persist_week_candidate(
                session,
                user,
                week.weekly_plan,
                commit=False,
            )
            session.add(
                TrainingCycleWeek(
                    cycle_revision_id=revision.id,
                    training_plan_revision_id=weekly_revision.id,
                    owner_user_id=user.id,
                    position=week.position,
                    week_start=week.week_start,
                    phase=week.phase,
                )
            )
        cycle.current_revision_id = revision.id
        session.commit()
    except Exception:
        session.rollback()
        raise
    return revision


def accept_training_cycle_revision(
    session: Session,
    user: User,
    *,
    cycle_id: int,
    revision_id: int,
) -> TrainingCycleRevision:
    cycle = session.scalar(
        select(TrainingCycle).where(TrainingCycle.id == cycle_id, TrainingCycle.user_id == user.id)
    )
    revision = session.scalar(
        select(TrainingCycleRevision).where(
            TrainingCycleRevision.id == revision_id,
            TrainingCycleRevision.cycle_id == cycle_id,
            TrainingCycleRevision.owner_user_id == user.id,
        )
    )
    if cycle is None or revision is None:
        raise TrainingCyclePersistenceError("Planrevision nicht gefunden.", code="cycle.not_found")
    if cycle.current_revision_id != revision.id:
        raise TrainingCyclePersistenceError(
            "Diese Planrevision ist nicht mehr die aktuelle Vorschau.",
            code="cycle.revision_stale",
        )
    if (
        revision.assumptions_json.get("deferred_quality_development_override")
        and not deferred_quality_templates_enabled()
    ):
        raise TrainingCyclePersistenceError(
            "Development-Qualitätstemplates können außerhalb des Testmodus nicht "
            "angenommen werden.",
            code="cycle.deferred_quality_disabled",
        )
    memberships = session.scalars(
        select(TrainingCycleWeek)
        .where(
            TrainingCycleWeek.cycle_revision_id == revision.id,
            TrainingCycleWeek.owner_user_id == user.id,
        )
        .order_by(TrainingCycleWeek.position)
    ).all()
    for membership in memberships:
        weekly_revision = session.get(TrainingPlanRevision, membership.training_plan_revision_id)
        if weekly_revision is None:
            raise TrainingCyclePersistenceError(
                "Eine Wochenrevision des Plans fehlt.", code="cycle.week_revision_missing"
            )
        plan = session.scalar(
            select(TrainingPlan).where(
                TrainingPlan.id == weekly_revision.plan_id,
                TrainingPlan.user_id == user.id,
            )
        )
        if plan is None:
            raise TrainingCyclePersistenceError(
                "Ein Wochenplan gehört nicht mehr zu diesem Athleten.",
                code="cycle.week_plan_invalid",
            )
        plan.current_revision_id = weekly_revision.id
    cycle.accepted_revision_id = revision.id
    session.commit()
    return revision


def _cycle_confidence(weekly_candidates: tuple[WeeklyPlanCandidate, ...]) -> str:
    confidences: list[str] = []
    for weekly in weekly_candidates:
        baseline = weekly.generation_context.get("baseline")
        if isinstance(baseline, dict) and isinstance(baseline.get("confidence"), str):
            confidences.append(baseline["confidence"])
    if not confidences or "insufficient" in confidences:
        return "insufficient"
    if "low" in confidences:
        return "low"
    if "medium" in confidences:
        return "medium"
    return "high"


def _phase_rationale(phase: str) -> str:
    return {
        "reentry": "Wiedereinstieg mit reduzierter Belastung und ohne Nachholstapel.",
        "base": "Basisphase für regelmässige, kontrollierte Laufhäufigkeit.",
        "build": "Aufbauphase mit vorsichtiger Progression nur auf einer Belastungsachse.",
        "specific": "Zielspezifische Phase innerhalb der verfügbaren, freigegebenen Templates.",
        "taper": "Taper mit reduziertem Umfang und ohne neue harte Reize.",
        "recovery": (
            "Erholungswoche nach einer Unterbrechung; verpasste Einheiten werden nicht nachgeholt."
        ),
    }[phase]

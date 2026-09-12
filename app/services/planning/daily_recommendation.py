"""Read-only daily selection over the ordinary running proposal generator."""

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta
from math import floor
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PreSessionFeedback, User, Workout, WorkoutRevision
from app.services.analytics.activity_semantics import is_running_sport
from app.services.analytics.athlete_data import AthleteDataService
from app.services.analytics.running_intensity import workout_pace_guidance
from app.services.planning.daily_adaptation import (
    DailyAdaptationError,
    DailyAdaptationService,
    adaptation_load,
)
from app.services.planning.planning_queries import (
    get_planning_inputs,
    list_accepted_training_cycles,
    list_availability,
)
from app.services.planning.registry import get_knowledge_registry
from app.services.planning.registry_models import IntervalStructure
from app.services.planning.training_fit import (
    TrainingFitOutcome,
    assess_training_fit,
    recent_training_facts,
    supported_adverse_evidence,
)
from app.services.planning.validator import WorkoutInput
from app.services.planning.weekly_planner import _count_consistent_weeks
from app.services.planning.workout_definition import (
    RepeatBlockV2,
    StepBlockV2,
    TimeEnd,
    WorkoutDefinitionModel,
    workout_metrics,
)
from app.services.planning.workout_proposals import RunningProposalService, RunningTemplateId
from app.services.planning.workout_revision import RevisionMetadata, workout_content_hash
from app.services.planning.workout_service import WorkoutService, WorkoutTransitionError
from app.services.planning.workout_templates import TemplateParameters

DAILY_RULES_VERSION = "daily-running-v1"
SUSTAINED = {"threshold_cruise", "vo2_intervals"}
QUALITY_SESSION_SIZE_CEILING = 1.3
OBSERVED_WEEK_VOLUME_FLAG = 1.2
REENTRY_DURATION_FACTOR = 0.7
FATIGUE_DURATION_FACTOR = 0.8


@dataclass(frozen=True)
class DailyRecommendation:
    as_of: date
    state: Literal["workout", "rest", "completed", "scheduled", "clarification"]
    name: str
    context_fingerprint: str
    reasons: tuple[str, ...]
    assumptions: tuple[str, ...]
    confidence: str
    available_minutes: int
    template_id: RunningTemplateId | None = None
    definition: WorkoutDefinitionModel | None = None
    duration_seconds: float = 0
    work_seconds: float = 0
    workout_id: int | None = None
    revision_id: int | None = None
    activity_ids: tuple[int, ...] = ()
    adaptation: str | None = None
    data: WorkoutInput | None = None
    metadata: RevisionMetadata | None = None

    def summary(self) -> dict[str, object]:
        return {
            "as_of": self.as_of.isoformat(),
            "state": self.state,
            "name": self.name,
            "context_fingerprint": self.context_fingerprint,
            "reasons": self.reasons,
            "assumptions": self.assumptions,
            "confidence": self.confidence,
            "source": DAILY_RULES_VERSION,
            "available_minutes": self.available_minutes,
            "template_id": self.template_id,
            "duration_seconds": self.duration_seconds,
            "work_seconds": self.work_seconds,
            "workout_id": self.workout_id,
            "revision_id": self.revision_id,
            "activity_ids": self.activity_ids,
            "adaptation": self.adaptation,
            "definition": self.definition.model_dump(mode="json") if self.definition else None,
            "guidance": self.metadata.guidance_json if self.metadata else None,
        }


def _fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, allow_nan=False).encode()
    ).hexdigest()


def recommend_today(
    session: Session,
    user: User,
    *,
    as_of: date,
    available_minutes: int | None = None,
    snapshot_as_of: date | None = None,
    planning_goal: str | None = None,
) -> DailyRecommendation:
    """Use a trusted date; no proposal, external call, or chat is created."""
    if available_minutes is not None and not 0 <= available_minutes <= 1440:
        raise ValueError("Zeitbudget muss zwischen 0 und 1440 Minuten liegen.")

    observed_on = snapshot_as_of or as_of

    nominal = snapshot_as_of is not None and as_of > snapshot_as_of

    inputs = get_planning_inputs(session, user.id, as_of=observed_on)
    availability = list_availability(session, user.id)
    today_slot = next((slot for slot in availability if slot.weekday == as_of.weekday()), None)

    athlete = AthleteDataService(session, user.id, as_of=observed_on)
    shadow = athlete.get_running_shadow_analysis(performance_anchors=inputs.performance_anchors)
    recent = shadow.baseline.window(28)

    facts = recent_training_facts(session, user.id, as_of=observed_on)
    normal_minutes = floor((recent.per_run_duration_s.median or 1800) / 60)
    assumptions: list[str] = []
    budget = available_minutes
    if budget is None and today_slot is not None:
        budget = today_slot.available_minutes
    current_feedback = session.scalar(
        select(PreSessionFeedback)
        .where(
            PreSessionFeedback.user_id == user.id,
            PreSessionFeedback.recorded_at >= datetime.combine(as_of, time.min),
            PreSessionFeedback.recorded_at <= datetime.combine(as_of, time.max),
            PreSessionFeedback.available_minutes.is_not(None),
        )
        .order_by(PreSessionFeedback.recorded_at.desc(), PreSessionFeedback.id.desc())
        .limit(1)
    )

    if (
        not nominal
        and current_feedback is not None
        and current_feedback.available_minutes is not None
    ):
        budget = (
            min(budget, current_feedback.available_minutes)
            if budget is not None
            else current_feedback.available_minutes
        )
    if budget is None:
        budget = max(20, min(normal_minutes, 90))
        assumptions.append(
            f"Vorläufiges Zeitbudget: {budget} Minuten aus üblichen Läufen; "
            "ohne Historie 30 Minuten. Bitte bei Bedarf ändern."
        )
    if not recent.quality.history_complete:
        assumptions.append(
            "Die Laufhistorie ist unvollständig. Fehlende Einträge belegen keine "
            "ausgelassenen Einheiten; es wird kein Fortschritt unterstellt."
        )
    if today_slot is not None and (not today_slot.available or today_slot.available_minutes == 0):
        budget = 0
    elif today_slot and today_slot.available_minutes is not None:
        budget = min(budget, today_slot.available_minutes)
    assessment = assess_training_fit(
        session,
        user.id,
        effective_workout_date=as_of,
        revision_fingerprint=DAILY_RULES_VERSION,
        evaluated_at=datetime.combine(as_of, time.max),
    )
    accepted = list(
        session.execute(
            select(Workout, WorkoutRevision)
            .join(WorkoutRevision, WorkoutRevision.id == Workout.accepted_revision_id)
            .where(
                Workout.user_id == user.id,
                Workout.deleted_at.is_(None),
                Workout.local_schedule_status == "scheduled",
                Workout.scheduled_for >= as_of - timedelta(days=6),
                Workout.scheduled_for <= as_of + timedelta(days=6),
            )
            .order_by(Workout.scheduled_for, Workout.id)
        )
    )
    cycles = tuple(
        c
        for c in list_accepted_training_cycles(session, user.id)
        if c.revision.start_date <= as_of <= c.revision.target_date
    )
    fingerprint = _fingerprint(
        {
            "rules": DAILY_RULES_VERSION,
            "user": user.id,
            "as_of": as_of,
            "snapshot_as_of": snapshot_as_of,
            "shadow": shadow.context_fingerprint,
            "inputs": asdict(inputs),
            "availability": [asdict(s) for s in availability],
            "budget": budget,
            "facts": [asdict(f) for f in facts],
            "fit": assessment.authoritative_input_fingerprint,
            "cycles": [asdict(c) for c in cycles],
            "accepted": [(w.id, w.scheduled_for, r.id, r.content_hash) for w, r in accepted],
            "knowledge": get_knowledge_registry().version,
        }
    )
    base = DailyRecommendation(
        as_of,
        "rest",
        "Ruhetag",
        fingerprint,
        (),
        tuple(assumptions),
        recent.quality.confidence,
        budget,
    )
    completed = tuple(f for f in facts if f.running and f.day == as_of)
    if completed:
        return replace(
            base,
            state="completed",
            name="Lauf bereits erledigt",
            duration_seconds=sum(f.duration_s or 0 for f in completed),
            activity_ids=tuple(f.activity_id for f in completed),
            reasons=(
                f"{as_of:%d.%m.}: Dein Lauf ist erfasst; "
                "heute wird kein zweiter Lauf vorgeschlagen.",
            ),
        )
    scheduled = [(w, r) for w, r in accepted if w.scheduled_for == as_of]
    if scheduled:
        workout, revision = scheduled[0]
        reasons = [f"{as_of:%d.%m.}: Deine angenommene, eingeplante Einheit hat Vorrang."]
        try:
            adaptation_preview = DailyAdaptationService(session, user, as_of=as_of).assess_today(
                workout.id
            )
            candidate = next(c for c in adaptation_preview.assessment.candidates if c.recommended)
            adaptation = candidate.label
            reasons.append(candidate.rationale)
            stress_dates = [
                code.split(":", 1)[1]
                for code in candidate.reason_codes
                if code.startswith("training.recent_demand:")
            ]
            if stress_dates:
                reasons.append(
                    "Anstrengende oder lange Aktivität am "
                    + ", ".join(stress_dates)
                    + "; der Erholungsabstand wird nach Kalendertagen beurteilt."
                )
            fingerprint = _fingerprint((fingerprint, adaptation_preview.context_fingerprint))
        except DailyAdaptationError as exc:
            adaptation = str(exc)
        return replace(
            base,
            state="scheduled",
            name=revision.name,
            context_fingerprint=fingerprint,
            workout_id=workout.id,
            revision_id=revision.id,
            definition=revision.definition_model,
            template_id=revision.template_id
            if revision.template_id in get_knowledge_registry().workouts
            else None,
            duration_seconds=workout_metrics(revision.definition_model).duration_seconds,
            work_seconds=sum(
                block.iterations * step.end.seconds
                for block in revision.definition_model.blocks
                if isinstance(block, RepeatBlockV2)
                for step in block.children
                if isinstance(step, StepBlockV2)
                and step.step_type == "interval"
                and isinstance(step.end, TimeEnd)
            ),
            reasons=tuple(reasons),
            adaptation=adaptation,
        )
    if budget < 20:
        return replace(
            base,
            reasons=(
                f"{as_of:%d.%m.}: Heute ist keine Laufzeit verfügbar "
                "oder das Zeitfenster ist kürzer als 20 Minuten.",
            ),
        )

    adverse = () if nominal else supported_adverse_evidence(assessment)

    if not nominal and assessment.outcome == TrainingFitOutcome.ELEVATED:
        dates = ", ".join(sorted({f"{e.observed_on:%d.%m.}" for e in adverse}))
        return replace(
            base,
            reasons=(
                f"{dates}: Ernsthafte Beschwerden oder mehrere ungünstige "
                "Erholungsbefunde sprechen heute für Ruhe.",
            ),
        )
    runs_week = [f for f in facts if f.running and 0 <= (as_of - f.day).days < 7]
    habitual = max(1, round(recent.frequency_per_week))
    planned_week = {
        w.scheduled_for
        for w, r in accepted
        if w.scheduled_for and w.scheduled_for < as_of and is_running_sport(r.sport)
    }
    observed_days = {f.day for f in runs_week} | planned_week

    if not nominal and recent.runs >= 4 and len(observed_days) >= habitual:
        return replace(
            base,
            reasons=(
                f"{as_of:%d.%m.}: Mit {len(observed_days)} erfassten/geplanten Lauftagen "
                f"ist deine übliche Häufigkeit von etwa {habitual} pro Woche erreicht. "
                "Kein zusätzlicher Erholungslauf.",
            ),
        )
    reentry = shadow.baseline.reentry.active or bool(
        inputs.profile and inputs.profile.self_declared_reentry
    )
    # No observations after a gap justify caution, but do not prove missed training.
    uncertain_gap = any(i.current and i.inactive_days >= 14 for i in shadow.baseline.interruptions)
    reentry = reentry or uncertain_gap
    long_reference = recent.weekly_longest_duration_s.median or recent.longest_duration.value or 0
    stress = [
        f
        for f in facts
        if 0 <= (as_of - f.day).days <= 2
        and (
            f.hard
            or f.running
            and f.duration_s is not None
            and f.duration_s >= max(2400, normal_minutes * 60 * 1.3)
        )
    ]
    unknown_recent = any(not f.intensity_known and 0 <= (as_of - f.day).days <= 2 for f in facts)
    sustained_week = any(
        (f.hard and f.template_id != "strides" or f.template_id in SUSTAINED)
        and 0 <= (as_of - f.day).days < 7
        for f in facts
    )
    planned_quality = any(
        r.template_id != "strides"
        and w.scheduled_for
        and (
            adaptation_load(
                r.definition_model, load_estimate=r.load_estimate_json
            ).dimensions.intensity_score
            >= 2
            or not adaptation_load(
                r.definition_model, load_estimate=r.load_estimate_json
            ).intensity_comparable
            and abs((w.scheduled_for - as_of).days) <= 2
        )
        for w, r in accepted
    )
    high_volume = bool(
        recent.weekly_duration_s.median
        and (shadow.baseline.window(7).total_duration_s or 0)
        > recent.weekly_duration_s.median * OBSERVED_WEEK_VOLUME_FLAG
    )
    minutes = min(budget, max(20, normal_minutes), 90)
    reasons = [f"{as_of:%d.%m.}: Lockere Ausdauer passt zu deiner aktuellen Laufbasis."]
    if stress:
        reasons[0] = (
            f"{stress[-1].day:%d.%m.}: Die jüngste anstrengende oder lange Aktivität "
            "spricht heute für lockeres Laufen; der Abstand wird nach Kalendertagen beurteilt."
        )
    if reentry:
        minutes = min(minutes, max(20, floor(normal_minutes * REENTRY_DURATION_FACTOR)), 30)
        reasons[0] = (
            f"{as_of:%d.%m.}: Wiedereinstieg oder längere Beobachtungslücke: "
            "kurzer lockerer Lauf, keine Nachholeinheit."
        )
    if adverse:
        minutes = max(20, floor(minutes * FATIGUE_DURATION_FACTOR))
        reasons[0] = (
            f"{adverse[-1].observed_on:%d.%m.}: Aktuelle Ermüdungshinweise sprechen "
            "für weniger Umfang und lockere Intensität."
        )
    template: RunningTemplateId = "easy_run"
    parameters = TemplateParameters(duration_minutes=minutes)
    goals = [g for g in inputs.goals if g.target_date is None or g.target_date >= as_of]
    goals.sort(key=lambda g: (g.target_date or date.max, g.id))

    goal = planning_goal or (goals[0].event_type if goals else "general_fitness")
    goal_date = goals[0].target_date if goals else None
    phase = None
    if cycles:
        goal = cycles[0].revision.event_type
        goal_date = cycles[0].revision.target_date
        for item in cycles[0].revision.phase_plan_json:
            start = date.fromisoformat(str(item["week_start"]))
            if start <= as_of <= start + timedelta(days=6):
                phase = item.get("phase")
    near_event = goal_date is not None and 0 <= (goal_date - as_of).days <= 7
    if near_event or phase in {"taper", "recovery"}:
        minutes = min(minutes, max(20, floor(normal_minutes * REENTRY_DURATION_FACTOR)))
        parameters = TemplateParameters(duration_minutes=minutes)
        reasons[0] = (
            f"{as_of:%d.%m.}: Wettkampfnähe oder Entlastungsphase: "
            "kurzer lockerer Lauf; heute kein zusätzlicher Qualitätsreiz."
        )
    if len({c.revision.event_type for c in cycles}) > 1:
        return replace(
            base,
            state="clarification",
            name="Trainingsziel klären",
            reasons=(
                "Mehrere angenommene Trainingszyklen mit unterschiedlichen Zielen gelten heute. "
                "Welcher Zyklus soll Vorrang haben?",
            ),
        )
    preferred_long_day = bool(
        inputs.profile and inputs.profile.preferred_long_run_weekday == as_of.weekday()
    )

    consistent = _count_consistent_weeks(session, user.id, observed_on)
    quality_ok = (
        not any(
            (stress, unknown_recent, sustained_week, planned_quality, reentry, high_volume, adverse)
        )
        and habitual >= 3
        and consistent >= 6
        and phase not in {"taper", "recovery"}
        and not near_event
    )
    # Comparable work requires linked accepted content and successful effective feedback.
    comparable: list[tuple[date, WorkoutRevision]] = []
    linked_revisions = {
        workout_id: revision
        for workout_id, revision in session.execute(
            select(Workout.id, WorkoutRevision)
            .join(WorkoutRevision, Workout.accepted_revision_id == WorkoutRevision.id)
            .where(
                Workout.user_id == user.id,
                Workout.id.in_({f.workout_id for f in facts if f.workout_id is not None}),
            )
        )
    }
    for fact in facts:
        if (
            not fact.running
            or fact.workout_id is None
            or fact.effort is None
            or fact.effort > 8
            or fact.feel is None
            or fact.feel < 3
            or not fact.completion_supported
        ):
            continue
        revision = linked_revisions.get(fact.workout_id)
        if (
            revision
            and revision.template_id in SUSTAINED
            and fact.duration_s
            and fact.duration_s >= workout_metrics(revision.definition_model).duration_seconds * 0.9
        ):
            comparable.append((fact.day, revision))
    if quality_ok and not preferred_long_day and goal in {"5k", "10k", "half_marathon", "marathon"}:
        template = "threshold_cruise"
        if (
            goal in {"5k", "10k"}
            and consistent >= 8
            and workout_pace_guidance(shadow.intensity, "vo2_intervals") is not None
            and len(comparable) >= 2
            and any(r.template_id == "vo2_intervals" for _, r in comparable)
            and comparable[-1][1].template_id != "vo2_intervals"
        ):
            template = "vo2_intervals"
        parameters = automatic_interval_parameters(
            template,
            min(budget, floor(normal_minutes * QUALITY_SESSION_SIZE_CEILING)),
            comparable=[r for _, r in comparable if r.template_id == template],
        )
        if parameters is None:
            template, parameters = "easy_run", TemplateParameters(duration_minutes=minutes)
            reasons[0] = (
                "Der kleinste gültige Qualitätsreiz passt mit Aufwärmen und Auslaufen "
                "nicht in deinen Zeit- und Belastungsrahmen. Stattdessen ein lockerer Lauf."
            )
        else:
            reasons[0] = (
                f"{as_of:%d.%m.}: Kontinuierliches Lauftraining und dein Ziel {goal} "
                "erlauben heute einen kontrollierten Qualitätsreiz; in den letzten "
                "sieben Tagen ist kein anstrengender Dauerreiz erfasst."
            )
    elif (
        not any((stress, reentry, high_volume, adverse, unknown_recent))
        and inputs.profile
        and inputs.profile.preferred_long_run_weekday == as_of.weekday()
        and recent.frequency_per_week >= 2
        and not near_event
        and phase not in {"taper", "recovery"}
    ):
        long_minutes = min(budget, floor(long_reference / 60), 120)
        if long_minutes >= 60:
            template, parameters = "long_run", TemplateParameters(duration_minutes=long_minutes)
            reasons[0] = (
                f"{as_of:%d.%m.}: Dein bevorzugter langer Lauftag; "
                "der Umfang bleibt innerhalb deiner jüngsten langen Läufe."
            )
    if (
        template == "easy_run"
        and quality_ok
        and any(f.template_id == "strides" and f.completion_supported for f in facts)
        and not any(f.template_id == "strides" and (as_of - f.day).days < 7 for f in facts)
    ):
        # Four familiar short strides; their time is included in the ordinary dose.
        easy_minutes = min(minutes - 8, 60)
        if easy_minutes >= 20:
            template, parameters = (
                "strides",
                TemplateParameters(duration_minutes=easy_minutes, repetitions=4),
            )
            reasons[0] = (
                f"{as_of:%d.%m.}: Bekannte kurze Steigerungen ergänzen deinen lockeren Lauf; "
                "sie zählen nicht als volle VO₂-Einheit."
            )

    data, metadata = RunningProposalService(session, user, as_of=observed_on).build_candidate(
        template_id=template,
        suggested_for=as_of,
        available_minutes=budget,
        edit_source="generator",
        parameters=parameters,
    )
    load = metadata.load_estimate_json or {}
    domains = load.get("time_by_intensity_domain_seconds", {})
    assert isinstance(domains, dict)
    work = float(domains.get("moderate", 0)) + float(domains.get("high", 0))
    duration = workout_metrics(data.definition).duration_seconds
    reasons.append(
        f"{as_of:%d.%m.}: {duration / 60:g} Minuten gesamt, "
        f"{work / 60:g} Minuten zügige Arbeit; übliche Laufdauer {normal_minutes} Minuten. "
        "Freie Zeit ist nur die Obergrenze."
    )
    guidance = metadata.guidance_json or {}
    pace = guidance.get("pace_target")
    if isinstance(pace, dict):
        reasons.append(
            f"{pace['source_day']}: {pace['label']}; daraus ein gerundeter, "
            "formatspezifischer Pace-Bereich. RPE und Sprechtest bleiben maßgeblich."
        )
    elif isinstance(guidance.get("device_target"), dict):
        reasons.append(
            "Deine konfigurierte Garmin-HF-Zone 2 ergänzt RPE 2–3 und Sprechtest; "
            "sie belegt keine gemessene physiologische Schwelle."
        )
    else:
        reasons.append(
            "Ohne passenden belastbaren Leistungsanker steuert RPE mit Sprechtest "
            "die Intensität; es wird kein präzises Geräte-Ziel erfunden."
        )
    context = {
        **(metadata.generation_context_json or {}),
        "daily_recommendation": {
            "version": DAILY_RULES_VERSION,
            "context_fingerprint": fingerprint,
            "as_of": as_of.isoformat(),
            "reasons": reasons,
            "assumptions": assumptions,
            "goal": goal,
            "phase": phase,
            "recent_training": [asdict(f) | {"day": f.day.isoformat()} for f in facts],
        },
    }
    metadata = replace(
        metadata,
        generation_context_json=context,
        guidance_json={
            **(metadata.guidance_json or {}),
            "reasons": reasons,
            "assumptions": assumptions,
        },
    )
    # Include exact target/profile and chosen structure in the preview identity.
    fingerprint = _fingerprint((fingerprint, workout_content_hash(data), metadata.guidance_json))
    context["daily_recommendation"]["context_fingerprint"] = fingerprint
    return replace(
        base,
        state="workout",
        name=data.name,
        context_fingerprint=fingerprint,
        reasons=tuple(reasons),
        template_id=template,
        definition=data.definition,
        duration_seconds=duration,
        work_seconds=work,
        data=data,
        metadata=metadata,
    )


def automatic_interval_parameters(
    template_id: RunningTemplateId, budget: int, *, comparable: list[WorkoutRevision]
) -> TemplateParameters | None:
    """Maintain observed work; no completion evidence means the minimum valid dose."""
    structure = get_knowledge_registry().workouts[template_id].structure
    assert isinstance(structure, IntervalStructure)
    repetitions, work = (3, 5) if template_id == "threshold_cruise" else (4, 3)
    if comparable:
        selected = (comparable[-1].generation_context_json or {}).get("selected_parameters", {})
        if isinstance(selected, dict) and selected:
            repetitions = int(selected.get("repetitions", repetitions))
            work = int(selected.get("work_minutes", structure.work_minutes.default))
        else:
            for block in comparable[-1].definition_model.blocks:
                if isinstance(block, RepeatBlockV2):
                    for step in block.children:
                        if (
                            isinstance(step, StepBlockV2)
                            and step.step_type == "interval"
                            and isinstance(step.end, TimeEnd)
                        ):
                            repetitions, work = block.iterations, int(step.end.seconds // 60)
    work = min(structure.work_minutes.maximum, max(structure.work_minutes.minimum, work))
    for count in range(
        min(repetitions, structure.repetitions.maximum), structure.repetitions.minimum - 1, -1
    ):
        total = (
            structure.warmup_minutes.default
            + count * (work + structure.recovery_minutes.default)
            + structure.cooldown_minutes.default
        )
        if (
            structure.total_work_minutes.minimum
            <= count * work
            <= structure.total_work_minutes.maximum
            and total <= budget
        ):
            return TemplateParameters(repetitions=count, work_minutes=work)
    return None


def save_recommendation(
    session: Session,
    user: User,
    *,
    as_of: date,
    expected_fingerprint: str,
    available_minutes: int | None = None,
    option: Literal["standard", "shorter", "easier"] = "standard",
    original_minutes: int | None = None,
) -> Workout | DailyRecommendation:
    preview = recommend_option(
        session,
        user,
        as_of=as_of,
        available_minutes=available_minutes,
        option=option,
        original_minutes=original_minutes,
    )
    if preview.context_fingerprint != expected_fingerprint or preview.state != "workout":
        return preview
    service = WorkoutService(session, user)
    key = f"daily:{as_of}:{preview.context_fingerprint}"
    existing = service.idempotent_proposal(
        idempotency_key=key, request_fingerprint=preview.context_fingerprint
    )
    if existing is not None:
        if existing.approval_status == "rejected":
            raise WorkoutTransitionError(
                "Dieser Vorschlag wurde bereits abgelehnt.", code="recommendation.rejected"
            )
        return existing
    assert preview.data is not None and preview.metadata is not None
    return service.create_proposal(
        preview.data,
        preview.metadata,
        idempotency_key=key,
        request_fingerprint=preview.context_fingerprint,
    )


def recommend_option(
    session: Session,
    user: User,
    *,
    as_of: date,
    available_minutes: int | None = None,
    option: Literal["standard", "shorter", "easier"] = "standard",
    original_minutes: int | None = None,
) -> DailyRecommendation:
    if option == "standard":
        return recommend_today(session, user, as_of=as_of, available_minutes=available_minutes)
    original = recommend_today(session, user, as_of=as_of, available_minutes=original_minutes)
    if original.state != "workout":
        return original
    budget = min(
        available_minutes
        if available_minutes is not None
        else floor(original.duration_seconds / 60 * 0.75),
        max(0, floor(original.duration_seconds / 60) - 1),
    )
    preview = recommend_today(session, user, as_of=as_of, available_minutes=budget)
    if option == "easier" and preview.state == "workout":
        data, metadata = RunningProposalService(session, user, as_of=as_of).build_candidate(
            template_id="easy_run",
            suggested_for=as_of,
            available_minutes=budget,
            edit_source="generator",
            parameters=TemplateParameters(duration_minutes=min(budget, 90)),
        )
        preview = replace(
            preview,
            template_id="easy_run",
            data=data,
            metadata=metadata,
            name=data.name,
            definition=data.definition,
            duration_seconds=workout_metrics(data.definition).duration_seconds,
            work_seconds=0,
            reasons=(
                "Lockerer Lauf mit geringerem Umfang und ohne zügige Arbeitsblöcke.",
                "Deine konfigurierte Garmin-HF-Zone ergänzt RPE und Sprechtest."
                if (metadata.guidance_json or {}).get("device_target")
                else "Intensität nach RPE und Sprechtest; kein belastbarer Geräte-Zielwert.",
            ),
        )
    from app.services.planning.constraints import adaptation_does_not_increase_load

    if preview.definition is not None and original.definition is not None:
        original_load = adaptation_load(
            original.definition,
            load_estimate=original.metadata.load_estimate_json if original.metadata else None,
        )
        changed_load = adaptation_load(
            preview.definition,
            load_estimate=preview.metadata.load_estimate_json if preview.metadata else None,
        )
        if not adaptation_does_not_increase_load(original_load.dimensions, changed_load.dimensions):
            return recommend_option(
                session,
                user,
                as_of=as_of,
                available_minutes=0,
                option="easier",
                original_minutes=original_minutes,
            )
    reason = (
        f"{'Kürzer' if option == 'shorter' else 'Leichter'}: {original.duration_seconds / 60:g} → "
        f"{preview.duration_seconds / 60:g} Minuten gesamt; zügige Arbeit "
        f"{original.work_seconds / 60:g} → {preview.work_seconds / 60:g} Minuten. "
        "Wenn ein gültiger Intervallablauf nicht passt, entfällt der Qualitätsreiz."
    )
    reasons = (reason,) + tuple(r for r in preview.reasons if "Qualitätsreiz" not in r)
    metadata = preview.metadata
    fingerprint = _fingerprint(
        (
            original.context_fingerprint,
            preview.context_fingerprint,
            option,
            preview.definition.model_dump(mode="json") if preview.definition else None,
            asdict(metadata) if metadata else None,
        )
    )
    if metadata is not None:
        metadata = replace(
            metadata,
            guidance_json={**(metadata.guidance_json or {}), "reasons": list(reasons)},
            generation_context_json={
                **(metadata.generation_context_json or {}),
                "alternative": option,
                "original_fingerprint": original.context_fingerprint,
            },
        )
    return replace(preview, reasons=reasons, metadata=metadata, context_fingerprint=fingerprint)

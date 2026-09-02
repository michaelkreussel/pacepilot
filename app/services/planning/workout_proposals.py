import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from math import ceil

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import coach_feature_enabled, get_settings
from app.models import GarminAccount, User, Workout, WorkoutRevision
from app.services.analytics.athlete_data import AthleteDataService
from app.services.analytics.running_intensity import RunningShadowAnalysis
from app.services.garmin.heart_rate_zones import is_valid_normalized_heart_rate_zone_profile
from app.services.planning.load_estimate import IntensityDomainTime, LoadEstimate
from app.services.planning.registry import WorkoutFormatId, get_knowledge_registry
from app.services.planning.registry_models import (
    ContinuousStructure,
    IntervalStructure,
    RpeRange,
    StridesStructure,
    WorkoutTemplate,
)
from app.services.planning.training_fit import (
    TrainingFitAssessment,
    TrainingFitOutcome,
    assess_training_fit,
)
from app.services.planning.validator import WorkoutInput
from app.services.planning.weekly_planner import _count_consistent_weeks
from app.services.planning.workout_definition import (
    HeartRateRangeTarget,
    RpeRangeTarget,
    StepBlockV2,
    TimeEnd,
    WorkoutDefinitionV2,
)
from app.services.planning.workout_revision import RevisionMetadata, workout_content_hash
from app.services.planning.workout_service import (
    ProposalOrigin,
    WorkoutService,
    WorkoutTransitionError,
)
from app.services.planning.workout_templates import (
    ExpandedWorkoutTemplate,
    TemplateEligibilityContext,
    TemplateParameters,
    expand_workout_template,
)

PROPOSAL_SOURCE = "coach_single"
PROPOSAL_RULE_SET_VERSION = "running-workout-candidate-v4"
RunningTemplateId = WorkoutFormatId
QUALITY_TEMPLATE_IDS = frozenset({"strides", "threshold_cruise", "vo2_intervals"})


class EasyRunProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    suggested_for: date
    available_minutes: int = Field(ge=20, le=1440)
    idempotency_key: str = Field(min_length=8, max_length=200)


class RunningProposalRequest(EasyRunProposalRequest):
    template_id: RunningTemplateId = "easy_run"


class WorkoutProposalError(ValueError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class EasyRunDeviceTarget:
    target: HeartRateRangeTarget
    provenance: dict[str, object]


def _easy_run_device_target(session: Session, user_id: int) -> EasyRunDeviceTarget | None:
    account = session.scalar(select(GarminAccount).where(GarminAccount.user_id == user_id))
    if (
        account is None
        or account.principal_fingerprint is None
        or account.heart_rate_zones_synced_at is None
        or not account.heart_rate_zone_profiles
    ):
        return None
    profiles = [
        profile
        for profile in account.heart_rate_zone_profiles
        if isinstance(profile, dict)
        and str(profile.get("sport", "")).upper() in {"RUNNING", "DEFAULT"}
    ]
    profiles.sort(key=lambda item: 0 if str(item.get("sport", "")).upper() == "RUNNING" else 1)
    for profile in profiles:
        if not is_valid_normalized_heart_rate_zone_profile(profile):
            continue
        raw_floors = profile.get("zone_floors")
        assert isinstance(raw_floors, list)
        normalized = [int(value) for value in raw_floors]
        lower_bpm = normalized[1]
        upper_bpm = normalized[2] - 1
        if not 30 <= lower_bpm < upper_bpm <= 250:
            continue
        sport = str(profile.get("sport", "")).upper()
        method = str(profile.get("training_method", "unknown"))
        return EasyRunDeviceTarget(
            target=HeartRateRangeTarget(
                type="heart_rate_range", lower_bpm=lower_bpm, upper_bpm=upper_bpm
            ),
            provenance={
                "type": "heart_rate_range",
                "lower_bpm": lower_bpm,
                "upper_bpm": upper_bpm,
                "source": "garmin_heart_rate_zone_profile",
                "principal_fingerprint": account.principal_fingerprint,
                "profile_sport": sport,
                "training_method": method,
                "synced_at": account.heart_rate_zones_synced_at.isoformat(),
                "synced_on": account.heart_rate_zones_synced_at.date().isoformat(),
                "policy": "personalized_aerobic_zone_2_bounds_v1",
            },
        )
    return None


def ensure_easy_run_device_target_current(
    session: Session, user_id: int, revision: WorkoutRevision
) -> None:
    guidance = revision.guidance_json
    device_target = guidance.get("device_target") if guidance else None
    if not isinstance(device_target, dict):
        return
    expected_principal = device_target.get("principal_fingerprint")
    account = session.scalar(select(GarminAccount).where(GarminAccount.user_id == user_id))
    if (
        not isinstance(expected_principal, str)
        or account is None
        or account.principal_fingerprint != expected_principal
    ):
        raise WorkoutTransitionError(
            "Das persönliche HF-Ziel gehört nicht mehr zum verbundenen Garmin-Konto. "
            "Bitte erstelle einen neuen Vorschlag.",
            code="proposal.device_target_principal_changed",
        )


def _request_fingerprint(request: EasyRunProposalRequest | RunningProposalRequest) -> str:
    payload = json.dumps(
        request.model_dump(mode="json", exclude={"idempotency_key"}),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _candidate_inputs(
    session: Session,
    user: User,
    *,
    as_of: date,
    suggested_for: date,
) -> RunningShadowAnalysis:
    if suggested_for < as_of:
        raise WorkoutProposalError(
            "Das vorgeschlagene Datum darf nicht in der Vergangenheit liegen.",
            code="proposal.date_in_past",
        )
    shadow = AthleteDataService(session, user.id, as_of=as_of).get_running_shadow_analysis()
    return shadow


def quality_density_conflicts(
    session: Session,
    user_id: int,
    suggested_for: date,
    *,
    exclude_workout_id: int | None = None,
) -> tuple[int, ...]:
    rows = session.execute(
        select(Workout.id, Workout.scheduled_for, WorkoutRevision.suggested_for)
        .join(WorkoutRevision, WorkoutRevision.id == Workout.accepted_revision_id)
        .where(
            Workout.user_id == user_id,
            Workout.deleted_at.is_(None),
            Workout.accepted_revision_id.is_not(None),
            WorkoutRevision.template_id.in_(QUALITY_TEMPLATE_IDS),
        )
    ).all()
    conflicts = []
    for workout_id, scheduled_for, revision_suggested_for in rows:
        if exclude_workout_id is not None and workout_id == exclude_workout_id:
            continue
        existing_date = scheduled_for or revision_suggested_for
        if existing_date is not None and abs((existing_date - suggested_for).days) < 2:
            conflicts.append(workout_id)
    return tuple(sorted(conflicts))


def _template_context(
    session: Session,
    user_id: int,
    shadow: RunningShadowAnalysis,
    available_minutes: int,
    as_of: date,
) -> TemplateEligibilityContext:
    recent = shadow.baseline.window(28)
    consistent_weeks = _count_consistent_weeks(session, user_id, as_of)
    return TemplateEligibilityContext(
        consistent_running_weeks=consistent_weeks,
        runs_per_week=round(recent.frequency_per_week),
        available_minutes=available_minutes,
    )


def _training_fit_artifact(
    assessment: TrainingFitAssessment,
    shadow: RunningShadowAnalysis,
    template: WorkoutTemplate,
    context: TemplateEligibilityContext,
    *,
    as_of: date,
    quality_conflicts: tuple[int, ...],
) -> dict[str, object]:
    recent = shadow.baseline.window(56)
    advisories: list[dict[str, object]] = []
    if recent.runs == 0:
        advisories.append(
            {
                "code": "proposal.running_history_sparse",
                "source": "running.history",
                "observed_on": as_of.isoformat(),
                "value": 0,
                "unit": "runs_56_days",
                "severe": False,
            }
        )
    if context.runs_per_week < max(template.eligibility.min_runs_per_week, 2):
        advisories.append(
            {
                "code": "proposal.weekly_frequency_low",
                "source": "running.frequency",
                "observed_on": as_of.isoformat(),
                "value": context.runs_per_week,
                "unit": "runs_per_week",
                "severe": False,
            }
        )
    if context.consistent_running_weeks < max(template.eligibility.min_consistent_running_weeks, 4):
        advisories.append(
            {
                "code": "proposal.consistent_weeks_sparse",
                "source": "running.consistency",
                "observed_on": as_of.isoformat(),
                "value": context.consistent_running_weeks,
                "unit": "weeks",
                "severe": False,
            }
        )
    if quality_conflicts:
        advisories.append(
            {
                "code": "proposal.quality_spacing_close",
                "source": "workout.accepted_quality",
                "observed_on": assessment.effective_workout_date.isoformat(),
                "value": list(quality_conflicts),
                "unit": "workout_ids",
                "severe": False,
            }
        )

    warning_codes = list(
        dict.fromkeys([*assessment.warning_codes, *(str(item["code"]) for item in advisories)])
    )
    outcome = assessment.outcome
    if advisories and outcome == TrainingFitOutcome.NORMAL:
        outcome = TrainingFitOutcome.CAUTION
    if outcome == TrainingFitOutcome.ELEVATED:
        recommendation = (
            "Der angeforderte Entwurf bleibt verfügbar; für heute wird Auslassen oder eine "
            "konservativere Alternative empfohlen."
        )
    elif warning_codes:
        recommendation = (
            "Der angeforderte Entwurf bleibt verfügbar; prüfe vor der Annahme die "
            "konservativere Alternative."
        )
    else:
        recommendation = "Der angeforderte Entwurf passt zu den aktuell verfügbaren Daten."

    return {
        "outcome": outcome.value,
        "policy_version": assessment.policy_version,
        "evaluated_at": assessment.evaluated_at.isoformat(),
        "effective_workout_date": assessment.effective_workout_date.isoformat(),
        "warning_codes": warning_codes,
        "evidence": [
            {
                "code": item.code,
                "source": item.source,
                "observed_on": item.observed_on.isoformat(),
                "value": item.value,
                "unit": item.unit,
                "personal_baseline": item.personal_baseline,
                "ratio_from_baseline": item.ratio_from_baseline,
                "severe": item.severe,
                "feedback_id": item.feedback_id,
            }
            for item in assessment.evidence
        ]
        + advisories,
        "coverage": [
            {
                "metric": item.metric,
                "current_day": item.current_day.isoformat() if item.current_day else None,
                "baseline_sample_count": item.baseline_sample_count,
                "minimum_baseline_samples": item.minimum_baseline_samples,
                "sufficient_for_elevation": item.sufficient_for_elevation,
            }
            for item in assessment.coverage
        ]
        + [
            {
                "metric": "running_history_56_days",
                "current_day": (
                    recent.quality.latest_run_day.isoformat()
                    if recent.quality.latest_run_day
                    else None
                ),
                "baseline_sample_count": recent.runs,
                "minimum_baseline_samples": 1,
                "sufficient_for_elevation": False,
            }
        ],
        "feedback_ids": list(assessment.feedback_ids),
        "authoritative_input_fingerprint": assessment.authoritative_input_fingerprint,
        "recommendation": recommendation,
        "alternative": {
            "type": "registry_fallback",
            "code": template.fallback_targets[0],
        },
    }


def _validation_report(template_id: str, duration_minutes: int) -> dict[str, object]:
    return {
        "valid": True,
        "issues": [],
        "rule_set_version": PROPOSAL_RULE_SET_VERSION,
        "checks": [
            {"code": "structure.valid", "result": "pass"},
            {"code": "template.duration", "result": "pass", "value": duration_minutes},
            {"code": "template.selected", "result": "pass", "value": template_id},
        ],
    }


def _generation_context(
    shadow: RunningShadowAnalysis,
    training_fit: dict[str, object],
    *,
    suggested_for: date,
    available_minutes: int,
    selected_minutes: int,
    template_id: str,
    as_of: date,
    quality_conflicts: tuple[int, ...],
) -> dict[str, object]:
    return {
        "schema_version": "running_workout_proposal_context.v2",
        "as_of": as_of.isoformat(),
        "request": {
            "suggested_for": suggested_for.isoformat(),
            "available_minutes": available_minutes,
            "selected_minutes": selected_minutes,
            "template_id": template_id,
        },
        "athlete": shadow.generation_context,
        "athlete_context_fingerprint": shadow.context_fingerprint,
        "training_fit": {
            "outcome": training_fit["outcome"],
            "policy_version": training_fit["policy_version"],
            "authoritative_input_fingerprint": training_fit["authoritative_input_fingerprint"],
        },
        "performance_model_version": shadow.intensity.intensity_version,
        "quality_density": {
            "checked": template_id in QUALITY_TEMPLATE_IDS,
            "minimum_spacing_hours": 48,
            "conflicting_workout_ids": list(quality_conflicts),
        },
        "units": {"duration": "seconds", "distance": "meters", "rpe": "1-10"},
    }


def _metadata(
    expanded: ExpandedWorkoutTemplate,
    shadow: RunningShadowAnalysis,
    assessment: TrainingFitAssessment,
    template_context: TemplateEligibilityContext,
    *,
    suggested_for: date,
    available_minutes: int,
    selected_minutes: int,
    edit_source: str,
    as_of: date,
    quality_conflicts: tuple[int, ...],
) -> RevisionMetadata:
    device_target = expanded.guidance.get("device_target")
    rationales = {
        "easy_run": "Ein lockerer, zeitbasierter Lauf erhält die aerobe Basis.",
        "recovery_run": "Ein sehr lockerer Lauf unterstützt die aktive Erholung.",
        "long_run": "Ein langer lockerer Lauf entwickelt aerobe Ausdauer und Ermüdungsresistenz.",
        "strides": "Steigerungen verbinden lockeres Laufen mit kurzen kontrollierten Impulsen.",
        "threshold_cruise": "Kontrollierte Schwellenintervalle entwickeln die Tempohärte.",
        "vo2_intervals": (
            "Kontrollierte VO₂max-Intervalle setzen einen hochintensiven aeroben Reiz."
        ),
    }
    rationale = rationales.get(expanded.template_id, expanded.purpose)
    if isinstance(device_target, dict):
        rationale += " Dein persönlicher Garmin-HF-Bereich dient als zusätzliches Geräte-Ziel."
    training_fit = _training_fit_artifact(
        assessment,
        shadow,
        get_knowledge_registry().workouts[expanded.template_id],
        template_context,
        as_of=as_of,
        quality_conflicts=quality_conflicts,
    )
    guidance = {
        **expanded.guidance,
        "rationale": rationale,
        "evidence_refs": list(expanded.evidence_refs),
        "training_fit": training_fit,
    }
    generation_context = _generation_context(
        shadow,
        training_fit,
        suggested_for=suggested_for,
        available_minutes=available_minutes,
        selected_minutes=selected_minutes,
        template_id=expanded.template_id,
        as_of=as_of,
        quality_conflicts=quality_conflicts,
    )
    if isinstance(device_target := guidance.get("device_target"), dict):
        generation_context["device_target"] = device_target
    return RevisionMetadata(
        purpose=expanded.purpose,
        guidance_json=guidance,
        load_estimate_json=expanded.load_estimate.model_dump(mode="json"),
        validation_report_json=_validation_report(expanded.template_id, selected_minutes),
        generation_context_json=generation_context,
        source_type=PROPOSAL_SOURCE,
        generator_version=expanded.generator_version,
        template_id=expanded.template_id,
        template_version=expanded.template_version,
        rule_set_version=PROPOSAL_RULE_SET_VERSION,
        knowledge_base_version=expanded.knowledge_base_version,
        edit_source=edit_source,
    )


class RunningProposalService:
    def __init__(
        self,
        session: Session,
        user: User,
        *,
        as_of: date,
        request_id: str | None = None,
    ) -> None:
        self.session = session
        self.user = user
        self.as_of = as_of
        self.request_id = request_id

    def create_easy_run(
        self, request: EasyRunProposalRequest, *, origin: ProposalOrigin | None = None
    ) -> Workout:
        return self.create(
            RunningProposalRequest(
                template_id="easy_run",
                suggested_for=request.suggested_for,
                available_minutes=request.available_minutes,
                idempotency_key=request.idempotency_key,
            ),
            origin=origin,
        )

    def create(
        self, request: RunningProposalRequest, *, origin: ProposalOrigin | None = None
    ) -> Workout:
        request_fingerprint = _request_fingerprint(request)
        workout_service = WorkoutService(self.session, self.user, request_id=self.request_id)
        existing = workout_service.idempotent_proposal(
            idempotency_key=request.idempotency_key,
            request_fingerprint=request_fingerprint,
        )
        if existing is not None:
            workout_service.verify_proposal_origin(existing, origin)
            return existing
        if not coach_feature_enabled(get_settings().coach_workout_proposals_enabled, self.user.id):
            raise WorkoutProposalError(
                "Trainingsvorschläge sind noch nicht freigeschaltet.",
                code="proposal.feature_disabled",
            )
        shadow = _candidate_inputs(
            self.session,
            self.user,
            as_of=self.as_of,
            suggested_for=request.suggested_for,
        )
        quality_conflicts: tuple[int, ...] = ()
        if request.template_id in QUALITY_TEMPLATE_IDS:
            quality_conflicts = quality_density_conflicts(
                self.session, self.user.id, request.suggested_for
            )
        registry = get_knowledge_registry()
        template = registry.workouts[request.template_id]
        template_context = _template_context(
            self.session,
            self.user.id,
            shadow,
            request.available_minutes,
            self.as_of,
        )
        parameters: TemplateParameters | None = None
        if isinstance(template.structure, ContinuousStructure):
            selected_minutes = min(
                request.available_minutes,
                template.structure.duration_minutes.maximum,
                90 if request.template_id == "easy_run" else request.available_minutes,
            )
            parameters = TemplateParameters(duration_minutes=selected_minutes)
        elif isinstance(template.structure, StridesStructure):
            repetitions = template.structure.repetitions.default
            overhead_seconds = repetitions * (
                template.structure.stride_duration_seconds.default
                + template.structure.recovery_duration_seconds.default
            )
            easy_minutes = min(
                template.structure.easy_duration_minutes.maximum,
                (request.available_minutes * 60 - overhead_seconds) // 60,
            )
            parameters = TemplateParameters(
                duration_minutes=max(easy_minutes, 1),
                repetitions=repetitions,
            )
        elif isinstance(template.structure, IntervalStructure):
            for repetitions in range(
                template.structure.repetitions.default,
                template.structure.repetitions.minimum - 1,
                -1,
            ):
                total_work = repetitions * template.structure.work_minutes.default
                duration = (
                    template.structure.warmup_minutes.default
                    + repetitions
                    * (
                        template.structure.work_minutes.default
                        + template.structure.recovery_minutes.default
                    )
                    + template.structure.cooldown_minutes.default
                )
                if (
                    template.structure.total_work_minutes.minimum
                    <= total_work
                    <= template.structure.total_work_minutes.maximum
                    and duration <= request.available_minutes
                ):
                    parameters = TemplateParameters(repetitions=repetitions)
                    break
        expanded = expand_workout_template(
            request.template_id,
            parameters,
            eligibility=template_context,
        )
        selected_minutes = ceil(expanded.load_estimate.duration_seconds / 60)
        device_target = (
            _easy_run_device_target(self.session, self.user.id)
            if request.template_id == "easy_run"
            else None
        )
        if device_target is not None:
            definition = expanded.definition.model_copy(deep=True)
            step = definition.blocks[0]
            if not isinstance(step, StepBlockV2):
                raise WorkoutProposalError(
                    "Das Easy-Run-Template besitzt keinen ausführbaren Hauptschritt.",
                    code="proposal.easy_run_structure_invalid",
                )
            step.target = device_target.target
            expanded = replace(
                expanded,
                definition=definition,
                guidance={
                    **expanded.guidance,
                    "device_target": device_target.provenance,
                },
            )
        data = WorkoutInput(
            name=expanded.name,
            sport="running",
            scheduled_for=request.suggested_for,
            description=f"Deterministischer {expanded.name}-Vorschlag von PacePilot.",
            definition=expanded.definition,
            definition_version=expanded.definition_version,
        )
        assessment = assess_training_fit(
            self.session,
            self.user.id,
            effective_workout_date=request.suggested_for,
            revision_fingerprint=workout_content_hash(data),
            evaluated_at=datetime.combine(self.as_of, time.max),
        )
        metadata = _metadata(
            expanded,
            shadow,
            assessment,
            template_context,
            suggested_for=request.suggested_for,
            available_minutes=request.available_minutes,
            selected_minutes=selected_minutes,
            edit_source="generator",
            as_of=self.as_of,
            quality_conflicts=quality_conflicts,
        )
        return workout_service.create_proposal(
            data,
            metadata,
            idempotency_key=request.idempotency_key,
            request_fingerprint=request_fingerprint,
            origin=origin,
        )


def edited_easy_run_metadata(
    session: Session,
    user: User,
    current: WorkoutRevision,
    data: WorkoutInput,
) -> RevisionMetadata:
    as_of = date.today()
    ensure_easy_run_device_target_current(session, user.id, current)
    if current.template_id != "easy_run" or current.generation_context_json is None:
        raise WorkoutTransitionError(
            "Dieser generierte Vorschlag kann noch nicht im Easy-Run-Editor bearbeitet werden.",
            code="proposal.edit_unsupported",
        )
    if data.scheduled_for is None:
        raise WorkoutTransitionError(
            "Ein Vorschlag benötigt weiterhin ein Wunschdatum.",
            code="proposal.date_required",
        )
    if data.sport != "running":
        raise WorkoutTransitionError(
            "Ein Easy-Run-Vorschlag muss die Sportart Laufen behalten.",
            code="proposal.easy_run_sport_invalid",
        )
    definition = data.definition
    if not isinstance(definition, WorkoutDefinitionV2) or len(definition.blocks) != 1:
        raise WorkoutTransitionError(
            "Der Easy Run muss aus genau einem zeitbasierten Schritt bestehen.",
            code="proposal.easy_run_structure_invalid",
        )
    step = definition.blocks[0]
    current_definition = current.definition_model
    current_step = current_definition.blocks[0] if len(current_definition.blocks) == 1 else None
    if (
        not isinstance(step, StepBlockV2)
        or not isinstance(step.end, TimeEnd)
        or not isinstance(current_step, StepBlockV2)
        or step.target != current_step.target
        or not isinstance(step.target, (RpeRangeTarget, HeartRateRangeTarget))
        or (
            isinstance(step.target, RpeRangeTarget)
            and (step.target.lower_rpe != 2 or step.target.upper_rpe != 3)
        )
        or not any("RPE 2–3" in instruction for instruction in step.instructions)
        or not any("vollständigen Sätzen" in instruction for instruction in step.instructions)
    ):
        raise WorkoutTransitionError(
            "Der Easy Run muss sein persönliches Geräte-Ziel sowie RPE 2–3 und den "
            "Sprechtest behalten.",
            code="proposal.easy_run_intensity_invalid",
        )
    if step.end.seconds % 60:
        raise WorkoutTransitionError(
            "Die Easy-Run-Dauer muss in ganzen Minuten angegeben werden.",
            code="proposal.easy_run_duration_invalid",
        )
    selected_minutes = round(step.end.seconds / 60)
    request_context = current.generation_context_json.get("request", {})
    if not isinstance(request_context, dict):
        raise WorkoutTransitionError(
            "Der ursprüngliche Zeitkontext dieses Vorschlags fehlt.",
            code="proposal.context_invalid",
        )
    available_minutes = int(request_context.get("available_minutes", 0))
    if not 20 <= selected_minutes <= min(90, available_minutes):
        raise WorkoutTransitionError(
            "Der Easy Run muss zwischen 20 Minuten und deinem Zeitbudget liegen.",
            code="proposal.easy_run_duration_invalid",
        )
    shadow = _candidate_inputs(session, user, as_of=as_of, suggested_for=data.scheduled_for)
    estimate = LoadEstimate(
        duration_seconds=selected_minutes * 60,
        distance_meters=None,
        time_by_intensity_domain_seconds=IntensityDomainTime(
            low=selected_minutes * 60, moderate=0, high=0
        ),
        mechanical_load="low",
        session_rpe=RpeRange(minimum=2, maximum=3),
        confidence="moderate",
        uncertainty=[
            "distance_unknown_for_time_based_workout",
            "individual_response_requires_baseline_validation",
        ],
    )
    template_context = _template_context(session, user.id, shadow, available_minutes, as_of)
    expanded = expand_workout_template(
        "easy_run",
        TemplateParameters(duration_minutes=selected_minutes),
        eligibility=template_context,
    )
    current_device_target = (
        current.guidance_json.get("device_target") if current.guidance_json else None
    )
    if isinstance(current_device_target, dict):
        expanded = replace(
            expanded,
            guidance={**expanded.guidance, "device_target": current_device_target},
        )
    assessment = assess_training_fit(
        session,
        user.id,
        effective_workout_date=data.scheduled_for,
        revision_fingerprint=workout_content_hash(data),
        evaluated_at=datetime.combine(as_of, time.max),
    )
    metadata = _metadata(
        expanded,
        shadow,
        assessment,
        template_context,
        suggested_for=data.scheduled_for,
        available_minutes=available_minutes,
        selected_minutes=selected_minutes,
        edit_source="user",
        as_of=as_of,
        quality_conflicts=(),
    )
    generation_context = deepcopy(metadata.generation_context_json)
    assert generation_context is not None
    generation_context["parent_revision_id"] = current.id
    return RevisionMetadata(
        purpose=metadata.purpose,
        guidance_json=metadata.guidance_json,
        load_estimate_json=estimate.model_dump(mode="json"),
        validation_report_json=metadata.validation_report_json,
        generation_context_json=generation_context,
        source_type=metadata.source_type,
        generator_version=metadata.generator_version,
        template_id=metadata.template_id,
        template_version=metadata.template_version,
        rule_set_version=metadata.rule_set_version,
        knowledge_base_version=metadata.knowledge_base_version,
        edit_source=metadata.edit_source,
    )

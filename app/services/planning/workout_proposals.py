import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import date

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import GarminAccount, User, Workout, WorkoutRevision
from app.services.analytics.athlete_data import AthleteDataService
from app.services.analytics.running_intensity import RunningShadowAnalysis
from app.services.garmin.heart_rate_zones import is_valid_normalized_heart_rate_zone_profile
from app.services.planning.load_estimate import IntensityDomainTime, LoadEstimate
from app.services.planning.registry_models import RpeRange
from app.services.planning.safety_triage import (
    SAFETY_RULE_SET_VERSION,
    SafetyContext,
    TriageOutcome,
    build_proposal_safety_context,
)
from app.services.planning.validator import WorkoutInput
from app.services.planning.workout_definition import (
    HeartRateRangeTarget,
    RpeRangeTarget,
    StepBlockV2,
    TimeEnd,
    WorkoutDefinitionV2,
)
from app.services.planning.workout_revision import RevisionMetadata
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
PROPOSAL_RULE_SET_VERSION = f"easy-run-candidate-v2+{SAFETY_RULE_SET_VERSION}"


class EasyRunProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    suggested_for: date
    available_minutes: int = Field(ge=20, le=1440)
    idempotency_key: str = Field(min_length=8, max_length=200)


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


def _request_fingerprint(request: EasyRunProposalRequest) -> str:
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
    suggested_for: date,
) -> tuple[RunningShadowAnalysis, SafetyContext]:
    if suggested_for < date.today():
        raise WorkoutProposalError(
            "Das vorgeschlagene Datum darf nicht in der Vergangenheit liegen.",
            code="proposal.date_in_past",
        )
    shadow = AthleteDataService(session, user.id, as_of=date.today()).get_running_shadow_analysis()
    recent = shadow.baseline.window(56)
    if recent.runs == 0 or recent.quality.latest_run_age_days is None:
        raise WorkoutProposalError(
            "Für einen sicheren Vorschlag fehlt ein beobachteter Lauf aus den letzten 56 Tagen.",
            code="proposal.running_history_required",
        )
    safety = build_proposal_safety_context(session, user.id, suggested_for=suggested_for)
    if not safety.report.valid:
        message = (
            "Ein aktueller Sicherheitshinweis blockiert einen Laufvorschlag."
            if safety.report.outcome == TriageOutcome.SAFETY_STOP
            else "Bitte kläre zuerst die offenen Sicherheitsangaben."
        )
        raise WorkoutProposalError(message, code="proposal.safety_blocked")
    return shadow, safety


def _eligibility(
    shadow: RunningShadowAnalysis,
    safety: SafetyContext,
    available_minutes: int,
) -> TemplateEligibilityContext:
    recent = shadow.baseline.window(28)
    contraindications: set[str] = set()
    issue_codes = {issue.code for issue in safety.report.issues}
    if "safety.pain_alters_gait" in issue_codes:
        contraindications.add("active_pain_affecting_gait")
    if "safety.fever_or_systemic_illness" in issue_codes:
        contraindications.add("fever_or_systemic_illness")
    return TemplateEligibilityContext(
        consistent_running_weeks=0,
        runs_per_week=round(recent.frequency_per_week),
        available_minutes=available_minutes,
        safety_stop=safety.report.outcome == TriageOutcome.SAFETY_STOP,
        facts=set(),
        active_contraindications=contraindications,
    )


def _validation_report(duration_minutes: int) -> dict[str, object]:
    return {
        "valid": True,
        "issues": [],
        "rule_set_version": PROPOSAL_RULE_SET_VERSION,
        "checks": [
            {"code": "structure.valid", "result": "pass"},
            {"code": "easy_run.duration", "result": "pass", "value": duration_minutes},
            {"code": "easy_run.rpe_talk_test", "result": "pass"},
            {"code": "athlete.recent_running_history", "result": "pass"},
            {"code": "safety.current_context", "result": "pass"},
        ],
    }


def _generation_context(
    shadow: RunningShadowAnalysis,
    safety: SafetyContext,
    *,
    suggested_for: date,
    available_minutes: int,
    selected_minutes: int,
) -> dict[str, object]:
    return {
        "schema_version": "easy_run_proposal_context.v1",
        "as_of": date.today().isoformat(),
        "request": {
            "suggested_for": suggested_for.isoformat(),
            "available_minutes": available_minutes,
            "selected_minutes": selected_minutes,
        },
        "athlete": shadow.generation_context,
        "athlete_context_fingerprint": shadow.context_fingerprint,
        "safety": {
            "outcome": safety.report.outcome.value,
            "feedback_ids": list(safety.feedback_ids),
            "rule_set_version": SAFETY_RULE_SET_VERSION,
        },
        "performance_model_version": shadow.intensity.intensity_version,
        "units": {"duration": "seconds", "distance": "meters", "rpe": "1-10"},
    }


def _metadata(
    expanded: ExpandedWorkoutTemplate,
    shadow: RunningShadowAnalysis,
    safety: SafetyContext,
    *,
    suggested_for: date,
    available_minutes: int,
    selected_minutes: int,
    edit_source: str,
) -> RevisionMetadata:
    device_target = expanded.guidance.get("device_target")
    rationale = (
        "Ein lockerer, zeitbasierter Lauf erhält die aerobe Basis. Dein persönlicher "
        "Garmin-HF-Bereich dient als Geräte-Ziel; RPE und Sprechtest bleiben zusätzliche "
        "Leitplanken. Pace und Distanz werden nicht geschätzt."
        if isinstance(device_target, dict)
        else "Ein lockerer, zeitbasierter Lauf erhält die aerobe Basis. Ohne valides "
        "persönliches Geräte-Ziel bleiben RPE und Sprechtest maßgeblich; Pace und Distanz "
        "werden nicht geschätzt."
    )
    guidance = {
        **expanded.guidance,
        "rationale": rationale,
        "evidence_refs": list(expanded.evidence_refs),
    }
    generation_context = _generation_context(
        shadow,
        safety,
        suggested_for=suggested_for,
        available_minutes=available_minutes,
        selected_minutes=selected_minutes,
    )
    if isinstance(device_target := guidance.get("device_target"), dict):
        generation_context["device_target"] = device_target
    return RevisionMetadata(
        purpose=expanded.purpose,
        guidance_json=guidance,
        load_estimate_json=expanded.load_estimate.model_dump(mode="json"),
        validation_report_json=_validation_report(selected_minutes),
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
        request_id: str | None = None,
    ) -> None:
        self.session = session
        self.user = user
        self.request_id = request_id

    def create_easy_run(
        self, request: EasyRunProposalRequest, *, origin: ProposalOrigin | None = None
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
        if not get_settings().coach_workout_proposals_enabled:
            raise WorkoutProposalError(
                "Trainingsvorschläge sind noch nicht freigeschaltet.",
                code="proposal.feature_disabled",
            )
        shadow, safety = _candidate_inputs(
            self.session, self.user, suggested_for=request.suggested_for
        )
        selected_minutes = min(90, request.available_minutes)
        expanded = expand_workout_template(
            "easy_run",
            TemplateParameters(duration_minutes=selected_minutes),
            eligibility=_eligibility(shadow, safety, request.available_minutes),
        )
        device_target = _easy_run_device_target(self.session, self.user.id)
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
            description="Deterministischer Easy-Run-Vorschlag von PacePilot.",
            definition=expanded.definition,
            definition_version=expanded.definition_version,
        )
        metadata = _metadata(
            expanded,
            shadow,
            safety,
            suggested_for=request.suggested_for,
            available_minutes=request.available_minutes,
            selected_minutes=selected_minutes,
            edit_source="generator",
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
    shadow, safety = _candidate_inputs(session, user, suggested_for=data.scheduled_for)
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
    expanded = expand_workout_template(
        "easy_run",
        TemplateParameters(duration_minutes=selected_minutes),
        eligibility=_eligibility(shadow, safety, available_minutes),
    )
    current_device_target = (
        current.guidance_json.get("device_target") if current.guidance_json else None
    )
    if isinstance(current_device_target, dict):
        expanded = replace(
            expanded,
            guidance={**expanded.guidance, "device_target": current_device_target},
        )
    metadata = _metadata(
        expanded,
        shadow,
        safety,
        suggested_for=data.scheduled_for,
        available_minutes=available_minutes,
        selected_minutes=selected_minutes,
        edit_source="user",
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

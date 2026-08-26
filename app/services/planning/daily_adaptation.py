from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    PreSessionFeedback,
    User,
    Workout,
    WorkoutEvent,
    WorkoutRevision,
)
from app.repositories.workouts import find_workout
from app.services.analytics.athlete_data import AthleteDataService
from app.services.planning.constraints import (
    ConstraintEngine,
    LoadDimensions,
    adaptation_does_not_increase_load,
)
from app.services.planning.load_estimate import LoadEstimate
from app.services.planning.registry import KnowledgeRegistry, get_knowledge_registry
from app.services.planning.safety_triage import (
    SafetyReport,
    TriageOutcome,
    build_safety_context,
)
from app.services.planning.validator import WorkoutInput
from app.services.planning.workout_definition import (
    DistanceEnd,
    HeartRateRangeTarget,
    HeartRateZoneTarget,
    NoTarget,
    PaceRangeTarget,
    RepeatBlock,
    RepeatBlockV2,
    RpeRangeTarget,
    StepBlock,
    StepBlockV2,
    TimeEnd,
    WorkoutDefinitionModel,
    WorkoutDefinitionV2,
    workout_metrics,
)
from app.services.planning.workout_revision import RevisionIdentity, RevisionMetadata
from app.services.planning.workout_service import WorkoutService
from app.services.planning.workout_templates import (
    TemplateEligibilityContext,
    TemplateParameters,
    expand_workout_template,
)

ADAPTATION_GENERATOR_VERSION = "daily-adaptation-v1"
ADAPTATION_RULE_SET_VERSION = "daily-adaptation-rules-v1"


class DailyAdaptationClass(StrEnum):
    KEEP = "KEEP"
    REDUCE_VOLUME = "REDUCE_VOLUME"
    REPLACE_WITH_EASY = "REPLACE_WITH_EASY"
    REST = "REST"


@dataclass(frozen=True)
class AdaptationLoad:
    dimensions: LoadDimensions
    intensity_comparable: bool


@dataclass(frozen=True)
class DailyAdaptationCandidate:
    adaptation_class: DailyAdaptationClass
    definition: WorkoutDefinitionModel | None
    load: AdaptationLoad
    recommended: bool
    reason_codes: tuple[str, ...]

    @property
    def label(self) -> str:
        return {
            DailyAdaptationClass.KEEP: "Training beibehalten",
            DailyAdaptationClass.REDUCE_VOLUME: "Umfang reduzieren",
            DailyAdaptationClass.REPLACE_WITH_EASY: "Durch Easy Run ersetzen",
            DailyAdaptationClass.REST: "Ruhetag",
        }[self.adaptation_class]

    @property
    def rationale(self) -> str:
        return {
            DailyAdaptationClass.KEEP: (
                "Die angenommene Einheit und ihre Belastung bleiben unverändert."
            ),
            DailyAdaptationClass.REDUCE_VOLUME: (
                "Dauer und Distanz werden gleichmäßig reduziert; Ziele und Struktur"
                " bleiben erhalten."
            ),
            DailyAdaptationClass.REPLACE_WITH_EASY: (
                "Ein zeitbasierter Easy Run mit RPE 2–3 und Sprechtest ersetzt die Belastung."
            ),
            DailyAdaptationClass.REST: (
                "Heute ist kein Lauf vorgesehen; die ursprüngliche Revision bleibt"
                " im Audit erhalten."
            ),
        }[self.adaptation_class]

    @property
    def duration_minutes(self) -> int:
        return round(self.load.dimensions.duration_seconds / 60)

    @property
    def distance_kilometers(self) -> float:
        return round(self.load.dimensions.distance_meters / 1000, 1)


@dataclass(frozen=True)
class DailyAdaptationAssessment:
    safety_outcome: TriageOutcome
    original_load: AdaptationLoad
    candidates: tuple[DailyAdaptationCandidate, ...]
    blocked_reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlannedWeekWorkout:
    workout_id: int
    scheduled_for: date
    revision_id: int
    content_hash: str
    duration_seconds: float
    distance_meters: float


@dataclass(frozen=True)
class PlannedWeekContext:
    starts_on: date
    ends_on: date
    workouts: tuple[PlannedWeekWorkout, ...]
    fingerprint: str


@dataclass(frozen=True)
class DailyAdaptationWeekImpact:
    adaptation_class: DailyAdaptationClass
    duration_before_seconds: float
    duration_after_seconds: float
    distance_before_meters: float
    distance_after_meters: float

    @property
    def duration_delta_minutes(self) -> int:
        return round((self.duration_after_seconds - self.duration_before_seconds) / 60)

    @property
    def distance_delta_kilometers(self) -> float:
        return round((self.distance_after_meters - self.distance_before_meters) / 1000, 1)


@dataclass(frozen=True)
class DailyAdaptationPreview:
    as_of: date
    workout_id: int
    accepted_revision_id: int
    context_fingerprint: str
    baseline_fingerprint: str
    safety_fingerprint: str
    recovery_fingerprint: str
    week: PlannedWeekContext
    week_impacts: tuple[DailyAdaptationWeekImpact, ...]
    available_minutes: int | None
    assessment: DailyAdaptationAssessment

    def week_impact(self, adaptation_class: DailyAdaptationClass) -> DailyAdaptationWeekImpact:
        return next(
            impact for impact in self.week_impacts if impact.adaptation_class == adaptation_class
        )


@dataclass(frozen=True)
class DailyAdaptationApplyResult:
    workout: Workout
    adaptation_class: DailyAdaptationClass
    revision_created: bool


class DailyAdaptationError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class DailyAdaptationService:
    def __init__(
        self,
        session: Session,
        user: User,
        *,
        as_of: date | None = None,
        request_id: str | None = None,
    ) -> None:
        self.session = session
        self.user = user
        self.as_of = as_of or date.today()
        self.request_id = request_id

    def assess_today(
        self,
        workout_id: int,
        *,
        allow_open_candidate: bool = False,
        expected_replacement_id: int | None = None,
    ) -> DailyAdaptationPreview:
        if not get_settings().coach_daily_adaptation_enabled:
            raise DailyAdaptationError(
                "Die tägliche Trainingsanpassung ist noch nicht freigeschaltet.",
                code="adaptation.feature_disabled",
            )
        workout = find_workout(self.session, self.user.id, workout_id)
        if workout is None:
            raise DailyAdaptationError("Workout nicht gefunden.", code="adaptation.not_found")
        revision = self._eligible_revision(workout, allow_open_candidate=allow_open_candidate)
        open_replacements = list(
            self.session.scalars(
                select(Workout).where(
                    Workout.user_id == self.user.id,
                    Workout.replaces_workout_id == workout.id,
                    Workout.source_type == "coach_daily_adaptation",
                    Workout.approval_status == "proposed",
                    Workout.accepted_revision_id.is_(None),
                    Workout.deleted_at.is_(None),
                )
            )
        )
        if len(open_replacements) > 1:
            raise DailyAdaptationError(
                "Für dieses Workout sind mehrere Ersatzvorschläge offen.",
                code="adaptation.replacement_ambiguous",
            )
        if open_replacements and (
            not allow_open_candidate or open_replacements[0].id != expected_replacement_id
        ):
            raise DailyAdaptationError(
                "Für dieses Workout ist bereits ein Ersatzvorschlag offen.",
                code="adaptation.candidate_already_open",
            )
        evaluated_at = datetime.combine(self.as_of, time.max)
        safety = build_safety_context(
            self.session,
            self.user.id,
            workout,
            revision,
            mode="acceptance",
            now=evaluated_at,
        )
        feedback_cutoff = datetime.combine(self.as_of, time.min)
        latest_feedback = self.session.scalar(
            select(PreSessionFeedback)
            .where(
                PreSessionFeedback.user_id == self.user.id,
                PreSessionFeedback.workout_id == workout.id,
                PreSessionFeedback.recorded_at >= feedback_cutoff,
                PreSessionFeedback.recorded_at <= evaluated_at,
            )
            .order_by(PreSessionFeedback.recorded_at.desc(), PreSessionFeedback.id.desc())
            .limit(1)
        )
        available_minutes = latest_feedback.available_minutes if latest_feedback else None
        athlete = AthleteDataService(self.session, self.user.id, as_of=self.as_of)
        baseline = athlete.get_running_baseline()
        recovery_fingerprint = _fingerprint(asdict(athlete.get_current_recovery_state()))
        week = self._week_context()
        context_fingerprint = _fingerprint(
            {
                "schema_version": "daily_adaptation_context.v1",
                "as_of": self.as_of,
                "workout_id": workout.id,
                "accepted_revision_id": revision.id,
                "accepted_revision_number": revision.revision_number,
                "accepted_content_hash": revision.content_hash,
                "baseline_fingerprint": baseline.input_fingerprint,
                "safety_fingerprint": safety.fingerprint,
                "recovery_fingerprint": recovery_fingerprint,
                "week_fingerprint": week.fingerprint,
                "available_minutes": available_minutes,
                "knowledge_base_version": get_knowledge_registry().version,
                "generator_version": ADAPTATION_GENERATOR_VERSION,
            }
        )
        assessment = generate_daily_adaptation_candidates(
            revision.definition_model,
            safety_report=safety.report,
            load_estimate=revision.load_estimate_json,
            available_minutes=available_minutes,
        )
        week_duration = sum(item.duration_seconds for item in week.workouts)
        week_distance = sum(item.distance_meters for item in week.workouts)
        week_impacts = tuple(
            DailyAdaptationWeekImpact(
                adaptation_class=candidate.adaptation_class,
                duration_before_seconds=week_duration,
                duration_after_seconds=(
                    week_duration
                    - assessment.original_load.dimensions.duration_seconds
                    + candidate.load.dimensions.duration_seconds
                ),
                distance_before_meters=week_distance,
                distance_after_meters=(
                    week_distance
                    - assessment.original_load.dimensions.distance_meters
                    + candidate.load.dimensions.distance_meters
                ),
            )
            for candidate in assessment.candidates
        )
        return DailyAdaptationPreview(
            as_of=self.as_of,
            workout_id=workout.id,
            accepted_revision_id=revision.id,
            context_fingerprint=context_fingerprint,
            baseline_fingerprint=baseline.input_fingerprint,
            safety_fingerprint=safety.fingerprint,
            recovery_fingerprint=recovery_fingerprint,
            week=week,
            week_impacts=week_impacts,
            available_minutes=available_minutes,
            assessment=assessment,
        )

    def apply(
        self,
        workout_id: int,
        adaptation_class: DailyAdaptationClass,
        *,
        expected_context_fingerprint: str,
        idempotency_key: str,
    ) -> DailyAdaptationApplyResult:
        if not idempotency_key.strip() or len(idempotency_key) > 200:
            raise DailyAdaptationError(
                "Der Wiederholungsschlüssel ist ungültig.",
                code="adaptation.idempotency_key_invalid",
            )
        replayed_workout = self._verify_replay_event(
            workout_id,
            adaptation_class,
            expected_context_fingerprint=expected_context_fingerprint,
            idempotency_key=idempotency_key,
        )
        if replayed_workout is not None:
            return DailyAdaptationApplyResult(replayed_workout, adaptation_class, False)
        try:
            preview = self.assess_today(workout_id)
        except DailyAdaptationError as exc:
            if exc.code not in {
                "adaptation.candidate_already_open",
                "adaptation.workout_not_eligible",
            }:
                raise
            replayed_workout = self._verify_replay_event(
                workout_id,
                adaptation_class,
                expected_context_fingerprint=expected_context_fingerprint,
                idempotency_key=idempotency_key,
            )
            if replayed_workout is None:
                raise
            return DailyAdaptationApplyResult(replayed_workout, adaptation_class, False)
        if preview.context_fingerprint != expected_context_fingerprint:
            raise DailyAdaptationError(
                "Der Kontext dieser Anpassung ist nicht mehr aktuell.",
                code="adaptation.context_stale",
            )
        candidate = next(
            (
                item
                for item in preview.assessment.candidates
                if item.adaptation_class == adaptation_class
            ),
            None,
        )
        if candidate is None:
            raise DailyAdaptationError(
                "Diese Anpassung ist im aktuellen Kontext nicht zulässig.",
                code="adaptation.candidate_not_allowed",
            )
        workout = find_workout(self.session, self.user.id, workout_id)
        if workout is None:
            raise DailyAdaptationError("Workout nicht gefunden.", code="adaptation.not_found")
        revision = self.session.get(WorkoutRevision, preview.accepted_revision_id)
        if revision is None or revision.workout_id != workout.id:
            raise DailyAdaptationError(
                "Die angenommene Workout-Revision ist nicht verfügbar.",
                code="adaptation.accepted_revision_missing",
            )
        identity = RevisionIdentity(
            revision_id=revision.id,
            revision_number=revision.revision_number,
            content_hash=revision.content_hash,
            lock_version=workout.lock_version,
        )
        service = WorkoutService(self.session, self.user, request_id=self.request_id)
        if adaptation_class == DailyAdaptationClass.KEEP:
            result = service.record_adaptation_keep(
                workout.id,
                context_fingerprint=preview.context_fingerprint,
                expected_identity=identity,
                idempotency_key=idempotency_key,
            )
            return DailyAdaptationApplyResult(result, adaptation_class, False)
        if adaptation_class == DailyAdaptationClass.REST:
            result = service.apply_adaptation_rest(
                workout.id,
                context_fingerprint=preview.context_fingerprint,
                expected_identity=identity,
                idempotency_key=idempotency_key,
            )
            return DailyAdaptationApplyResult(result, adaptation_class, False)
        if candidate.definition is None:
            raise DailyAdaptationError(
                "Dieser Kandidat besitzt keine ausführbare Workout-Definition.",
                code="adaptation.definition_missing",
            )
        impact = preview.week_impact(adaptation_class)
        data = WorkoutInput(
            name=_adapted_name(revision.name, adaptation_class),
            sport="running",
            scheduled_for=workout.scheduled_for,
            description=revision.description or "Tägliche Anpassung von PacePilot.",
            definition=candidate.definition,
            definition_version=2 if isinstance(candidate.definition, WorkoutDefinitionV2) else 1,
        )
        metadata = _adaptation_metadata(preview, candidate, workout, revision, impact)
        if adaptation_class == DailyAdaptationClass.REPLACE_WITH_EASY:
            result = service.propose_adaptation_replacement(
                workout.id,
                data,
                metadata,
                context_fingerprint=preview.context_fingerprint,
                expected_identity=identity,
                idempotency_key=idempotency_key,
            )
        else:
            result = service.propose_adaptation_revision(
                workout.id,
                data,
                metadata,
                adaptation_class=adaptation_class.value,
                context_fingerprint=preview.context_fingerprint,
                expected_identity=identity,
                idempotency_key=idempotency_key,
            )
        return DailyAdaptationApplyResult(result, adaptation_class, True)

    def _verify_replay_event(
        self,
        workout_id: int,
        adaptation_class: DailyAdaptationClass,
        *,
        expected_context_fingerprint: str,
        idempotency_key: str,
    ) -> Workout | None:
        action_by_class = {
            DailyAdaptationClass.KEEP: "adapt_keep",
            DailyAdaptationClass.REST: "adapt_rest",
            DailyAdaptationClass.REDUCE_VOLUME: "adapt_propose",
            DailyAdaptationClass.REPLACE_WITH_EASY: "adapt_replace_propose",
        }
        event = self.session.scalar(
            select(WorkoutEvent).where(
                WorkoutEvent.owner_user_id == self.user.id,
                WorkoutEvent.action == action_by_class[adaptation_class],
                WorkoutEvent.idempotency_key == idempotency_key,
            )
        )
        if (
            event is None
            or event.workout_id != workout_id
            or event.safe_metadata_json.get("context_fingerprint") != expected_context_fingerprint
            or event.safe_metadata_json.get("adaptation_class") != adaptation_class.value
        ):
            if event is not None:
                raise DailyAdaptationError(
                    "Dieser Wiederholungsschlüssel gehört zu einer anderen Anpassung.",
                    code="adaptation.idempotency_conflict",
                )
            return None
        result_workout_id = workout_id
        if adaptation_class == DailyAdaptationClass.REPLACE_WITH_EASY:
            replacement_id = event.safe_metadata_json.get("replacement_workout_id")
            if not isinstance(replacement_id, int):
                raise DailyAdaptationError(
                    "Dem Ersatz-Audit fehlt das neue Workout.",
                    code="adaptation.replay_invalid",
                )
            result_workout_id = replacement_id
        workout = find_workout(self.session, self.user.id, result_workout_id)
        if workout is None:
            raise DailyAdaptationError("Workout nicht gefunden.", code="adaptation.not_found")
        return workout

    def _eligible_revision(
        self, workout: Workout, *, allow_open_candidate: bool = False
    ) -> WorkoutRevision:
        if (
            workout.sport != "running"
            or workout.accepted_revision_id is None
            or workout.local_schedule_status != "scheduled"
            or workout.scheduled_for != self.as_of
        ):
            raise DailyAdaptationError(
                "Nur ein heute angenommenes und eingeplantes Lauftraining kann angepasst werden.",
                code="adaptation.workout_not_eligible",
            )
        if workout.current_revision_id != workout.accepted_revision_id and not allow_open_candidate:
            raise DailyAdaptationError(
                "Für dieses Workout ist bereits eine neue Revision offen.",
                code="adaptation.candidate_already_open",
            )
        revision = self.session.get(WorkoutRevision, workout.accepted_revision_id)
        if revision is None or revision.workout_id != workout.id:
            raise DailyAdaptationError(
                "Die angenommene Workout-Revision ist nicht verfügbar.",
                code="adaptation.accepted_revision_missing",
            )
        if allow_open_candidate and workout.current_revision_id != revision.id:
            current = self.session.get(WorkoutRevision, workout.current_revision_id)
            if (
                current is None
                or current.workout_id != workout.id
                or current.source_type != "coach_daily_adaptation"
                or current.parent_revision_id != revision.id
            ):
                raise DailyAdaptationError(
                    "Die offene Revision ist keine gültige tägliche Anpassung.",
                    code="adaptation.candidate_invalid",
                )
        return revision

    def _week_context(self) -> PlannedWeekContext:
        starts_on = self.as_of - timedelta(days=self.as_of.weekday())
        ends_on = starts_on + timedelta(days=6)
        rows = self.session.execute(
            select(Workout, WorkoutRevision)
            .join(WorkoutRevision, WorkoutRevision.id == Workout.accepted_revision_id)
            .where(
                Workout.user_id == self.user.id,
                Workout.deleted_at.is_(None),
                Workout.local_schedule_status == "scheduled",
                Workout.scheduled_for >= starts_on,
                Workout.scheduled_for <= ends_on,
            )
            .order_by(Workout.scheduled_for, Workout.id)
        )
        entries_list: list[PlannedWeekWorkout] = []
        for workout, revision in rows:
            if workout.scheduled_for is None:
                continue
            load = adaptation_load(
                revision.definition_model,
                load_estimate=revision.load_estimate_json,
            )
            entries_list.append(
                PlannedWeekWorkout(
                    workout_id=workout.id,
                    scheduled_for=workout.scheduled_for,
                    revision_id=revision.id,
                    content_hash=revision.content_hash,
                    duration_seconds=load.dimensions.duration_seconds,
                    distance_meters=load.dimensions.distance_meters,
                )
            )
        entries = tuple(entries_list)
        fingerprint = _fingerprint(
            {
                "starts_on": starts_on,
                "ends_on": ends_on,
                "workouts": [asdict(entry) for entry in entries],
            }
        )
        return PlannedWeekContext(starts_on, ends_on, entries, fingerprint)


def generate_daily_adaptation_candidates(
    definition: WorkoutDefinitionModel,
    *,
    safety_report: SafetyReport,
    load_estimate: LoadEstimate | dict[str, object] | None = None,
    available_minutes: int | None = None,
    registry: KnowledgeRegistry | None = None,
) -> DailyAdaptationAssessment:
    """Generate the initial Phase 10 classes without model-authored workout content."""
    knowledge = registry or get_knowledge_registry()
    original = adaptation_load(definition, load_estimate=load_estimate)
    issue_codes = tuple(sorted(issue.code for issue in safety_report.issues))

    if safety_report.outcome == TriageOutcome.CLARIFY:
        return DailyAdaptationAssessment(
            safety_outcome=safety_report.outcome,
            original_load=original,
            candidates=(),
            blocked_reason_codes=issue_codes or ("adaptation.safety_clarification_required",),
        )

    rest = DailyAdaptationCandidate(
        adaptation_class=DailyAdaptationClass.REST,
        definition=None,
        load=AdaptationLoad(LoadDimensions(0, 0, 0, 0), True),
        recommended=(safety_report.outcome == TriageOutcome.SAFETY_STOP or available_minutes == 0),
        reason_codes=(
            issue_codes
            if safety_report.outcome == TriageOutcome.SAFETY_STOP
            else ("constraint.no_time_available",)
            if available_minutes == 0
            else ("adaptation.rest_available",)
        ),
    )
    if safety_report.outcome == TriageOutcome.SAFETY_STOP or available_minutes == 0:
        return DailyAdaptationAssessment(
            safety_outcome=safety_report.outcome,
            original_load=original,
            candidates=(rest,),
        )

    strained = safety_report.outcome == TriageOutcome.WARN
    time_limited = _time_budget_requires_reduction(original, available_minutes)
    keep = DailyAdaptationCandidate(
        adaptation_class=DailyAdaptationClass.KEEP,
        definition=definition.model_copy(deep=True),
        load=original,
        recommended=not strained and not time_limited,
        reason_codes=("adaptation.keep_current",),
    )
    reduced_definition = reduce_volume(
        definition,
        available_minutes=available_minutes,
        estimated_duration_seconds=original.dimensions.duration_seconds,
        registry=knowledge,
    )
    easy = _easy_replacement(
        original,
        available_minutes=available_minutes,
        safety_report=safety_report,
        registry=knowledge,
    )
    requires_proven_low_intensity = bool(
        {"safety.mild_illness", "safety.pain_warning"} & set(issue_codes)
    )
    warning_requires_rest = strained and (
        not original.intensity_comparable
        or original.dimensions.intensity_score > 1
        and easy is None
    )
    reduced = DailyAdaptationCandidate(
        adaptation_class=DailyAdaptationClass.REDUCE_VOLUME,
        definition=reduced_definition,
        load=_same_intensity_load(reduced_definition, original, definition),
        recommended=(
            not warning_requires_rest
            and (
                not strained
                and time_limited
                or strained
                and original.intensity_comparable
                and original.dimensions.intensity_score <= 1
            )
        ),
        reason_codes=(
            issue_codes
            if strained
            else ("constraint.available_time_reduction",)
            if time_limited
            else ("adaptation.reduce_volume_available",)
        ),
    )
    candidates = [reduced] if time_limited else [keep, reduced]
    if requires_proven_low_intensity and (
        not original.intensity_comparable or original.dimensions.intensity_score > 1
    ):
        candidates = []

    if easy is not None:
        candidates.append(
            DailyAdaptationCandidate(
                adaptation_class=DailyAdaptationClass.REPLACE_WITH_EASY,
                definition=easy[0],
                load=easy[1],
                recommended=strained and original.dimensions.intensity_score > 1,
                reason_codes=(
                    issue_codes if strained else ("adaptation.easy_replacement_available",)
                ),
            )
        )
    if warning_requires_rest:
        rest = DailyAdaptationCandidate(
            adaptation_class=rest.adaptation_class,
            definition=rest.definition,
            load=rest.load,
            recommended=True,
            reason_codes=issue_codes or ("adaptation.warn_conservative_rest",),
        )
    candidates.append(rest)

    if not any(candidate.recommended for candidate in candidates):
        if time_limited:
            raise RuntimeError("Time-limited adaptation generated no recommendation")
        keep = DailyAdaptationCandidate(
            adaptation_class=keep.adaptation_class,
            definition=keep.definition,
            load=keep.load,
            recommended=True,
            reason_codes=keep.reason_codes,
        )
        candidates[0] = keep

    engine = ConstraintEngine(knowledge)
    for candidate in candidates:
        if not engine.adaptation_allows(original.dimensions, candidate.load.dimensions):
            raise RuntimeError("Daily adaptation generated an escalating candidate")
    return DailyAdaptationAssessment(
        safety_outcome=safety_report.outcome,
        original_load=original,
        candidates=tuple(candidates),
    )


def adaptation_load(
    definition: WorkoutDefinitionModel,
    *,
    load_estimate: LoadEstimate | dict[str, object] | None = None,
) -> AdaptationLoad:
    metrics = workout_metrics(definition)
    estimate = _parse_load_estimate(load_estimate)
    if estimate is not None:
        domains = estimate.time_by_intensity_domain_seconds
        intensity = 3.0 if domains.high else 2.0 if domains.moderate else 1.0
        density = domains.high / estimate.duration_seconds
        return AdaptationLoad(
            LoadDimensions(
                estimate.duration_seconds,
                (
                    estimate.distance_meters
                    if estimate.distance_meters is not None
                    else metrics.distance_meters
                ),
                intensity,
                density,
            ),
            True,
        )

    targets = list(_expanded_targets(definition.blocks))
    target_scores = [_target_intensity(target) for target in targets]
    comparable = bool(target_scores) and all(score is not None for score in target_scores)
    known_scores = [score for score in target_scores if score is not None]
    intensity = max(known_scores, default=0.0)
    high_steps = sum(score == 3.0 for score in known_scores)
    density = high_steps / len(known_scores) if known_scores else 0.0
    return AdaptationLoad(
        LoadDimensions(
            metrics.duration_seconds,
            metrics.distance_meters,
            intensity,
            density,
        ),
        comparable,
    )


def reduce_volume(
    definition: WorkoutDefinitionModel,
    *,
    available_minutes: int | None = None,
    estimated_duration_seconds: float | None = None,
    registry: KnowledgeRegistry | None = None,
) -> WorkoutDefinitionModel:
    knowledge = registry or get_knowledge_registry()
    rule = knowledge.constraints["ADAPT-VOLUME-REDUCTION-001"]
    if rule.status != "active" or rule.implementation != "adaptation.reduce_volume":
        raise RuntimeError("The daily volume-reduction rule is not active")
    factor = rule.parameters.get("factor")
    if not isinstance(factor, float) or isinstance(factor, bool) or not 0 < factor < 1:
        raise RuntimeError("ADAPT-VOLUME-REDUCTION-001.factor must be between zero and one")
    duration_seconds = estimated_duration_seconds or workout_metrics(definition).duration_seconds
    if (
        available_minutes is not None
        and available_minutes > 0
        and duration_seconds > available_minutes * 60
    ):
        factor = min(factor, available_minutes * 60 / duration_seconds)

    candidate = definition.model_copy(deep=True)
    for step in _steps(candidate.blocks):
        if isinstance(step.end, TimeEnd):
            step.end.seconds = _scaled_value(step.end.seconds, factor)
        elif isinstance(step.end, DistanceEnd):
            step.end.meters = _scaled_value(step.end.meters, factor)
    return candidate


def _easy_replacement(
    original: AdaptationLoad,
    *,
    available_minutes: int | None,
    safety_report: SafetyReport,
    registry: KnowledgeRegistry,
) -> tuple[WorkoutDefinitionModel, AdaptationLoad] | None:
    original_minutes = int(original.dimensions.duration_seconds // 60)
    if not original.intensity_comparable or original_minutes < 20:
        return None
    selected_minutes = min(original_minutes, available_minutes or original_minutes, 45)
    if selected_minutes < 20:
        return None
    expanded = expand_workout_template(
        "easy_run",
        TemplateParameters(duration_minutes=selected_minutes),
        eligibility=TemplateEligibilityContext(
            consistent_running_weeks=0,
            runs_per_week=0,
            available_minutes=selected_minutes,
            safety_stop=safety_report.outcome == TriageOutcome.SAFETY_STOP,
        ),
        registry=registry,
    )
    candidate_load = adaptation_load(expanded.definition, load_estimate=expanded.load_estimate)
    if not adaptation_does_not_increase_load(original.dimensions, candidate_load.dimensions):
        return None
    return expanded.definition, candidate_load


def _same_intensity_load(
    definition: WorkoutDefinitionModel,
    original: AdaptationLoad,
    original_definition: WorkoutDefinitionModel,
) -> AdaptationLoad:
    original_metrics = workout_metrics(original_definition)
    candidate_metrics = workout_metrics(definition)
    if original_metrics.duration_seconds > 0:
        factor = candidate_metrics.duration_seconds / original_metrics.duration_seconds
    elif original_metrics.distance_meters > 0:
        factor = candidate_metrics.distance_meters / original_metrics.distance_meters
    else:
        factor = 0
    return AdaptationLoad(
        LoadDimensions(
            original.dimensions.duration_seconds * factor,
            original.dimensions.distance_meters * factor,
            original.dimensions.intensity_score,
            original.dimensions.density_score,
        ),
        original.intensity_comparable,
    )


def _time_budget_requires_reduction(
    original: AdaptationLoad, available_minutes: int | None
) -> bool:
    return (
        available_minutes is not None
        and original.dimensions.duration_seconds > available_minutes * 60
    )


def _parse_load_estimate(
    value: LoadEstimate | dict[str, object] | None,
) -> LoadEstimate | None:
    if value is None:
        return None
    return value if isinstance(value, LoadEstimate) else LoadEstimate.model_validate(value)


def _steps(blocks: Sequence[object]):
    for block in blocks:
        if isinstance(block, (RepeatBlock, RepeatBlockV2)):
            yield from _steps(block.children)
        elif isinstance(block, (StepBlock, StepBlockV2)):
            yield block


def _expanded_targets(blocks: Sequence[object]):
    for block in blocks:
        if isinstance(block, (RepeatBlock, RepeatBlockV2)):
            for _ in range(block.iterations):
                yield from _expanded_targets(block.children)
        elif isinstance(block, (StepBlock, StepBlockV2)):
            yield block.target


def _target_intensity(target: object) -> float | None:
    if isinstance(target, RpeRangeTarget):
        return 1.0 if target.upper_rpe <= 3 else 2.0 if target.upper_rpe <= 6 else 3.0
    if isinstance(target, HeartRateZoneTarget):
        return 1.0 if target.zone <= 2 else 2.0 if target.zone == 3 else 3.0
    if isinstance(target, (PaceRangeTarget, HeartRateRangeTarget, NoTarget)):
        return None
    return None


def _scaled_value(value: float, factor: float) -> float:
    return max(round(value * factor, 6), 0.000001)


def _fingerprint(value: object) -> str:
    payload = json.dumps(
        value,
        default=lambda item: item.isoformat() if isinstance(item, date) else str(item),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _adapted_name(name: str, adaptation_class: DailyAdaptationClass) -> str:
    suffix = "reduziert" if adaptation_class == DailyAdaptationClass.REDUCE_VOLUME else "Easy Run"
    base = name.split(" · ", 1)[0]
    return f"{base} · {suffix}"[:200]


def _adaptation_metadata(
    preview: DailyAdaptationPreview,
    candidate: DailyAdaptationCandidate,
    original_workout: Workout,
    original: WorkoutRevision,
    impact: DailyAdaptationWeekImpact,
) -> RevisionMetadata:
    dimensions = candidate.load.dimensions
    before = preview.assessment.original_load.dimensions
    guidance: dict[str, object] = {
        "rationale": candidate.rationale,
        "adaptation_class": candidate.adaptation_class.value,
        "reason_codes": list(candidate.reason_codes),
        "before_after": {
            "before": _dimensions_json(before),
            "after": _dimensions_json(dimensions),
        },
        "week_impact": _week_impact_json(impact),
    }
    if candidate.adaptation_class == DailyAdaptationClass.REPLACE_WITH_EASY:
        guidance["instructions"] = [
            "Halte die Anstrengung bei RPE 2–3 von 10.",
            "Laufe so locker, dass du in vollständigen Sätzen sprechen kannst.",
        ]
    elif original.guidance_json:
        guidance["original_guidance"] = original.guidance_json
        if isinstance(original.guidance_json.get("device_target"), dict):
            guidance["device_target"] = original.guidance_json["device_target"]
    load_estimate = _candidate_load_estimate(candidate, original)
    return RevisionMetadata(
        purpose=(
            "aerobic_base"
            if candidate.adaptation_class == DailyAdaptationClass.REPLACE_WITH_EASY
            else original.purpose
        ),
        guidance_json=guidance,
        load_estimate_json=load_estimate,
        validation_report_json={
            "valid": True,
            "issues": [],
            "rule_set_version": ADAPTATION_RULE_SET_VERSION,
            "checks": [
                {"code": "structure.valid", "result": "pass"},
                {"code": "adaptation.no_load_increase", "result": "pass"},
                {"code": "adaptation.safety_context", "result": "pass"},
                {"code": "adaptation.week_context", "result": "pass"},
            ],
        },
        generation_context_json={
            "schema_version": "daily_adaptation_context.v1",
            "adaptation_class": candidate.adaptation_class.value,
            "as_of": preview.as_of.isoformat(),
            "adaptation_context_fingerprint": preview.context_fingerprint,
            "baseline_fingerprint": preview.baseline_fingerprint,
            "safety_fingerprint": preview.safety_fingerprint,
            "recovery_fingerprint": preview.recovery_fingerprint,
            "week_fingerprint": preview.week.fingerprint,
            "original_revision": {
                "id": original.id,
                "number": original.revision_number,
                "content_hash": original.content_hash,
            },
            "original_workout": {
                "id": original_workout.id,
                "scheduled_for": (
                    original_workout.scheduled_for.isoformat()
                    if original_workout.scheduled_for is not None
                    else None
                ),
                "proposal_lock_version": original_workout.lock_version,
                "acceptance_lock_version": original_workout.lock_version + 1,
            },
            "reason_codes": list(candidate.reason_codes),
            "before_after": {
                "before": _dimensions_json(before),
                "after": _dimensions_json(dimensions),
            },
            "week_impact": _week_impact_json(impact),
        },
        source_type="coach_daily_adaptation",
        generator_version=ADAPTATION_GENERATOR_VERSION,
        template_id=(
            "easy_run"
            if candidate.adaptation_class == DailyAdaptationClass.REPLACE_WITH_EASY
            else original.template_id
        ),
        template_version=(
            get_knowledge_registry().workouts["easy_run"].version
            if candidate.adaptation_class == DailyAdaptationClass.REPLACE_WITH_EASY
            else original.template_version
        ),
        rule_set_version=ADAPTATION_RULE_SET_VERSION,
        knowledge_base_version=get_knowledge_registry().version,
        edit_source="generator",
    )


def _candidate_load_estimate(
    candidate: DailyAdaptationCandidate, original: WorkoutRevision
) -> dict[str, object]:
    dimensions = candidate.load.dimensions
    duration = max(1, round(dimensions.duration_seconds))
    high = min(duration, round(duration * dimensions.density_score))
    remaining = duration - high
    if dimensions.intensity_score <= 1:
        low, moderate = remaining, 0
    elif dimensions.intensity_score <= 2:
        low, moderate = 0, remaining
    else:
        low, moderate = remaining, 0
    original_estimate = original.load_estimate_json or {}
    session_rpe = (
        {"minimum": 2, "maximum": 3}
        if candidate.adaptation_class == DailyAdaptationClass.REPLACE_WITH_EASY
        else original_estimate.get("session_rpe")
    )
    mechanical = original_estimate.get("mechanical_load", "low")
    if mechanical not in {"low", "moderate", "high"}:
        mechanical = "low"
    return {
        "duration_seconds": duration,
        "distance_meters": dimensions.distance_meters or None,
        "time_by_intensity_domain_seconds": {
            "low": low,
            "moderate": moderate,
            "high": high,
        },
        "mechanical_load": mechanical,
        "session_rpe": session_rpe,
        "confidence": "low",
        "uncertainty": [
            "daily_adaptation_load_estimate",
            "individual_response_requires_feedback",
        ],
    }


def _dimensions_json(dimensions: LoadDimensions) -> dict[str, float]:
    return {
        "duration_seconds": dimensions.duration_seconds,
        "distance_meters": dimensions.distance_meters,
        "intensity_score": dimensions.intensity_score,
        "density_score": dimensions.density_score,
    }


def _week_impact_json(impact: DailyAdaptationWeekImpact) -> dict[str, object]:
    return {
        "duration_before_seconds": impact.duration_before_seconds,
        "duration_after_seconds": impact.duration_after_seconds,
        "duration_delta_minutes": impact.duration_delta_minutes,
        "distance_before_meters": impact.distance_before_meters,
        "distance_after_meters": impact.distance_after_meters,
        "distance_delta_kilometers": impact.distance_delta_kilometers,
        "moves_workout": False,
        "creates_hard_day_conflict": False,
        "creates_long_run_conflict": False,
    }

import re
from collections.abc import Sequence
from datetime import date, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import select

from app.config import get_settings
from app.models import (
    GarminAccount,
    PreSessionFeedback,
    User,
    Workout,
    WorkoutEvent,
    WorkoutGarminBinding,
    WorkoutGarminRemoteIdentity,
    WorkoutRevision,
)
from app.models.user import utcnow
from app.services.planning.constraints import adaptation_does_not_increase_load
from app.services.planning.daily_adaptation import (
    DailyAdaptationClass,
    DailyAdaptationError,
    DailyAdaptationService,
    generate_daily_adaptation_candidates,
    reduce_volume,
)
from app.services.planning.feedback_service import FeedbackCommands
from app.services.planning.load_estimate import IntensityDomainTime, LoadEstimate
from app.services.planning.safety_triage import (
    PreSessionFeedbackInput,
    SafetyIssue,
    SafetyReport,
    TriageOutcome,
)
from app.services.planning.training_fit import TrainingFitAssessment, TrainingFitOutcome
from app.services.planning.validator import WorkoutInput
from app.services.planning.workout_definition import (
    DistanceEnd,
    NoTarget,
    RepeatBlock,
    RepeatBlockV2,
    RpeRangeTarget,
    StepBlock,
    StepBlockV2,
    TimeEnd,
    WorkoutDefinition,
    WorkoutDefinitionV2,
    default_definition,
    workout_metrics,
)
from app.services.planning.workout_revision import (
    AcceptRevisionCommand,
    RejectRevisionCommand,
    RevisionIdentity,
    ScheduleWorkoutCommand,
)
from app.services.planning.workout_service import WorkoutService, WorkoutTransitionError
from app.services.planning.workout_templates import (
    TemplateEligibilityContext,
    TemplateParameters,
    expand_workout_template,
)


def _report(outcome: TriageOutcome, *codes: str) -> SafetyReport:
    return SafetyReport(
        outcome,
        tuple(SafetyIssue(code, outcome, code, "TEST-RULE", ("pre:1",)) for code in codes),
    )


def _easy(minutes: int = 45):
    return expand_workout_template(
        "easy_run",
        TemplateParameters(duration_minutes=minutes),
        eligibility=TemplateEligibilityContext(
            consistent_running_weeks=0,
            runs_per_week=0,
            available_minutes=minutes,
        ),
    )


def _mixed_definition(time_seconds: float = 1200, distance_meters: float = 4000):
    target = RpeRangeTarget(type="rpe_range", lower_rpe=5, upper_rpe=6)
    return WorkoutDefinitionV2(
        blocks=[
            StepBlockV2(
                id="warmup",
                kind="step",
                step_type="warmup",
                end=TimeEnd(type="time", seconds=time_seconds),
                target=target,
            ),
            RepeatBlockV2(
                id="repeat",
                kind="repeat",
                iterations=3,
                children=[
                    StepBlockV2(
                        id="work",
                        kind="step",
                        step_type="interval",
                        end=DistanceEnd(type="distance", meters=distance_meters),
                        target=target,
                    ),
                    StepBlockV2(
                        id="recovery",
                        kind="step",
                        step_type="recovery",
                        end=TimeEnd(type="time", seconds=120),
                        target=NoTarget(type="none"),
                    ),
                ],
            ),
        ]
    )


def _steps(blocks: Sequence[object]):
    for block in blocks:
        if isinstance(block, (RepeatBlock, RepeatBlockV2)):
            yield from _steps(block.children)
        elif isinstance(block, (StepBlock, StepBlockV2)):
            yield block


def test_identical_inputs_generate_identical_candidates() -> None:
    expanded = _easy()

    first = generate_daily_adaptation_candidates(
        expanded.definition,
        safety_report=_report(TriageOutcome.ALLOW),
        load_estimate=expanded.load_estimate,
        available_minutes=45,
    )
    second = generate_daily_adaptation_candidates(
        expanded.definition,
        safety_report=_report(TriageOutcome.ALLOW),
        load_estimate=expanded.load_estimate,
        available_minutes=45,
    )

    assert first == second
    assert [candidate.adaptation_class for candidate in first.candidates] == [
        DailyAdaptationClass.KEEP,
        DailyAdaptationClass.REDUCE_VOLUME,
        DailyAdaptationClass.REPLACE_WITH_EASY,
        DailyAdaptationClass.REST,
    ]
    assert [
        candidate.adaptation_class for candidate in first.candidates if candidate.recommended
    ] == [DailyAdaptationClass.KEEP]


@given(
    time_seconds=st.floats(min_value=1, max_value=100_000, allow_nan=False),
    distance_meters=st.floats(min_value=1, max_value=1_000_000, allow_nan=False),
)
def test_generated_candidates_never_increase_load(
    time_seconds: float, distance_meters: float
) -> None:
    assessment = generate_daily_adaptation_candidates(
        _mixed_definition(time_seconds, distance_meters),
        safety_report=_report(TriageOutcome.ALLOW),
    )

    assert all(
        adaptation_does_not_increase_load(
            assessment.original_load.dimensions, candidate.load.dimensions
        )
        for candidate in assessment.candidates
    )
    for candidate in assessment.candidates:
        if candidate.definition is not None:
            assert all(
                (step.end.seconds if isinstance(step.end, TimeEnd) else step.end.meters) > 0
                for step in _steps(candidate.definition.blocks)
            )


def test_safety_stop_only_allows_rest() -> None:
    expanded = _easy()
    assessment = generate_daily_adaptation_candidates(
        expanded.definition,
        safety_report=_report(TriageOutcome.SAFETY_STOP, "safety.pain_alters_gait"),
        load_estimate=expanded.load_estimate,
    )

    assert len(assessment.candidates) == 1
    assert assessment.candidates[0].adaptation_class == DailyAdaptationClass.REST
    assert assessment.candidates[0].recommended
    assert assessment.candidates[0].definition is None


def test_clarification_produces_no_executable_candidate() -> None:
    expanded = _easy()
    assessment = generate_daily_adaptation_candidates(
        expanded.definition,
        safety_report=_report(TriageOutcome.CLARIFY, "safety.pain_unclear"),
        load_estimate=expanded.load_estimate,
    )

    assert assessment.candidates == ()
    assert assessment.blocked_reason_codes == ("safety.pain_unclear",)


def test_reduce_volume_scales_time_distance_and_repeats_without_changing_targets() -> None:
    original = _mixed_definition()
    reduced = reduce_volume(original)
    original_steps = list(_steps(original.blocks))
    reduced_steps = list(_steps(reduced.blocks))

    assert [step.id for step in reduced_steps] == [step.id for step in original_steps]
    assert [step.target for step in reduced_steps] == [step.target for step in original_steps]
    assert isinstance(reduced.blocks[1], RepeatBlockV2)
    assert reduced.blocks[1].iterations == 3
    assert (
        workout_metrics(reduced).duration_seconds
        == workout_metrics(original).duration_seconds * 0.75
    )
    assert (
        workout_metrics(reduced).distance_meters == workout_metrics(original).distance_meters * 0.75
    )
    reduced_v1 = reduce_volume(default_definition())
    assert isinstance(reduced_v1, WorkoutDefinition)
    assert workout_metrics(reduced_v1).duration_seconds == 1800


def test_easy_replacement_requires_comparable_load_and_keeps_low_intensity() -> None:
    moderate = WorkoutDefinitionV2(
        blocks=[
            StepBlockV2(
                id="moderate",
                kind="step",
                step_type="interval",
                end=TimeEnd(type="time", seconds=3600),
                target=RpeRangeTarget(type="rpe_range", lower_rpe=4, upper_rpe=5),
            )
        ]
    )
    warned = generate_daily_adaptation_candidates(
        moderate,
        safety_report=_report(TriageOutcome.WARN, "readiness.subjective_strain"),
        available_minutes=40,
    )
    replacement = next(
        candidate
        for candidate in warned.candidates
        if candidate.adaptation_class == DailyAdaptationClass.REPLACE_WITH_EASY
    )

    assert replacement.recommended
    assert replacement.definition is not None
    assert workout_metrics(replacement.definition).duration_seconds == 40 * 60
    step = replacement.definition.blocks[0]
    assert isinstance(step, StepBlockV2)
    assert isinstance(step.target, RpeRangeTarget)
    assert (step.target.lower_rpe, step.target.upper_rpe) == (2, 3)
    assert any("vollständigen Sätzen" in text for text in step.instructions)

    unknown = WorkoutDefinitionV2(
        blocks=[
            StepBlockV2(
                id="unknown",
                kind="step",
                step_type="interval",
                end=TimeEnd(type="time", seconds=3600),
                target=NoTarget(type="none"),
            )
        ]
    )
    unknown_assessment = generate_daily_adaptation_candidates(
        unknown,
        safety_report=_report(TriageOutcome.WARN, "readiness.subjective_strain"),
    )
    assert DailyAdaptationClass.REPLACE_WITH_EASY not in {
        candidate.adaptation_class for candidate in unknown_assessment.candidates
    }


def test_zero_available_time_only_allows_rest() -> None:
    expanded = _easy()
    assessment = generate_daily_adaptation_candidates(
        expanded.definition,
        safety_report=_report(TriageOutcome.ALLOW),
        load_estimate=expanded.load_estimate,
        available_minutes=0,
    )

    assert [candidate.adaptation_class for candidate in assessment.candidates] == [
        DailyAdaptationClass.REST
    ]


def test_available_time_removes_keep_and_caps_reduced_duration() -> None:
    expanded = _easy(60)
    assessment = generate_daily_adaptation_candidates(
        expanded.definition,
        safety_report=_report(TriageOutcome.ALLOW),
        load_estimate=expanded.load_estimate,
        available_minutes=30,
    )

    assert DailyAdaptationClass.KEEP not in {
        candidate.adaptation_class for candidate in assessment.candidates
    }
    reduced = next(
        candidate
        for candidate in assessment.candidates
        if candidate.adaptation_class == DailyAdaptationClass.REDUCE_VOLUME
    )
    assert reduced.recommended
    assert reduced.load.dimensions.duration_seconds == 30 * 60


def test_distance_workout_uses_estimated_duration_for_budget_and_comparison() -> None:
    definition = WorkoutDefinitionV2(
        blocks=[
            StepBlockV2(
                id="distance",
                kind="step",
                step_type="interval",
                end=DistanceEnd(type="distance", meters=10_000),
                target=RpeRangeTarget(type="rpe_range", lower_rpe=4, upper_rpe=5),
            )
        ]
    )
    estimate = LoadEstimate(
        duration_seconds=3600,
        distance_meters=10_000,
        time_by_intensity_domain_seconds=IntensityDomainTime(low=0, moderate=3600, high=0),
        mechanical_load="moderate",
        session_rpe=None,
        confidence="moderate",
        uncertainty=["estimated_duration"],
    )

    assessment = generate_daily_adaptation_candidates(
        definition,
        safety_report=_report(TriageOutcome.ALLOW),
        load_estimate=estimate,
        available_minutes=30,
    )
    reduced = next(
        candidate
        for candidate in assessment.candidates
        if candidate.adaptation_class == DailyAdaptationClass.REDUCE_VOLUME
    )

    assert assessment.original_load.dimensions.duration_seconds == 3600
    assert DailyAdaptationClass.KEEP not in {
        candidate.adaptation_class for candidate in assessment.candidates
    }
    assert reduced.load.dimensions.duration_seconds == 1800
    assert reduced.load.dimensions.distance_meters == 5000


@pytest.mark.parametrize(
    "definition",
    [
        WorkoutDefinitionV2(
            blocks=[
                StepBlockV2(
                    id="short-hard",
                    kind="step",
                    step_type="interval",
                    end=TimeEnd(type="time", seconds=600),
                    target=RpeRangeTarget(type="rpe_range", lower_rpe=8, upper_rpe=9),
                )
            ]
        ),
        WorkoutDefinitionV2(
            blocks=[
                StepBlockV2(
                    id="unknown",
                    kind="step",
                    step_type="interval",
                    end=TimeEnd(type="time", seconds=3600),
                    target=NoTarget(type="none"),
                )
            ]
        ),
    ],
)
def test_warn_without_provably_easy_replacement_recommends_rest(
    definition: WorkoutDefinitionV2,
) -> None:
    assessment = generate_daily_adaptation_candidates(
        definition,
        safety_report=_report(TriageOutcome.WARN, "safety.mild_illness"),
    )

    assert [candidate.adaptation_class for candidate in assessment.candidates] == [
        DailyAdaptationClass.REST
    ]
    assert assessment.candidates[0].recommended


def _accepted_workout(session, user: User, day: date) -> Workout:
    service = WorkoutService(session, user)
    expanded = _easy()
    workout = service.create(
        WorkoutInput(
            name="Heutiger Lauf",
            sport="running",
            scheduled_for=day,
            description="",
            definition=expanded.definition,
            definition_version=2,
        )
    )
    revision = session.get(WorkoutRevision, workout.current_revision_id)
    assert revision is not None
    service.accept(
        workout.id,
        AcceptRevisionCommand(
            identity=RevisionIdentity(
                revision_id=revision.id,
                revision_number=revision.revision_number,
                content_hash=revision.content_hash,
                lock_version=workout.lock_version,
            ),
            context_fingerprint=service.acceptance_context(workout.id).fingerprint,
        ),
    )
    service.schedule(
        workout.id,
        ScheduleWorkoutCommand(
            revision_id=revision.id,
            scheduled_for=day,
            expected_lock_version=workout.lock_version,
        ),
    )
    return workout


def test_elevated_keep_requires_acknowledgement_and_records_authorization(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "coach_daily_adaptation_enabled", True)
    today = date.today()
    with session_factory() as session:
        user = User(display_name="Elevated Keep Runner")
        session.add(user)
        session.flush()
        workout = _accepted_workout(session, user, today)
        preview = DailyAdaptationService(session, user, as_of=today).assess_today(workout.id)

        def elevated_assessment(*_args, **_kwargs) -> TrainingFitAssessment:
            return TrainingFitAssessment(
                outcome=TrainingFitOutcome.ELEVATED,
                policy_version="training-fit-test",
                evaluated_at=utcnow(),
                effective_workout_date=today,
                warning_codes=("safety.test",),
                evidence=(),
                coverage=(),
                feedback_ids=(),
                authoritative_input_fingerprint="f" * 64,
            )

        monkeypatch.setattr(
            "app.services.planning.workout_service.assess_training_fit",
            elevated_assessment,
        )
        adaptation = DailyAdaptationService(session, user, as_of=today)
        with pytest.raises(WorkoutTransitionError) as required:
            adaptation.apply(
                workout.id,
                DailyAdaptationClass.KEEP,
                expected_context_fingerprint=preview.context_fingerprint,
                idempotency_key="elevated-keep",
            )
        assert required.value.code == "workout.training_fit_acknowledgement_required"

        result = adaptation.apply(
            workout.id,
            DailyAdaptationClass.KEEP,
            expected_context_fingerprint=preview.context_fingerprint,
            idempotency_key="elevated-keep",
            acknowledge_elevated_warning=True,
        )

        assert result.workout.id == workout.id
        event = session.scalar(
            select(WorkoutEvent).where(
                WorkoutEvent.workout_id == workout.id,
                WorkoutEvent.action == "adapt_keep",
            )
        )
        assert event is not None
        authorization = event.safe_metadata_json["training_fit_authorization"]
        assert authorization["authorized_revision_id"] == workout.accepted_revision_id
        assert authorization["effective_date"] == today.isoformat()


def test_only_owned_accepted_scheduled_running_workout_today_is_eligible(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "coach_daily_adaptation_enabled", True)
    today = date.today()
    with session_factory() as session:
        user = User(display_name="Daily Runner")
        other = User(display_name="Other Runner")
        session.add_all([user, other])
        session.flush()
        eligible = _accepted_workout(session, user, today)
        future = _accepted_workout(session, user, today + timedelta(days=7))

        preview = DailyAdaptationService(session, user, as_of=today).assess_today(eligible.id)

        assert preview.workout_id == eligible.id
        assert preview.accepted_revision_id == eligible.accepted_revision_id
        assert [item.workout_id for item in preview.week.workouts] == [eligible.id]
        with pytest.raises(DailyAdaptationError) as wrong_day:
            DailyAdaptationService(session, user, as_of=today).assess_today(future.id)
        assert wrong_day.value.code == "adaptation.workout_not_eligible"
        with pytest.raises(DailyAdaptationError) as foreign:
            DailyAdaptationService(session, other, as_of=today).assess_today(eligible.id)
        assert foreign.value.code == "adaptation.not_found"


def test_feedback_and_week_changes_invalidate_adaptation_context(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "coach_daily_adaptation_enabled", True)
    today = date.today()
    with session_factory() as session:
        user = User(display_name="Context Runner")
        session.add(user)
        session.flush()
        workout = _accepted_workout(session, user, today)
        service = DailyAdaptationService(session, user, as_of=today)
        initial = service.assess_today(workout.id)

        FeedbackCommands(session, user).record_pre_session(
            workout.id,
            PreSessionFeedbackInput(fatigue=5, available_minutes=30),
        )
        session.commit()
        after_feedback = service.assess_today(workout.id)

        assert after_feedback.context_fingerprint != initial.context_fingerprint
        assert after_feedback.safety_fingerprint != initial.safety_fingerprint
        assert after_feedback.available_minutes == 30
        assert DailyAdaptationClass.KEEP not in {
            candidate.adaptation_class for candidate in after_feedback.assessment.candidates
        }

        same_week_day = today + timedelta(days=1 if today.weekday() < 6 else -1)
        _accepted_workout(session, user, same_week_day)
        after_week_change = service.assess_today(workout.id)
        assert after_week_change.week.fingerprint != after_feedback.week.fingerprint
        assert after_week_change.context_fingerprint != after_feedback.context_fingerprint


def test_availability_uses_only_target_workout_feedback_from_today(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "coach_daily_adaptation_enabled", True)
    today = date.today()
    with session_factory() as session:
        user = User(display_name="Freshness Runner")
        session.add(user)
        session.flush()
        target = _accepted_workout(session, user, today)
        other = _accepted_workout(session, user, today + timedelta(days=1))
        session.add(
            PreSessionFeedback(
                user_id=user.id,
                workout_id=other.id,
                workout_user_id=user.id,
                available_minutes=0,
                illness_signal="none",
                pain_present=False,
                source="test",
                content_hash="a" * 64,
                recorded_at=datetime.combine(today, datetime.min.time()),
            )
        )
        session.commit()

        unrelated = DailyAdaptationService(session, user, as_of=today).assess_today(target.id)
        assert unrelated.available_minutes is None

        session.add(
            PreSessionFeedback(
                user_id=user.id,
                workout_id=target.id,
                workout_user_id=user.id,
                available_minutes=30,
                illness_signal="none",
                pain_present=False,
                source="test",
                content_hash="c" * 64,
                recorded_at=datetime.combine(today, datetime.min.time()),
            )
        )
        session.commit()
        current = DailyAdaptationService(session, user, as_of=today).assess_today(target.id)
        assert current.available_minutes == 30

        old_user = User(display_name="Expired Feedback Runner")
        session.add(old_user)
        session.flush()
        old_target = _accepted_workout(session, old_user, today)
        session.add(
            PreSessionFeedback(
                user_id=old_user.id,
                workout_id=old_target.id,
                workout_user_id=old_user.id,
                available_minutes=0,
                illness_signal="none",
                pain_present=False,
                source="test",
                content_hash="b" * 64,
                recorded_at=datetime.combine(today - timedelta(days=8), datetime.min.time()),
            )
        )
        session.commit()

        expired = DailyAdaptationService(session, old_user, as_of=today).assess_today(old_target.id)
        assert expired.available_minutes is None
        assert DailyAdaptationClass.KEEP in {
            candidate.adaptation_class for candidate in expired.assessment.candidates
        }


def test_assessment_is_blocked_while_feature_flag_is_off(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "coach_daily_adaptation_enabled", False)
    with session_factory() as session:
        user = User(display_name="Flag Runner")
        session.add(user)
        session.flush()

        with pytest.raises(DailyAdaptationError) as disabled:
            DailyAdaptationService(session, user, as_of=date.today()).assess_today(1)

        assert disabled.value.code == "adaptation.feature_disabled"


def test_content_adaptation_appends_revision_and_preserves_original(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "coach_daily_adaptation_enabled", True)
    today = date.today()
    with session_factory() as session:
        user = User(display_name="Revision Runner")
        session.add(user)
        session.flush()
        workout = _accepted_workout(session, user, today)
        original_revision_id = workout.accepted_revision_id
        original_hash = (
            session.get(WorkoutRevision, original_revision_id).content_hash
            if original_revision_id is not None
            else None
        )
        service = DailyAdaptationService(session, user, as_of=today)
        preview = service.assess_today(workout.id)

        result = service.apply(
            workout.id,
            DailyAdaptationClass.REDUCE_VOLUME,
            expected_context_fingerprint=preview.context_fingerprint,
            idempotency_key="adapt-reduce-1",
        )

        assert result.revision_created
        session.refresh(workout)
        current = session.get(WorkoutRevision, workout.current_revision_id)
        accepted = session.get(WorkoutRevision, workout.accepted_revision_id)
        assert current is not None and accepted is not None
        assert current.id != accepted.id
        assert current.parent_revision_id == accepted.id
        assert current.source_type == "coach_daily_adaptation"
        assert current.generation_context_json is not None
        assert current.generation_context_json["adaptation_class"] == "REDUCE_VOLUME"
        assert accepted.content_hash == original_hash

        replay = service.apply(
            workout.id,
            DailyAdaptationClass.REDUCE_VOLUME,
            expected_context_fingerprint=preview.context_fingerprint,
            idempotency_key="adapt-reduce-1",
        )
        assert not replay.revision_created
        session.refresh(workout)
        assert workout.current_revision_id == current.id

        accept_service = WorkoutService(session, user)
        revision = session.get(WorkoutRevision, workout.current_revision_id)
        assert revision is not None
        accept_service.accept(
            workout.id,
            AcceptRevisionCommand(
                identity=RevisionIdentity(
                    revision_id=revision.id,
                    revision_number=revision.revision_number,
                    content_hash=revision.content_hash,
                    lock_version=workout.lock_version,
                ),
                context_fingerprint=accept_service.acceptance_context(workout.id).fingerprint,
            ),
        )
        session.refresh(workout)
        assert workout.accepted_revision_id == current.id


def test_discard_then_new_adaptation_uses_next_revision_number(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "coach_daily_adaptation_enabled", True)
    today = date.today()
    with session_factory() as session:
        user = User(display_name="Discard Runner")
        session.add(user)
        session.flush()
        workout = _accepted_workout(session, user, today)
        adaptation = DailyAdaptationService(session, user, as_of=today)
        first_preview = adaptation.assess_today(workout.id)
        first = adaptation.apply(
            workout.id,
            DailyAdaptationClass.REDUCE_VOLUME,
            expected_context_fingerprint=first_preview.context_fingerprint,
            idempotency_key="adapt-discard-first",
        )
        assert first.revision_created
        first_revision = session.get(WorkoutRevision, workout.current_revision_id)
        assert first_revision is not None
        WorkoutService(session, user).discard_adaptation_revision(
            workout.id,
            RejectRevisionCommand(
                identity=RevisionIdentity(
                    revision_id=first_revision.id,
                    revision_number=first_revision.revision_number,
                    content_hash=first_revision.content_hash,
                    lock_version=workout.lock_version,
                )
            ),
        )

        replay = adaptation.apply(
            workout.id,
            DailyAdaptationClass.REDUCE_VOLUME,
            expected_context_fingerprint=first_preview.context_fingerprint,
            idempotency_key="adapt-discard-first",
        )
        assert not replay.revision_created

        second_preview = adaptation.assess_today(workout.id)
        second = adaptation.apply(
            workout.id,
            DailyAdaptationClass.REDUCE_VOLUME,
            expected_context_fingerprint=second_preview.context_fingerprint,
            idempotency_key="adapt-discard-second",
        )
        assert second.revision_created
        second_revision = session.get(WorkoutRevision, workout.current_revision_id)
        assert second_revision is not None
        assert first_revision.revision_number == 2
        assert second_revision.revision_number == 3


def test_easy_replacement_is_separate_workout_and_acceptance_swaps_schedule(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "coach_daily_adaptation_enabled", True)
    today = date.today()
    with session_factory() as session:
        user = User(display_name="Replacement Runner")
        session.add(user)
        session.flush()
        original = _accepted_workout(session, user, today)
        original_revision_id = original.accepted_revision_id
        adaptation = DailyAdaptationService(
            session, user, as_of=today, request_id="replacement-request"
        )
        preview = adaptation.assess_today(original.id)

        result = adaptation.apply(
            original.id,
            DailyAdaptationClass.REPLACE_WITH_EASY,
            expected_context_fingerprint=preview.context_fingerprint,
            idempotency_key="adapt-replacement-create",
        )
        replacement = result.workout
        replacement_revision = session.get(WorkoutRevision, replacement.current_revision_id)

        assert result.revision_created
        assert replacement.id != original.id
        assert replacement.replaces_workout_id == original.id
        assert replacement.accepted_revision_id is None
        assert replacement.local_schedule_status == "unscheduled"
        assert replacement_revision is not None
        assert replacement_revision.revision_number == 1
        assert replacement_revision.parent_revision_id is None
        assert replacement_revision.generation_context_json is not None
        assert replacement_revision.generation_context_json["original_workout"]["id"] == (
            original.id
        )
        assert original.accepted_revision_id == original_revision_id
        assert original.local_schedule_status == "scheduled"
        assert original.scheduled_for == today

        replay = adaptation.apply(
            original.id,
            DailyAdaptationClass.REPLACE_WITH_EASY,
            expected_context_fingerprint=preview.context_fingerprint,
            idempotency_key="adapt-replacement-create",
        )
        assert not replay.revision_created
        assert replay.workout.id == replacement.id

        service = WorkoutService(session, user, request_id="replacement-accept")
        service.accept(
            replacement.id,
            AcceptRevisionCommand(
                identity=RevisionIdentity(
                    revision_id=replacement_revision.id,
                    revision_number=replacement_revision.revision_number,
                    content_hash=replacement_revision.content_hash,
                    lock_version=replacement.lock_version,
                ),
                context_fingerprint=service.acceptance_context(replacement.id).fingerprint,
            ),
        )

        assert replacement.accepted_revision_id == replacement_revision.id
        assert replacement.local_schedule_status == "scheduled"
        assert replacement.scheduled_for == today
        assert original.accepted_revision_id == original_revision_id
        assert original.local_schedule_status == "cancelled"
        assert original.scheduled_for is None
        events = list(
            session.scalars(
                select(WorkoutEvent).where(
                    WorkoutEvent.action.in_(
                        {
                            "adapt_replace_propose",
                            "supersede",
                            "accept",
                        }
                    )
                )
            )
        )
        relevant_events = [
            event
            for event in events
            if event.action != "accept" or event.workout_id == replacement.id
        ]
        assert {event.action for event in relevant_events} == {
            "adapt_replace_propose",
            "supersede",
            "accept",
        }
        assert all(event.request_id is not None for event in relevant_events)


def test_discarded_replacement_preserves_original_and_allows_another(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "coach_daily_adaptation_enabled", True)
    today = date.today()
    with session_factory() as session:
        user = User(display_name="Replacement Reject Runner")
        session.add(user)
        session.flush()
        original = _accepted_workout(session, user, today)
        adaptation = DailyAdaptationService(session, user, as_of=today)
        preview = adaptation.assess_today(original.id)
        first = adaptation.apply(
            original.id,
            DailyAdaptationClass.REPLACE_WITH_EASY,
            expected_context_fingerprint=preview.context_fingerprint,
            idempotency_key="adapt-replacement-reject",
        ).workout
        revision = session.get(WorkoutRevision, first.current_revision_id)
        assert revision is not None

        WorkoutService(session, user).discard_adaptation_revision(
            first.id,
            RejectRevisionCommand(
                identity=RevisionIdentity(
                    revision_id=revision.id,
                    revision_number=revision.revision_number,
                    content_hash=revision.content_hash,
                    lock_version=first.lock_version,
                )
            ),
        )

        assert first.approval_status == "rejected"
        assert original.local_schedule_status == "scheduled"
        next_preview = adaptation.assess_today(original.id)
        second = adaptation.apply(
            original.id,
            DailyAdaptationClass.REPLACE_WITH_EASY,
            expected_context_fingerprint=next_preview.context_fingerprint,
            idempotency_key="adapt-replacement-second",
        ).workout
        assert second.id != first.id
        assert second.replaces_workout_id == original.id


def test_adaptation_events_keep_request_id(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "coach_daily_adaptation_enabled", True)
    today = date.today()
    with session_factory() as session:
        user = User(display_name="Audit Runner")
        session.add(user)
        session.flush()
        workout = _accepted_workout(session, user, today)
        adaptation = DailyAdaptationService(
            session, user, as_of=today, request_id="request-phase-10"
        )
        preview = adaptation.assess_today(workout.id)

        adaptation.apply(
            workout.id,
            DailyAdaptationClass.KEEP,
            expected_context_fingerprint=preview.context_fingerprint,
            idempotency_key="adapt-audit-request",
        )

        event = session.scalar(select(WorkoutEvent).where(WorkoutEvent.action == "adapt_keep"))
        assert event is not None
        assert event.request_id == "request-phase-10"


def test_keep_preserves_execution_and_rest_cancels_only_today(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "coach_daily_adaptation_enabled", True)
    today = date.today()
    with session_factory() as session:
        user = User(display_name="Keep Rest Runner")
        session.add(user)
        session.flush()
        keep_workout = _accepted_workout(session, user, today)
        service = DailyAdaptationService(session, user, as_of=today)
        preview = service.assess_today(keep_workout.id)

        result = service.apply(
            keep_workout.id,
            DailyAdaptationClass.KEEP,
            expected_context_fingerprint=preview.context_fingerprint,
            idempotency_key="adapt-keep-1",
        )

        assert not result.revision_created
        session.refresh(keep_workout)
        assert keep_workout.current_revision_id == keep_workout.accepted_revision_id
        assert keep_workout.local_schedule_status == "scheduled"
        keeps = list(
            session.scalars(select(WorkoutEvent).where(WorkoutEvent.action == "adapt_keep"))
        )
        assert len(keeps) == 1

        rest_preview = DailyAdaptationService(session, user, as_of=today).assess_today(
            keep_workout.id
        )
        rest_result = service.apply(
            keep_workout.id,
            DailyAdaptationClass.REST,
            expected_context_fingerprint=rest_preview.context_fingerprint,
            idempotency_key="adapt-rest-1",
        )

        assert not rest_result.revision_created
        session.refresh(keep_workout)
        assert keep_workout.scheduled_for is None
        assert keep_workout.local_schedule_status == "cancelled"
        assert keep_workout.accepted_revision_id is not None
        assert keep_workout.current_revision_id == keep_workout.accepted_revision_id
        rests = list(
            session.scalars(select(WorkoutEvent).where(WorkoutEvent.action == "adapt_rest"))
        )
        assert len(rests) == 1


def test_apply_rejects_stale_context(session_factory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "coach_daily_adaptation_enabled", True)
    today = date.today()
    with session_factory() as session:
        user = User(display_name="Stale Runner")
        session.add(user)
        session.flush()
        workout = _accepted_workout(session, user, today)
        service = DailyAdaptationService(session, user, as_of=today)
        stale = service.assess_today(workout.id)

        FeedbackCommands(session, user).record_pre_session(
            workout.id,
            PreSessionFeedbackInput(fatigue=5, available_minutes=30),
        )
        session.commit()

        with pytest.raises(DailyAdaptationError) as conflict:
            service.apply(
                workout.id,
                DailyAdaptationClass.REDUCE_VOLUME,
                expected_context_fingerprint=stale.context_fingerprint,
                idempotency_key="adapt-stale-1",
            )

        assert conflict.value.code == "adaptation.context_stale"
        session.refresh(workout)
        assert workout.current_revision_id == workout.accepted_revision_id


def test_synced_adaptation_updates_known_remote_identity_without_upload(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "coach_daily_adaptation_enabled", True)
    monkeypatch.setattr(get_settings(), "coach_garmin_sync_enabled", True)
    today = date.today()

    class FakeGarmin:
        def __init__(self) -> None:
            self.uploads = 0
            self.updates: list[str] = []
            self.scheduled: dict[str, tuple[str, str]] = {}

        def upload_workout(self, _payload: dict[str, object]) -> dict[str, str]:
            self.uploads += 1
            return {"workoutId": "remote-1"}

        def update_workout(self, workout_id: str, _payload: dict[str, object]) -> None:
            self.updates.append(workout_id)

        def get_scheduled_workouts(self, _year: int, _month: int) -> dict[str, object]:
            return {
                "items": [
                    {"id": key, "workoutId": workout_id, "date": day}
                    for key, (workout_id, day) in self.scheduled.items()
                ]
            }

        def schedule_workout(self, workout_id: str, day: str) -> None:
            key = f"schedule-{len(self.scheduled) + 1}"
            self.scheduled[key] = (workout_id, day)

    garmin = FakeGarmin()
    with session_factory() as session:
        user = User(display_name="Garmin Adaptation Runner")
        session.add(user)
        session.flush()
        workout = _accepted_workout(session, user, today)
        session.add(GarminAccount(user_id=user.id, connected_at=utcnow()))
        session.commit()
        sync_service = WorkoutService(session, user, connect_garmin=lambda *_args: garmin)
        revision = session.get(WorkoutRevision, workout.accepted_revision_id)
        assert revision is not None
        sync_service.publish(workout.id)
        assert garmin.uploads == 1
        identity_id = session.scalar(
            select(WorkoutGarminBinding.active_remote_identity_id).where(
                WorkoutGarminBinding.workout_id == workout.id
            )
        )

        adaptation = DailyAdaptationService(session, user, as_of=today)
        preview = adaptation.assess_today(workout.id)
        adaptation.apply(
            workout.id,
            DailyAdaptationClass.REDUCE_VOLUME,
            expected_context_fingerprint=preview.context_fingerprint,
            idempotency_key="adapt-garmin-1",
        )
        candidate = session.get(WorkoutRevision, workout.current_revision_id)
        assert candidate is not None
        sync_service.accept(
            workout.id,
            AcceptRevisionCommand(
                identity=RevisionIdentity(
                    revision_id=candidate.id,
                    revision_number=candidate.revision_number,
                    content_hash=candidate.content_hash,
                    lock_version=workout.lock_version,
                ),
                context_fingerprint=sync_service.acceptance_context(workout.id).fingerprint,
            ),
        )
        sync_service.publish(workout.id)

        assert garmin.uploads == 1
        assert garmin.updates == ["remote-1"]
        binding = session.scalar(
            select(WorkoutGarminBinding).where(WorkoutGarminBinding.workout_id == workout.id)
        )
        assert binding is not None
        assert binding.active_remote_identity_id == identity_id
        assert binding.content_status == "synced"


def test_synced_replacement_retires_old_calendar_before_new_upload(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "coach_daily_adaptation_enabled", True)
    monkeypatch.setattr(get_settings(), "coach_garmin_sync_enabled", True)
    today = date.today()

    class FakeGarmin:
        def __init__(self) -> None:
            self.uploads = 0
            self.events: list[str] = []
            self.scheduled: dict[str, tuple[str, str]] = {}

        def upload_workout(self, _payload: dict[str, object]) -> dict[str, str]:
            self.uploads += 1
            remote_id = f"remote-{self.uploads}"
            self.events.append(f"upload:{remote_id}")
            return {"workoutId": remote_id}

        def get_scheduled_workouts(self, _year: int, _month: int) -> dict[str, object]:
            return {
                "items": [
                    {"id": key, "workoutId": workout_id, "date": day}
                    for key, (workout_id, day) in self.scheduled.items()
                ]
            }

        def schedule_workout(self, workout_id: str, day: str) -> None:
            key = f"schedule-{len(self.scheduled) + 1}"
            self.scheduled[key] = (workout_id, day)
            self.events.append(f"schedule:{workout_id}")

        def unschedule_workout(self, scheduled_id: str) -> None:
            workout_id, _day = self.scheduled.pop(scheduled_id)
            self.events.append(f"unschedule:{workout_id}")

    garmin = FakeGarmin()
    with session_factory() as session:
        user = User(display_name="Garmin Replacement Runner")
        session.add(user)
        session.flush()
        original = _accepted_workout(session, user, today)
        session.add(GarminAccount(user_id=user.id, connected_at=utcnow()))
        session.commit()
        service = WorkoutService(session, user, connect_garmin=lambda *_args: garmin)
        service.publish(original.id)

        adaptation = DailyAdaptationService(session, user, as_of=today)
        preview = adaptation.assess_today(original.id)
        replacement = adaptation.apply(
            original.id,
            DailyAdaptationClass.REPLACE_WITH_EASY,
            expected_context_fingerprint=preview.context_fingerprint,
            idempotency_key="adapt-garmin-replacement",
        ).workout
        revision = session.get(WorkoutRevision, replacement.current_revision_id)
        assert revision is not None
        service.accept(
            replacement.id,
            AcceptRevisionCommand(
                identity=RevisionIdentity(
                    revision_id=revision.id,
                    revision_number=revision.revision_number,
                    content_hash=revision.content_hash,
                    lock_version=replacement.lock_version,
                ),
                context_fingerprint=service.acceptance_context(replacement.id).fingerprint,
            ),
        )
        service.publish(replacement.id)

        assert garmin.events == [
            "upload:remote-1",
            "schedule:remote-1",
            "unschedule:remote-1",
            "upload:remote-2",
            "schedule:remote-2",
        ]
        original_binding = session.scalar(
            select(WorkoutGarminBinding).where(WorkoutGarminBinding.workout_id == original.id)
        )
        replacement_binding = session.scalar(
            select(WorkoutGarminBinding).where(WorkoutGarminBinding.workout_id == replacement.id)
        )
        assert original_binding is not None and replacement_binding is not None
        assert original_binding.remote_scheduled_for is None
        assert original_binding.calendar_status == "not_requested"
        assert replacement_binding.remote_scheduled_for == today
        assert original_binding.active_remote_identity_id is not None
        assert replacement_binding.active_remote_identity_id is not None
        original_identity = session.get(
            WorkoutGarminRemoteIdentity, original_binding.active_remote_identity_id
        )
        replacement_identity = session.get(
            WorkoutGarminRemoteIdentity, replacement_binding.active_remote_identity_id
        )
        assert original_identity is not None and replacement_identity is not None
        assert original_identity.garmin_workout_id == "remote-1"
        assert replacement_identity.garmin_workout_id == "remote-2"


def test_safety_stop_rest_can_remove_existing_garmin_calendar_entry(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "coach_daily_adaptation_enabled", True)
    monkeypatch.setattr(get_settings(), "coach_garmin_sync_enabled", True)
    today = date.today()

    class FakeGarmin:
        def __init__(self) -> None:
            self.uploads = 0
            self.unschedules = 0
            self.scheduled: dict[str, tuple[str, str]] = {}

        def upload_workout(self, _payload: dict[str, object]) -> dict[str, str]:
            self.uploads += 1
            return {"workoutId": "remote-rest"}

        def get_scheduled_workouts(self, _year: int, _month: int) -> dict[str, object]:
            return {
                "items": [
                    {"id": key, "workoutId": workout_id, "date": day}
                    for key, (workout_id, day) in self.scheduled.items()
                ]
            }

        def schedule_workout(self, workout_id: str, day: str) -> None:
            self.scheduled["schedule-rest"] = (workout_id, day)

        def unschedule_workout(self, scheduled_id: str) -> None:
            self.unschedules += 1
            self.scheduled.pop(scheduled_id)

    garmin = FakeGarmin()
    with session_factory() as session:
        user = User(display_name="Safety Rest Runner")
        session.add(user)
        session.flush()
        workout = _accepted_workout(session, user, today)
        session.add(GarminAccount(user_id=user.id, connected_at=utcnow()))
        session.commit()
        service = WorkoutService(session, user, connect_garmin=lambda *_args: garmin)
        service.publish(workout.id)
        FeedbackCommands(session, user).record_pre_session(
            workout.id,
            PreSessionFeedbackInput(illness_signal="fever"),
        )
        session.commit()
        adaptation = DailyAdaptationService(session, user, as_of=today)
        preview = adaptation.assess_today(workout.id)
        assert [item.adaptation_class for item in preview.assessment.candidates] == [
            DailyAdaptationClass.REST
        ]
        adaptation.apply(
            workout.id,
            DailyAdaptationClass.REST,
            expected_context_fingerprint=preview.context_fingerprint,
            idempotency_key="adapt-safety-rest",
        )

        service.publish(workout.id)

        assert garmin.uploads == 1
        assert garmin.unschedules == 1
        assert garmin.scheduled == {}
        binding = session.scalar(
            select(WorkoutGarminBinding).where(WorkoutGarminBinding.workout_id == workout.id)
        )
        assert binding is not None
        assert binding.remote_scheduled_for is None
        assert binding.calendar_status == "not_requested"


def test_original_cannot_be_deleted_while_replacement_is_active(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "coach_daily_adaptation_enabled", True)
    today = date.today()
    with session_factory() as session:
        user = User(display_name="Protected Original Runner")
        session.add(user)
        session.flush()
        original = _accepted_workout(session, user, today)
        adaptation = DailyAdaptationService(session, user, as_of=today)
        preview = adaptation.assess_today(original.id)
        replacement = adaptation.apply(
            original.id,
            DailyAdaptationClass.REPLACE_WITH_EASY,
            expected_context_fingerprint=preview.context_fingerprint,
            idempotency_key="adapt-delete-guard",
        ).workout

        with pytest.raises(WorkoutTransitionError) as proposed_guard:
            WorkoutService(session, user).delete(original.id)
        assert proposed_guard.value.code == "adaptation.replacement_active"

        revision = session.get(WorkoutRevision, replacement.current_revision_id)
        assert revision is not None
        service = WorkoutService(session, user)
        service.accept(
            replacement.id,
            AcceptRevisionCommand(
                identity=RevisionIdentity(
                    revision_id=revision.id,
                    revision_number=revision.revision_number,
                    content_hash=revision.content_hash,
                    lock_version=replacement.lock_version,
                ),
                context_fingerprint=service.acceptance_context(replacement.id).fingerprint,
            ),
        )
        with pytest.raises(WorkoutTransitionError) as accepted_guard:
            service.delete(original.id)
        assert accepted_guard.value.code == "adaptation.replacement_active"


def test_adaptation_routes_are_user_scoped_and_flagged(
    client, session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    today = date.today()
    with session_factory() as session:
        user = session.query(User).first()
        assert user is not None
        workout = _accepted_workout(session, user, today)

    monkeypatch.setattr(get_settings(), "coach_daily_adaptation_enabled", False)
    page_off = client.get(f"/workouts/{workout.id}")
    assert page_off.status_code == 200
    assert "Tägliche Anpassung" not in page_off.text
    blocked = client.post(
        f"/workouts/{workout.id}/adaptation/apply",
        data={
            "adaptation_class": "KEEP",
            "context_fingerprint": "x",
            "idempotency_key": "route-off-1",
        },
        follow_redirects=False,
    )
    assert blocked.status_code == 303
    assert "error" in blocked.headers["location"]

    monkeypatch.setattr(get_settings(), "coach_daily_adaptation_enabled", True)
    page_on = client.get(f"/workouts/{workout.id}")
    assert page_on.status_code == 200
    assert "Tägliche Anpassung" in page_on.text
    assert "Training beibehalten anwenden" in page_on.text
    invalid_key = client.post(
        f"/workouts/{workout.id}/adaptation/apply",
        data={
            "adaptation_class": "KEEP",
            "context_fingerprint": "x",
            "idempotency_key": "",
        },
        follow_redirects=False,
    )
    assert invalid_key.status_code == 422

    fingerprint_match = re.search(r'name="context_fingerprint" value="([0-9a-f]+)"', page_on.text)
    assert fingerprint_match is not None
    applied = client.post(
        f"/workouts/{workout.id}/adaptation/apply",
        data={
            "adaptation_class": "REDUCE_VOLUME",
            "context_fingerprint": fingerprint_match.group(1),
            "idempotency_key": "route-apply-1",
        },
        follow_redirects=False,
    )
    assert applied.status_code == 303
    detail = client.get(f"/workouts/{workout.id}")
    assert "Anpassung verwerfen" in detail.text
    assert ">Bearbeiten</a>" not in detail.text

    discard_form = re.search(
        r'action="/workouts/' + str(workout.id) + r'/adaptation/discard"', detail.text
    )
    assert discard_form is not None
    discarded = client.post(
        f"/workouts/{workout.id}/adaptation/discard",
        data=_discard_fields(detail.text),
        follow_redirects=False,
    )
    assert discarded.status_code == 303
    final_page = client.get(f"/workouts/{workout.id}")
    assert "Anpassung verwerfen" not in final_page.text

    with session_factory() as session:
        foreign = User(display_name="Foreign Runner")
        session.add(foreign)
        session.flush()
        foreign_workout = _accepted_workout(session, foreign, today)

    assert client.get(f"/workouts/{foreign_workout.id}").status_code == 404


def _discard_fields(page_html: str) -> dict[str, str]:
    fields = {}
    for name in ("revision_id", "revision_number", "content_hash", "lock_version"):
        match = re.search(rf'name="{name}" value="([^"]+)"', page_html)
        assert match is not None, name
        fields[name] = match.group(1)
    return fields


def test_week_impact_is_explicit_and_non_increasing(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "coach_daily_adaptation_enabled", True)
    today = date.today()
    with session_factory() as session:
        user = User(display_name="Week Impact Runner")
        session.add(user)
        session.flush()
        workout = _accepted_workout(session, user, today)

        preview = DailyAdaptationService(session, user, as_of=today).assess_today(workout.id)

        assert len(preview.week_impacts) == len(preview.assessment.candidates)
        for impact in preview.week_impacts:
            assert impact.duration_after_seconds <= impact.duration_before_seconds
            assert impact.distance_after_meters <= impact.distance_before_meters


def test_unknown_remote_state_blocks_adaptation_acceptance(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "coach_daily_adaptation_enabled", True)
    today = date.today()
    with session_factory() as session:
        user = User(display_name="Unknown State Runner")
        session.add(user)
        session.flush()
        workout = _accepted_workout(session, user, today)
        binding = session.scalar(
            select(WorkoutGarminBinding).where(WorkoutGarminBinding.workout_id == workout.id)
        )
        assert binding is not None
        binding.content_status = "unknown"
        session.commit()

        adaptation = DailyAdaptationService(session, user, as_of=today)
        preview = adaptation.assess_today(workout.id)
        adaptation.apply(
            workout.id,
            DailyAdaptationClass.REDUCE_VOLUME,
            expected_context_fingerprint=preview.context_fingerprint,
            idempotency_key="adapt-unknown-1",
        )
        candidate = session.get(WorkoutRevision, workout.current_revision_id)
        assert candidate is not None

        with pytest.raises(WorkoutTransitionError) as blocked:
            WorkoutService(session, user).accept(
                workout.id,
                AcceptRevisionCommand(
                    identity=RevisionIdentity(
                        revision_id=candidate.id,
                        revision_number=candidate.revision_number,
                        content_hash=candidate.content_hash,
                        lock_version=workout.lock_version,
                    ),
                    context_fingerprint=WorkoutService(session, user)
                    .acceptance_context(workout.id)
                    .fingerprint,
                ),
            )

        assert blocked.value.code == "garmin.state_unknown"
        session.refresh(workout)
        assert workout.accepted_revision_id != candidate.id

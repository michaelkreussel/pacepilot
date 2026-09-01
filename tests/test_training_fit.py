from dataclasses import replace
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    Activity,
    DailyFitness,
    DailyHealth,
    PostSessionFeedback,
    PreSessionFeedback,
    User,
)
from app.services.planning.training_fit import (
    TrainingFitOutcome,
    assess_training_fit,
    load_training_fit_policy,
)


def _user(session: Session) -> User:
    user = User(display_name="Training Fit")
    session.add(user)
    session.flush()
    return user


def _seed_health(
    session: Session,
    user_id: int,
    as_of: date,
    *,
    baseline_samples: int = 14,
    current_day_offset: int = 0,
    resting_hr: int = 50,
    hrv: float = 60,
) -> DailyHealth:
    session.add_all(
        DailyHealth(
            user_id=user_id,
            day=as_of - timedelta(days=7 + offset),
            resting_hr=50,
            hrv_average=60,
            sleep_seconds=28_800,
            stress_average=30,
        )
        for offset in range(baseline_samples)
    )
    current = DailyHealth(
        user_id=user_id,
        day=as_of - timedelta(days=current_day_offset),
        resting_hr=resting_hr,
        hrv_average=hrv,
        sleep_seconds=28_800,
        stress_average=30,
    )
    session.add(current)
    session.commit()
    return current


def _assess(
    session: Session,
    user_id: int,
    as_of: date,
    *,
    effective_date: date | None = None,
    revision_fingerprint: str = "a" * 64,
    policy=None,
):
    return assess_training_fit(
        session,
        user_id,
        effective_workout_date=effective_date or as_of,
        revision_fingerprint=revision_fingerprint,
        evaluated_at=datetime.combine(as_of, datetime.min.time()).replace(hour=12),
        policy=policy,
    )


def test_normal_requires_adequate_personal_coverage_without_warning_signals(
    session_factory: sessionmaker[Session],
) -> None:
    as_of = date(2026, 8, 20)
    with session_factory() as session:
        user = _user(session)
        _seed_health(session, user.id, as_of)

        assessment = _assess(session, user.id, as_of)

    assert assessment.outcome == TrainingFitOutcome.NORMAL
    assert assessment.warning_codes == ()
    assert assessment.policy_version.startswith("training-fit-v1+")
    assert assessment.evaluated_at.date() == as_of
    assert assessment.effective_workout_date == as_of
    assert len(assessment.authoritative_input_fingerprint) == 64
    assert not hasattr(assessment, "valid")


def test_missing_or_sparse_personal_data_is_caution_not_elevated(
    session_factory: sessionmaker[Session],
) -> None:
    as_of = date(2026, 8, 20)
    with session_factory() as session:
        missing_user = _user(session)
        sparse_user = _user(session)
        _seed_health(
            session,
            sparse_user.id,
            as_of,
            baseline_samples=5,
            resting_hr=70,
            hrv=35,
        )

        missing = _assess(session, missing_user.id, as_of)
        sparse = _assess(session, sparse_user.id, as_of)

    assert missing.outcome == TrainingFitOutcome.CAUTION
    assert "coverage.health_missing" in missing.warning_codes
    assert sparse.outcome == TrainingFitOutcome.CAUTION
    assert "coverage.personal_baseline_sparse" in sparse.warning_codes


def test_one_severe_anomaly_and_one_low_readiness_score_are_only_caution(
    session_factory: sessionmaker[Session],
) -> None:
    as_of = date(2026, 8, 20)
    with session_factory() as session:
        user = _user(session)
        _seed_health(session, user.id, as_of, resting_hr=70)
        session.add(
            DailyFitness(
                user_id=user.id,
                day=as_of,
                garmin_training_readiness_score=20,
            )
        )
        session.commit()

        assessment = _assess(session, user.id, as_of)

    assert assessment.outcome == TrainingFitOutcome.CAUTION
    assert "health.resting_hr.severe_deviation" in assessment.warning_codes
    assert "readiness.low_score" in assessment.warning_codes
    assert sum(item.severe for item in assessment.evidence) == 1


def test_two_recent_severe_personal_deviations_are_elevated(
    session_factory: sessionmaker[Session],
) -> None:
    as_of = date(2026, 8, 20)
    with session_factory() as session:
        user = _user(session)
        _seed_health(session, user.id, as_of, resting_hr=70, hrv=35)

        assessment = _assess(session, user.id, as_of)

    assert assessment.outcome == TrainingFitOutcome.ELEVATED
    assert {item.source for item in assessment.evidence if item.severe} == {
        "health.hrv",
        "health.resting_hr",
    }
    assert all(item.observed_on == as_of for item in assessment.evidence)
    assert all(item.sufficient_for_elevation for item in assessment.coverage[:2])


def test_stale_severe_health_and_future_session_are_not_elevated(
    session_factory: sessionmaker[Session],
) -> None:
    as_of = date(2026, 8, 20)
    with session_factory() as session:
        stale_user = _user(session)
        future_user = _user(session)
        _seed_health(session, stale_user.id, as_of, current_day_offset=3, resting_hr=70, hrv=35)
        _seed_health(session, future_user.id, as_of, resting_hr=70, hrv=35)

        stale = _assess(session, stale_user.id, as_of)
        future = _assess(
            session,
            future_user.id,
            as_of,
            effective_date=as_of + timedelta(days=1),
        )

    assert stale.outcome == TrainingFitOutcome.CAUTION
    assert future.outcome == TrainingFitOutcome.CAUTION
    assert "health.hrv.severe_deviation" in future.warning_codes
    assert "health.resting_hr.severe_deviation" in future.warning_codes


def test_explicit_recent_serious_feedback_is_elevated_without_diagnosis(
    session_factory: sessionmaker[Session],
) -> None:
    as_of = date(2026, 8, 20)
    with session_factory() as session:
        user = _user(session)
        feedback = PreSessionFeedback(
            user_id=user.id,
            workout_id=None,
            workout_user_id=None,
            pain_present=False,
            illness_signal="fever",
            source="workout_safety",
            content_hash="f" * 64,
            recorded_at=datetime.combine(as_of, datetime.min.time()).replace(hour=9),
        )
        session.add(feedback)
        session.commit()

        assessment = _assess(session, user.id, as_of)

    assert assessment.outcome == TrainingFitOutcome.ELEVATED
    assert assessment.feedback_ids == (f"pre:{feedback.id}",)
    assert assessment.evidence[0].observed_on == as_of
    assert assessment.evidence[0].source == "feedback.pre_session"
    assert "safety.fever_or_systemic_illness" in assessment.warning_codes


def test_mild_illness_is_caution_and_future_serious_feedback_is_not_elevated(
    session_factory: sessionmaker[Session],
) -> None:
    as_of = date(2026, 8, 20)
    with session_factory() as session:
        mild_user = _user(session)
        serious_user = _user(session)
        session.add_all(
            [
                PreSessionFeedback(
                    user_id=mild_user.id,
                    pain_present=False,
                    illness_signal="mild_upper_respiratory",
                    source="workout_safety",
                    content_hash="m" * 64,
                    recorded_at=datetime.combine(as_of, datetime.min.time()).replace(hour=9),
                ),
                PreSessionFeedback(
                    user_id=serious_user.id,
                    pain_present=False,
                    illness_signal="systemic",
                    source="workout_safety",
                    content_hash="s" * 64,
                    recorded_at=datetime.combine(as_of, datetime.min.time()).replace(hour=9),
                ),
            ]
        )
        session.commit()

        mild = _assess(session, mild_user.id, as_of)
        future = _assess(
            session,
            serious_user.id,
            as_of,
            effective_date=as_of + timedelta(days=1),
        )

    assert mild.outcome == TrainingFitOutcome.CAUTION
    assert "safety.mild_illness" in mild.warning_codes
    assert future.outcome == TrainingFitOutcome.CAUTION


def test_garmin_feedback_precedence_is_preserved(
    session_factory: sessionmaker[Session],
) -> None:
    as_of = date(2026, 8, 20)
    with session_factory() as session:
        user = _user(session)
        _seed_health(session, user.id, as_of)
        activity = Activity(
            user_id=user.id,
            garmin_activity_id="training-fit-feedback",
            name="Run",
            activity_type="running",
            started_at=datetime.combine(as_of, datetime.min.time()).replace(hour=8),
            workout_rpe=4,
            workout_feel=4,
        )
        session.add(activity)
        session.flush()
        session.add(
            PostSessionFeedback(
                user_id=user.id,
                activity_id=activity.id,
                activity_user_id=user.id,
                session_rpe=9,
                overall_feel=1,
                pain_present=False,
                source="manual",
                content_hash="p" * 64,
                recorded_at=datetime.combine(as_of, datetime.min.time()).replace(hour=9),
            )
        )
        session.commit()

        garmin_override = _assess(session, user.id, as_of)
        activity.workout_rpe = 8
        session.commit()
        garmin_warning = _assess(session, user.id, as_of)

    assert "recovery.difficult_session" not in garmin_override.warning_codes
    assert "recovery.difficult_session" in garmin_warning.warning_codes
    assert (
        garmin_warning.authoritative_input_fingerprint
        != garmin_override.authoritative_input_fingerprint
    )


def test_garmin_only_difficult_activity_is_dated_caution(
    session_factory: sessionmaker[Session],
) -> None:
    as_of = date(2026, 8, 20)
    activity_day = as_of - timedelta(days=1)
    with session_factory() as session:
        user = _user(session)
        _seed_health(session, user.id, as_of)
        activity = Activity(
            user_id=user.id,
            garmin_activity_id="training-fit-garmin-only",
            name="Run",
            activity_type="running",
            started_at=datetime.combine(activity_day, datetime.min.time()).replace(hour=8),
            workout_rpe=9,
            workout_feel=1,
        )
        session.add(activity)
        session.commit()

        assessment = _assess(session, user.id, as_of)
        activity.started_at = datetime.combine(as_of, datetime.min.time()).replace(hour=8)
        session.commit()
        changed_date = _assess(session, user.id, as_of)

    difficult = next(
        item for item in assessment.evidence if item.code == "recovery.difficult_session"
    )
    assert assessment.outcome == TrainingFitOutcome.CAUTION
    assert difficult.source == "feedback.activity"
    assert difficult.observed_on == activity_day
    assert (
        changed_date.authoritative_input_fingerprint != assessment.authoritative_input_fingerprint
    )


def test_fingerprint_changes_with_each_authoritative_input(
    session_factory: sessionmaker[Session],
) -> None:
    as_of = date(2026, 8, 20)
    with session_factory() as session:
        user = _user(session)
        current = _seed_health(session, user.id, as_of)
        base = _assess(session, user.id, as_of)
        changed_revision = _assess(session, user.id, as_of, revision_fingerprint="b" * 64)
        changed_date = _assess(
            session,
            user.id,
            as_of,
            effective_date=as_of + timedelta(days=1),
        )
        changed_policy = _assess(
            session,
            user.id,
            as_of,
            policy=replace(load_training_fit_policy(), version="training-fit-test"),
        )
        current.resting_hr = 51
        session.commit()
        changed_health = _assess(session, user.id, as_of)
        session.add(
            PreSessionFeedback(
                user_id=user.id,
                pain_present=False,
                illness_signal="mild_upper_respiratory",
                source="workout_safety",
                content_hash="c" * 64,
                recorded_at=datetime.combine(as_of, datetime.min.time()).replace(hour=10),
            )
        )
        session.commit()
        changed_feedback = _assess(session, user.id, as_of)

    fingerprints = {
        item.authoritative_input_fingerprint
        for item in (
            base,
            changed_revision,
            changed_date,
            changed_policy,
            changed_health,
            changed_feedback,
        )
    }
    assert len(fingerprints) == 6

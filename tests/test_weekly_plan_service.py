from dataclasses import replace
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    CoachConversation,
    CoachMessage,
    TrainingPlan,
    TrainingPlanRevision,
    TrainingPlanWorkout,
    User,
    Workout,
    WorkoutGarminOperation,
    WorkoutRevision,
)
from app.services.planning.registry import get_knowledge_registry
from app.services.planning.validator import WorkoutInput
from app.services.planning.weekly_plan_service import (
    persist_week_candidate,
    persist_week_candidate_in_transaction,
    plan_proposals_between,
)
from app.services.planning.weekly_planner import (
    DayAvailability,
    GoalSummary,
    WeeklyPlanCandidate,
    WeeklyPlannerSnapshot,
    compose_week,
)
from app.services.planning.workout_definition import parse_definition
from app.services.planning.workout_revision import (
    AcceptRevisionCommand,
    RejectRevisionCommand,
    RevisionIdentity,
)
from app.services.planning.workout_service import WorkoutService, WorkoutTransitionError
from app.services.planning.workout_views import workout_detail_view

MONDAY = date(2026, 8, 31)


def _candidate(
    *, enforce_history_gates: bool = True, sparse_history: bool = False
) -> WeeklyPlanCandidate:
    return compose_week(
        WeeklyPlannerSnapshot(
            week_start=MONDAY,
            as_of=date(2026, 8, 26),
            availability=(
                DayAvailability(weekday=0, available_minutes=60),
                DayAvailability(weekday=2, available_minutes=75),
                DayAvailability(weekday=6, available_minutes=120),
            ),
            preferred_long_run_weekday=6,
            experience_level="intermediate",
            effective_reentry=False,
            goals=(GoalSummary(event_type="10k", status="active", target_date=None),),
            baseline_confidence="medium",
            typical_weekly_runs_median=1.0 if sparse_history else 3.0,
            observed_runs_per_week=1.0 if sparse_history else 3.0,
            consistent_running_weeks=1 if sparse_history else 4,
            longest_run_28d_seconds=4800,
            typical_longest_run_seconds=4200,
            median_run_seconds=2700,
            hard_runs_28d=1,
            intensity_mode="rpe_talk_test",
            intensity_confidence="medium",
            baseline_fingerprint="b" * 64,
            intensity_fingerprint="i" * 64,
            knowledge_base_version=get_knowledge_registry().version,
        ),
        enforce_history_gates=enforce_history_gates,
    )


def _user(session: Session) -> User:
    user = User(display_name="Plan Runner")
    session.add(user)
    session.flush()
    return user


def test_persist_week_creates_plan_revision_and_normal_workout_proposals(
    session_factory,
) -> None:
    candidate = _candidate()
    with session_factory() as session:
        user = _user(session)
        revision = persist_week_candidate(session, user, candidate)

        plan = session.scalar(select(TrainingPlan))
        assert plan is not None
        assert plan.current_revision_id == revision.id
        assert revision.revision_number == 1
        memberships = list(
            session.scalars(select(TrainingPlanWorkout).order_by(TrainingPlanWorkout.position))
        )
        workouts = list(session.scalars(select(Workout).order_by(Workout.id)))
        assert len(memberships) == len(workouts) == len(candidate.sessions)
        assert [membership.scheduled_for for membership in memberships] == [
            item.scheduled_for for item in candidate.sessions
        ]
        assert all(workout.approval_status == "proposed" for workout in workouts)
        assert all(workout.scheduled_for is None for workout in workouts)
        assert all(workout.source_type == "coach_weekly_plan" for workout in workouts)
        assert session.scalar(select(func.count()).select_from(WorkoutGarminOperation)) == 0


def test_persist_week_records_source_assistant_message(session_factory) -> None:
    with session_factory() as session:
        user = _user(session)
        conversation = CoachConversation(user_id=user.id, title="Training")
        session.add(conversation)
        session.flush()
        message = CoachMessage(
            conversation_id=conversation.id,
            role="assistant",
            content="Wochenplan",
        )
        session.add(message)
        session.flush()

        revision = persist_week_candidate(
            session,
            user,
            _candidate(),
            source_assistant_message_id=message.id,
        )

        assert revision.source_assistant_message_id == message.id


def test_persist_week_in_transaction_leaves_rollback_to_caller(session_factory) -> None:
    candidate = _candidate()
    with session_factory() as session:
        user = _user(session)
        session.commit()

        persist_week_candidate_in_transaction(session, user, candidate)

        assert session.scalar(select(func.count()).select_from(TrainingPlan)) == 1
        assert session.scalar(select(func.count()).select_from(TrainingPlanRevision)) == 1
        assert session.scalar(select(func.count()).select_from(TrainingPlanWorkout)) == len(
            candidate.sessions
        )
        assert session.scalar(select(func.count()).select_from(Workout)) == len(candidate.sessions)

        session.rollback()

        assert session.scalar(select(func.count()).select_from(TrainingPlan)) == 0
        assert session.scalar(select(func.count()).select_from(TrainingPlanRevision)) == 0
        assert session.scalar(select(func.count()).select_from(TrainingPlanWorkout)) == 0
        assert session.scalar(select(func.count()).select_from(Workout)) == 0


def test_persist_week_accepts_candidate_with_bypassed_history_gates(session_factory) -> None:
    candidate = _candidate(enforce_history_gates=False, sparse_history=True)
    with session_factory() as session:
        user = _user(session)

        persist_week_candidate(session, user, candidate)

        roles = set(session.scalars(select(TrainingPlanWorkout.role)))
        assert "long_run" in roles
        assert len(roles) == 2


def test_persist_week_is_idempotent_and_calendar_is_user_scoped(session_factory) -> None:
    candidate = _candidate()
    with session_factory() as session:
        user = _user(session)
        other = User(display_name="Other Runner")
        session.add(other)
        session.flush()

        first = persist_week_candidate(session, user, candidate)
        second = persist_week_candidate(session, user, candidate)

        assert first.id == second.id
        assert session.scalar(select(func.count()).select_from(TrainingPlanRevision)) == 1
        assert session.scalar(select(func.count()).select_from(Workout)) == len(candidate.sessions)
        assert len(
            plan_proposals_between(session, user.id, MONDAY, MONDAY + timedelta(days=6))
        ) == len(candidate.sessions)
        assert plan_proposals_between(session, other.id, MONDAY, MONDAY + timedelta(days=6)) == []


def test_idempotent_week_persistence_does_not_commit_unrelated_changes(session_factory) -> None:
    candidate = _candidate()
    with session_factory() as session:
        user = _user(session)
        persist_week_candidate(session, user, candidate)
        session.add(User(display_name="Pending Runner"))

        persist_week_candidate(session, user, candidate)
        session.rollback()

        assert session.scalar(select(func.count()).select_from(User)) == 1


def test_new_revision_is_current_without_mutating_previous_revision(session_factory) -> None:
    candidate = _candidate()
    changed = replace(candidate, input_fingerprint="c" * 64)
    with session_factory() as session:
        user = _user(session)
        first = persist_week_candidate(session, user, candidate)
        second = persist_week_candidate(session, user, changed)

        assert first.revision_number == 1
        assert second.revision_number == 2
        assert first.input_fingerprint == candidate.input_fingerprint
        plan = session.scalar(select(TrainingPlan))
        assert plan is not None
        assert plan.current_revision_id == second.id
        visible = plan_proposals_between(session, user.id, MONDAY, MONDAY + timedelta(days=6))
        assert {membership.plan_revision_id for membership, _ in visible} == {second.id}

        reactivated = persist_week_candidate(session, user, candidate)
        assert reactivated.id == first.id
        assert plan.current_revision_id == first.id
        visible = plan_proposals_between(session, user.id, MONDAY, MONDAY + timedelta(days=6))
        assert {membership.plan_revision_id for membership, _ in visible} == {first.id}


def test_persist_week_rolls_back_everything_when_one_workout_fails(session_factory) -> None:
    candidate = _candidate()
    invalid_session = replace(candidate.sessions[1], template_id="missing-template")
    invalid = replace(
        candidate,
        sessions=(candidate.sessions[0], invalid_session, *candidate.sessions[2:]),
        input_fingerprint="d" * 64,
    )
    with session_factory() as session:
        user = _user(session)
        session.commit()
        with pytest.raises(ValueError):
            persist_week_candidate(session, user, invalid)

        assert session.scalar(select(func.count()).select_from(TrainingPlan)) == 0
        assert session.scalar(select(func.count()).select_from(TrainingPlanRevision)) == 0
        assert session.scalar(select(func.count()).select_from(TrainingPlanWorkout)) == 0
        assert session.scalar(select(func.count()).select_from(Workout)) == 0


def test_plan_revisions_and_memberships_are_immutable(session_factory) -> None:
    with session_factory() as session:
        user = _user(session)
        persisted = persist_week_candidate(session, user, _candidate())
        persisted.planner_version = "rewritten"
        with pytest.raises(ValueError, match="immutable"):
            session.flush()
        session.rollback()

        with pytest.raises(IntegrityError, match="immutable"):
            session.execute(
                update(TrainingPlanRevision)
                .where(TrainingPlanRevision.id == persisted.id)
                .values(planner_version="rewritten")
            )
        session.rollback()

        membership = session.scalar(select(TrainingPlanWorkout))
        assert membership is not None
        with pytest.raises(IntegrityError, match="immutable"):
            session.execute(
                update(TrainingPlanWorkout)
                .where(TrainingPlanWorkout.id == membership.id)
                .values(role="rewritten")
            )
        session.rollback()


def test_weekly_workout_lifecycle_uses_plan_and_garmin_flags(session_factory, monkeypatch) -> None:
    with session_factory() as session:
        user = _user(session)
        persist_week_candidate(session, user, _candidate())
        workouts = list(session.scalars(select(Workout).order_by(Workout.id)))
        first, second = workouts[:2]
        first_revision = session.get(WorkoutRevision, first.current_revision_id)
        assert first_revision is not None
        identity = RevisionIdentity(
            revision_id=first_revision.id,
            revision_number=first_revision.revision_number,
            content_hash=first_revision.content_hash,
            lock_version=first.lock_version,
        )
        service = WorkoutService(session, user)

        monkeypatch.setattr(get_settings(), "coach_plan_generation_enabled", False)
        with pytest.raises(WorkoutTransitionError) as disabled:
            service.accept(first.id, AcceptRevisionCommand(identity, "unused"))
        assert disabled.value.code == "plan.feature_disabled"

        monkeypatch.setattr(get_settings(), "coach_plan_generation_enabled", True)
        data = WorkoutInput(
            name=first_revision.name,
            sport=first_revision.sport,
            scheduled_for=first_revision.suggested_for,
            description=first_revision.description,
            definition=parse_definition(
                first_revision.definition, first_revision.definition_version
            ),
            definition_version=first_revision.definition_version,
        )
        with pytest.raises(WorkoutTransitionError) as edit_blocked:
            service.update(first.id, data)
        assert edit_blocked.value.code == "plan.edit_not_supported"

        view = workout_detail_view(session, first)
        accepted = service.accept(
            first.id,
            AcceptRevisionCommand(
                identity=identity,
                context_fingerprint=view.current.context_fingerprint,
            ),
        )
        assert accepted.approval_status == "accepted"
        assert all(
            workout.id != first.id
            for _, workout in plan_proposals_between(
                session, user.id, MONDAY, MONDAY + timedelta(days=6)
            )
        )

        monkeypatch.setattr(get_settings(), "coach_garmin_sync_enabled", False)
        with pytest.raises(WorkoutTransitionError) as sync_disabled:
            service.publish(first.id)
        assert sync_disabled.value.code == "coach.garmin_sync_disabled"

        second_revision = session.get(WorkoutRevision, second.current_revision_id)
        assert second_revision is not None
        rejected = service.reject(
            second.id,
            RejectRevisionCommand(
                RevisionIdentity(
                    revision_id=second_revision.id,
                    revision_number=second_revision.revision_number,
                    content_hash=second_revision.content_hash,
                    lock_version=second.lock_version,
                )
            ),
        )
        assert rejected.approval_status == "rejected"

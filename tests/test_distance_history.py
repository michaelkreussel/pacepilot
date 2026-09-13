from datetime import datetime, timedelta

import pytest

from app.models import Activity, AthleteGoal, WorkoutRevision
from app.services.planning.daily_recommendation import (
    automatic_interval_parameters,
    recommend_today,
)
from app.services.planning.workout_definition import estimated_duration_seconds
from app.services.planning.workout_proposals import RunningProposalRequest, RunningProposalService
from app.services.planning.workout_service import WorkoutService
from app.services.planning.workout_templates import TemplateParameters
from tests.test_daily_recommendation import DAY, runner
from tests.test_interval_variety import prepared_runner


def test_scheduled_easy_run_does_not_become_quality_work(session_factory):
    with session_factory() as session:
        user = runner(session)
        saved = RunningProposalService(session, user, as_of=DAY).create(
            RunningProposalRequest(
                template_id="easy_run",
                suggested_for=DAY,
                available_minutes=30,
                idempotency_key="scheduled-easy-target",
            )
        )
        saved.accepted_revision_id = saved.current_revision_id
        saved.approval_status = "accepted"
        saved.scheduled_for = DAY
        saved.local_schedule_status = "scheduled"
        session.flush()
        assert recommend_today(session, user, as_of=DAY).work_seconds == 0


def test_completed_distance_dose_remains_eligible_without_increasing_work(session_factory):
    with session_factory() as session:
        user = prepared_runner(session)
        service = RunningProposalService(session, user, as_of=DAY)
        data, metadata = service.build_candidate(
            template_id="threshold_cruise",
            suggested_for=DAY,
            available_minutes=49,
            edit_source="generator",
            work_distance_meters=1000,
        )
        saved = WorkoutService(session, user).create_proposal(
            data,
            metadata,
            idempotency_key="distance-history",
            request_fingerprint="distance-history",
        )
        revision = session.get(WorkoutRevision, saved.current_revision_id)
        assert revision is not None
        parameters = automatic_interval_parameters("threshold_cruise", 60, comparable=[revision])
        assert parameters is not None
        next_data, _ = service.build_candidate(
            template_id="threshold_cruise",
            suggested_for=DAY + timedelta(days=7),
            available_minutes=60,
            edit_source="generator",
            parameters=parameters,
        )
        assert estimated_duration_seconds(next_data.definition, work_only=True) >= 900
        assert estimated_duration_seconds(
            next_data.definition, work_only=True
        ) <= estimated_duration_seconds(data.definition, work_only=True)


@pytest.mark.parametrize("completed", [False, True])
def test_partial_distance_run_cannot_become_successful_dose_evidence(session_factory, completed):
    from app.models import PerformanceAnchor
    from tests.test_running_baseline import _complete_activity_history

    with session_factory() as session:
        user = runner(session, 60, 5)
        old_day = DAY - timedelta(days=10)
        session.add(_complete_activity_history(user.id, old_day))
        session.add(
            PerformanceAnchor(
                user_id=user.id,
                kind="race",
                source="manual",
                achieved_on=old_day - timedelta(days=5),
                distance_m=5000,
                duration_s=1500,
                reliable=True,
            )
        )
        session.add(AthleteGoal(user_id=user.id, event_type="10k"))
        session.flush()
        data, metadata = RunningProposalService(session, user, as_of=old_day).build_candidate(
            template_id="threshold_cruise",
            suggested_for=old_day,
            available_minutes=90,
            edit_source="generator",
            work_distance_meters=1000,
            parameters=TemplateParameters(repetitions=6, work_minutes=6, vary_structure=True),
        )
        service = WorkoutService(session, user)
        saved = service.create_proposal(
            data,
            metadata,
            idempotency_key="partial-distance",
            request_fingerprint="partial-distance",
        )
        revision = session.get(WorkoutRevision, saved.current_revision_id)
        assert revision is not None
        saved.accepted_revision_id = revision.id
        saved.approval_status = "accepted"
        session.add(
            Activity(
                user_id=user.id,
                garmin_activity_id="partial-intervals",
                name="Intervalllauf",
                activity_type="running",
                workout_id=saved.id,
                started_at=datetime.combine(old_day, datetime.min.time()),
                duration_s=estimated_duration_seconds(data.definition) if completed else 2200,
                workout_rpe=7,
                workout_feel=4,
            )
        )
        session.flush()
        result = recommend_today(session, user, as_of=DAY, available_minutes=90)
        assert result.template_id == "threshold_cruise"
        if completed:
            assert result.work_seconds > 900
        else:
            assert result.work_seconds == 900

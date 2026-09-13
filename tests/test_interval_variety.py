from datetime import timedelta

import pytest

from app.models import PerformanceAnchor, Workout
from app.services.garmin.workout_export import compile_workout_with_report
from app.services.planning.load_estimate import LoadEstimate
from app.services.planning.workout_definition import (
    DistanceEnd,
    PaceRangeTarget,
    RepeatBlockV2,
    StepBlockV2,
    TimeEnd,
)
from app.services.planning.workout_proposals import RunningProposalService
from app.services.planning.workout_service import WorkoutService
from tests.test_daily_recommendation import DAY, runner
from tests.test_running_baseline import _complete_activity_history


def prepared_runner(session):
    user = runner(session, 55, 4)
    session.add(_complete_activity_history(user.id, DAY))
    session.add(
        PerformanceAnchor(
            user_id=user.id,
            kind="race",
            source="manual",
            achieved_on=DAY - timedelta(days=5),
            distance_m=5000,
            duration_s=1500,
            reliable=True,
        )
    )
    session.flush()
    return user


@pytest.mark.parametrize(
    "distance,template",
    [(400, "vo2_intervals"), (800, "vo2_intervals"), (1000, "threshold_cruise")],
)
def test_requested_distance_and_personal_pace_survive_persistence_and_export(
    session_factory, distance, template
):
    with session_factory() as session:
        user = prepared_runner(session)
        data, metadata = RunningProposalService(session, user, as_of=DAY).build_candidate(
            template_id=template,
            suggested_for=DAY,
            available_minutes=60,
            edit_source="generator",
            work_distance_meters=distance,
        )
        repeat = data.definition.blocks[1]
        assert isinstance(repeat, RepeatBlockV2)
        work = repeat.children[0]
        assert isinstance(work, StepBlockV2)
        assert isinstance(work.end, DistanceEnd) and work.end.meters == distance
        assert isinstance(work.target, PaceRangeTarget)
        assert work.target.fastest_seconds_per_km >= 300
        load = LoadEstimate.model_validate(metadata.load_estimate_json)
        assert 30 * 60 < load.duration_seconds <= 3600
        saved = WorkoutService(session, user).create_proposal(
            data,
            metadata,
            idempotency_key=f"distance-test-{distance}",
            request_fingerprint=f"distance-{distance}",
        )
        assert isinstance(saved, Workout)
        assert saved.definition_model == data.definition
        exported = compile_workout_with_report(saved)
        assert not any(w.code == "garmin.rpe_target_degraded" for w in exported.warnings)
        steps = exported.payload["workoutSegments"][0]["workoutSteps"]
        exported_work = steps[1]["workoutSteps"][0]
        assert exported_work["endConditionValue"] == distance
        assert exported_work["endCondition"]["conditionTypeKey"] == "distance"
        assert exported_work["targetValueOne"] == pytest.approx(
            1000 / work.target.fastest_seconds_per_km
        )


def test_automatic_quality_varies_across_weeks_but_same_preview_is_stable(session_factory):
    with session_factory() as session:
        user = prepared_runner(session)
        service = RunningProposalService(session, user, as_of=DAY)
        definitions = []
        for week in range(5):
            day = DAY + timedelta(weeks=week)
            data, metadata = service.build_candidate(
                template_id="vo2_intervals",
                suggested_for=day,
                available_minutes=65,
                edit_source="generator",
            )
            repeat = data.definition.blocks[1]
            assert isinstance(repeat, RepeatBlockV2)
            work = repeat.children[0]
            assert isinstance(work, StepBlockV2)
            definitions.append(work.end.model_dump_json())
            assert (
                service.build_candidate(
                    template_id="vo2_intervals",
                    suggested_for=day,
                    available_minutes=65,
                    edit_source="generator",
                )[0].definition
                == data.definition
            )
            assert (
                LoadEstimate.model_validate(metadata.load_estimate_json).duration_seconds <= 65 * 60
            )
            warmup = data.definition.blocks[0]
            assert isinstance(warmup, StepBlockV2) and isinstance(warmup.end, TimeEnd)
            assert warmup.end.seconds in (600, 900)
        assert len(set(definitions)) >= 3


@pytest.mark.parametrize("template", ["easy_run", "long_run", "recovery_run"])
def test_endurance_runs_have_performance_based_watch_targets(session_factory, template):
    with session_factory() as session:
        user = prepared_runner(session)
        data, metadata = RunningProposalService(session, user, as_of=DAY).build_candidate(
            template_id=template,
            suggested_for=DAY,
            available_minutes=70,
            edit_source="generator",
        )
        step = data.definition.blocks[0]
        assert isinstance(step, StepBlockV2)
        target = step.target
        assert isinstance(target, PaceRangeTarget)
        assert target.fastest_seconds_per_km > 300
        assert metadata.guidance_json is not None
        pace = metadata.guidance_json["pace_target"]
        assert isinstance(pace, dict) and pace["source"] == "race_performance"


def test_distance_requires_evidence_and_rejects_impossible_budget(session_factory):
    from app.services.planning.workout_templates import TemplateExpansionError

    with session_factory() as session:
        user = runner(session)
        with pytest.raises(TemplateExpansionError, match="Leistungsdaten"):
            RunningProposalService(session, user, as_of=DAY).build_candidate(
                template_id="vo2_intervals",
                suggested_for=DAY,
                available_minutes=60,
                edit_source="generator",
                work_distance_meters=400,
            )
        prepared = prepared_runner(session)
        with pytest.raises(TemplateExpansionError):
            RunningProposalService(session, prepared, as_of=DAY).build_candidate(
                template_id="vo2_intervals",
                suggested_for=DAY,
                available_minutes=20,
                edit_source="generator",
                work_distance_meters=400,
            )


def test_reducing_distance_intervals_preserves_preparation_and_rep_pace(session_factory):
    from app.services.planning.daily_adaptation import reduce_volume
    from app.services.planning.workout_definition import estimated_duration_seconds
    from app.services.planning.workout_templates import TemplateParameters

    with session_factory() as session:
        user = prepared_runner(session)
        data, metadata = RunningProposalService(session, user, as_of=DAY).build_candidate(
            template_id="threshold_cruise",
            suggested_for=DAY,
            available_minutes=90,
            edit_source="generator",
            work_distance_meters=1000,
            parameters=TemplateParameters(repetitions=6, work_minutes=6, vary_structure=True),
        )
        load = LoadEstimate.model_validate(metadata.load_estimate_json)
        reduced = reduce_volume(
            data.definition,
            template_id="threshold_cruise",
            estimated_duration_seconds=load.duration_seconds,
        )
        assert reduced.blocks[0] == data.definition.blocks[0]
        assert reduced.blocks[-1] == data.definition.blocks[-1]
        smaller, original = reduced.blocks[1], data.definition.blocks[1]
        assert isinstance(smaller, RepeatBlockV2) and isinstance(original, RepeatBlockV2)
        assert smaller.children == original.children
        assert smaller.iterations < original.iterations
        assert estimated_duration_seconds(reduced) <= load.duration_seconds * 0.75


def test_week_persistence_keeps_paced_distance_preview_and_estimated_duration(session_factory):
    from dataclasses import replace

    from sqlalchemy import select

    from app.services.planning.weekly_plan_service import persist_week_candidate
    from app.services.planning.weekly_planner import _session_from_preview
    from tests.test_weekly_plan_service import _candidate

    with session_factory() as session:
        user = prepared_runner(session)
        candidate = _candidate()
        candidate = replace(candidate, week_start=DAY + timedelta(days=3))
        original = replace(candidate.sessions[0], scheduled_for=candidate.week_start)
        data, metadata = RunningProposalService(session, user, as_of=DAY).build_candidate(
            template_id="vo2_intervals",
            suggested_for=original.scheduled_for,
            available_minutes=60,
            edit_source="generator",
            work_distance_meters=400,
        )
        item = _session_from_preview(original, data, metadata, "Distanzintervalle")
        assert (
            item.planned_minutes * 60
            >= LoadEstimate.model_validate(metadata.load_estimate_json).duration_seconds
        )
        persist_week_candidate(session, user, replace(candidate, sessions=(item,)))
        saved = session.scalar(select(Workout).where(Workout.user_id == user.id))
        assert saved.definition_model == data.definition

from datetime import timedelta

import pytest

from app.models import DailyFitness, PerformanceAnchor
from app.services.analytics.athlete_data import AthleteDataService
from app.services.analytics.running_intensity import (
    PerformanceAnchorInput,
    PerformanceAnchorLike,
    _critical_speed,
    workout_pace_guidance,
)
from app.services.planning.workout_definition import RepeatBlockV2, StepBlockV2
from app.services.planning.workout_proposals import RunningProposalService
from tests.test_daily_recommendation import DAY, runner
from tests.test_running_baseline import _complete_activity_history


@pytest.mark.parametrize(
    "durations,days",
    [
        ((60, 600), (1, 3)),
        ((300, 900), (1, 1)),
        ((300, 900), (1, 50)),
        ((300, 310), (1, 2)),
        ((1200, 2700), (1, 5)),
    ],
)
def test_unsuitable_cs_pairs_are_unavailable(durations, days):
    anchors: list[tuple[PerformanceAnchorLike, float]] = [
        (
            PerformanceAnchorInput("race", DAY - timedelta(days=age), duration * 4 + 200, duration),
            (duration * 4 + 200) / duration,
        )
        for duration, age in zip(durations, days, strict=True)
    ]
    assert not _critical_speed(anchors, "high", as_of=DAY).available


def test_single_performance_is_not_cs():
    assert not _critical_speed(
        [(PerformanceAnchorInput("race", DAY, 5000, 1200), 5000 / 1200)], "high", as_of=DAY
    ).available


def test_stored_anchors_become_distance_aware_pace_targets(session_factory):
    with session_factory() as session:
        user = runner(session, 50, 4)
        session.add(_complete_activity_history(user.id, DAY))
        session.add(
            PerformanceAnchor(
                user_id=user.id,
                kind="race",
                source="manual",
                achieved_on=DAY - timedelta(days=5),
                distance_m=5000,
                duration_s=1200,
                reliable=True,
            )
        )
        session.flush()
        data, metadata = RunningProposalService(session, user, as_of=DAY).build_candidate(
            template_id="threshold_cruise",
            suggested_for=DAY,
            available_minutes=70,
            edit_source="generator",
        )
        assert metadata.guidance_json is not None
        pace = metadata.guidance_json["pace_target"]
        assert isinstance(pace, dict)
        assert pace["source"] == "race_performance"
        assert pace["fastest_seconds_per_km"] > 240
        assert pace["slowest_seconds_per_km"] > pace["fastest_seconds_per_km"]
        repeat = data.definition.blocks[1]
        assert isinstance(repeat, RepeatBlockV2)
        work = repeat.children[0]
        assert isinstance(work, StepBlockV2)
        assert work.target.type == "pace_range"
        assert any("RPE" in text for text in work.instructions)


def test_arbitrary_short_pr_does_not_displace_threshold(session_factory):
    with session_factory() as session:
        user = runner(session, 50, 4)
        session.add(_complete_activity_history(user.id, DAY))
        session.add(DailyFitness(user_id=user.id, day=DAY, lactate_threshold_speed_mps=3.5))
        session.flush()
        shadow = AthleteDataService(session, user.id, as_of=DAY).get_running_shadow_analysis(
            performance_anchors=(PerformanceAnchorInput("manual", DAY, 1000, 200, source="garmin"),)
        )
        assert shadow.intensity.primary_source == "garmin_lactate_threshold"
        assert workout_pace_guidance(shadow.intensity, "vo2_intervals") is None


def test_recent_pr_is_labeled_and_fresh_threshold_has_priority(session_factory):
    with session_factory() as session:
        user = runner(session, 50, 4)
        session.add(_complete_activity_history(user.id, DAY))
        session.flush()
        anchors = (
            PerformanceAnchorInput("manual", DAY - timedelta(days=12), 5000, 1500, source="garmin"),
        )
        athlete = AthleteDataService(session, user.id, as_of=DAY)
        guidance = athlete.get_running_shadow_analysis(performance_anchors=anchors).intensity
        target = workout_pace_guidance(guidance, "threshold_cruise")
        assert target is not None
        assert target["source"] == "garmin_personal_record"
        assert target["confidence"] == "low"
        assert target["label"] == "Garmin-Bestzeit (kein bestätigter Maximaltest)"
        session.add(DailyFitness(user_id=user.id, day=DAY, lactate_threshold_speed_mps=3.4))
        session.flush()
        assert (
            athlete.get_running_shadow_analysis(
                performance_anchors=anchors
            ).intensity.primary_source
            == "garmin_lactate_threshold"
        )


@pytest.mark.parametrize(
    "distance,duration", [(float("nan"), 1200), (5000, float("inf")), (5000, -5), (5000, 1)]
)
def test_nonfinite_or_implausible_performances_never_supply_targets(
    session_factory, distance, duration
):
    with session_factory() as session:
        user = runner(session, 50, 4)
        session.add(_complete_activity_history(user.id, DAY))
        session.flush()
        guidance = (
            AthleteDataService(session, user.id, as_of=DAY)
            .get_running_shadow_analysis(
                performance_anchors=(PerformanceAnchorInput("race", DAY, distance, duration),)
            )
            .intensity
        )
        assert guidance.pace_anchor is None
        assert workout_pace_guidance(guidance, "threshold_cruise") is None

from datetime import date, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from app.models import (
    Activity,
    AthleteAvailability,
    AthleteGoal,
    AthletePlanningProfile,
    GarminSyncState,
    PerformanceAnchor,
    TrainingCycle,
    TrainingCycleRevision,
    User,
)
from app.services.analytics import AthleteDataService
from app.services.planning.planning_commands import (
    AcceptedCycleReference,
    AvailabilityInput,
    GoalCreateInput,
    GoalUpdateInput,
    PerformanceAnchorCreateInput,
    PerformanceAnchorUpdateInput,
    PlanningInputCommandError,
    PlanningInputCommands,
    PlanningProfileUpdateInput,
    ReferencedGoalChangeConfirmation,
)
from app.services.planning.planning_queries import (
    get_active_goal,
    get_planning_inputs,
    list_performance_anchors,
)


def _user(session, name: str = "Planner") -> User:
    user = User(display_name=name)
    session.add(user)
    session.flush()
    return user


def _history(session, user_id: int, as_of: date) -> None:
    for index, age in enumerate((0, 7, 14, 21, 28, 35)):
        session.add(
            Activity(
                user_id=user_id,
                garmin_activity_id=f"planning-run-{index}",
                name=f"Run {index}",
                activity_type="running",
                started_at=datetime.combine(as_of - timedelta(days=age), datetime.min.time()),
                duration_s=2400,
                distance_m=6000,
                aerobic_training_effect=2.0,
                workout_rpe=3,
            )
        )
    session.add(
        GarminSyncState(
            user_id=user_id,
            resource="activities",
            status="ok",
            backfill_complete=True,
            oldest_synced_date=as_of - timedelta(days=365),
            newest_synced_date=as_of,
        )
    )
    session.flush()


def test_planning_inputs_are_user_scoped_and_validated(session_factory) -> None:
    with session_factory() as session:
        first = _user(session, "First")
        second = _user(session, "Second")
        session.add_all(
            [
                AthletePlanningProfile(
                    user_id=first.id,
                    experience_level="intermediate",
                    preferred_long_run_weekday=5,
                ),
                AthletePlanningProfile(user_id=second.id, experience_level=None),
                AthleteGoal(
                    user_id=first.id,
                    event_type="10k",
                    event_name="Stadtlauf",
                    target_date=date(2026, 10, 11),
                ),
                AthleteGoal(user_id=first.id, event_type="half_marathon"),
                AthleteGoal(user_id=second.id, event_type="general_fitness"),
                AthleteAvailability(
                    user_id=first.id, weekday=0, available=True, available_minutes=60
                ),
                AthleteAvailability(
                    user_id=first.id, weekday=5, available=True, available_minutes=120
                ),
                AthleteAvailability(user_id=second.id, weekday=0, available=False),
                PerformanceAnchor(
                    user_id=first.id,
                    kind="race",
                    distance_m=10_000,
                    duration_s=2_700,
                    achieved_on=date(2026, 6, 15),
                ),
                PerformanceAnchor(
                    user_id=first.id,
                    kind="time_trial",
                    distance_m=5_000,
                    duration_s=1_350,
                    achieved_on=date(2026, 7, 20),
                    reliable=False,
                    notes="Kopfwind",
                ),
            ]
        )
        session.commit()

        first_inputs = get_planning_inputs(session, first.id, as_of=date(2026, 8, 1))
        second_inputs = get_planning_inputs(session, second.id, as_of=date(2026, 8, 1))

        assert first_inputs.as_of == date(2026, 8, 1)
        assert first_inputs.profile is not None
        assert first_inputs.profile.experience_level == "intermediate"
        assert [goal.event_type for goal in first_inputs.goals] == [
            "half_marathon",
            "10k",
        ]
        assert [slot.weekday for slot in first_inputs.availability] == [0, 5]
        assert [anchor.kind for anchor in first_inputs.performance_anchors] == [
            "race",
            "time_trial",
        ]
        assert second_inputs.profile is not None
        assert second_inputs.profile.experience_level is None
        assert [goal.event_type for goal in second_inputs.goals] == ["general_fitness"]
        assert second_inputs.availability == ()
        assert second_inputs.performance_anchors == ()
        assert get_active_goal(session, first.id, event_type="10k") == first_inputs.goals[1]
        assert get_active_goal(session, first.id, goal_id=second_inputs.goals[0].id) is None

        session.add(
            AthleteAvailability(user_id=first.id, weekday=0, available=True, available_minutes=45)
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    for invalid in (
        lambda: AthleteAvailability(user_id=1, weekday=7, available=False),
        lambda: AthleteAvailability(user_id=1, weekday=0, available=True),
        lambda: AthleteAvailability(user_id=1, weekday=0, available=True, available_minutes=0),
        lambda: AthleteAvailability(user_id=1, weekday=0, available=True, available_minutes=2000),
        lambda: AthleteGoal(user_id=1, event_type="ultra"),
        lambda: AthleteGoal(user_id=1, event_type="10k", status="deleted"),
        lambda: AthletePlanningProfile(user_id=1, preferred_long_run_weekday=8),
        lambda: AthletePlanningProfile(user_id=1, experience_level="elite"),
        lambda: PerformanceAnchor(
            user_id=1,
            kind="swim",
            distance_m=1000,
            duration_s=1000,
            achieved_on=date(2026, 1, 1),
        ),
        lambda: PerformanceAnchor(
            user_id=1,
            kind="race",
            distance_m=0,
            duration_s=1000,
            achieved_on=date(2026, 1, 1),
        ),
        lambda: PerformanceAnchor(
            user_id=1,
            kind="race",
            distance_m=1000,
            duration_s=-5,
            achieved_on=date(2026, 1, 1),
        ),
    ):
        with session_factory() as session:
            session.add(invalid())
            with pytest.raises(IntegrityError):
                session.commit()


def test_persisted_anchors_feed_intensity_guidance(session_factory) -> None:
    as_of = date.today()
    with session_factory() as session:
        user = _user(session)
        _history(session, user.id, as_of)
        session.add(
            PerformanceAnchor(
                user_id=user.id,
                kind="race",
                distance_m=5_000,
                duration_s=1_500,
                achieved_on=as_of - timedelta(days=30),
            )
        )
        session.commit()

        shadow = AthleteDataService(session, user.id, as_of=as_of).get_running_shadow_analysis(
            performance_anchors=list_performance_anchors(session, user.id)
        )

        assert shadow.intensity.mode == "pace_anchor"
        assert shadow.intensity.pace_anchor is not None
        assert shadow.intensity.pace_anchor.source == "race_performance"
        assert shadow.intensity.pace_anchor.speed_mps == pytest.approx(5000 / 1500, abs=0.01)


def test_stale_anchor_is_ignored_with_warning(session_factory) -> None:
    as_of = date.today()
    with session_factory() as session:
        user = _user(session)
        _history(session, user.id, as_of)
        session.add(
            PerformanceAnchor(
                user_id=user.id,
                kind="race",
                distance_m=5_000,
                duration_s=1_500,
                achieved_on=as_of - timedelta(days=200),
            )
        )
        session.commit()
        shadow = AthleteDataService(session, user.id, as_of=as_of).get_running_shadow_analysis(
            performance_anchors=list_performance_anchors(session, user.id)
        )

        assert shadow.intensity.pace_anchor is None
        assert "performance_anchor_outside_180_day_window" in shadow.intensity.warnings


def test_planning_commands_persist_clear_changes_and_return_facts(session_factory) -> None:
    as_of = date(2026, 8, 1)
    with session_factory() as session:
        user = _user(session)
        session.commit()
        commands = PlanningInputCommands(session, user, as_of=as_of)

        profile = commands.update_profile(
            PlanningProfileUpdateInput(
                experience_level="intermediate",
                preferred_long_run_weekday=5,
                self_declared_reentry=True,
                constraint_note="Freitags keine langen Läufe",
            )
        )
        goal = commands.create_goal(
            GoalCreateInput(
                event_type="10k",
                event_name="Herbstlauf",
                target_date=date(2026, 10, 11),
            )
        )
        availability = commands.set_availability(
            AvailabilityInput(weekday=5, available=True, available_minutes=120)
        )
        anchor = commands.create_performance_anchor(
            PerformanceAnchorCreateInput(
                kind="race",
                distance_m=10_000,
                duration_s=2_700,
                achieved_on=date(2026, 7, 20),
                notes="Flache Strecke",
            )
        )

        assert profile.experience_level == "intermediate"
        assert profile.constraint_note == "Freitags keine langen Läufe"
        assert goal.event_name == "Herbstlauf"
        assert availability.available_minutes == 120
        assert anchor.kind == "race"
        assert anchor.notes == "Flache Strecke"

        updated_goal = commands.update_goal(
            goal.id,
            GoalUpdateInput(event_name="Herbstlauf 10 km", target_date=date(2026, 10, 18)),
        )
        updated_profile = commands.update_profile(
            PlanningProfileUpdateInput(self_declared_reentry=False)
        )
        updated_availability = commands.set_availability(
            AvailabilityInput(weekday=5, available=True, available_minutes=90)
        )
        updated_anchor = commands.update_performance_anchor(
            anchor.id,
            PerformanceAnchorUpdateInput(reliable=False, notes="GPS ungenau"),
        )

        assert updated_goal.event_name == "Herbstlauf 10 km"
        assert updated_goal.target_date == date(2026, 10, 18)
        assert updated_profile.self_declared_reentry is False
        assert updated_availability.available_minutes == 90
        assert updated_anchor.reliable is False
        assert updated_anchor.notes == "GPS ungenau"

        unavailable = commands.deactivate_availability(weekday=5)
        deactivated_anchor = commands.deactivate_performance_anchor(anchor.id)
        deactivated_goal = commands.deactivate_goal(goal.id)

        assert unavailable.available is False
        assert unavailable.available_minutes is None
        assert deactivated_anchor.reliable is False
        assert deactivated_goal.status == "archived"

    with session_factory() as session:
        persisted = get_planning_inputs(session, user.id, as_of=as_of)
        assert persisted.profile is not None
        assert persisted.profile.self_declared_reentry is False
        assert persisted.goals == ()
        assert persisted.availability == ()
        assert persisted.performance_anchors[0].reliable is False


def test_planning_commands_reject_invalid_dates_and_unavailable_fields(session_factory) -> None:
    as_of = date(2026, 8, 1)
    with session_factory() as session:
        user = _user(session)
        session.commit()
        commands = PlanningInputCommands(session, user, as_of=as_of)

        with pytest.raises(PlanningInputCommandError) as missing_goal_date:
            commands.create_goal(GoalCreateInput(event_type="10k"))
        assert missing_goal_date.value.code == "planning.goal_target_date_required"

        with pytest.raises(PlanningInputCommandError) as past_goal:
            commands.create_goal(GoalCreateInput(event_type="10k", target_date=date(2026, 7, 31)))
        assert past_goal.value.code == "planning.goal_target_date_invalid"

        with pytest.raises(PlanningInputCommandError) as future_anchor:
            commands.create_performance_anchor(
                PerformanceAnchorCreateInput(
                    kind="race",
                    distance_m=5_000,
                    duration_s=1_500,
                    achieved_on=date(2026, 8, 2),
                )
            )
        assert future_anchor.value.code == "planning.anchor_date_invalid"

    with pytest.raises(ValidationError) as unavailable_minutes:
        AvailabilityInput(weekday=2, available=False, available_minutes=60)
    assert unavailable_minutes.value.errors()[0]["loc"] == ()

    with pytest.raises(ValidationError) as unavailable_field:
        GoalCreateInput.model_validate(
            {"event_type": "10k", "target_date": "2026-10-11", "sport": "cycling"}
        )
    assert unavailable_field.value.errors()[0]["type"] == "extra_forbidden"

    for invalid_update in (
        lambda: GoalUpdateInput(event_type=None),
        lambda: PlanningProfileUpdateInput(self_declared_reentry=None),
        lambda: PerformanceAnchorUpdateInput(distance_m=None),
    ):
        with pytest.raises(ValidationError):
            invalid_update()


def test_planning_command_ids_are_user_scoped(session_factory) -> None:
    with session_factory() as session:
        owner = _user(session, "Owner")
        other = _user(session, "Other")
        goal = AthleteGoal(user_id=owner.id, event_type="10k")
        anchor = PerformanceAnchor(
            user_id=owner.id,
            kind="race",
            distance_m=5_000,
            duration_s=1_500,
            achieved_on=date(2026, 7, 1),
        )
        session.add_all([goal, anchor])
        session.commit()
        commands = PlanningInputCommands(session, other, as_of=date(2026, 8, 1))

        with pytest.raises(PlanningInputCommandError) as missing_goal:
            commands.update_goal(goal.id, GoalUpdateInput(event_name="Fremdes Ziel"))
        assert missing_goal.value.code == "planning.goal_not_found"

        with pytest.raises(PlanningInputCommandError) as missing_anchor:
            commands.update_performance_anchor(
                anchor.id, PerformanceAnchorUpdateInput(notes="Fremder Anker")
            )
        assert missing_anchor.value.code == "planning.anchor_not_found"


def test_referenced_goal_changes_require_exact_accepted_cycle_confirmation(
    session_factory,
) -> None:
    with session_factory() as session:
        user = _user(session)
        goal = AthleteGoal(
            user_id=user.id,
            event_type="10k",
            target_date=date(2026, 10, 11),
        )
        session.add(goal)
        session.flush()
        cycle = TrainingCycle(
            user_id=user.id,
            goal_id=goal.id,
            event_type="10k",
            start_date=date(2026, 8, 17),
            target_date=date(2026, 10, 11),
        )
        session.add(cycle)
        session.flush()
        revision = TrainingCycleRevision(
            cycle_id=cycle.id,
            owner_user_id=user.id,
            revision_number=1,
            event_type="10k",
            start_date=cycle.start_date,
            target_date=cycle.target_date,
            planner_version="test",
            knowledge_base_version="test",
            input_fingerprint="a" * 64,
            confidence="high",
            phase_plan_json=[],
            assumptions_json={},
            impact_json={},
            validation_report_json={"valid": True},
        )
        session.add(revision)
        session.flush()
        cycle.current_revision_id = revision.id
        cycle.accepted_revision_id = revision.id
        session.commit()
        commands = PlanningInputCommands(session, user, as_of=date(2026, 8, 1))

        with pytest.raises(PlanningInputCommandError) as missing_confirmation:
            commands.update_goal(goal.id, GoalUpdateInput(target_date=date(2026, 10, 18)))
        assert missing_confirmation.value.code == "planning.goal_confirmation_required"
        session.refresh(goal)
        assert goal.target_date == date(2026, 10, 11)

        wrong_confirmation = ReferencedGoalChangeConfirmation(
            goal_id=goal.id,
            operation="update",
            accepted_cycles=(
                AcceptedCycleReference(
                    cycle_id=cycle.id,
                    accepted_revision_id=revision.id,
                ),
            ),
        )
        with pytest.raises(PlanningInputCommandError) as mismatched_confirmation:
            commands.deactivate_goal(goal.id, confirmation=wrong_confirmation)
        assert mismatched_confirmation.value.code == "planning.goal_confirmation_mismatch"

        exact_confirmation = ReferencedGoalChangeConfirmation(
            goal_id=goal.id,
            operation="update",
            accepted_cycles=(
                AcceptedCycleReference(
                    cycle_id=cycle.id,
                    accepted_revision_id=revision.id,
                ),
            ),
        )
        changed = commands.update_goal(
            goal.id,
            GoalUpdateInput(target_date=date(2026, 10, 18)),
            confirmation=exact_confirmation,
        )
        deactivation_confirmation = ReferencedGoalChangeConfirmation(
            goal_id=goal.id,
            operation="deactivate",
            accepted_cycles=exact_confirmation.accepted_cycles,
        )
        deactivated = commands.deactivate_goal(goal.id, confirmation=deactivation_confirmation)

        assert changed.target_date == date(2026, 10, 18)
        assert deactivated.status == "archived"
        assert session.get(TrainingCycle, cycle.id).accepted_revision_id == revision.id

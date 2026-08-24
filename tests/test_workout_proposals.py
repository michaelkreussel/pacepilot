import json
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.models import (
    Activity,
    GarminAccount,
    GarminSyncState,
    PreSessionFeedback,
    User,
    Workout,
    WorkoutEvent,
    WorkoutRevision,
    WorkoutValidationRun,
)
from app.models.user import utcnow
from app.repositories.workouts import workouts_between
from app.services.garmin.workout_export import compile_workout_with_report
from app.services.planning.validator import WorkoutInput
from app.services.planning.workout_definition import (
    HeartRateRangeTarget,
    RpeRangeTarget,
    StepBlockV2,
    TimeEnd,
)
from app.services.planning.workout_proposals import (
    EasyRunProposalRequest,
    RunningProposalService,
    WorkoutProposalError,
    _easy_run_device_target,
)
from app.services.planning.workout_revision import (
    AcceptRevisionCommand,
    RejectRevisionCommand,
    RevisionIdentity,
    ScheduleWorkoutCommand,
)
from app.services.planning.workout_service import (
    WorkoutConflictError,
    WorkoutService,
    WorkoutTransitionError,
)


def _user(session) -> User:
    user = User(display_name="Phase 8 Runner")
    session.add(user)
    session.flush()
    return user


def _history(session, user_id: int, as_of: date) -> None:
    for index, age in enumerate((0, 7, 14, 21, 28, 35)):
        session.add(
            Activity(
                user_id=user_id,
                garmin_activity_id=f"phase8-run-{index}",
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


def _request(*, minutes: int = 45, key: str = "phase8-request-1") -> EasyRunProposalRequest:
    return EasyRunProposalRequest(
        suggested_for=date.today() + timedelta(days=1),
        available_minutes=minutes,
        idempotency_key=key,
    )


def _identity(workout: Workout, revision: WorkoutRevision) -> RevisionIdentity:
    return RevisionIdentity(
        revision_id=revision.id,
        revision_number=revision.revision_number,
        content_hash=revision.content_hash,
        lock_version=workout.lock_version,
    )


def test_easy_run_proposal_is_deterministic_revisioned_and_unscheduled(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "coach_workout_proposals_enabled", True)
    with session_factory() as session:
        user = _user(session)
        _history(session, user.id, date.today())

        service = RunningProposalService(session, user)
        workout = service.create_easy_run(_request())
        repeated = service.create_easy_run(_request())
        revision = session.get(WorkoutRevision, workout.current_revision_id)

        assert repeated.id == workout.id
        assert revision is not None
        assert workout.source_type == "coach_single"
        assert workout.approval_status == "proposed"
        assert workout.accepted_revision_id is None
        assert workout.scheduled_for is None
        assert workout.local_schedule_status == "unscheduled"
        assert revision.suggested_for == date.today() + timedelta(days=1)
        assert revision.source_type == "coach_single"
        assert revision.edit_source == "generator"
        assert revision.template_id == "easy_run"
        assert revision.definition_version == 2
        assert revision.generation_context_json is not None
        assert revision.validation_report_json is not None
        assert revision.load_estimate_json == {
            "duration_seconds": 2700,
            "distance_meters": None,
            "time_by_intensity_domain_seconds": {"low": 2700, "moderate": 0, "high": 0},
            "mechanical_load": "low",
            "session_rpe": {"minimum": 2, "maximum": 3},
            "confidence": "moderate",
            "uncertainty": [
                "distance_unknown_for_time_based_workout",
                "individual_response_requires_baseline_validation",
            ],
        }
        step = revision.definition_model.blocks[0]
        assert isinstance(step, StepBlockV2)
        assert isinstance(step.end, TimeEnd)
        assert step.end.seconds == 2700
        assert isinstance(step.target, RpeRangeTarget)
        assert (
            workouts_between(session, user.id, date.today(), date.today() + timedelta(days=7)) == []
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(WorkoutEvent)
                .where(WorkoutEvent.workout_id == workout.id)
            )
            == 2
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(WorkoutValidationRun)
                .where(WorkoutValidationRun.workout_id == workout.id)
            )
            == 2
        )
        session.add(
            PreSessionFeedback(
                user_id=user.id,
                illness_signal="fever",
                pain_present=False,
                content_hash="b" * 64,
            )
        )
        session.commit()
        assert service.create_easy_run(_request()).id == workout.id
        with pytest.raises(WorkoutConflictError) as conflict:
            service.create_easy_run(_request(minutes=35))
        assert conflict.value.code == "proposal.idempotency_conflict"


def test_stale_idempotency_precheck_rolls_back_duplicate_proposal(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "coach_workout_proposals_enabled", True)
    with session_factory() as session:
        user = _user(session)
        _history(session, user.id, date.today())
        service = RunningProposalService(session, user)
        request = _request(key="phase8-race-regression")
        winner = service.create_easy_run(request)

        original_idempotent_proposal = WorkoutService.idempotent_proposal
        stale_checks = 0

        def stale_idempotent_proposal(
            workout_service: WorkoutService,
            *,
            idempotency_key: str,
            request_fingerprint: str,
        ) -> Workout | None:
            nonlocal stale_checks
            stale_checks += 1
            if stale_checks <= 2:
                return None
            return original_idempotent_proposal(
                workout_service,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
            )

        monkeypatch.setattr(WorkoutService, "idempotent_proposal", stale_idempotent_proposal)
        replay = service.create_easy_run(request)

        assert replay.id == winner.id
        assert session.scalar(select(func.count()).select_from(Workout)) == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(WorkoutEvent)
                .where(WorkoutEvent.action == "propose")
            )
            == 1
        )


def test_proposal_requires_recent_history_and_respects_safety_stop(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "coach_workout_proposals_enabled", True)
    with session_factory() as session:
        user = _user(session)
        service = RunningProposalService(session, user)

        with pytest.raises(WorkoutProposalError) as missing_history:
            service.create_easy_run(_request(key="phase8-no-history"))
        assert missing_history.value.code == "proposal.running_history_required"

        _history(session, user.id, date.today())
        session.add(
            PreSessionFeedback(
                user_id=user.id,
                illness_signal="fever",
                pain_present=False,
                content_hash="a" * 64,
            )
        )
        session.flush()

        with pytest.raises(WorkoutProposalError) as safety_stop:
            service.create_easy_run(_request(key="phase8-safety-stop"))
        assert safety_stop.value.code == "proposal.safety_blocked"
        assert session.scalar(select(func.count()).select_from(Workout)) == 0


def test_easy_run_uses_personal_garmin_hr_range_as_device_target(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "coach_workout_proposals_enabled", True)
    with session_factory() as session:
        user = _user(session)
        _history(session, user.id, date.today())
        account = GarminAccount(
            user_id=user.id,
            principal_fingerprint="a" * 64,
            connected_at=utcnow(),
            heart_rate_zone_profiles=[
                {
                    "sport": "DEFAULT",
                    "training_method": "HR_MAX",
                    "zone_floors": [105, 125, 144, 164, 185],
                    "max_hr": 205,
                    "resting_hr": 67,
                    "lactate_threshold_hr": None,
                },
                {
                    "sport": "RUNNING",
                    "training_method": "LACTATE_THRESHOLD",
                    "zone_floors": [136, 150, 164, 177, 191],
                    "max_hr": 205,
                    "resting_hr": 67,
                    "lactate_threshold_hr": 177,
                },
            ],
            heart_rate_zones_synced_at=utcnow(),
        )
        session.add(account)
        session.flush()

        workout = RunningProposalService(session, user).create_easy_run(
            _request(key="phase8-personal-hr")
        )
        revision = session.get(WorkoutRevision, workout.current_revision_id)
        assert revision is not None
        step = revision.definition_model.blocks[0]
        assert isinstance(step, StepBlockV2)
        assert step.target == HeartRateRangeTarget(
            type="heart_rate_range", lower_bpm=150, upper_bpm=163
        )
        assert "RPE 2–3" in step.instructions[0]
        assert revision.guidance_json is not None
        assert revision.generation_context_json is not None
        assert revision.guidance_json["device_target"] == {
            "type": "heart_rate_range",
            "lower_bpm": 150,
            "upper_bpm": 163,
            "source": "garmin_heart_rate_zone_profile",
            "principal_fingerprint": "a" * 64,
            "profile_sport": "RUNNING",
            "training_method": "LACTATE_THRESHOLD",
            "synced_at": revision.generation_context_json["device_target"]["synced_at"],
            "synced_on": date.today().isoformat(),
            "policy": "personalized_aerobic_zone_2_bounds_v1",
        }
        compiled = compile_workout_with_report(revision)
        compiled_step = compiled.payload["workoutSegments"][0]["workoutSteps"][0]
        assert compiled_step["targetType"]["workoutTargetTypeKey"] == "heart.rate.zone"
        assert compiled_step["targetValueOne"] == 150.0
        assert compiled_step["targetValueTwo"] == 163.0
        assert {warning.code for warning in compiled.warnings} == {"garmin.instructions_omitted"}

        changed_target = revision.definition_model.model_copy(deep=True)
        changed_step = changed_target.blocks[0]
        assert isinstance(changed_step, StepBlockV2)
        changed_step.target = HeartRateRangeTarget(
            type="heart_rate_range", lower_bpm=151, upper_bpm=163
        )
        with pytest.raises(WorkoutTransitionError) as target_error:
            WorkoutService(session, user).update(
                workout.id,
                WorkoutInput(
                    name=revision.name,
                    sport=revision.sport,
                    scheduled_for=revision.suggested_for,
                    description=revision.description or "",
                    definition=changed_target,
                    definition_version=2,
                ),
                expected_identity=_identity(workout, revision),
                idempotency_key="phase8-personal-hr-target-change",
            )
        assert target_error.value.code == "proposal.easy_run_intensity_invalid"

        edited_definition = revision.definition_model.model_copy(deep=True)
        edited_step = edited_definition.blocks[0]
        assert isinstance(edited_step, StepBlockV2)
        assert isinstance(edited_step.end, TimeEnd)
        edited_step.end.seconds = 40 * 60
        WorkoutService(session, user).update(
            workout.id,
            WorkoutInput(
                name=revision.name,
                sport=revision.sport,
                scheduled_for=revision.suggested_for,
                description=revision.description or "",
                definition=edited_definition,
                definition_version=2,
            ),
            expected_identity=_identity(workout, revision),
            idempotency_key="phase8-personal-hr-edit",
        )
        edited = session.get(WorkoutRevision, workout.current_revision_id)
        assert edited is not None
        edited_step = edited.definition_model.blocks[0]
        assert isinstance(edited_step, StepBlockV2)
        assert edited_step.target == step.target
        assert edited.guidance_json is not None
        assert edited.guidance_json["device_target"] == revision.guidance_json["device_target"]

        account.principal_fingerprint = "b" * 64
        with pytest.raises(WorkoutTransitionError) as principal_error:
            WorkoutService(session, user).accept(
                workout.id,
                AcceptRevisionCommand(
                    identity=_identity(workout, edited),
                    context_fingerprint=WorkoutService(session, user)
                    .acceptance_context(workout.id)
                    .fingerprint,
                ),
            )
        assert principal_error.value.code == "proposal.device_target_principal_changed"


def test_easy_run_hr_target_falls_back_to_valid_default_profile(session_factory) -> None:
    with session_factory() as session:
        user = _user(session)
        session.add(
            GarminAccount(
                user_id=user.id,
                principal_fingerprint="a" * 64,
                connected_at=utcnow(),
                heart_rate_zone_profiles=[
                    {
                        "sport": "DEFAULT",
                        "training_method": "HR_MAX",
                        "zone_floors": [105, 125, 144, 164, 185],
                        "max_hr": 205,
                        "resting_hr": 67,
                        "lactate_threshold_hr": None,
                    },
                    {
                        "sport": "RUNNING",
                        "training_method": "HR_MAX",
                        "zone_floors": [136, 150, 164, 177, 191],
                        "resting_hr": 67,
                        "lactate_threshold_hr": None,
                    },
                ],
                heart_rate_zones_synced_at=utcnow(),
            )
        )
        session.flush()

        selected = _easy_run_device_target(session, user.id)

        assert selected is not None
        assert selected.target == HeartRateRangeTarget(
            type="heart_rate_range", lower_bpm=125, upper_bpm=143
        )
        assert selected.provenance["profile_sport"] == "DEFAULT"


def test_proposal_edit_accept_schedule_and_reject_lifecycle(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "coach_workout_proposals_enabled", True)
    with session_factory() as session:
        user = _user(session)
        _history(session, user.id, date.today())
        workout = RunningProposalService(session, user).create_easy_run(_request())
        service = WorkoutService(session, user)
        revision = session.get(WorkoutRevision, workout.current_revision_id)
        assert revision is not None
        definition = revision.definition_model.model_copy(deep=True)
        step = definition.blocks[0]
        assert isinstance(step, StepBlockV2)
        assert isinstance(step.end, TimeEnd)
        step.end.seconds = 35 * 60

        edit_data = WorkoutInput(
            name=revision.name,
            sport=revision.sport,
            scheduled_for=revision.suggested_for,
            description=revision.description or "",
            definition=definition,
            definition_version=2,
        )
        edit_identity = _identity(workout, revision)
        service.update(
            workout.id,
            edit_data,
            expected_identity=edit_identity,
            idempotency_key="phase8-edit-35",
        )
        edited = session.get(WorkoutRevision, workout.current_revision_id)
        assert edited is not None
        assert edited.revision_number == 2
        assert edited.edit_source == "user"
        assert edited.load_estimate_json is not None
        assert edited.load_estimate_json["duration_seconds"] == 2100
        assert workout.approval_status == "proposed"
        service.update(
            workout.id,
            edit_data,
            expected_identity=edit_identity,
            idempotency_key="phase8-edit-35",
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(WorkoutRevision)
                .where(WorkoutRevision.workout_id == workout.id)
            )
            == 2
        )

        safety = service.acceptance_context(workout.id)
        accept_command = AcceptRevisionCommand(
            identity=_identity(workout, edited),
            context_fingerprint=safety.fingerprint,
        )
        service.accept(workout.id, accept_command)
        service.accept(workout.id, accept_command)
        assert workout.accepted_revision_id == edited.id
        assert workout.scheduled_for is None
        assert (
            session.scalar(
                select(func.count())
                .select_from(WorkoutValidationRun)
                .where(
                    WorkoutValidationRun.workout_id == workout.id,
                    WorkoutValidationRun.validation_kind == "acceptance",
                )
            )
            == 1
        )

        schedule = ScheduleWorkoutCommand(
            revision_id=edited.id,
            scheduled_for=edited.suggested_for,
            expected_lock_version=workout.lock_version,
        )
        service.schedule(workout.id, schedule)
        service.schedule(workout.id, schedule)
        assert workout.scheduled_for == edited.suggested_for
        assert (
            session.scalar(
                select(func.count())
                .select_from(WorkoutEvent)
                .where(WorkoutEvent.workout_id == workout.id, WorkoutEvent.action == "schedule")
            )
            == 1
        )

        second = RunningProposalService(session, user).create_easy_run(
            _request(key="phase8-request-2")
        )
        second_revision = session.get(WorkoutRevision, second.current_revision_id)
        assert second_revision is not None
        service.reject(
            second.id, RejectRevisionCommand(identity=_identity(second, second_revision))
        )
        service.reject(
            second.id, RejectRevisionCommand(identity=_identity(second, second_revision))
        )
        assert second.approval_status == "rejected"
        with pytest.raises(WorkoutTransitionError) as rejected:
            service.accept(
                second.id,
                AcceptRevisionCommand(
                    identity=_identity(second, second_revision),
                    context_fingerprint=service.acceptance_context(second.id).fingerprint,
                ),
            )
        assert rejected.value.code == "workout.proposal_rejected"


def test_generated_edit_and_schedule_enforce_proposal_contract(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "coach_workout_proposals_enabled", True)
    with session_factory() as session:
        user = _user(session)
        _history(session, user.id, date.today())
        workout = RunningProposalService(session, user).create_easy_run(
            _request(key="phase8-contract")
        )
        service = WorkoutService(session, user)
        revision = session.get(WorkoutRevision, workout.current_revision_id)
        assert revision is not None

        with pytest.raises(WorkoutTransitionError) as sport_error:
            service.update(
                workout.id,
                WorkoutInput(
                    name=revision.name,
                    sport="cycling",
                    scheduled_for=revision.suggested_for,
                    description=revision.description or "",
                    definition=revision.definition_model,
                    definition_version=2,
                ),
                expected_identity=_identity(workout, revision),
                idempotency_key="phase8-cross-sport",
            )
        assert sport_error.value.code == "proposal.easy_run_sport_invalid"

        monkeypatch.setattr(get_settings(), "coach_workout_proposals_enabled", False)
        with pytest.raises(WorkoutTransitionError) as feature_error:
            service.accept(
                workout.id,
                AcceptRevisionCommand(
                    identity=_identity(workout, revision),
                    context_fingerprint=service.acceptance_context(workout.id).fingerprint,
                ),
            )
        assert feature_error.value.code == "coach.workout_proposals_disabled"

        monkeypatch.setattr(get_settings(), "coach_workout_proposals_enabled", True)
        service.accept(
            workout.id,
            AcceptRevisionCommand(
                identity=_identity(workout, revision),
                context_fingerprint=service.acceptance_context(workout.id).fingerprint,
            ),
        )
        with pytest.raises(WorkoutConflictError) as date_error:
            service.schedule(
                workout.id,
                ScheduleWorkoutCommand(
                    revision_id=revision.id,
                    scheduled_for=date.today() + timedelta(days=2),
                    expected_lock_version=workout.lock_version,
                ),
            )
        assert date_error.value.code == "workout.schedule_date_mismatch"


def test_proposal_route_is_feature_gated_and_renders_detail(
    client, session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "coach_workout_proposals_enabled", False)
    response = client.get("/coach")
    assert "Easy Run vorschlagen" not in response.text

    blocked = client.post(
        "/coach/workout-proposals/easy-run",
        data={
            "suggested_for": (date.today() + timedelta(days=1)).isoformat(),
            "available_minutes": "45",
            "idempotency_key": "phase8-route-disabled",
        },
    )
    assert blocked.status_code == 403
    assert "noch nicht freigeschaltet" in blocked.text

    monkeypatch.setattr(get_settings(), "coach_workout_proposals_enabled", True)
    with session_factory() as session:
        user = session.scalar(select(User))
        assert user is not None
        _history(session, user.id, date.today())
        session.commit()

    page = client.get("/coach")
    assert "Easy Run vorschlagen" in page.text
    created = client.post(
        "/coach/workout-proposals/easy-run",
        data={
            "suggested_for": (date.today() + timedelta(days=1)).isoformat(),
            "available_minutes": "35",
            "idempotency_key": "phase8-route-enabled",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    detail = client.get(created.headers["location"])
    assert detail.status_code == 200
    assert "Deterministischer Vorschlag" in detail.text
    assert "35 Minuten" in detail.text
    assert "Distanz bewusst offen" in detail.text
    assert "Vorschlag annehmen" in detail.text
    assert "Vorschlag ablehnen" in detail.text

    workout_id = int(created.headers["location"].split("/", 3)[2].split("?", 1)[0])
    with session_factory() as session:
        workout = session.get(Workout, workout_id)
        assert workout is not None
        revision = session.get(WorkoutRevision, workout.current_revision_id)
        assert revision is not None
        assert revision.suggested_for is not None
        definition = json.loads(json.dumps(revision.definition))
        definition["blocks"][0]["end"]["seconds"] = 30 * 60
        edit_payload = {
            "name": revision.name,
            "sport": "running",
            "scheduled_for": revision.suggested_for.isoformat(),
            "description": revision.description or "",
            "definition_version": "2",
            "definition": json.dumps(definition),
            "revision_id": str(revision.id),
            "revision_number": str(revision.revision_number),
            "content_hash": revision.content_hash,
            "lock_version": str(workout.lock_version),
            "idempotency_key": "phase8-route-edit",
        }
    edited_response = client.post(
        f"/workouts/{workout_id}", data=edit_payload, follow_redirects=False
    )
    assert edited_response.status_code == 303
    edited_detail = client.get(f"/workouts/{workout_id}")
    assert "Revision 1 → 2" in edited_detail.text
    assert "Geändert: Ablauf" in edited_detail.text

    with session_factory() as session:
        workout = session.get(Workout, workout_id)
        user = session.scalar(select(User))
        assert workout is not None and user is not None
        revision = session.get(WorkoutRevision, workout.current_revision_id)
        assert revision is not None
        context_fingerprint = (
            WorkoutService(session, user).acceptance_context(workout.id).fingerprint
        )
        accept_payload = {
            "revision_id": str(revision.id),
            "revision_number": str(revision.revision_number),
            "content_hash": revision.content_hash,
            "lock_version": str(workout.lock_version),
            "context_fingerprint": context_fingerprint,
        }
    accepted_response = client.post(
        f"/workouts/{workout_id}/confirm", data=accept_payload, follow_redirects=False
    )
    assert accepted_response.status_code == 303

    with session_factory() as session:
        workout = session.get(Workout, workout_id)
        assert workout is not None and workout.accepted_revision_id is not None
        accepted = session.get(WorkoutRevision, workout.accepted_revision_id)
        assert accepted is not None and accepted.suggested_for is not None
        schedule_payload = {
            "revision_id": str(accepted.id),
            "lock_version": str(workout.lock_version),
            "scheduled_for": accepted.suggested_for.isoformat(),
        }
    scheduled_response = client.post(
        f"/workouts/{workout_id}/schedule", data=schedule_payload, follow_redirects=False
    )
    assert scheduled_response.status_code == 303
    with session_factory() as session:
        workout = session.get(Workout, workout_id)
        assert workout is not None
        assert workout.local_schedule_status == "scheduled"


def test_generated_proposal_uses_shared_idempotent_garmin_service(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeGarmin:
        uploads = 0
        schedules = 0

        def upload_workout(self, _payload):
            self.uploads += 1
            return {"workoutId": "phase8-remote"}

        def get_scheduled_workouts(self, _year: int, _month: int):
            return {"items": []}

        def schedule_workout(self, _workout_id: str, _day: str) -> None:
            self.schedules += 1

    monkeypatch.setattr(get_settings(), "coach_workout_proposals_enabled", True)
    with session_factory() as session:
        user = _user(session)
        _history(session, user.id, date.today())
        garmin = FakeGarmin()
        proposal = RunningProposalService(session, user).create_easy_run(
            _request(key="phase8-garmin")
        )
        service = WorkoutService(session, user, connect_garmin=lambda *_args: garmin)
        revision = session.get(WorkoutRevision, proposal.current_revision_id)
        assert revision is not None
        service.accept(
            proposal.id,
            AcceptRevisionCommand(
                identity=_identity(proposal, revision),
                context_fingerprint=service.acceptance_context(proposal.id).fingerprint,
            ),
        )
        assert revision.suggested_for is not None
        service.schedule(
            proposal.id,
            ScheduleWorkoutCommand(
                revision_id=revision.id,
                scheduled_for=revision.suggested_for,
                expected_lock_version=proposal.lock_version,
            ),
        )

        with pytest.raises(WorkoutTransitionError) as disabled:
            service.publish(proposal.id)
        assert disabled.value.code == "coach.garmin_sync_disabled"

        monkeypatch.setattr(get_settings(), "coach_garmin_sync_enabled", True)
        session.add(GarminAccount(user_id=user.id, connected_at=utcnow()))
        session.commit()
        service.publish(proposal.id)
        service.publish(proposal.id)

        assert garmin.uploads == 1
        assert garmin.schedules == 1

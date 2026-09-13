import re
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.models import Activity, CoachConversation, TrainingPlanRevision, User, Workout
from app.services.planning.daily_recommendation import (
    recommend_option,
    recommend_today,
    save_recommendation,
)
from tests.test_daily_recommendation import DAY, accepted_generated, runner


def test_overview_is_read_only_without_provider(client, session_factory, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "llm_api_key", "")
    with session_factory() as session:
        before = [
            session.scalar(select(func.count()).select_from(model))
            for model in (Workout, TrainingPlanRevision, CoachConversation)
        ]
    for url in ("/coach", "/coach?option=shorter", "/coach?option=easier"):
        response = client.get(url)
        assert response.status_code == 200
        assert "Diese Woche" in response.text
        assert "Coach fragen" in response.text
        assert "Warum diese Einheit?" in response.text
    with session_factory() as session:
        after = [
            session.scalar(select(func.count()).select_from(model))
            for model in (Workout, TrainingPlanRevision, CoachConversation)
        ]
    assert after == before


def test_shorter_and_easier_are_smaller_exact_idempotent_proposals(session_factory):
    with session_factory() as session:
        user = runner(session, minutes=55, frequency=4)
        original = recommend_today(session, user, as_of=DAY)
        for option in ("shorter", "easier"):
            preview = recommend_option(session, user, as_of=DAY, option=option)
            assert preview.duration_seconds < original.duration_seconds
            assert preview.work_seconds <= original.work_seconds
            assert preview.state == "workout"
            saved = save_recommendation(
                session,
                user,
                as_of=DAY,
                option=option,
                expected_fingerprint=preview.context_fingerprint,
            )
            replay = save_recommendation(
                session,
                user,
                as_of=DAY,
                option=option,
                expected_fingerprint=preview.context_fingerprint,
            )
            assert isinstance(saved, Workout) and isinstance(replay, Workout)
            assert saved.id == replay.id
            assert saved.definition_model == preview.definition
            assert saved.accepted_revision_id is None


def test_short_window_becomes_rest(client):
    response = client.get("/coach?option=shorter&available_minutes=10")
    assert response.status_code == 200
    assert "Ruhetag" in response.text
    assert "Als Vorschlag speichern" not in response.text


def test_alternatives_preserve_original_custom_time_ceiling(session_factory):
    with session_factory() as session:
        user = runner(session, minutes=55, frequency=4)
        original = recommend_today(session, user, as_of=DAY, available_minutes=25)
        for option in ("shorter", "easier"):
            preview = recommend_option(
                session,
                user,
                as_of=DAY,
                original_minutes=25,
                available_minutes=20,
                option=option,
            )
            assert preview.duration_seconds < original.duration_seconds
            assert preview.duration_seconds <= 20 * 60
            assert preview.work_seconds <= original.work_seconds
            if option == "easier" and preview.state == "workout":
                assert preview.template_id == "easy_run"
                assert "ohne zügige Arbeitsblöcke" in " ".join(preview.reasons)


def test_scheduled_preview_and_adaptation_keep_accepted_content(client, session_factory):
    with session_factory() as session:
        user = session.scalar(select(User))
        workout = accepted_generated(session, user, day=date.today())
        workout_id, revision_id = workout.id, workout.accepted_revision_id
        session.commit()
    response = client.get("/coach")
    assert response.status_code == 200
    assert "Eingeplant" in response.text
    assert "Angenommene Einheit anpassen" in response.text
    assert f'action="/workouts/{workout_id}/adaptation/apply"' in response.text
    scope = client.get(f"/coach/chat?workout_id={workout_id}&revision_id={revision_id}")
    assert scope.status_code == 200
    assert f'name="revision_id" value="{revision_id}"' in scope.text
    with session_factory() as session:
        workout = session.get(Workout, workout_id)
        assert workout.accepted_revision_id == revision_id
        assert workout.current_revision_id == revision_id


def test_completed_without_linkage_is_never_labeled_missed(client, session_factory):
    with session_factory() as session:
        user = session.scalar(select(User))
        workout = accepted_generated(session, user, day=date.today())
        session.add(
            Activity(
                user_id=user.id,
                garmin_activity_id="unlinked-today",
                name="Mein Morgenlauf",
                activity_type="running",
                started_at=datetime.now(),
                duration_s=1800,
            )
        )
        session.commit()
        workout_id = workout.id
    response = client.get("/coach")
    assert response.status_code == 200
    assert "Erledigt" in response.text and "Mein Morgenlauf" in response.text
    assert "Lauf bereits erledigt" in response.text
    assert f'href="/workouts/{workout_id}"' in response.text
    assert "Verpasst" not in response.text
    assert "Als Vorschlag speichern" not in response.text


def test_scoped_chat_rejects_foreign_and_missing_references(client, session_factory):
    with session_factory() as session:
        foreign = runner(session)
        workout = accepted_generated(session, foreign, day=date.today())
        workout_id, revision_id = workout.id, workout.accepted_revision_id
        session.commit()
    assert (
        client.get(f"/coach/chat?workout_id={workout_id}&revision_id={revision_id}").status_code
        == 404
    )
    assert client.get(f"/coach/chat?workout_id={workout_id}").status_code == 422
    assert client.get("/coach/chat?recommendation_fingerprint=" + "0" * 64).status_code == 409


def test_alternative_route_rejects_stale_and_csrf_submissions(client):
    stale = client.post(
        "/coach/today/save", data={"option": "shorter", "context_fingerprint": "0" * 64}
    )
    assert stale.status_code == 409
    assert "Bitte prüfe die aktuelle Vorschau" in stale.text
    client.headers.clear()
    assert (
        client.post(
            "/coach/today/save", data={"option": "easier", "context_fingerprint": "0" * 64}
        ).status_code
        == 403
    )


def test_alternative_save_accept_schedule_reopen_flow(client, session_factory):
    from app.models import WorkoutRevision
    from app.services.planning.workout_service import WorkoutService

    preview = client.get("/coach/today?option=shorter").json()
    assert preview["state"] == "workout"
    response = client.post(
        "/coach/today/save",
        data={"option": "shorter", "context_fingerprint": preview["context_fingerprint"]},
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    workout_id = int(location.rsplit("/", 1)[1])
    assert "Vorgeschlagen · nicht eingeplant" in client.get("/coach").text
    with session_factory() as session:
        user = session.scalar(select(User))
        workout = session.get(Workout, workout_id)
        revision = session.get(WorkoutRevision, workout.current_revision_id)
        assert revision.definition_model.model_dump(mode="json") == preview["definition"]
        accept = {
            "revision_id": revision.id,
            "revision_number": revision.revision_number,
            "content_hash": revision.content_hash,
            "lock_version": workout.lock_version,
            "context_fingerprint": WorkoutService(session, user)
            .acceptance_context(workout_id)
            .fingerprint,
        }
    assert (
        client.post(location + "/confirm", data=accept, follow_redirects=False).status_code == 303
    )
    with session_factory() as session:
        workout = session.get(Workout, workout_id)
        schedule = {
            "revision_id": workout.accepted_revision_id,
            "lock_version": workout.lock_version,
            "scheduled_for": date.today().isoformat(),
        }
    assert (
        client.post(location + "/schedule", data=schedule, follow_redirects=False).status_code
        == 303
    )
    assert "Eingeplant" in client.get("/coach").text
    with session_factory() as session:
        workout = session.get(Workout, workout_id)
        assert workout.garmin_workout_id is None
        assert workout.status not in {"published", "pushed"}


def test_linked_completion_is_one_week_entry_with_both_detail_links(client, session_factory):
    from app.services.coach.overview import overview_context

    with session_factory() as session:
        user = session.scalar(select(User))
        workout = accepted_generated(session, user, day=date.today())
        activity = Activity(
            user_id=user.id,
            garmin_activity_id="linked-completed-run",
            name="Absolvierter Intervalllauf",
            activity_type="running",
            started_at=datetime.now(),
            duration_s=2400,
            workout_id=workout.id,
        )
        session.add(activity)
        session.commit()
        workout_id, activity_id = workout.id, activity.id
        overview = overview_context(
            session, user, recommend_today(session, user, as_of=date.today())
        )
        days = overview["week_days"]
        assert isinstance(days, list)
        day = next(day for day in days if day["date"] == date.today())
        assert [item.id for item in day["activities"]] == [activity_id]
        assert day["workouts"] == []
        assert overview["remaining_seconds"] == 0
        assert overview["completed_seconds"] == 2400

    response = client.get("/coach")
    assert response.status_code == 200
    week = re.search(
        r'<section\b[^>]*aria-labelledby="week-title"[^>]*>(.*?)</section>', response.text, re.S
    )
    assert week is not None
    assert week.group(1).count("Erledigt") == 1
    assert f'href="/activities/{activity_id}"' in week.group(1)
    assert f'href="/workouts/{workout_id}"' in week.group(1)


@pytest.mark.parametrize("day_offset", [0, 1], ids=["today", "occupied-future-day"])
def test_saved_proposals_are_collapsed_alternatives_without_losing_content(
    client, session_factory, monkeypatch, day_offset
):
    from app.routes import coach as coach_routes
    from app.services.planning.workout_proposals import (
        RunningProposalRequest,
        RunningProposalService,
    )

    class FixedDate(date):
        @classmethod
        def today(cls):
            return DAY

    monkeypatch.setattr(coach_routes, "date", FixedDate)
    suggested_for = DAY + timedelta(days=day_offset)
    with session_factory() as session:
        user = session.scalar(select(User))
        scheduled_id = None
        if day_offset:
            scheduled_id = accepted_generated(session, user, day=suggested_for).id
        proposals = [
            RunningProposalService(session, user, as_of=DAY).create(
                RunningProposalRequest(
                    suggested_for=suggested_for,
                    available_minutes=minutes,
                    idempotency_key=f"overview-alternative-{minutes}",
                )
            )
            for minutes in (20, 25, 30)
        ]
        proposal_ids = [workout.id for workout in proposals]
        foreign = User(display_name="Andere Person")
        session.add(foreign)
        session.flush()
        foreign_proposal = RunningProposalService(session, foreign, as_of=DAY).create(
            RunningProposalRequest(
                suggested_for=suggested_for,
                available_minutes=30,
                idempotency_key="foreign-overview-alternative",
            )
        )
        foreign_id = foreign_proposal.id
        session.commit()

    response = client.get("/coach")
    assert response.status_code == 200
    alternatives = [
        (attributes, contents)
        for attributes, contents in re.findall(
            r"<details\b([^>]*)>(.*?)</details>", response.text, re.S
        )
        if re.search(r"<summary\b[^>]*>.*?Alternativen.*?</summary>", contents, re.S)
    ]
    assert len(alternatives) == 1
    attributes, contents = alternatives[0]
    assert re.search(r"\bopen\b", attributes) is None
    for workout_id in proposal_ids:
        assert f'href="/workouts/{workout_id}"' in contents
        assert response.text.count(f'href="/workouts/{workout_id}"') == 1
    assert f'href="/workouts/{foreign_id}"' not in response.text
    if scheduled_id:
        assert f'href="/workouts/{scheduled_id}"' in response.text
        assert f'href="/workouts/{scheduled_id}"' not in contents
    assert response.text.count('id="today-title"') == 1
    with session_factory() as session:
        for workout_id in proposal_ids:
            workout = session.get(Workout, workout_id)
            assert workout.deleted_at is None
            assert workout.approval_status == "proposed"
            assert workout.accepted_revision_id is None
            assert workout.scheduled_for is None

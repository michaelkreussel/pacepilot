import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError

from app.migrations import upgrade_database


def test_initial_migration_matches_models(tmp_path: Path) -> None:
    database_path = tmp_path / "migration.db"
    config = Config("alembic.ini")
    config.attributes["database_url"] = f"sqlite:///{database_path.as_posix()}"

    command.upgrade(config, "head")
    command.check(config)

    inspector = inspect(create_engine(f"sqlite:///{database_path.as_posix()}"))
    assert {
        "activities",
        "activity_exercise_sets",
        "activity_splits",
        "activity_zones",
        "alembic_version",
        "athlete_availability",
        "athlete_goals",
        "athlete_planning_profiles",
        "coach_conversations",
        "coach_assistant_runs",
        "coach_messages",
        "coach_tool_calls",
        "daily_data_statuses",
        "daily_fitness",
        "daily_health",
        "garmin_accounts",
        "garmin_devices",
        "garmin_sync_states",
        "oauth_identities",
        "performance_anchors",
        "post_session_feedback",
        "pre_session_feedback",
        "sleep_stages",
        "sync_events",
        "sync_runs",
        "training_plan_revisions",
        "training_plan_workouts",
        "training_plans",
        "training_cycle_revisions",
        "training_cycle_weeks",
        "training_cycles",
        "users",
        "workout_events",
        "workout_garmin_bindings",
        "workout_garmin_attempts",
        "workout_garmin_operations",
        "workout_garmin_remote_identities",
        "workout_revisions",
        "workout_steps",
        "workout_validation_runs",
        "workouts",
    } == set(inspector.get_table_names())


def test_subjective_feedback_migration_enforces_privacy_links(tmp_path: Path) -> None:
    database_path = tmp_path / "feedback.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")
    engine = create_engine(database_url)

    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.exec_driver_sql(
            "INSERT INTO users (id, display_name, created_at) VALUES "
            "(1, 'Runner', '2026-08-22'), (2, 'Other', '2026-08-22')"
        )
        connection.exec_driver_sql(
            "INSERT INTO activities "
            "(id, user_id, garmin_activity_id, name, activity_type, started_at, "
            "details_complete, splits_complete, zones_complete, synced_at) "
            "VALUES (1, 1, 'run-1', 'Run', 'running', '2026-08-22', 0, 0, 0, '2026-08-22')"
        )
        connection.exec_driver_sql(
            "INSERT INTO post_session_feedback "
            "(id, user_id, activity_id, activity_user_id, completion_percent, "
            "session_rpe, overall_feel, "
            "pain_present, source, content_hash, recorded_at, created_at) "
            "VALUES (1, 1, 1, 1, 100, 5, 4, 0, 'explicit_form', ?, "
            "'2026-08-22', '2026-08-22')",
            ("a" * 64,),
        )
        connection.exec_driver_sql("DELETE FROM activities WHERE id = 1")
        assert (
            connection.exec_driver_sql(
                "SELECT activity_id FROM post_session_feedback WHERE id = 1"
            ).scalar_one()
            is None
        )
        connection.exec_driver_sql("DELETE FROM users WHERE id = 1")
        assert (
            connection.exec_driver_sql("SELECT count(*) FROM post_session_feedback").scalar_one()
            == 0
        )

    with engine.begin() as connection, pytest.raises(IntegrityError):
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.exec_driver_sql(
            "INSERT INTO users (id, display_name, created_at) VALUES (3, 'Third', '2026-08-22')"
        )
        connection.exec_driver_sql(
            "INSERT INTO activities "
            "(id, user_id, garmin_activity_id, name, activity_type, started_at, "
            "details_complete, splits_complete, zones_complete, synced_at) "
            "VALUES (2, 2, 'run-2', 'Run', 'running', '2026-08-22', 0, 0, 0, '2026-08-22')"
        )
        connection.exec_driver_sql(
            "INSERT INTO post_session_feedback "
            "(user_id, activity_id, activity_user_id, completion_percent, session_rpe, "
            "overall_feel, pain_present, source, content_hash, recorded_at, created_at) "
            "VALUES (3, 2, 3, 100, 5, 4, 0, 'explicit_form', ?, "
            "'2026-08-22', '2026-08-22')",
            ("c" * 64,),
        )

    with engine.begin() as connection, pytest.raises(IntegrityError):
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.exec_driver_sql(
            "INSERT INTO pre_session_feedback "
            "(user_id, motivation, fatigue, leg_freshness, soreness, pain_present, "
            "illness_signal, source, content_hash, recorded_at, created_at) "
            "VALUES (2, 6, 1, 5, 0, 0, 'none', 'explicit_form', ?, "
            "'2026-08-22', '2026-08-22')",
            ("b" * 64,),
        )


def test_feedback_owner_migration_upgrades_applied_revision_22(tmp_path: Path) -> None:
    database_path = tmp_path / "feedback-revision-22.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "20260822_22")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.exec_driver_sql(
            "INSERT INTO users (id, display_name, created_at) VALUES (1, 'Runner', '2026-08-22')"
        )
        connection.exec_driver_sql(
            "INSERT INTO workouts "
            "(id, user_id, name, sport, status, source_type, approval_status, "
            "local_schedule_status, lock_version, definition_version, definition, "
            "created_at, updated_at) "
            "VALUES (1, 1, 'Run', 'running', 'draft', 'manual', 'draft', "
            "'unscheduled', 0, 1, '{\"blocks\": []}', '2026-08-22', '2026-08-22')"
        )
        connection.exec_driver_sql(
            "INSERT INTO activities "
            "(id, user_id, garmin_activity_id, name, activity_type, started_at, "
            "details_complete, splits_complete, zones_complete, synced_at) "
            "VALUES (1, 1, 'run-1', 'Run', 'running', '2026-08-22', 0, 0, 0, '2026-08-22')"
        )
        connection.exec_driver_sql(
            "INSERT INTO pre_session_feedback "
            "(id, user_id, workout_id, motivation, fatigue, leg_freshness, soreness, "
            "pain_present, illness_signal, source, content_hash, recorded_at, created_at) "
            "VALUES (1, 1, 1, 3, 3, 3, 0, 0, 'none', 'explicit_form', ?, "
            "'2026-08-22', '2026-08-22')",
            ("a" * 64,),
        )
        connection.exec_driver_sql(
            "INSERT INTO post_session_feedback "
            "(id, user_id, workout_id, activity_id, completion_percent, session_rpe, "
            "overall_feel, pain_present, source, content_hash, recorded_at, created_at) "
            "VALUES (1, 1, 1, 1, 100, 5, 4, 0, 'explicit_form', ?, "
            "'2026-08-22', '2026-08-22')",
            ("b" * 64,),
        )

    command.upgrade(config, "head")

    with engine.connect() as connection:
        assert (
            connection.exec_driver_sql(
                "SELECT workout_user_id FROM pre_session_feedback WHERE id = 1"
            ).scalar_one()
            == 1
        )
        assert connection.exec_driver_sql(
            "SELECT workout_user_id, activity_user_id FROM post_session_feedback WHERE id = 1"
        ).one() == (1, 1)
        assert connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one() == ("20260828_32")


def test_application_migration_uses_absolute_project_paths(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "startup" / "app.db"
    database_path.parent.mkdir()
    monkeypatch.chdir(tmp_path)

    upgrade_database(f"sqlite:///{database_path.as_posix()}")

    inspector = inspect(create_engine(f"sqlite:///{database_path.as_posix()}"))
    assert "workouts" in inspector.get_table_names()
    assert {column["name"] for column in inspector.get_columns("workouts")} >= {
        "definition",
        "definition_version",
        "current_revision_id",
        "accepted_revision_id",
        "materialized_revision_id",
        "approval_status",
        "local_schedule_status",
        "lock_version",
    }


def test_workout_revision_migration_resumes_after_added_columns(tmp_path: Path) -> None:
    database_path = tmp_path / "partial-workout-revision.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "20260819_15")
    engine = create_engine(database_url)
    partial_columns = (
        "source_type VARCHAR(30) DEFAULT 'manual' NOT NULL",
        "approval_status VARCHAR(30) DEFAULT 'draft' NOT NULL",
        "local_schedule_status VARCHAR(30) DEFAULT 'unscheduled' NOT NULL",
        "current_revision_id INTEGER",
        "accepted_revision_id INTEGER",
        "materialized_revision_id INTEGER",
        "accepted_at DATETIME",
        "accepted_by_user_id INTEGER",
        "expires_at DATETIME",
        "lock_version INTEGER DEFAULT '0' NOT NULL",
        "replaces_workout_id INTEGER",
        "originating_conversation_id INTEGER",
        "originating_user_message_id INTEGER",
        "originating_assistant_message_id INTEGER",
        "deleted_at DATETIME",
    )
    with engine.begin() as connection:
        for column in partial_columns:
            connection.exec_driver_sql(f"ALTER TABLE workouts ADD COLUMN {column}")

    command.upgrade(config, "head")

    inspector = inspect(engine)
    with engine.connect() as connection:
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
        integrity = connection.exec_driver_sql("PRAGMA integrity_check").scalar_one()
    assert revision == "20260828_32"
    assert integrity == "ok"
    assert "workout_revisions" in inspector.get_table_names()


def test_reverted_athlete_profile_revision_upgrades_to_head(tmp_path: Path) -> None:
    database_path = tmp_path / "revision-14.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "20260812_14")

    upgrade_database(database_url)

    engine = create_engine(database_url)
    inspector = inspect(engine)
    assert not {"athlete_profiles", "athlete_manual_anchors"} & set(inspector.get_table_names())
    assert not {"fit_file", "fit_import_status", "fit_synced_at"} & {
        column["name"] for column in inspector.get_columns("activities")
    }
    goal_columns = {column["name"] for column in inspector.get_columns("athlete_goals")}
    assert {"id", "user_id", "event_type", "status"} <= goal_columns
    assert {"sport", "event_name"} <= {
        column["name"] for column in inspector.get_columns("athlete_goals")
    }
    with engine.connect() as connection:
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    assert revision == "20260828_32"


def test_principal_fingerprint_migration_upgrades_applied_phase_4_schema(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "phase-4-followup.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "20260822_17")
    engine = create_engine(database_url)
    assert "principal_fingerprint" not in {
        column["name"] for column in inspect(engine).get_columns("garmin_accounts")
    }

    command.upgrade(config, "head")

    assert "principal_fingerprint" in {
        column["name"] for column in inspect(engine).get_columns("garmin_accounts")
    }
    assert "principal_fingerprint" in {
        column["name"] for column in inspect(engine).get_columns("workout_garmin_remote_identities")
    }


def test_activity_zone_completion_migration_marks_only_complete_zone_data(tmp_path: Path) -> None:
    database_path = tmp_path / "activity-zones.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "20260822_18")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO users (id, display_name, created_at) VALUES (1, 'Runner', '2026-08-22')"
        )
        for activity_id, average_hr, max_hr, max_power in (
            (1, 150, 180, None),
            (2, 145, 175, None),
            (3, None, None, None),
            (4, None, 190, None),
            (5, None, None, 350),
        ):
            connection.exec_driver_sql(
                "INSERT INTO activities "
                "(id, user_id, garmin_activity_id, name, activity_type, started_at, average_hr, "
                "max_hr, max_power_watts, details_complete, splits_complete, synced_at) "
                "VALUES (?, 1, ?, 'Run', 'running', '2026-08-22', ?, ?, ?, 1, 1, '2026-08-22')",
                (activity_id, str(activity_id), average_hr, max_hr, max_power),
            )
        connection.exec_driver_sql(
            "INSERT INTO activity_zones "
            "(activity_id, zone_type, zone_number, seconds) "
            "VALUES (2, 'heart_rate', 1, 600)"
        )

    command.upgrade(config, "head")

    with engine.connect() as connection:
        values = connection.exec_driver_sql(
            "SELECT id, zones_complete FROM activities ORDER BY id"
        ).all()
    assert values == [(1, 0), (2, 1), (3, 1), (4, 0), (5, 0)]


def test_workout_rpe_migration_normalizes_garmin_scale(tmp_path: Path) -> None:
    database_path = tmp_path / "rpe.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "20260809_08")

    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO users (id, display_name, created_at) VALUES (1, 'Runner', '2026-08-11')"
        )
        for activity_id, rpe in ((1, 30), (2, 70), (3, 8)):
            connection.exec_driver_sql(
                "INSERT INTO activities "
                "(id, user_id, garmin_activity_id, name, activity_type, started_at, "
                "workout_rpe, details_complete, splits_complete, synced_at) "
                "VALUES (?, 1, ?, 'Run', 'running', '2026-08-11', ?, 0, 0, '2026-08-11')",
                (activity_id, str(activity_id), rpe),
            )

    command.upgrade(config, "head")

    with engine.connect() as connection:
        values = (
            connection.exec_driver_sql("SELECT workout_rpe FROM activities ORDER BY id")
            .scalars()
            .all()
        )
    assert values == [3, 7, 8]


def test_simplified_feedback_migration_normalizes_feel_and_allows_short_entries(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "simplified-feedback.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "20260822_23")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO users (id, display_name, created_at) VALUES (1, 'Runner', '2026-08-24')"
        )
        connection.exec_driver_sql(
            "INSERT INTO activities "
            "(id, user_id, garmin_activity_id, name, activity_type, started_at, workout_feel, "
            "details_complete, splits_complete, zones_complete, synced_at) "
            "VALUES (1, 1, 'run-1', 'Run', 'running', '2026-08-24', 75, 0, 0, 0, "
            "'2026-08-24')"
        )

    command.upgrade(config, "head")

    columns = {
        column["name"]: column for column in inspect(engine).get_columns("pre_session_feedback")
    }
    post_columns = {
        column["name"]: column for column in inspect(engine).get_columns("post_session_feedback")
    }
    with engine.connect() as connection:
        feel = connection.exec_driver_sql(
            "SELECT workout_feel FROM activities WHERE id = 1"
        ).scalar_one()
    assert feel == 4
    assert all(
        columns[name]["nullable"] for name in ("motivation", "fatigue", "leg_freshness", "soreness")
    )
    assert all(
        post_columns[name]["nullable"]
        for name in ("completion_percent", "session_rpe", "overall_feel")
    )

    command.downgrade(config, "20260822_23")

    with engine.connect() as connection:
        assert (
            connection.exec_driver_sql(
                "SELECT workout_feel FROM activities WHERE id = 1"
            ).scalar_one()
            == 4
        )


def test_workout_definition_migration_preserves_repeat_semantics(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy-workout.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "20260808_05")

    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO users (id, display_name, created_at) VALUES (1, 'Runner', '2026-08-09')"
        )
        connection.exec_driver_sql(
            "INSERT INTO workouts (id, user_id, name, sport, status, created_at, updated_at) "
            "VALUES (1, 1, '8 x 400 m', 'running', 'confirmed', '2026-08-09', '2026-08-09')"
        )
        connection.exec_driver_sql(
            "INSERT INTO workout_steps "
            "(id, workout_id, position, step_type, duration_type, duration_value, "
            "target_type, target_min, target_max, repeat_count) VALUES "
            "(1, 1, 1, 'warmup', 'time', 600, 'no_target', NULL, NULL, 1), "
            "(2, 1, 2, 'interval', 'distance', 400, 'pace', 235, 255, 8), "
            "(3, 1, 3, 'recovery', 'time', 60, 'no_target', NULL, NULL, 8), "
            "(4, 1, 4, 'cooldown', 'time', 600, 'no_target', NULL, NULL, 1)"
        )

    command.upgrade(config, "head")

    with engine.connect() as connection:
        row = connection.exec_driver_sql(
            "SELECT definition_version, definition FROM workouts WHERE id = 1"
        ).one()
    definition = json.loads(row.definition)
    repeat = definition["blocks"][1]
    assert row.definition_version == 1
    assert repeat["kind"] == "repeat"
    assert repeat["iterations"] == 8
    assert [child["step_type"] for child in repeat["children"]] == [
        "interval",
        "recovery",
    ]
    assert len({block["id"] for block in definition["blocks"]}) == 3


def test_workout_revision_backfill_matrix(tmp_path: Path) -> None:
    database_path = tmp_path / "workout-revisions.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "20260819_15")

    engine = create_engine(database_url)
    definition = {
        "blocks": [
            {
                "id": "step",
                "kind": "step",
                "step_type": "interval",
                "end": {"type": "time", "seconds": 600},
                "target": {"type": "none"},
            }
        ]
    }
    cases: list[tuple[int, str, bool, bool]] = []
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO users (id, display_name, created_at) VALUES (1, 'Runner', '2026-08-22')"
        )
        connection.exec_driver_sql(
            "INSERT INTO garmin_accounts "
            "(id, user_id, email, connected_at, last_sync_at, rate_limit_until, "
            "sync_status, sync_error) VALUES "
            "(1, 1, NULL, NULL, NULL, NULL, 'not_connected', NULL)"
        )
        workout_id = 1
        for status in ("draft", "confirmed", "published", "pushed"):
            for has_remote in (False, True):
                for has_date in (False, True):
                    cases.append((workout_id, status, has_remote, has_date))
                    connection.exec_driver_sql(
                        "INSERT INTO workouts "
                        "(id, user_id, name, sport, scheduled_for, description, status, "
                        "garmin_workout_id, definition_version, definition, created_at, "
                        "updated_at) "
                        "VALUES (?, 1, ?, 'running', ?, 'Description', ?, ?, 1, ?, "
                        "'2026-08-20', '2026-08-20')",
                        (
                            workout_id,
                            f"Workout {workout_id}",
                            "2026-08-25" if has_date else None,
                            status,
                            f"remote-{workout_id}" if has_remote else None,
                            json.dumps(definition),
                        ),
                    )
                    workout_id += 1

    command.upgrade(config, "head")

    with engine.connect() as connection:
        rows = {
            row.id: row
            for row in connection.exec_driver_sql(
                "SELECT w.id, w.status, w.scheduled_for, w.approval_status, "
                "w.local_schedule_status, w.current_revision_id, w.accepted_revision_id, "
                "w.materialized_revision_id, r.revision_number, r.name, r.suggested_for, "
                "r.definition, r.content_hash, b.content_status, b.calendar_status, "
                "b.device_status, b.last_error_code, ri.garmin_workout_id "
                "FROM workouts w JOIN workout_revisions r ON r.id = w.current_revision_id "
                "JOIN workout_garmin_bindings b ON b.workout_id = w.id "
                "LEFT JOIN workout_garmin_remote_identities ri "
                "ON ri.id = b.active_remote_identity_id ORDER BY w.id"
            )
        }
        event_count = connection.exec_driver_sql(
            "SELECT count(*) FROM workout_events WHERE action = 'legacy_backfill'"
        ).scalar_one()
        revision_count = connection.exec_driver_sql(
            "SELECT count(*) FROM workout_revisions"
        ).scalar_one()

    assert revision_count == event_count == len(cases)
    with engine.begin() as connection, pytest.raises(IntegrityError, match="immutable"):
        connection.exec_driver_sql("UPDATE workout_revisions SET name = 'Mutation' WHERE id = 1")
    for workout_id, status, has_remote, has_date in cases:
        row = rows[workout_id]
        accepted = status != "draft"
        assert row.revision_number == 1
        assert row.name == f"Workout {workout_id}"
        assert json.loads(row.definition) == definition
        assert len(row.content_hash) == 64
        assert row.current_revision_id == row.materialized_revision_id
        assert (row.accepted_revision_id == row.current_revision_id) is accepted
        assert row.approval_status == ("accepted" if accepted else "draft")
        assert row.local_schedule_status == (
            "scheduled" if accepted and has_date else "unscheduled"
        )
        assert row.scheduled_for == ("2026-08-25" if accepted and has_date else None)
        assert row.suggested_for == ("2026-08-25" if has_date else None)
        assert row.garmin_workout_id == (f"remote-{workout_id}" if has_remote else None)

        if not has_remote and status in {"draft", "confirmed"}:
            assert (row.content_status, row.calendar_status, row.device_status) == (
                "not_requested",
                "not_requested",
                "not_requested",
            )
            assert row.last_error_code is None
        elif has_remote and status in {"draft", "confirmed"}:
            assert (row.content_status, row.calendar_status, row.device_status) == (
                "unknown",
                "unknown",
                "unknown",
            )
            assert row.last_error_code == "legacy_remote_state_requires_review"
        elif has_remote:
            assert row.content_status == "synced"
            assert row.calendar_status == ("unknown" if has_date else "not_requested")
            assert row.device_status == (
                "request_accepted" if status == "pushed" else "not_requested"
            )
        else:
            assert (row.content_status, row.calendar_status, row.device_status) == (
                "unknown",
                "unknown",
                "unknown",
            )
            assert row.last_error_code == "legacy_remote_state_requires_review"


def test_workout_revision_constraints(tmp_path: Path) -> None:
    database_path = tmp_path / "workout-revision-constraints.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "20260819_15")
    engine = create_engine(database_url)
    definition = json.dumps(
        {
            "blocks": [
                {
                    "id": "step",
                    "kind": "step",
                    "step_type": "interval",
                    "end": {"type": "time", "seconds": 60},
                    "target": {"type": "none"},
                }
            ]
        }
    )
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO users (id, display_name, created_at) VALUES "
            "(1, 'One', '2026-08-22'), (2, 'Two', '2026-08-22')"
        )
        for workout_id, user_id in ((1, 1), (2, 2)):
            connection.exec_driver_sql(
                "INSERT INTO workouts "
                "(id, user_id, name, sport, status, definition_version, definition, "
                "created_at, updated_at) VALUES (?, ?, ?, 'running', 'draft', 1, ?, "
                "'2026-08-22', '2026-08-22')",
                (workout_id, user_id, f"Workout {workout_id}", definition),
            )
    command.upgrade(config, "head")

    with engine.connect() as connection:
        revisions = connection.exec_driver_sql(
            "SELECT workout_id, id FROM workout_revisions ORDER BY workout_id"
        ).all()
        bindings = connection.exec_driver_sql(
            "SELECT workout_id, id FROM workout_garmin_bindings ORDER BY workout_id"
        ).all()
    revision_one = revisions[0].id
    revision_two = revisions[1].id
    binding_one = bindings[0].id

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE workouts SET current_revision_id = ? WHERE id = 1", (revision_two,)
        )

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE workout_revisions SET parent_revision_id = ? WHERE id = ?",
            (revision_two, revision_one),
        )

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.exec_driver_sql("UPDATE workouts SET replaces_workout_id = 2 WHERE id = 1")

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO workout_garmin_operations "
            "(workout_id, binding_id, operation_type, revision_id, remote_identity_id, "
            "idempotency_key, status, created_at) VALUES "
            "(1, ?, 'upload', ?, NULL, ?, 'pending', '2026-08-22')",
            (binding_one, revision_two, "x" * 64),
        )

    with engine.begin() as connection:
        operation_id = connection.exec_driver_sql(
            "INSERT INTO workout_garmin_operations "
            "(workout_id, binding_id, operation_type, revision_id, remote_identity_id, "
            "idempotency_key, status, created_at) VALUES "
            "(1, ?, 'upload', ?, NULL, ?, 'pending', '2026-08-22')",
            (binding_one, revision_one, "y" * 64),
        ).lastrowid
        connection.exec_driver_sql(
            "INSERT INTO workout_garmin_attempts "
            "(operation_id, attempt_number, attempt_kind, status, started_at) "
            "VALUES (?, 1, 'execute', 'pending', '2026-08-22')",
            (operation_id,),
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO workout_garmin_attempts "
            "(operation_id, attempt_number, attempt_kind, status, started_at) "
            "VALUES (?, 1, 'execute', 'pending', '2026-08-22')",
            (operation_id,),
        )


def test_athlete_planning_inputs_fresh_and_filled_upgrade(tmp_path: Path) -> None:
    database_path = tmp_path / "planning.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url

    command.upgrade(config, "20260824_25")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO users (id, display_name, created_at) "
            "VALUES (1, 'Legacy Runner', '2026-08-24')"
        )
        connection.exec_driver_sql(
            "INSERT INTO workouts (id, user_id, name, sport, status, definition_version, "
            "definition, created_at, updated_at) "
            "VALUES (1, 1, 'Legacy Run', 'running', 'draft', 1, '{\"blocks\": []}', "
            "'2026-08-24', '2026-08-24')"
        )
    engine.dispose()

    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(database_url)
    with engine.begin() as connection:
        version = connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
        tables = set(
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        )
        legacy = connection.exec_driver_sql("SELECT name FROM workouts WHERE id = 1").scalar()
    engine.dispose()

    assert version == "20260828_32"
    assert {
        "athlete_planning_profiles",
        "athlete_goals",
        "athlete_availability",
        "performance_anchors",
    } <= tables
    assert legacy == "Legacy Run"


def test_training_plan_migration_creates_revision_and_membership_tables(tmp_path: Path) -> None:
    database_path = tmp_path / "training-plans.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url

    command.upgrade(config, "head")
    command.check(config)
    inspector = inspect(create_engine(database_url))

    assert {
        "training_plans",
        "training_plan_revisions",
        "training_plan_workouts",
        "training_cycles",
        "training_cycle_revisions",
        "training_cycle_weeks",
    } <= set(inspector.get_table_names())
    assert {"user_id", "week_start", "current_revision_id"} <= {
        column["name"] for column in inspector.get_columns("training_plans")
    }
    assert {"input_fingerprint", "generation_context_json", "validation_report_json"} <= {
        column["name"] for column in inspector.get_columns("training_plan_revisions")
    }
    assert "owner_user_id" in {
        column["name"] for column in inspector.get_columns("training_plan_revisions")
    }
    assert "owner_user_id" in {
        column["name"] for column in inspector.get_columns("training_plan_workouts")
    }
    assert (
        next(
            column
            for column in inspector.get_columns("training_plan_revisions")
            if column["name"] == "owner_user_id"
        )["nullable"]
        is False
    )
    assert (
        next(
            column
            for column in inspector.get_columns("training_plan_workouts")
            if column["name"] == "owner_user_id"
        )["nullable"]
        is False
    )
    assert {
        (tuple(foreign_key["constrained_columns"]), foreign_key["referred_table"])
        for foreign_key in inspector.get_foreign_keys("training_plan_workouts")
    } == {
        (("plan_revision_id", "owner_user_id"), "training_plan_revisions"),
        (("workout_id", "owner_user_id"), "workouts"),
    }
    cycle_goal_key = next(
        foreign_key
        for foreign_key in inspector.get_foreign_keys("training_cycles")
        if foreign_key["constrained_columns"] == ["goal_id", "user_id"]
    )
    assert cycle_goal_key["options"] == {}


def test_planning_constraint_rebuild_preserves_revision_graph(tmp_path: Path) -> None:
    database_path = tmp_path / "planning-rebuild.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "20260826_30")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO users (id, display_name, created_at) VALUES (1, 'Runner', '2026-08-26')"
        )
        connection.exec_driver_sql(
            "INSERT INTO athlete_goals "
            "(id, user_id, sport, event_type, event_name, target_date, status, created_at, "
            "updated_at) VALUES "
            "(1, 1, 'running', '10k', 'Herbstlauf', '2026-10-25', 'active', "
            "'2026-08-26', '2026-08-26')"
        )
        connection.exec_driver_sql(
            "INSERT INTO workouts "
            "(id, user_id, name, sport, status, definition_version, definition, created_at, "
            "updated_at) VALUES "
            "(1, 1, 'Easy Run', 'running', 'draft', 1, '{\"blocks\": []}', "
            "'2026-08-26', '2026-08-26')"
        )
        connection.exec_driver_sql(
            "INSERT INTO training_plans "
            "(id, user_id, week_start, status, current_revision_id, created_at, updated_at) "
            "VALUES (1, 1, '2026-08-31', 'active', NULL, '2026-08-26', '2026-08-26')"
        )
        connection.exec_driver_sql(
            "INSERT INTO training_plan_revisions "
            "(id, plan_id, owner_user_id, revision_number, week_start, week_end, "
            "planner_version, knowledge_base_version, input_fingerprint, "
            "generation_context_json, validation_report_json, created_at) VALUES "
            "(1, 1, 1, 1, '2026-08-31', '2026-09-06', 'weekly-v1', 'kb-v1', "
            "'weekly-fingerprint', '{}', '{\"valid\": true}', '2026-08-26')"
        )
        connection.exec_driver_sql("UPDATE training_plans SET current_revision_id = 1 WHERE id = 1")
        connection.exec_driver_sql(
            "INSERT INTO training_plan_workouts "
            "(id, plan_revision_id, workout_id, owner_user_id, position, role, scheduled_for) "
            "VALUES (1, 1, 1, 1, 0, 'easy_run', '2026-08-31')"
        )
        connection.exec_driver_sql(
            "INSERT INTO training_cycles "
            "(id, user_id, goal_id, event_type, start_date, target_date, status, "
            "current_revision_id, accepted_revision_id, created_at, updated_at) VALUES "
            "(1, 1, 1, '10k', '2026-08-31', '2026-10-25', 'active', NULL, NULL, "
            "'2026-08-26', '2026-08-26')"
        )
        for revision_id, parent_id in ((1, None), (2, 1)):
            connection.exec_driver_sql(
                "INSERT INTO training_cycle_revisions "
                "(id, cycle_id, owner_user_id, parent_revision_id, revision_number, event_type, "
                "start_date, target_date, planner_version, knowledge_base_version, "
                "input_fingerprint, confidence, phase_plan_json, assumptions_json, "
                "impact_json, validation_report_json, created_at) VALUES "
                "(?, 1, 1, ?, ?, '10k', '2026-08-31', '2026-10-25', 'multiweek-v1', "
                "'kb-v1', ?, 'medium', '[]', '{}', '{}', '{\"valid\": true}', "
                "'2026-08-26')",
                (revision_id, parent_id, revision_id, f"cycle-fingerprint-{revision_id}"),
            )
            connection.exec_driver_sql(
                "INSERT INTO training_cycle_weeks "
                "(id, cycle_revision_id, training_plan_revision_id, owner_user_id, position, "
                "week_start, phase) VALUES (?, ?, 1, 1, 0, '2026-08-31', 'base')",
                (revision_id, revision_id),
            )
        connection.exec_driver_sql(
            "UPDATE training_cycles SET current_revision_id = 2, accepted_revision_id = 1 "
            "WHERE id = 1"
        )
    engine.dispose()

    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT current_revision_id, accepted_revision_id FROM training_cycles WHERE id = 1"
        ).one() == (2, 1)
        assert connection.exec_driver_sql(
            "SELECT id, parent_revision_id FROM training_cycle_revisions ORDER BY id"
        ).all() == [(1, None), (2, 1)]
        assert connection.exec_driver_sql(
            "SELECT cycle_revision_id, training_plan_revision_id "
            "FROM training_cycle_weeks ORDER BY cycle_revision_id"
        ).all() == [(1, 1), (2, 1)]
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert (
            connection.exec_driver_sql(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' "
                "AND name = 'validate_training_cycle_revision_pointers_insert'"
            ).scalar_one()
            == 1
        )
    engine.dispose()


def test_coach_message_lineage_migration_preserves_runs_and_workouts(tmp_path: Path) -> None:
    database_path = tmp_path / "coach-message-lineage.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "20260826_31")
    engine = create_engine(database_url)

    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO users (id, display_name, created_at) VALUES (1, 'Runner', '2026-08-28')"
        )
        connection.exec_driver_sql(
            "INSERT INTO coach_conversations (id, user_id, title, created_at, updated_at) "
            "VALUES (1, 1, 'Training', '2026-08-28', '2026-08-28')"
        )
        connection.exec_driver_sql(
            "INSERT INTO coach_messages "
            "(id, conversation_id, role, content, status, model_id, created_at, completed_at) "
            "VALUES "
            "(1, 1, 'user', 'Plan', 'completed', NULL, '2026-08-28', '2026-08-28'), "
            "(2, 1, 'assistant', 'Vorschlag', 'completed', 'model-a', "
            "'2026-08-28', '2026-08-28'), "
            "(3, 1, 'user', 'Nochmal', 'completed', NULL, '2026-08-28', '2026-08-28'), "
            "(4, 1, 'assistant', '', 'failed', 'model-b', '2026-08-28', '2026-08-28'), "
            "(5, 1, 'assistant', '', 'interrupted', NULL, '2026-08-28', '2026-08-28')"
        )
        connection.exec_driver_sql(
            "INSERT INTO workouts "
            "(id, user_id, name, sport, status, definition_version, definition, source_type, "
            "approval_status, local_schedule_status, lock_version, originating_conversation_id, "
            "originating_user_message_id, originating_assistant_message_id, created_at, "
            "updated_at) "
            "VALUES "
            "(1, 1, 'Coach Run', 'running', 'confirmed', 1, '{\"blocks\": []}', "
            "'coach', 'accepted', 'unscheduled', 0, 1, 1, 2, '2026-08-28', '2026-08-28'), "
            "(2, 1, 'Manual Run', 'running', 'draft', 1, '{\"blocks\": []}', "
            "'manual', 'draft', 'unscheduled', 0, NULL, NULL, NULL, "
            "'2026-08-28', '2026-08-28')"
        )
        connection.exec_driver_sql(
            "INSERT INTO workout_revisions "
            "(id, workout_id, revision_number, name, sport, definition_version, definition, "
            "generation_context_json, source_type, generator_version, model_provider, model_id, "
            "prompt_template_version, content_hash, edit_source, created_at) VALUES "
            "(1, 1, 1, 'Coach Run', 'running', 1, '{\"blocks\": []}', "
            "'{\"source\": \"coach\"}', 'coach', 'proposal-v1', 'openrouter', 'model-a', "
            "'coach-prompt-v2', ?, 'coach', '2026-08-28'), "
            "(2, 2, 1, 'Manual Run', 'running', 1, '{\"blocks\": []}', NULL, "
            "'manual', NULL, NULL, NULL, NULL, ?, 'manual', '2026-08-28')",
            ("a" * 64, "b" * 64),
        )
        connection.exec_driver_sql(
            "UPDATE workouts SET current_revision_id = 1, accepted_revision_id = 1, "
            "materialized_revision_id = 1, accepted_at = '2026-08-28' WHERE id = 1"
        )
        connection.exec_driver_sql(
            "UPDATE workouts SET current_revision_id = 2, materialized_revision_id = 2 WHERE id = 2"
        )
        connection.exec_driver_sql(
            "INSERT INTO workout_garmin_bindings "
            "(id, workout_id, content_status, calendar_status, device_status) "
            "VALUES (1, 1, 'not_requested', 'not_requested', 'not_requested')"
        )
        connection.exec_driver_sql(
            "INSERT INTO workout_events "
            "(id, workout_id, revision_id, owner_user_id, actor_type, action, request_id, "
            "safe_metadata_json, created_at) "
            "VALUES (1, 1, 1, 1, 'coach', 'propose', 'request-1', '{}', '2026-08-28')"
        )
        connection.exec_driver_sql(
            "INSERT INTO coach_assistant_runs "
            "(id, conversation_id, user_message_id, assistant_message_id, workout_id, status, "
            "model_id, request_id, created_at, completed_at) VALUES "
            "(1, 1, 1, 2, 1, 'completed', 'model-a', 'request-1', "
            "'2026-08-28', '2026-08-28'), "
            "(2, 1, 3, 4, NULL, 'failed', 'model-b', 'request-2', "
            "'2026-08-28', '2026-08-28')"
        )
    engine.dispose()

    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(database_url)
    inspector = inspect(engine)
    with engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT request_id, prompt_template_version, operation_contract_version, "
            "failure_category FROM coach_messages WHERE role = 'assistant' ORDER BY id"
        ).all() == [
            ("request-1", "coach-prompt-v2", None, None),
            ("request-2", None, None, "failed"),
            (None, None, None, "interrupted"),
        ]
        assert connection.exec_driver_sql(
            "SELECT source_assistant_message_id FROM workouts ORDER BY id"
        ).scalars().all() == [2, None]
        assert connection.exec_driver_sql(
            "SELECT conversation_id, user_message_id, assistant_message_id, workout_id, status, "
            "model_id, request_id FROM coach_assistant_runs ORDER BY id"
        ).all() == [
            (1, 1, 2, 1, "completed", "model-a", "request-1"),
            (1, 3, 4, None, "failed", "model-b", "request-2"),
        ]
        assert connection.exec_driver_sql(
            "SELECT current_revision_id, accepted_revision_id, materialized_revision_id, "
            "originating_conversation_id, originating_user_message_id, "
            "originating_assistant_message_id FROM workouts WHERE id = 1"
        ).one() == (1, 1, 1, 1, 1, 2)
        assert connection.exec_driver_sql(
            "SELECT generation_context_json, prompt_template_version, content_hash "
            "FROM workout_revisions WHERE id = 1"
        ).one() == ('{"source": "coach"}', "coach-prompt-v2", "a" * 64)
        assert connection.exec_driver_sql("SELECT count(*) FROM workout_events").scalar_one() == 1
        assert (
            connection.exec_driver_sql("SELECT count(*) FROM workout_garmin_bindings").scalar_one()
            == 1
        )
        assert (
            connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == "20260828_32"
        )
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    source_key = next(
        key
        for key in inspector.get_foreign_keys("workouts")
        if key["constrained_columns"] == ["source_assistant_message_id"]
    )
    assert source_key["referred_table"] == "coach_messages"
    assert source_key["options"] == {"ondelete": "SET NULL"}
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE workouts SET source_assistant_message_id = 999 WHERE id = 1"
        )
    engine.dispose()


@pytest.mark.parametrize("conflict", ["conversation", "user", "role", "workout", "missing_workout"])
def test_coach_message_lineage_migration_rejects_conflicts(tmp_path: Path, conflict: str) -> None:
    database_path = tmp_path / f"coach-message-lineage-{conflict}.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "20260826_31")
    engine = create_engine(database_url)

    assistant_conversation_id = 2 if conflict == "conversation" else 1
    workout_user_id = 2 if conflict == "user" else 1
    user_role = "assistant" if conflict == "role" else "user"
    origin_assistant_message_id = 3 if conflict == "workout" else 2
    origin_ids = (
        (None, None, None) if conflict == "missing_workout" else (1, 1, origin_assistant_message_id)
    )
    run_workout_id = 2 if conflict == "missing_workout" else 1
    with engine.begin() as connection:
        if conflict == "missing_workout":
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql(
            "INSERT INTO users (id, display_name, created_at) VALUES "
            "(1, 'Runner', '2026-08-28'), (2, 'Other', '2026-08-28')"
        )
        connection.exec_driver_sql(
            "INSERT INTO coach_conversations (id, user_id, title, created_at, updated_at) VALUES "
            "(1, 1, 'One', '2026-08-28', '2026-08-28'), "
            "(2, 2, 'Two', '2026-08-28', '2026-08-28')"
        )
        connection.exec_driver_sql(
            "INSERT INTO coach_messages "
            "(id, conversation_id, role, content, status, created_at, completed_at) VALUES "
            "(1, 1, ?, 'Question', 'completed', '2026-08-28', '2026-08-28'), "
            "(2, ?, 'assistant', 'Answer', 'completed', '2026-08-28', '2026-08-28'), "
            "(3, 1, 'assistant', 'Other', 'completed', '2026-08-28', '2026-08-28')",
            (user_role, assistant_conversation_id),
        )
        connection.exec_driver_sql(
            "INSERT INTO workouts "
            "(id, user_id, name, sport, status, definition_version, definition, source_type, "
            "approval_status, local_schedule_status, lock_version, originating_conversation_id, "
            "originating_user_message_id, originating_assistant_message_id, created_at, "
            "updated_at) "
            "VALUES (1, ?, 'Run', 'running', 'draft', 1, '{\"blocks\": []}', 'coach', "
            "'proposed', 'unscheduled', 0, ?, ?, ?, '2026-08-28', '2026-08-28')",
            (workout_user_id, *origin_ids),
        )
        connection.exec_driver_sql(
            "INSERT INTO coach_assistant_runs "
            "(id, conversation_id, user_message_id, assistant_message_id, workout_id, status, "
            "created_at, completed_at) VALUES "
            "(1, 1, 1, 2, ?, 'completed', '2026-08-28', '2026-08-28')",
            (run_workout_id,),
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="coach lineage conflict"):
        command.upgrade(config, "head")

    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert (
            connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == "20260826_31"
        )
    assert "request_id" not in {
        column["name"] for column in inspect(engine).get_columns("coach_messages")
    }
    assert "source_assistant_message_id" not in {
        column["name"] for column in inspect(engine).get_columns("workouts")
    }
    engine.dispose()

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
        "coach_conversations",
        "coach_messages",
        "coach_tool_calls",
        "daily_data_statuses",
        "daily_fitness",
        "daily_health",
        "garmin_accounts",
        "garmin_devices",
        "garmin_sync_states",
        "oauth_identities",
        "sleep_stages",
        "sync_events",
        "sync_runs",
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
    assert revision == "20260822_21"
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
    assert not {
        "athlete_profiles",
        "athlete_goals",
        "athlete_availability",
        "athlete_manual_anchors",
    } & set(inspector.get_table_names())
    assert not {"fit_file", "fit_import_status", "fit_synced_at"} & {
        column["name"] for column in inspector.get_columns("activities")
    }
    with engine.connect() as connection:
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    assert revision == "20260822_21"


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

import json
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

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
        "athlete_manual_anchors",
        "athlete_profiles",
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
        "workout_steps",
        "workouts",
    } == set(inspector.get_table_names())
    assert {"fit_file", "fit_import_status", "fit_synced_at"} <= {
        column["name"] for column in inspector.get_columns("activities")
    }


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
    }


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


def test_performance_snapshot_migration_preserves_legacy_data(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy-performance.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "20260811_12")

    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO users (id, display_name, created_at) VALUES (1, 'Runner', '2026-08-11')"
        )
        connection.exec_driver_sql(
            "INSERT INTO athlete_imported_metrics "
            "(user_id, sport, metric, resource, value, source_day, fetched_at) VALUES "
            "(1, 'running', 'threshold_hr', 'lactate_threshold', 171, "
            "'2026-08-10', '2026-08-11 07:00:00'), "
            "(1, 'running', 'reference_5k_seconds', 'personal_records', 1195, "
            "'2026-08-10', '2026-08-11 07:00:00')"
        )
        connection.exec_driver_sql(
            "INSERT INTO athlete_zone_settings "
            "(user_id, sport, zone_type, zone_number, lower_boundary, upper_boundary, "
            "resource, fetched_at) VALUES "
            "(1, 'running', 'heart_rate', 1, 100, 130, 'zones', "
            "'2026-08-11 07:00:00'), "
            "(1, 'running', 'heart_rate', 2, 130, 150, 'zones', "
            "'2026-08-11 07:00:00')"
        )

    command.upgrade(config, "head")

    inspector = inspect(engine)
    assert "athlete_imported_metrics" not in inspector.get_table_names()
    assert "athlete_zone_settings" not in inspector.get_table_names()
    with engine.connect() as connection:
        metrics = connection.exec_driver_sql(
            "SELECT lactate_threshold_hr, personal_record_5k_seconds "
            "FROM daily_fitness WHERE user_id = 1 AND day = '2026-08-10'"
        ).one()
        zones = connection.exec_driver_sql(
            "SELECT heart_rate_zones FROM daily_fitness WHERE user_id = 1 AND day = '2026-08-11'"
        ).scalar_one()
    assert metrics == (171, 1195)
    assert json.loads(zones) == [
        {"sport": "running", "zone": 1, "lower": 100.0, "upper": 130.0},
        {"sport": "running", "zone": 2, "lower": 130.0, "upper": 150.0},
    ]

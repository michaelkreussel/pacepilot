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

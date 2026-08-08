from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


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

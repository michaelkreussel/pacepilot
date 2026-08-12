"""integrate performance data into daily fitness

Revision ID: 20260812_13
Revises: 20260811_12
Create Date: 2026-08-12 10:00:00
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260812_13"
down_revision: str | None = "20260811_12"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    existing_columns = {column["name"] for column in inspector.get_columns("daily_fitness")}
    new_columns = (
        sa.Column("personal_record_1k_seconds", sa.Integer()),
        sa.Column("personal_record_5k_seconds", sa.Integer()),
        sa.Column("personal_record_10k_seconds", sa.Integer()),
        sa.Column("personal_record_half_seconds", sa.Integer()),
        sa.Column("personal_record_marathon_seconds", sa.Integer()),
        sa.Column("configured_max_hr", sa.Integer()),
        sa.Column("heart_rate_zones", sa.JSON()),
        sa.Column("power_zones", sa.JSON()),
    )
    if missing_columns := [column for column in new_columns if column.name not in existing_columns]:
        with op.batch_alter_table("daily_fitness") as batch_op:
            for column in missing_columns:
                batch_op.add_column(column)

    inspector = sa.inspect(connection)
    tables = set(inspector.get_table_names())
    if "athlete_imported_metrics" in tables:
        rows = connection.execute(
            sa.text(
                "SELECT user_id, sport, metric, value, source_day, fetched_at "
                "FROM athlete_imported_metrics ORDER BY fetched_at"
            )
        ).mappings()
        for row in rows:
            day = str(row["source_day"] or row["fetched_at"])[:10]
            _ensure_fitness_row(connection, int(row["user_id"]), day, str(row["fetched_at"]))
            field = _metric_field(str(row["metric"]))
            if field is not None:
                connection.execute(
                    sa.text(
                        f"UPDATE daily_fitness SET {field} = :value "
                        "WHERE user_id = :user_id AND day = :day"
                    ),
                    {"value": row["value"], "user_id": row["user_id"], "day": day},
                )

    if "athlete_zone_settings" in tables:
        rows = list(
            connection.execute(
                sa.text(
                    "SELECT user_id, sport, zone_type, zone_number, lower_boundary, "
                    "upper_boundary, fetched_at FROM athlete_zone_settings "
                    "ORDER BY user_id, fetched_at, zone_type, sport, zone_number"
                )
            ).mappings()
        )
        grouped: dict[tuple[int, str, str], list[dict[str, object]]] = {}
        for row in rows:
            day = str(row["fetched_at"])[:10]
            key = (int(row["user_id"]), day, str(row["zone_type"]))
            grouped.setdefault(key, []).append(
                {
                    "sport": row["sport"],
                    "zone": row["zone_number"],
                    "lower": row["lower_boundary"],
                    "upper": row["upper_boundary"],
                }
            )
        for (user_id, day, zone_type), zones in grouped.items():
            _ensure_fitness_row(connection, user_id, day, day)
            field = "heart_rate_zones" if zone_type == "heart_rate" else "power_zones"
            connection.execute(
                sa.text(
                    f"UPDATE daily_fitness SET {field} = :zones "
                    "WHERE user_id = :user_id AND day = :day"
                ),
                {"zones": json.dumps(zones), "user_id": user_id, "day": day},
            )

    if "athlete_zone_settings" in tables:
        op.drop_table("athlete_zone_settings")
    if "athlete_imported_metrics" in tables:
        op.drop_table("athlete_imported_metrics")


def _ensure_fitness_row(connection: sa.Connection, user_id: int, day: str, updated_at: str) -> None:
    connection.execute(
        sa.text(
            "INSERT OR IGNORE INTO daily_fitness (user_id, day, updated_at) "
            "VALUES (:user_id, :day, :updated_at)"
        ),
        {"user_id": user_id, "day": day, "updated_at": updated_at},
    )


def _metric_field(metric: str) -> str | None:
    return {
        "threshold_hr": "lactate_threshold_hr",
        "threshold_speed_mps": "lactate_threshold_speed_mps",
        "running_threshold_power_watts": "running_ftp_watts",
        "cycling_ftp_watts": "cycling_ftp_watts",
        "prediction_5k_seconds": "race_prediction_5k_seconds",
        "prediction_10k_seconds": "race_prediction_10k_seconds",
        "prediction_half_seconds": "race_prediction_half_seconds",
        "prediction_marathon_seconds": "race_prediction_marathon_seconds",
        "reference_1k_seconds": "personal_record_1k_seconds",
        "reference_5k_seconds": "personal_record_5k_seconds",
        "reference_10k_seconds": "personal_record_10k_seconds",
        "reference_half_seconds": "personal_record_half_seconds",
        "reference_marathon_seconds": "personal_record_marathon_seconds",
        "max_hr": "configured_max_hr",
    }.get(metric)


def downgrade() -> None:
    with op.batch_alter_table("daily_fitness") as batch_op:
        batch_op.drop_column("power_zones")
        batch_op.drop_column("heart_rate_zones")
        batch_op.drop_column("configured_max_hr")
        batch_op.drop_column("personal_record_marathon_seconds")
        batch_op.drop_column("personal_record_half_seconds")
        batch_op.drop_column("personal_record_10k_seconds")
        batch_op.drop_column("personal_record_5k_seconds")
        batch_op.drop_column("personal_record_1k_seconds")

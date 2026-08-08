"""Add athlete history foundation.

Revision ID: 20260808_01
Revises: 20260805_02
Create Date: 2026-08-08 10:09:24.434145
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260808_01"
down_revision: str | None = "20260805_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "daily_data_statuses",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("resource", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("synced_at", sa.DateTime(), nullable=False),
        sa.Column("error", sa.String(length=1000), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "day", "resource"),
    )
    op.create_index(
        op.f("ix_daily_data_statuses_day"), "daily_data_statuses", ["day"], unique=False
    )
    op.create_index(
        op.f("ix_daily_data_statuses_user_id"), "daily_data_statuses", ["user_id"], unique=False
    )
    op.create_table(
        "daily_fitness",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("vo2max", sa.Float(), nullable=True),
        sa.Column("training_status", sa.String(length=50), nullable=True),
        sa.Column("training_load", sa.Float(), nullable=True),
        sa.Column("acute_load", sa.Float(), nullable=True),
        sa.Column("chronic_load", sa.Float(), nullable=True),
        sa.Column("load_ratio", sa.Float(), nullable=True),
        sa.Column("garmin_training_readiness_score", sa.Integer(), nullable=True),
        sa.Column("garmin_training_readiness_level", sa.String(length=50), nullable=True),
        sa.Column("recovery_time_minutes", sa.Integer(), nullable=True),
        sa.Column("endurance_score", sa.Float(), nullable=True),
        sa.Column("hill_score", sa.Float(), nullable=True),
        sa.Column("fitness_age", sa.Float(), nullable=True),
        sa.Column("lactate_threshold_hr", sa.Integer(), nullable=True),
        sa.Column("lactate_threshold_speed_mps", sa.Float(), nullable=True),
        sa.Column("running_ftp_watts", sa.Float(), nullable=True),
        sa.Column("cycling_ftp_watts", sa.Float(), nullable=True),
        sa.Column("race_prediction_5k_seconds", sa.Integer(), nullable=True),
        sa.Column("race_prediction_10k_seconds", sa.Integer(), nullable=True),
        sa.Column("race_prediction_half_seconds", sa.Integer(), nullable=True),
        sa.Column("race_prediction_marathon_seconds", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "day"),
    )
    op.create_index(op.f("ix_daily_fitness_day"), "daily_fitness", ["day"], unique=False)
    op.create_index(op.f("ix_daily_fitness_user_id"), "daily_fitness", ["user_id"], unique=False)
    op.create_table(
        "garmin_sync_states",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("resource", sa.String(length=50), nullable=False),
        sa.Column("oldest_synced_date", sa.Date(), nullable=True),
        sa.Column("newest_synced_date", sa.Date(), nullable=True),
        sa.Column("backfill_cursor_date", sa.Date(), nullable=True),
        sa.Column("cursor", sa.String(length=200), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("backfill_complete", sa.Boolean(), nullable=False),
        sa.Column("error", sa.String(length=1000), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "resource"),
    )
    op.create_index(
        op.f("ix_garmin_sync_states_user_id"), "garmin_sync_states", ["user_id"], unique=False
    )
    op.create_table(
        "sleep_stages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("daily_health_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(length=20), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["daily_health_id"], ["daily_health.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("daily_health_id", "position"),
    )
    op.create_index(
        op.f("ix_sleep_stages_daily_health_id"), "sleep_stages", ["daily_health_id"], unique=False
    )
    op.create_table(
        "activity_exercise_sets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("activity_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("set_type", sa.String(length=30), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("duration_s", sa.Float(), nullable=True),
        sa.Column("repetitions", sa.Integer(), nullable=True),
        sa.Column("weight_kg", sa.Float(), nullable=True),
        sa.Column("exercise_category", sa.String(length=100), nullable=True),
        sa.Column("exercise_name", sa.String(length=150), nullable=True),
        sa.Column("workout_step_index", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["activity_id"], ["activities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("activity_id", "position"),
    )
    op.create_index(
        op.f("ix_activity_exercise_sets_activity_id"),
        "activity_exercise_sets",
        ["activity_id"],
        unique=False,
    )
    op.create_table(
        "activity_splits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("activity_id", sa.Integer(), nullable=False),
        sa.Column("split_type", sa.String(length=30), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("intensity_type", sa.String(length=30), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("duration_s", sa.Float(), nullable=True),
        sa.Column("elapsed_duration_s", sa.Float(), nullable=True),
        sa.Column("moving_duration_s", sa.Float(), nullable=True),
        sa.Column("distance_m", sa.Float(), nullable=True),
        sa.Column("elevation_gain_m", sa.Float(), nullable=True),
        sa.Column("elevation_loss_m", sa.Float(), nullable=True),
        sa.Column("average_hr", sa.Integer(), nullable=True),
        sa.Column("max_hr", sa.Integer(), nullable=True),
        sa.Column("average_speed_mps", sa.Float(), nullable=True),
        sa.Column("max_speed_mps", sa.Float(), nullable=True),
        sa.Column("average_cadence", sa.Float(), nullable=True),
        sa.Column("max_cadence", sa.Float(), nullable=True),
        sa.Column("average_power_watts", sa.Float(), nullable=True),
        sa.Column("max_power_watts", sa.Float(), nullable=True),
        sa.Column("normalized_power_watts", sa.Float(), nullable=True),
        sa.Column("stride_length_cm", sa.Float(), nullable=True),
        sa.Column("ground_contact_time_ms", sa.Float(), nullable=True),
        sa.Column("vertical_oscillation_cm", sa.Float(), nullable=True),
        sa.Column("vertical_ratio", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["activity_id"], ["activities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("activity_id", "split_type", "position"),
    )
    op.create_index(
        op.f("ix_activity_splits_activity_id"), "activity_splits", ["activity_id"], unique=False
    )
    op.create_table(
        "activity_zones",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("activity_id", sa.Integer(), nullable=False),
        sa.Column("zone_type", sa.String(length=20), nullable=False),
        sa.Column("zone_number", sa.Integer(), nullable=False),
        sa.Column("low_boundary", sa.Float(), nullable=True),
        sa.Column("seconds", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["activity_id"], ["activities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("activity_id", "zone_type", "zone_number"),
    )
    op.create_index(
        op.f("ix_activity_zones_activity_id"), "activity_zones", ["activity_id"], unique=False
    )
    op.add_column("activities", sa.Column("elapsed_duration_s", sa.Float(), nullable=True))
    op.add_column("activities", sa.Column("moving_duration_s", sa.Float(), nullable=True))
    op.add_column("activities", sa.Column("average_speed_mps", sa.Float(), nullable=True))
    op.add_column("activities", sa.Column("max_speed_mps", sa.Float(), nullable=True))
    op.add_column("activities", sa.Column("min_hr", sa.Integer(), nullable=True))
    op.add_column("activities", sa.Column("elevation_loss_m", sa.Float(), nullable=True))
    op.add_column("activities", sa.Column("average_cadence", sa.Float(), nullable=True))
    op.add_column("activities", sa.Column("max_cadence", sa.Float(), nullable=True))
    op.add_column("activities", sa.Column("average_power_watts", sa.Float(), nullable=True))
    op.add_column("activities", sa.Column("max_power_watts", sa.Float(), nullable=True))
    op.add_column("activities", sa.Column("normalized_power_watts", sa.Float(), nullable=True))
    op.add_column("activities", sa.Column("aerobic_training_effect", sa.Float(), nullable=True))
    op.add_column("activities", sa.Column("anaerobic_training_effect", sa.Float(), nullable=True))
    op.add_column(
        "activities", sa.Column("training_effect_label", sa.String(length=100), nullable=True)
    )
    op.add_column("activities", sa.Column("exercise_load", sa.Float(), nullable=True))
    op.add_column("activities", sa.Column("vo2max", sa.Float(), nullable=True))
    op.add_column("activities", sa.Column("stride_length_cm", sa.Float(), nullable=True))
    op.add_column("activities", sa.Column("ground_contact_time_ms", sa.Float(), nullable=True))
    op.add_column("activities", sa.Column("vertical_oscillation_cm", sa.Float(), nullable=True))
    op.add_column("activities", sa.Column("vertical_ratio", sa.Float(), nullable=True))
    op.add_column("activities", sa.Column("workout_rpe", sa.Integer(), nullable=True))
    op.add_column("activities", sa.Column("workout_feel", sa.Integer(), nullable=True))
    op.add_column(
        "activities", sa.Column("moderate_intensity_minutes", sa.Integer(), nullable=True)
    )
    op.add_column(
        "activities", sa.Column("vigorous_intensity_minutes", sa.Integer(), nullable=True)
    )
    op.add_column("activities", sa.Column("body_battery_change", sa.Integer(), nullable=True))
    op.add_column(
        "activities",
        sa.Column("associated_garmin_workout_id", sa.String(length=100), nullable=True),
    )
    op.add_column("activities", sa.Column("workout_id", sa.Integer(), nullable=True))
    op.add_column("activities", sa.Column("details_file", sa.String(length=500), nullable=True))
    op.add_column("activities", sa.Column("source_updated_at", sa.DateTime(), nullable=True))
    op.add_column(
        "activities",
        sa.Column("details_complete", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "activities",
        sa.Column("splits_complete", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("activities", sa.Column("details_synced_at", sa.DateTime(), nullable=True))
    op.create_index(
        op.f("ix_activities_associated_garmin_workout_id"),
        "activities",
        ["associated_garmin_workout_id"],
        unique=False,
    )
    op.create_index(op.f("ix_activities_workout_id"), "activities", ["workout_id"], unique=False)
    with op.batch_alter_table("activities") as batch_op:
        batch_op.create_foreign_key(
            "fk_activities_workout_id_workouts",
            "workouts",
            ["workout_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.add_column("daily_health", sa.Column("sleep_score_duration", sa.Integer(), nullable=True))
    op.add_column("daily_health", sa.Column("sleep_score_stress", sa.Integer(), nullable=True))
    op.add_column("daily_health", sa.Column("sleep_score_awake_count", sa.Integer(), nullable=True))
    op.add_column(
        "daily_health", sa.Column("sleep_score_rem_percentage", sa.Integer(), nullable=True)
    )
    op.add_column(
        "daily_health", sa.Column("sleep_score_restlessness", sa.Integer(), nullable=True)
    )
    op.add_column(
        "daily_health", sa.Column("sleep_score_light_percentage", sa.Integer(), nullable=True)
    )
    op.add_column(
        "daily_health", sa.Column("sleep_score_deep_percentage", sa.Integer(), nullable=True)
    )
    op.add_column("daily_health", sa.Column("sleep_start_at", sa.DateTime(), nullable=True))
    op.add_column("daily_health", sa.Column("sleep_end_at", sa.DateTime(), nullable=True))
    op.add_column("daily_health", sa.Column("deep_sleep_seconds", sa.Integer(), nullable=True))
    op.add_column("daily_health", sa.Column("light_sleep_seconds", sa.Integer(), nullable=True))
    op.add_column("daily_health", sa.Column("rem_sleep_seconds", sa.Integer(), nullable=True))
    op.add_column("daily_health", sa.Column("awake_sleep_seconds", sa.Integer(), nullable=True))
    op.add_column("daily_health", sa.Column("nap_seconds", sa.Integer(), nullable=True))
    op.add_column("daily_health", sa.Column("sleep_need_seconds", sa.Integer(), nullable=True))
    op.add_column(
        "daily_health", sa.Column("sleep_need_baseline_seconds", sa.Integer(), nullable=True)
    )
    op.add_column("daily_health", sa.Column("next_sleep_need_seconds", sa.Integer(), nullable=True))
    op.add_column("daily_health", sa.Column("sleep_average_hr", sa.Float(), nullable=True))
    op.add_column("daily_health", sa.Column("sleep_average_stress", sa.Float(), nullable=True))
    op.add_column(
        "daily_health", sa.Column("sleep_body_battery_change", sa.Integer(), nullable=True)
    )
    op.add_column("daily_health", sa.Column("min_hr", sa.Integer(), nullable=True))
    op.add_column("daily_health", sa.Column("max_hr", sa.Integer(), nullable=True))
    op.add_column("daily_health", sa.Column("hrv_weekly_average", sa.Float(), nullable=True))
    op.add_column("daily_health", sa.Column("hrv_five_min_high", sa.Float(), nullable=True))
    op.add_column("daily_health", sa.Column("hrv_status", sa.String(length=30), nullable=True))
    op.add_column("daily_health", sa.Column("hrv_baseline_low", sa.Float(), nullable=True))
    op.add_column("daily_health", sa.Column("hrv_baseline_balanced_low", sa.Float(), nullable=True))
    op.add_column(
        "daily_health", sa.Column("hrv_baseline_balanced_high", sa.Float(), nullable=True)
    )
    op.add_column("daily_health", sa.Column("distance_m", sa.Float(), nullable=True))
    op.add_column("daily_health", sa.Column("total_calories", sa.Integer(), nullable=True))
    op.add_column("daily_health", sa.Column("active_calories", sa.Integer(), nullable=True))
    op.add_column("daily_health", sa.Column("stress_max", sa.Integer(), nullable=True))
    op.add_column("daily_health", sa.Column("body_battery_low", sa.Integer(), nullable=True))
    op.add_column("daily_health", sa.Column("body_battery_charged", sa.Integer(), nullable=True))
    op.add_column("daily_health", sa.Column("body_battery_drained", sa.Integer(), nullable=True))
    op.add_column(
        "daily_health", sa.Column("waking_respiration_average", sa.Float(), nullable=True)
    )
    op.add_column("daily_health", sa.Column("sleep_respiration_average", sa.Float(), nullable=True))
    op.add_column("daily_health", sa.Column("spo2_average", sa.Float(), nullable=True))
    op.add_column("daily_health", sa.Column("sleep_spo2_average", sa.Float(), nullable=True))
    op.add_column("daily_health", sa.Column("spo2_lowest", sa.Float(), nullable=True))
    op.add_column(
        "daily_health", sa.Column("moderate_intensity_minutes", sa.Integer(), nullable=True)
    )
    op.add_column(
        "daily_health", sa.Column("vigorous_intensity_minutes", sa.Integer(), nullable=True)
    )
    op.add_column(
        "daily_health",
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default="2026-08-08 00:00:00",
        ),
    )


def downgrade() -> None:
    op.drop_column("daily_health", "updated_at")
    op.drop_column("daily_health", "vigorous_intensity_minutes")
    op.drop_column("daily_health", "moderate_intensity_minutes")
    op.drop_column("daily_health", "spo2_lowest")
    op.drop_column("daily_health", "sleep_spo2_average")
    op.drop_column("daily_health", "spo2_average")
    op.drop_column("daily_health", "sleep_respiration_average")
    op.drop_column("daily_health", "waking_respiration_average")
    op.drop_column("daily_health", "body_battery_drained")
    op.drop_column("daily_health", "body_battery_charged")
    op.drop_column("daily_health", "body_battery_low")
    op.drop_column("daily_health", "stress_max")
    op.drop_column("daily_health", "active_calories")
    op.drop_column("daily_health", "total_calories")
    op.drop_column("daily_health", "distance_m")
    op.drop_column("daily_health", "hrv_baseline_balanced_high")
    op.drop_column("daily_health", "hrv_baseline_balanced_low")
    op.drop_column("daily_health", "hrv_baseline_low")
    op.drop_column("daily_health", "hrv_status")
    op.drop_column("daily_health", "hrv_five_min_high")
    op.drop_column("daily_health", "hrv_weekly_average")
    op.drop_column("daily_health", "max_hr")
    op.drop_column("daily_health", "min_hr")
    op.drop_column("daily_health", "sleep_body_battery_change")
    op.drop_column("daily_health", "sleep_average_stress")
    op.drop_column("daily_health", "sleep_average_hr")
    op.drop_column("daily_health", "next_sleep_need_seconds")
    op.drop_column("daily_health", "sleep_need_baseline_seconds")
    op.drop_column("daily_health", "sleep_need_seconds")
    op.drop_column("daily_health", "nap_seconds")
    op.drop_column("daily_health", "awake_sleep_seconds")
    op.drop_column("daily_health", "rem_sleep_seconds")
    op.drop_column("daily_health", "light_sleep_seconds")
    op.drop_column("daily_health", "deep_sleep_seconds")
    op.drop_column("daily_health", "sleep_end_at")
    op.drop_column("daily_health", "sleep_start_at")
    op.drop_column("daily_health", "sleep_score_deep_percentage")
    op.drop_column("daily_health", "sleep_score_light_percentage")
    op.drop_column("daily_health", "sleep_score_restlessness")
    op.drop_column("daily_health", "sleep_score_rem_percentage")
    op.drop_column("daily_health", "sleep_score_awake_count")
    op.drop_column("daily_health", "sleep_score_stress")
    op.drop_column("daily_health", "sleep_score_duration")
    with op.batch_alter_table("activities") as batch_op:
        batch_op.drop_constraint("fk_activities_workout_id_workouts", type_="foreignkey")
    op.drop_index(op.f("ix_activities_workout_id"), table_name="activities")
    op.drop_index(op.f("ix_activities_associated_garmin_workout_id"), table_name="activities")
    op.drop_column("activities", "details_synced_at")
    op.drop_column("activities", "splits_complete")
    op.drop_column("activities", "details_complete")
    op.drop_column("activities", "source_updated_at")
    op.drop_column("activities", "details_file")
    op.drop_column("activities", "workout_id")
    op.drop_column("activities", "associated_garmin_workout_id")
    op.drop_column("activities", "body_battery_change")
    op.drop_column("activities", "vigorous_intensity_minutes")
    op.drop_column("activities", "moderate_intensity_minutes")
    op.drop_column("activities", "workout_feel")
    op.drop_column("activities", "workout_rpe")
    op.drop_column("activities", "vertical_ratio")
    op.drop_column("activities", "vertical_oscillation_cm")
    op.drop_column("activities", "ground_contact_time_ms")
    op.drop_column("activities", "stride_length_cm")
    op.drop_column("activities", "vo2max")
    op.drop_column("activities", "exercise_load")
    op.drop_column("activities", "training_effect_label")
    op.drop_column("activities", "anaerobic_training_effect")
    op.drop_column("activities", "aerobic_training_effect")
    op.drop_column("activities", "normalized_power_watts")
    op.drop_column("activities", "max_power_watts")
    op.drop_column("activities", "average_power_watts")
    op.drop_column("activities", "max_cadence")
    op.drop_column("activities", "average_cadence")
    op.drop_column("activities", "elevation_loss_m")
    op.drop_column("activities", "min_hr")
    op.drop_column("activities", "max_speed_mps")
    op.drop_column("activities", "average_speed_mps")
    op.drop_column("activities", "moving_duration_s")
    op.drop_column("activities", "elapsed_duration_s")
    op.drop_index(op.f("ix_activity_zones_activity_id"), table_name="activity_zones")
    op.drop_table("activity_zones")
    op.drop_index(op.f("ix_activity_splits_activity_id"), table_name="activity_splits")
    op.drop_table("activity_splits")
    op.drop_index(
        op.f("ix_activity_exercise_sets_activity_id"), table_name="activity_exercise_sets"
    )
    op.drop_table("activity_exercise_sets")
    op.drop_index(op.f("ix_sleep_stages_daily_health_id"), table_name="sleep_stages")
    op.drop_table("sleep_stages")
    op.drop_index(op.f("ix_garmin_sync_states_user_id"), table_name="garmin_sync_states")
    op.drop_table("garmin_sync_states")
    op.drop_index(op.f("ix_daily_fitness_user_id"), table_name="daily_fitness")
    op.drop_index(op.f("ix_daily_fitness_day"), table_name="daily_fitness")
    op.drop_table("daily_fitness")
    op.drop_index(op.f("ix_daily_data_statuses_user_id"), table_name="daily_data_statuses")
    op.drop_index(op.f("ix_daily_data_statuses_day"), table_name="daily_data_statuses")
    op.drop_table("daily_data_statuses")

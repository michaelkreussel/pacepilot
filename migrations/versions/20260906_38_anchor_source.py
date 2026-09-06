"""track performance anchor origin for Garmin sync

Revision ID: 20260906_38
Revises: 20260901_37
Create Date: 2026-09-06 14:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260906_38"
down_revision: str | None = "20260901_37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _rebuild(table: str, create_sql: str, columns: str, select_sql: str) -> None:
    op.execute(create_sql)
    op.execute(f"INSERT INTO {table}_new ({columns}) {select_sql}")
    op.execute(f"DROP TABLE {table}")
    op.execute(f"ALTER TABLE {table}_new RENAME TO {table}")


def upgrade() -> None:
    connection = op.get_bind()
    connection.exec_driver_sql("PRAGMA foreign_keys=OFF")

    _rebuild(
        "performance_anchors",
        """
        CREATE TABLE performance_anchors_new (
            id INTEGER NOT NULL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            kind VARCHAR(20) NOT NULL,
            source VARCHAR(20) NOT NULL DEFAULT 'manual',
            distance_m FLOAT NOT NULL,
            duration_s FLOAT NOT NULL,
            achieved_on DATE NOT NULL,
            reliable BOOLEAN NOT NULL,
            notes TEXT,
            created_at DATETIME NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
            CONSTRAINT ck_performance_anchors_kind CHECK (
                kind IN ('race', 'time_trial', 'manual')
            ),
            CONSTRAINT ck_performance_anchors_source CHECK (
                source IN ('manual', 'garmin')
            ),
            CONSTRAINT ck_performance_anchors_distance_positive CHECK (distance_m > 0),
            CONSTRAINT ck_performance_anchors_duration_positive CHECK (duration_s > 0)
        )
        """,
        "id, user_id, kind, source, distance_m, duration_s, achieved_on, reliable, "
        "notes, created_at",
        "SELECT id, user_id, kind, 'manual', distance_m, duration_s, achieved_on, "
        "reliable, notes, created_at FROM performance_anchors",
    )
    op.create_index(op.f("ix_performance_anchors_user_id"), "performance_anchors", ["user_id"])
    op.create_index(
        "ix_performance_anchors_user_achieved",
        "performance_anchors",
        ["user_id", "achieved_on"],
    )

    connection.exec_driver_sql("PRAGMA foreign_keys=ON")


def downgrade() -> None:
    connection = op.get_bind()
    connection.exec_driver_sql("PRAGMA foreign_keys=OFF")

    _rebuild(
        "performance_anchors",
        """
        CREATE TABLE performance_anchors_new (
            id INTEGER NOT NULL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            kind VARCHAR(20) NOT NULL,
            distance_m FLOAT NOT NULL,
            duration_s FLOAT NOT NULL,
            achieved_on DATE NOT NULL,
            reliable BOOLEAN NOT NULL,
            notes TEXT,
            created_at DATETIME NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
            CONSTRAINT ck_performance_anchors_kind CHECK (
                kind IN ('race', 'time_trial', 'manual')
            ),
            CONSTRAINT ck_performance_anchors_distance_positive CHECK (distance_m > 0),
            CONSTRAINT ck_performance_anchors_duration_positive CHECK (duration_s > 0)
        )
        """,
        "id, user_id, kind, distance_m, duration_s, achieved_on, reliable, notes, created_at",
        "SELECT id, user_id, kind, distance_m, duration_s, achieved_on, "
        "reliable, notes, created_at FROM performance_anchors",
    )
    op.create_index(op.f("ix_performance_anchors_user_id"), "performance_anchors", ["user_id"])
    op.create_index(
        "ix_performance_anchors_user_achieved",
        "performance_anchors",
        ["user_id", "achieved_on"],
    )

    connection.exec_driver_sql("PRAGMA foreign_keys=ON")

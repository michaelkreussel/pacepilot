"""rebuild planning tables with final ownership constraints

Revision ID: 20260826_31
Revises: 20260826_30
Create Date: 2026-08-26 20:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260826_31"
down_revision: str | None = "20260826_30"
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

    for trigger in (
        "validate_training_plan_current_revision",
        "prevent_training_plan_revisions_update",
        "prevent_training_plan_workouts_update",
        "validate_training_cycle_revision_pointers",
        "validate_training_cycle_revision_pointers_insert",
        "prevent_training_cycle_revisions_update",
        "prevent_training_cycle_weeks_update",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")

    _rebuild(
        "athlete_planning_profiles",
        """
        CREATE TABLE athlete_planning_profiles_new (
            user_id INTEGER NOT NULL PRIMARY KEY,
            experience_level VARCHAR(20),
            preferred_long_run_weekday INTEGER,
            self_declared_reentry BOOLEAN NOT NULL,
            constraint_note TEXT,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
            CONSTRAINT ck_athlete_planning_profiles_experience_level CHECK (
                experience_level IS NULL OR experience_level IN
                ('novice', 'intermediate', 'advanced')
            ),
            CONSTRAINT ck_athlete_planning_profiles_long_run_weekday CHECK (
                preferred_long_run_weekday IS NULL OR preferred_long_run_weekday BETWEEN 0 AND 6
            )
        )
        """,
        "user_id, experience_level, preferred_long_run_weekday, self_declared_reentry, "
        "constraint_note, created_at, updated_at",
        "SELECT user_id, experience_level, preferred_long_run_weekday, self_declared_reentry, "
        "constraint_note, created_at, updated_at FROM athlete_planning_profiles",
    )

    _rebuild(
        "training_plans",
        """
        CREATE TABLE training_plans_new (
            id INTEGER NOT NULL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            week_start DATE NOT NULL,
            status VARCHAR(20) NOT NULL,
            current_revision_id INTEGER,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
            CONSTRAINT uq_training_plans_id_user_id UNIQUE (id, user_id),
            CONSTRAINT uq_training_plans_user_week UNIQUE (user_id, week_start),
            CONSTRAINT ck_training_plans_status CHECK (status IN ('active', 'archived'))
        )
        """,
        "id, user_id, week_start, status, current_revision_id, created_at, updated_at",
        "SELECT id, user_id, week_start, status, current_revision_id, created_at, updated_at "
        "FROM training_plans",
    )
    op.create_index("ix_training_plans_user_id", "training_plans", ["user_id"])
    op.create_index(
        "ix_training_plans_current_revision_id", "training_plans", ["current_revision_id"]
    )

    _rebuild(
        "training_plan_revisions",
        """
        CREATE TABLE training_plan_revisions_new (
            id INTEGER NOT NULL PRIMARY KEY,
            plan_id INTEGER NOT NULL,
            owner_user_id INTEGER NOT NULL,
            revision_number INTEGER NOT NULL,
            week_start DATE NOT NULL,
            week_end DATE NOT NULL,
            planner_version VARCHAR(100) NOT NULL,
            knowledge_base_version VARCHAR(200) NOT NULL,
            input_fingerprint VARCHAR(64) NOT NULL,
            generation_context_json JSON NOT NULL,
            validation_report_json JSON NOT NULL,
            created_at DATETIME NOT NULL,
            CONSTRAINT fk_training_plan_revisions_plan_owner \
            FOREIGN KEY(plan_id, owner_user_id) \
            REFERENCES training_plans (id, user_id) ON DELETE CASCADE,
            CONSTRAINT uq_training_plan_revisions_id_owner UNIQUE (id, owner_user_id),
            CONSTRAINT uq_training_plan_revisions_number UNIQUE (plan_id, revision_number),
            CONSTRAINT uq_training_plan_revisions_fingerprint UNIQUE (plan_id, input_fingerprint),
            CONSTRAINT ck_training_plan_revisions_number_positive CHECK (revision_number >= 1)
        )
        """,
        "id, plan_id, owner_user_id, revision_number, week_start, week_end, planner_version, "
        "knowledge_base_version, input_fingerprint, generation_context_json, "
        "validation_report_json, created_at",
        "SELECT r.id, r.plan_id, COALESCE(r.owner_user_id, p.user_id), r.revision_number, "
        "r.week_start, r.week_end, r.planner_version, r.knowledge_base_version, "
        "r.input_fingerprint, r.generation_context_json, r.validation_report_json, r.created_at "
        "FROM training_plan_revisions r JOIN training_plans p ON p.id = r.plan_id",
    )
    op.create_index("ix_training_plan_revisions_plan_id", "training_plan_revisions", ["plan_id"])

    _rebuild(
        "training_plan_workouts",
        """
        CREATE TABLE training_plan_workouts_new (
            id INTEGER NOT NULL PRIMARY KEY,
            plan_revision_id INTEGER NOT NULL,
            workout_id INTEGER NOT NULL,
            owner_user_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            role VARCHAR(30) NOT NULL,
            scheduled_for DATE NOT NULL,
            CONSTRAINT fk_training_plan_workouts_revision_owner \
            FOREIGN KEY(plan_revision_id, owner_user_id) \
            REFERENCES training_plan_revisions (id, owner_user_id) ON DELETE CASCADE,
            CONSTRAINT fk_training_plan_workouts_workout_owner \
            FOREIGN KEY(workout_id, owner_user_id) \
            REFERENCES workouts (id, user_id) ON DELETE CASCADE,
            CONSTRAINT uq_training_plan_workouts_position UNIQUE (plan_revision_id, position),
            CONSTRAINT uq_training_plan_workouts_workout UNIQUE (plan_revision_id, workout_id)
        )
        """,
        "id, plan_revision_id, workout_id, owner_user_id, position, role, scheduled_for",
        "SELECT m.id, m.plan_revision_id, m.workout_id, "
        "COALESCE(m.owner_user_id, r.owner_user_id, w.user_id), m.position, m.role, "
        "m.scheduled_for FROM training_plan_workouts m "
        "JOIN training_plan_revisions r ON r.id = m.plan_revision_id "
        "JOIN workouts w ON w.id = m.workout_id",
    )
    op.create_index(
        "ix_training_plan_workouts_plan_revision_id",
        "training_plan_workouts",
        ["plan_revision_id"],
    )
    op.create_index(
        "ix_training_plan_workouts_workout_id", "training_plan_workouts", ["workout_id"]
    )
    op.create_index(
        "ix_training_plan_workouts_scheduled_for", "training_plan_workouts", ["scheduled_for"]
    )

    _rebuild(
        "training_cycles",
        """
        CREATE TABLE training_cycles_new (
            id INTEGER NOT NULL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            goal_id INTEGER,
            event_type VARCHAR(30) NOT NULL,
            start_date DATE NOT NULL,
            target_date DATE NOT NULL,
            status VARCHAR(20) NOT NULL,
            current_revision_id INTEGER,
            accepted_revision_id INTEGER,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
            CONSTRAINT fk_training_cycles_goal_owner FOREIGN KEY(goal_id, user_id) \
            REFERENCES athlete_goals (id, user_id),
            CONSTRAINT uq_training_cycles_id_user_id UNIQUE (id, user_id),
            CONSTRAINT uq_training_cycles_user_goal_start UNIQUE (user_id, goal_id, start_date),
            CONSTRAINT ck_training_cycles_event_type CHECK (
                event_type IN ('general_fitness', '5k', '10k', 'half_marathon', 'marathon')
            ),
            CONSTRAINT ck_training_cycles_status CHECK (status IN ('active', 'archived')),
            CONSTRAINT ck_training_cycles_dates CHECK (target_date > start_date)
        )
        """,
        "id, user_id, goal_id, event_type, start_date, target_date, status, "
        "current_revision_id, accepted_revision_id, created_at, updated_at",
        "SELECT id, user_id, goal_id, event_type, start_date, target_date, status, "
        "current_revision_id, accepted_revision_id, created_at, updated_at FROM training_cycles",
    )
    op.create_index("ix_training_cycles_user_id", "training_cycles", ["user_id"])
    op.create_index("ix_training_cycles_goal_id", "training_cycles", ["goal_id"])
    op.create_index(
        "ix_training_cycles_current_revision_id", "training_cycles", ["current_revision_id"]
    )
    op.create_index(
        "ix_training_cycles_accepted_revision_id", "training_cycles", ["accepted_revision_id"]
    )

    _rebuild(
        "training_cycle_revisions",
        """
        CREATE TABLE training_cycle_revisions_new (
            id INTEGER NOT NULL PRIMARY KEY,
            cycle_id INTEGER NOT NULL,
            owner_user_id INTEGER NOT NULL,
            parent_revision_id INTEGER,
            revision_number INTEGER NOT NULL,
            event_type VARCHAR(30) NOT NULL,
            start_date DATE NOT NULL,
            target_date DATE NOT NULL,
            planner_version VARCHAR(100) NOT NULL,
            knowledge_base_version VARCHAR(200) NOT NULL,
            input_fingerprint VARCHAR(64) NOT NULL,
            confidence VARCHAR(20) NOT NULL,
            phase_plan_json JSON NOT NULL,
            assumptions_json JSON NOT NULL,
            impact_json JSON NOT NULL,
            validation_report_json JSON NOT NULL,
            created_at DATETIME NOT NULL,
            CONSTRAINT fk_training_cycle_revisions_cycle_owner \
            FOREIGN KEY(cycle_id, owner_user_id) \
            REFERENCES training_cycles (id, user_id) ON DELETE CASCADE,
            CONSTRAINT fk_training_cycle_revisions_parent_same_cycle \
            FOREIGN KEY(parent_revision_id, cycle_id, owner_user_id) \
            REFERENCES training_cycle_revisions (id, cycle_id, owner_user_id),
            CONSTRAINT uq_training_cycle_revisions_id_owner UNIQUE (id, owner_user_id),
            CONSTRAINT uq_training_cycle_revisions_id_cycle_owner \
            UNIQUE (id, cycle_id, owner_user_id),
            CONSTRAINT uq_training_cycle_revisions_number UNIQUE (cycle_id, revision_number),
            CONSTRAINT uq_training_cycle_revisions_fingerprint UNIQUE (cycle_id, input_fingerprint),
            CONSTRAINT ck_training_cycle_revisions_number_positive CHECK (revision_number >= 1)
        )
        """,
        "id, cycle_id, owner_user_id, parent_revision_id, revision_number, event_type, "
        "start_date, target_date, planner_version, knowledge_base_version, input_fingerprint, "
        "confidence, phase_plan_json, assumptions_json, impact_json, validation_report_json, "
        "created_at",
        "SELECT id, cycle_id, owner_user_id, parent_revision_id, revision_number, event_type, "
        "start_date, target_date, planner_version, knowledge_base_version, input_fingerprint, "
        "confidence, phase_plan_json, assumptions_json, impact_json, validation_report_json, "
        "created_at FROM training_cycle_revisions",
    )
    op.create_index(
        "ix_training_cycle_revisions_cycle_id", "training_cycle_revisions", ["cycle_id"]
    )
    op.create_index(
        "ix_training_cycle_revisions_parent_revision_id",
        "training_cycle_revisions",
        ["parent_revision_id"],
    )

    _rebuild(
        "training_cycle_weeks",
        """
        CREATE TABLE training_cycle_weeks_new (
            id INTEGER NOT NULL PRIMARY KEY,
            cycle_revision_id INTEGER NOT NULL,
            training_plan_revision_id INTEGER NOT NULL,
            owner_user_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            week_start DATE NOT NULL,
            phase VARCHAR(20) NOT NULL,
            CONSTRAINT fk_training_cycle_weeks_revision_owner \
            FOREIGN KEY(cycle_revision_id, owner_user_id) \
            REFERENCES training_cycle_revisions (id, owner_user_id) ON DELETE CASCADE,
            CONSTRAINT fk_training_cycle_weeks_plan_revision_owner \
            FOREIGN KEY(training_plan_revision_id, owner_user_id) \
            REFERENCES training_plan_revisions (id, owner_user_id) ON DELETE CASCADE,
            CONSTRAINT uq_training_cycle_weeks_position UNIQUE (cycle_revision_id, position),
            CONSTRAINT uq_training_cycle_weeks_plan_revision \
            UNIQUE (cycle_revision_id, training_plan_revision_id)
        )
        """,
        "id, cycle_revision_id, training_plan_revision_id, owner_user_id, position, "
        "week_start, phase",
        "SELECT id, cycle_revision_id, training_plan_revision_id, owner_user_id, position, "
        "week_start, phase FROM training_cycle_weeks",
    )
    op.create_index(
        "ix_training_cycle_weeks_cycle_revision_id",
        "training_cycle_weeks",
        ["cycle_revision_id"],
    )
    op.create_index(
        "ix_training_cycle_weeks_training_plan_revision_id",
        "training_cycle_weeks",
        ["training_plan_revision_id"],
    )
    op.create_index(
        "ix_training_cycle_weeks_owner_user_id", "training_cycle_weeks", ["owner_user_id"]
    )
    op.create_index("ix_training_cycle_weeks_week_start", "training_cycle_weeks", ["week_start"])

    op.execute(
        "CREATE TRIGGER prevent_training_plan_revisions_update BEFORE UPDATE ON "
        "training_plan_revisions BEGIN SELECT RAISE(ABORT, "
        "'Training plan revisions and memberships are immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER prevent_training_plan_workouts_update BEFORE UPDATE ON "
        "training_plan_workouts BEGIN SELECT RAISE(ABORT, "
        "'Training plan revisions and memberships are immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER validate_training_plan_current_revision BEFORE UPDATE OF "
        "current_revision_id ON training_plans WHEN NEW.current_revision_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM training_plan_revisions WHERE "
        "id = NEW.current_revision_id AND plan_id = NEW.id) BEGIN SELECT RAISE(ABORT, "
        "'Current revision must belong to its plan'); END"
    )
    op.execute(
        "CREATE TRIGGER prevent_training_cycle_revisions_update BEFORE UPDATE ON "
        "training_cycle_revisions BEGIN SELECT RAISE(ABORT, "
        "'Training cycle revisions and memberships are immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER prevent_training_cycle_weeks_update BEFORE UPDATE ON "
        "training_cycle_weeks BEGIN SELECT RAISE(ABORT, "
        "'Training cycle revisions and memberships are immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER validate_training_cycle_revision_pointers BEFORE UPDATE OF "
        "current_revision_id, accepted_revision_id ON training_cycles WHEN "
        "(NEW.current_revision_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM "
        "training_cycle_revisions WHERE id = NEW.current_revision_id AND cycle_id = NEW.id "
        "AND owner_user_id = NEW.user_id)) OR (NEW.accepted_revision_id IS NOT NULL AND NOT "
        "EXISTS (SELECT 1 FROM training_cycle_revisions WHERE id = NEW.accepted_revision_id "
        "AND cycle_id = NEW.id AND owner_user_id = NEW.user_id)) BEGIN SELECT RAISE(ABORT, "
        "'Cycle revision must belong to its cycle'); END"
    )
    op.execute(
        "CREATE TRIGGER validate_training_cycle_revision_pointers_insert BEFORE INSERT ON "
        "training_cycles WHEN (NEW.current_revision_id IS NOT NULL AND NOT EXISTS (SELECT 1 "
        "FROM training_cycle_revisions WHERE id = NEW.current_revision_id AND cycle_id = NEW.id "
        "AND owner_user_id = NEW.user_id)) OR (NEW.accepted_revision_id IS NOT NULL AND NOT "
        "EXISTS (SELECT 1 FROM training_cycle_revisions WHERE id = NEW.accepted_revision_id "
        "AND cycle_id = NEW.id AND owner_user_id = NEW.user_id)) BEGIN SELECT RAISE(ABORT, "
        "'Cycle revision must belong to its cycle'); END"
    )

    connection.exec_driver_sql("PRAGMA foreign_keys=ON")


def downgrade() -> None:
    # This migration normalizes divergent historical schemas and is intentionally irreversible.
    pass

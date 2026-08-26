"""add immutable workout revisions and bindings

Revision ID: 20260822_16
Revises: 20260819_15
Create Date: 2026-08-22 18:00:00
"""

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "20260822_16"
down_revision: str | None = "20260819_15"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_value(value: object) -> object:
    return json.loads(value) if isinstance(value, str) else value


def _content_hash(workout: sa.RowMapping) -> str:
    suggested_for = workout["scheduled_for"]
    if isinstance(suggested_for, date):
        suggested_for = suggested_for.isoformat()
    payload = {
        "name": workout["name"],
        "sport": workout["sport"],
        "suggested_for": suggested_for,
        "description": workout["description"],
        "definition_version": workout["definition_version"],
        "definition": _json_value(workout["definition"]),
        "purpose": None,
        "guidance_json": None,
        "load_estimate_json": None,
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _validation_report(definition: object) -> dict[str, object]:
    parsed = _json_value(definition)
    valid = isinstance(parsed, dict) and bool(parsed.get("blocks"))
    return {
        "valid": valid,
        "issues": [] if valid else [{"code": "definition.blocks_required"}],
        "validator": "legacy_backfill_v1",
    }


def _garmin_states(
    status: str, remote_id: object, scheduled_for: object
) -> tuple[str, str, str, str | None]:
    has_remote = bool(remote_id)
    has_date = scheduled_for is not None
    if not has_remote and status in {"draft", "confirmed"}:
        return "not_requested", "not_requested", "not_requested", None
    if has_remote and status in {"draft", "confirmed"}:
        return "unknown", "unknown", "unknown", "legacy_remote_state_requires_review"
    if has_remote and status == "published":
        return "synced", "unknown" if has_date else "not_requested", "not_requested", None
    if has_remote and status == "pushed":
        return (
            "synced",
            "unknown" if has_date else "not_requested",
            "request_accepted",
            None,
        )
    return "unknown", "unknown", "unknown", "legacy_remote_state_requires_review"


def _add_workout_columns() -> None:
    existing_columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("workouts")
    }
    columns = (
        sa.Column("source_type", sa.String(length=30), server_default="manual", nullable=False),
        sa.Column("approval_status", sa.String(length=30), server_default="draft", nullable=False),
        sa.Column(
            "local_schedule_status",
            sa.String(length=30),
            server_default="unscheduled",
            nullable=False,
        ),
        sa.Column("current_revision_id", sa.Integer()),
        sa.Column("accepted_revision_id", sa.Integer()),
        sa.Column("materialized_revision_id", sa.Integer()),
        sa.Column("accepted_at", sa.DateTime()),
        sa.Column("accepted_by_user_id", sa.Integer()),
        sa.Column("expires_at", sa.DateTime()),
        sa.Column("lock_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("replaces_workout_id", sa.Integer()),
        sa.Column("originating_conversation_id", sa.Integer()),
        sa.Column("originating_user_message_id", sa.Integer()),
        sa.Column("originating_assistant_message_id", sa.Integer()),
        sa.Column("deleted_at", sa.DateTime()),
    )
    for column in columns:
        if column.name not in existing_columns:
            op.add_column("workouts", column)

    inspector = sa.inspect(op.get_bind())
    unique_names = {
        constraint["name"] for constraint in inspector.get_unique_constraints("workouts")
    }
    check_names = {constraint["name"] for constraint in inspector.get_check_constraints("workouts")}
    foreign_key_names = {
        constraint["name"] for constraint in inspector.get_foreign_keys("workouts")
    }
    with op.batch_alter_table("workouts") as batch_op:
        if "uq_workouts_id_user_id" not in unique_names:
            batch_op.create_unique_constraint("uq_workouts_id_user_id", ["id", "user_id"])
        if "ck_workouts_lock_version_nonnegative" not in check_names:
            batch_op.create_check_constraint(
                "ck_workouts_lock_version_nonnegative", "lock_version >= 0"
            )
        for name, target, local_column in (
            ("fk_workouts_accepted_by_user", "users", "accepted_by_user_id"),
            (
                "fk_workouts_originating_conversation",
                "coach_conversations",
                "originating_conversation_id",
            ),
            (
                "fk_workouts_originating_user_message",
                "coach_messages",
                "originating_user_message_id",
            ),
            (
                "fk_workouts_originating_assistant_message",
                "coach_messages",
                "originating_assistant_message_id",
            ),
        ):
            if name not in foreign_key_names:
                batch_op.create_foreign_key(
                    name,
                    target,
                    [local_column],
                    ["id"],
                    ondelete="SET NULL",
                )
    foreign_key_names = {
        constraint["name"] for constraint in sa.inspect(op.get_bind()).get_foreign_keys("workouts")
    }
    if "fk_workouts_replaces_same_user" not in foreign_key_names:
        with op.batch_alter_table("workouts") as batch_op:
            batch_op.create_foreign_key(
                "fk_workouts_replaces_same_user",
                "workouts",
                ["replaces_workout_id", "user_id"],
                ["id", "user_id"],
                deferrable=True,
                initially="DEFERRED",
            )


def _create_revision_tables() -> None:
    op.create_table(
        "workout_revisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workout_id", sa.Integer(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("parent_revision_id", sa.Integer()),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("sport", sa.String(length=30), nullable=False),
        sa.Column("suggested_for", sa.Date()),
        sa.Column("description", sa.Text()),
        sa.Column("definition_version", sa.Integer(), nullable=False),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("purpose", sa.Text()),
        sa.Column("guidance_json", sa.JSON()),
        sa.Column("load_estimate_json", sa.JSON()),
        sa.Column("validation_report_json", sa.JSON()),
        sa.Column("generation_context_json", sa.JSON()),
        sa.Column("source_type", sa.String(length=30), nullable=False),
        sa.Column("generator_version", sa.String(length=100)),
        sa.Column("template_id", sa.String(length=100)),
        sa.Column("template_version", sa.String(length=100)),
        sa.Column("rule_set_version", sa.String(length=100)),
        sa.Column("knowledge_base_version", sa.String(length=100)),
        sa.Column("model_provider", sa.String(length=100)),
        sa.Column("model_id", sa.String(length=200)),
        sa.Column("prompt_template_version", sa.String(length=100)),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("edit_source", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("revision_number >= 1", name="ck_workout_revisions_number_positive"),
        sa.ForeignKeyConstraint(["workout_id"], ["workouts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["parent_revision_id", "workout_id"],
            ["workout_revisions.id", "workout_revisions.workout_id"],
            name="fk_workout_revisions_parent_same_workout",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workout_id", "revision_number", name="uq_workout_revisions_number"),
        sa.UniqueConstraint("id", "workout_id", name="uq_workout_revisions_id_workout_id"),
    )
    op.create_index("ix_workout_revisions_workout_id", "workout_revisions", ["workout_id"])
    op.create_index(
        "ix_workout_revisions_parent_revision_id", "workout_revisions", ["parent_revision_id"]
    )
    op.create_index("ix_workout_revisions_content_hash", "workout_revisions", ["content_hash"])
    op.execute(
        "CREATE TRIGGER prevent_workout_revision_update "
        "BEFORE UPDATE ON workout_revisions "
        "BEGIN SELECT RAISE(ABORT, 'Workout revisions are immutable'); END"
    )

    op.create_table(
        "workout_validation_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workout_id", sa.Integer(), nullable=False),
        sa.Column("revision_id", sa.Integer(), nullable=False),
        sa.Column("validation_kind", sa.String(length=30), nullable=False),
        sa.Column("rule_set_version", sa.String(length=100), nullable=False),
        sa.Column("context_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("feedback_ids_json", sa.JSON(), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime()),
        sa.Column("valid", sa.Boolean(), nullable=False),
        sa.Column("report_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["revision_id", "workout_id"],
            ["workout_revisions.id", "workout_revisions.workout_id"],
            name="fk_workout_validation_runs_revision_same_workout",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_workout_validation_runs_workout_id", "workout_validation_runs", ["workout_id"]
    )
    op.create_index(
        "ix_workout_validation_runs_revision_id", "workout_validation_runs", ["revision_id"]
    )
    op.create_index(
        "ix_workout_validation_runs_revision_kind_evaluated",
        "workout_validation_runs",
        ["revision_id", "validation_kind", "evaluated_at"],
    )
    op.create_index(
        "ix_workout_validation_runs_context_fingerprint",
        "workout_validation_runs",
        ["context_fingerprint"],
    )

    op.create_table(
        "workout_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workout_id", sa.Integer(), nullable=False),
        sa.Column("revision_id", sa.Integer()),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("actor_type", sa.String(length=30), nullable=False),
        sa.Column("actor_user_id", sa.Integer()),
        sa.Column("action", sa.String(length=30), nullable=False),
        sa.Column("request_id", sa.String(length=100)),
        sa.Column("idempotency_key", sa.String(length=200)),
        sa.Column("safe_metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "idempotency_key IS NULL OR length(idempotency_key) > 0",
            name="ck_workout_events_idempotency_key_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["workout_id", "owner_user_id"],
            ["workouts.id", "workouts.user_id"],
            name="fk_workout_events_workout_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["revision_id", "workout_id"],
            ["workout_revisions.id", "workout_revisions.workout_id"],
            name="fk_workout_events_revision_same_workout",
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_workout_events_workout_id", "workout_events", ["workout_id"])
    op.create_index("ix_workout_events_revision_id", "workout_events", ["revision_id"])
    op.create_index("ix_workout_events_owner_user_id", "workout_events", ["owner_user_id"])
    op.create_index("ix_workout_events_created_at", "workout_events", ["created_at"])
    op.create_index(
        "uq_workout_events_owner_action_idempotency_key",
        "workout_events",
        ["owner_user_id", "action", "idempotency_key"],
        unique=True,
        sqlite_where=sa.text("idempotency_key IS NOT NULL AND idempotency_key <> ''"),
    )


def _create_binding_tables() -> None:
    op.create_table(
        "workout_garmin_bindings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workout_id", sa.Integer(), nullable=False),
        sa.Column("active_remote_identity_id", sa.Integer()),
        sa.Column("content_status", sa.String(length=30), nullable=False),
        sa.Column("calendar_status", sa.String(length=30), nullable=False),
        sa.Column("device_status", sa.String(length=30), nullable=False),
        sa.Column("remote_scheduled_for", sa.Date()),
        sa.Column("last_attempt_at", sa.DateTime()),
        sa.Column("last_success_at", sa.DateTime()),
        sa.Column("last_error_code", sa.String(length=100)),
        sa.Column("last_error_message", sa.String(length=1000)),
        sa.ForeignKeyConstraint(["workout_id"], ["workouts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workout_id", name="uq_workout_garmin_bindings_workout_id"),
        sa.UniqueConstraint("id", "workout_id", name="uq_workout_garmin_bindings_id_workout_id"),
    )
    op.create_index(
        "ix_workout_garmin_bindings_workout_id", "workout_garmin_bindings", ["workout_id"]
    )
    op.create_index(
        "ix_workout_garmin_bindings_active_remote_identity_id",
        "workout_garmin_bindings",
        ["active_remote_identity_id"],
    )

    op.create_table(
        "workout_garmin_remote_identities",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("binding_id", sa.Integer(), nullable=False),
        sa.Column("garmin_account_id", sa.Integer(), nullable=False),
        sa.Column("garmin_workout_id", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("removed_at", sa.DateTime()),
        sa.CheckConstraint(
            "status IN ('active', 'removed')",
            name="ck_workout_garmin_remote_identity_status",
        ),
        sa.CheckConstraint(
            "(status = 'active' AND removed_at IS NULL) OR "
            "(status = 'removed' AND removed_at IS NOT NULL)",
            name="ck_workout_garmin_remote_identity_removed_at",
        ),
        sa.ForeignKeyConstraint(["binding_id"], ["workout_garmin_bindings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["garmin_account_id"], ["garmin_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "garmin_account_id",
            "garmin_workout_id",
            name="uq_workout_garmin_remote_identity_account_workout",
        ),
        sa.UniqueConstraint(
            "id", "binding_id", name="uq_workout_garmin_remote_identity_id_binding"
        ),
    )
    op.create_index(
        "ix_workout_garmin_remote_identities_binding_id",
        "workout_garmin_remote_identities",
        ["binding_id"],
    )
    op.create_index(
        "ix_workout_garmin_remote_identities_garmin_account_id",
        "workout_garmin_remote_identities",
        ["garmin_account_id"],
    )
    op.create_index(
        "uq_workout_garmin_remote_identity_active_binding",
        "workout_garmin_remote_identities",
        ["binding_id"],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
    )


def _backfill() -> None:
    connection = op.get_bind()
    migrated_at = datetime.now(UTC).replace(tzinfo=None)
    workouts = list(
        connection.execute(
            sa.text(
                "SELECT id, user_id, name, sport, scheduled_for, description, status, "
                "garmin_workout_id, definition_version, definition, created_at "
                "FROM workouts ORDER BY id"
            )
        ).mappings()
    )
    valid_statuses = {"draft", "confirmed", "published", "pushed"}
    invalid = [row["id"] for row in workouts if row["status"] not in valid_statuses]
    if invalid:
        raise RuntimeError(f"Unsupported legacy workout status for IDs: {invalid}")

    for workout in workouts:
        workout_id = int(workout["id"])
        user_id = int(workout["user_id"])
        revision_result = connection.execute(
            sa.text(
                "INSERT INTO workout_revisions "
                "(workout_id, revision_number, parent_revision_id, name, sport, suggested_for, "
                "description, definition_version, definition, purpose, guidance_json, "
                "load_estimate_json, validation_report_json, generation_context_json, "
                "source_type, generator_version, template_id, template_version, "
                "rule_set_version, knowledge_base_version, model_provider, model_id, "
                "prompt_template_version, content_hash, edit_source, created_at) VALUES "
                "(:workout_id, 1, NULL, :name, :sport, :suggested_for, :description, "
                ":definition_version, :definition, NULL, NULL, NULL, :validation_report, NULL, "
                "'manual', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, :content_hash, "
                "'legacy_backfill', :created_at)"
            ),
            {
                "workout_id": workout_id,
                "name": workout["name"],
                "sport": workout["sport"],
                "suggested_for": workout["scheduled_for"],
                "description": workout["description"],
                "definition_version": workout["definition_version"],
                "definition": json.dumps(_json_value(workout["definition"]), separators=(",", ":")),
                "validation_report": json.dumps(
                    _validation_report(workout["definition"]), separators=(",", ":")
                ),
                "content_hash": _content_hash(workout),
                "created_at": workout["created_at"],
            },
        )
        revision_id = int(revision_result.lastrowid or 0)
        if not revision_id:
            raise RuntimeError(f"Failed to create revision for workout {workout_id}")

        status = str(workout["status"])
        accepted = status != "draft"
        local_status = (
            "scheduled" if accepted and workout["scheduled_for"] is not None else "unscheduled"
        )
        connection.execute(
            sa.text(
                "UPDATE workouts SET source_type = 'manual', approval_status = :approval, "
                "local_schedule_status = :local_status, current_revision_id = :revision_id, "
                "accepted_revision_id = :accepted_revision_id, "
                "materialized_revision_id = :revision_id, scheduled_for = :scheduled_for "
                "WHERE id = :workout_id"
            ),
            {
                "approval": "accepted" if accepted else "draft",
                "local_status": local_status,
                "revision_id": revision_id,
                "accepted_revision_id": revision_id if accepted else None,
                "scheduled_for": workout["scheduled_for"] if accepted else None,
                "workout_id": workout_id,
            },
        )

        content_status, calendar_status, device_status, error_code = _garmin_states(
            status, workout["garmin_workout_id"], workout["scheduled_for"]
        )
        binding_result = connection.execute(
            sa.text(
                "INSERT INTO workout_garmin_bindings "
                "(workout_id, active_remote_identity_id, content_status, calendar_status, "
                "device_status, remote_scheduled_for, last_attempt_at, last_success_at, "
                "last_error_code, last_error_message) VALUES "
                "(:workout_id, NULL, :content_status, :calendar_status, :device_status, NULL, "
                "NULL, NULL, :error_code, NULL)"
            ),
            {
                "workout_id": workout_id,
                "content_status": content_status,
                "calendar_status": calendar_status,
                "device_status": device_status,
                "error_code": error_code,
            },
        )
        binding_id = int(binding_result.lastrowid or 0)
        remote_id = workout["garmin_workout_id"]
        if remote_id:
            account_id = connection.execute(
                sa.text("SELECT id FROM garmin_accounts WHERE user_id = :user_id"),
                {"user_id": user_id},
            ).scalar_one_or_none()
            if account_id is None:
                account_result = connection.execute(
                    sa.text(
                        "INSERT INTO garmin_accounts "
                        "(user_id, email, connected_at, last_sync_at, rate_limit_until, "
                        "sync_status, sync_error) VALUES "
                        "(:user_id, NULL, NULL, NULL, NULL, 'not_connected', NULL)"
                    ),
                    {"user_id": user_id},
                )
                account_id = int(account_result.lastrowid or 0)
            identity_result = connection.execute(
                sa.text(
                    "INSERT INTO workout_garmin_remote_identities "
                    "(binding_id, garmin_account_id, garmin_workout_id, status, created_at, "
                    "removed_at) VALUES (:binding_id, :account_id, :remote_id, 'active', "
                    ":created_at, NULL)"
                ),
                {
                    "binding_id": binding_id,
                    "account_id": account_id,
                    "remote_id": str(remote_id),
                    "created_at": migrated_at,
                },
            )
            connection.execute(
                sa.text(
                    "UPDATE workout_garmin_bindings SET active_remote_identity_id = :identity_id "
                    "WHERE id = :binding_id"
                ),
                {
                    "identity_id": int(identity_result.lastrowid or 0),
                    "binding_id": binding_id,
                },
            )

        connection.execute(
            sa.text(
                "INSERT INTO workout_events "
                "(workout_id, revision_id, owner_user_id, actor_type, actor_user_id, action, "
                "request_id, idempotency_key, safe_metadata_json, created_at) VALUES "
                "(:workout_id, :revision_id, :owner_user_id, 'system', NULL, 'legacy_backfill', "
                "NULL, NULL, :metadata, :created_at)"
            ),
            {
                "workout_id": workout_id,
                "revision_id": revision_id,
                "owner_user_id": user_id,
                "metadata": json.dumps(
                    {
                        "legacy_status": status,
                        "had_remote_id": bool(remote_id),
                        "had_scheduled_for": workout["scheduled_for"] is not None,
                        "schema_revision": revision,
                    },
                    separators=(",", ":"),
                ),
                "created_at": migrated_at,
            },
        )


def _add_pointer_constraints() -> None:
    with op.batch_alter_table("workouts") as batch_op:
        for column, name in (
            ("current_revision_id", "fk_workouts_current_revision_same_workout"),
            ("accepted_revision_id", "fk_workouts_accepted_revision_same_workout"),
            ("materialized_revision_id", "fk_workouts_materialized_revision_same_workout"),
        ):
            batch_op.create_foreign_key(
                name,
                "workout_revisions",
                [column, "id"],
                ["id", "workout_id"],
                deferrable=True,
                initially="DEFERRED",
            )
    with op.batch_alter_table("workout_garmin_bindings") as batch_op:
        batch_op.create_foreign_key(
            "fk_workout_garmin_bindings_active_identity",
            "workout_garmin_remote_identities",
            ["active_remote_identity_id", "id"],
            ["id", "binding_id"],
            deferrable=True,
            initially="DEFERRED",
        )


def _create_workout_indexes() -> None:
    for column in (
        "source_type",
        "approval_status",
        "local_schedule_status",
        "current_revision_id",
        "accepted_revision_id",
        "materialized_revision_id",
        "replaces_workout_id",
        "deleted_at",
    ):
        op.create_index(f"ix_workouts_{column}", "workouts", [column])


def upgrade() -> None:
    _add_workout_columns()
    _create_revision_tables()
    _create_binding_tables()
    _add_pointer_constraints()
    _backfill()
    _create_workout_indexes()


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS prevent_workout_revision_update")
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE workouts SET scheduled_for = "
            "(SELECT suggested_for FROM workout_revisions "
            "WHERE workout_revisions.id = workouts.current_revision_id) "
            "WHERE status = 'draft'"
        )
    )

    for column in (
        "deleted_at",
        "replaces_workout_id",
        "materialized_revision_id",
        "accepted_revision_id",
        "current_revision_id",
        "local_schedule_status",
        "approval_status",
        "source_type",
    ):
        op.drop_index(f"ix_workouts_{column}", table_name="workouts")

    with op.batch_alter_table("workout_garmin_bindings") as batch_op:
        batch_op.drop_constraint("fk_workout_garmin_bindings_active_identity", type_="foreignkey")
    op.drop_table("workout_garmin_remote_identities")
    op.drop_table("workout_garmin_bindings")
    op.drop_table("workout_validation_runs")
    op.drop_table("workout_events")

    with op.batch_alter_table("workouts") as batch_op:
        batch_op.drop_constraint(
            "fk_workouts_materialized_revision_same_workout", type_="foreignkey"
        )
        batch_op.drop_constraint("fk_workouts_accepted_revision_same_workout", type_="foreignkey")
        batch_op.drop_constraint("fk_workouts_current_revision_same_workout", type_="foreignkey")
    op.drop_table("workout_revisions")

    with op.batch_alter_table("workouts") as batch_op:
        batch_op.drop_constraint("fk_workouts_replaces_same_user", type_="foreignkey")
        batch_op.drop_constraint("fk_workouts_originating_assistant_message", type_="foreignkey")
        batch_op.drop_constraint("fk_workouts_originating_user_message", type_="foreignkey")
        batch_op.drop_constraint("fk_workouts_originating_conversation", type_="foreignkey")
        batch_op.drop_constraint("fk_workouts_accepted_by_user", type_="foreignkey")
        batch_op.drop_constraint("ck_workouts_lock_version_nonnegative", type_="check")
        batch_op.drop_constraint("uq_workouts_id_user_id", type_="unique")
        for column in (
            "deleted_at",
            "originating_assistant_message_id",
            "originating_user_message_id",
            "originating_conversation_id",
            "replaces_workout_id",
            "lock_version",
            "expires_at",
            "accepted_by_user_id",
            "accepted_at",
            "materialized_revision_id",
            "accepted_revision_id",
            "current_revision_id",
            "local_schedule_status",
            "approval_status",
            "source_type",
        ):
            batch_op.drop_column(column)

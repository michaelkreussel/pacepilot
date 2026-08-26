from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    DDL,
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.user import utcnow

if TYPE_CHECKING:
    from app.models.activity import Activity
    from app.models.user import User


class Workout(Base):
    __tablename__ = "workouts"
    __table_args__ = (
        UniqueConstraint("id", "user_id", name="uq_workouts_id_user_id"),
        ForeignKeyConstraint(
            ["current_revision_id", "id"],
            ["workout_revisions.id", "workout_revisions.workout_id"],
            name="fk_workouts_current_revision_same_workout",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["accepted_revision_id", "id"],
            ["workout_revisions.id", "workout_revisions.workout_id"],
            name="fk_workouts_accepted_revision_same_workout",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["materialized_revision_id", "id"],
            ["workout_revisions.id", "workout_revisions.workout_id"],
            name="fk_workouts_materialized_revision_same_workout",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["replaces_workout_id", "user_id"],
            ["workouts.id", "workouts.user_id"],
            name="fk_workouts_replaces_same_user",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint("lock_version >= 0", name="ck_workouts_lock_version_nonnegative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement="ignore_fk")
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    sport: Mapped[str] = mapped_column(String(30), default="running")
    scheduled_for: Mapped[date | None] = mapped_column(Date, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="draft", index=True)
    garmin_workout_id: Mapped[str | None] = mapped_column(String(100))
    definition_version: Mapped[int] = mapped_column(Integer, default=1)
    definition: Mapped[dict[str, object]] = mapped_column(JSON, default=lambda: {"blocks": []})
    source_type: Mapped[str] = mapped_column(String(30), default="manual", index=True)
    approval_status: Mapped[str] = mapped_column(String(30), default="draft", index=True)
    local_schedule_status: Mapped[str] = mapped_column(
        String(30), default="unscheduled", index=True
    )
    current_revision_id: Mapped[int | None] = mapped_column(Integer, index=True)
    accepted_revision_id: Mapped[int | None] = mapped_column(Integer, index=True)
    materialized_revision_id: Mapped[int | None] = mapped_column(Integer, index=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime)
    accepted_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    lock_version: Mapped[int] = mapped_column(Integer, default=0)
    replaces_workout_id: Mapped[int | None] = mapped_column(Integer, index=True)
    originating_conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("coach_conversations.id", ondelete="SET NULL")
    )
    originating_user_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("coach_messages.id", ondelete="SET NULL")
    )
    originating_assistant_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("coach_messages.id", ondelete="SET NULL")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    user: Mapped["User"] = relationship(back_populates="workouts", foreign_keys=[user_id])
    steps: Mapped[list["WorkoutStep"]] = relationship(
        back_populates="workout", cascade="all, delete-orphan", order_by="WorkoutStep.position"
    )
    completed_activities: Mapped[list["Activity"]] = relationship(back_populates="workout")
    revisions: Mapped[list["WorkoutRevision"]] = relationship(
        back_populates="workout",
        cascade="all, delete-orphan",
        foreign_keys="WorkoutRevision.workout_id",
        order_by="WorkoutRevision.revision_number",
    )
    garmin_binding: Mapped["WorkoutGarminBinding | None"] = relationship(
        back_populates="workout", cascade="all, delete-orphan", uselist=False
    )

    @property
    def definition_model(self):
        from app.services.planning.workout_definition import parse_definition

        return parse_definition(self.definition, self.definition_version)

    @property
    def step_count(self) -> int:
        from app.services.planning.workout_definition import workout_metrics

        return workout_metrics(self.definition_model).step_count


class WorkoutStep(Base):
    __tablename__ = "workout_steps"
    __table_args__ = (UniqueConstraint("workout_id", "position"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workout_id: Mapped[int] = mapped_column(
        ForeignKey("workouts.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    step_type: Mapped[str] = mapped_column(String(30))
    duration_type: Mapped[str] = mapped_column(String(30))
    duration_value: Mapped[float | None] = mapped_column(Float)
    target_type: Mapped[str] = mapped_column(String(30), default="no_target")
    target_min: Mapped[float | None] = mapped_column(Float)
    target_max: Mapped[float | None] = mapped_column(Float)
    repeat_count: Mapped[int | None] = mapped_column(Integer)

    workout: Mapped[Workout] = relationship(back_populates="steps")


class WorkoutRevision(Base):
    __tablename__ = "workout_revisions"
    __table_args__ = (
        UniqueConstraint("workout_id", "revision_number", name="uq_workout_revisions_number"),
        UniqueConstraint("id", "workout_id", name="uq_workout_revisions_id_workout_id"),
        ForeignKeyConstraint(
            ["parent_revision_id", "workout_id"],
            ["workout_revisions.id", "workout_revisions.workout_id"],
            name="fk_workout_revisions_parent_same_workout",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint("revision_number >= 1", name="ck_workout_revisions_number_positive"),
        Index("ix_workout_revisions_content_hash", "content_hash"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workout_id: Mapped[int] = mapped_column(
        ForeignKey("workouts.id", ondelete="CASCADE"), index=True
    )
    revision_number: Mapped[int] = mapped_column(Integer)
    parent_revision_id: Mapped[int | None] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(200))
    sport: Mapped[str] = mapped_column(String(30))
    suggested_for: Mapped[date | None] = mapped_column(Date)
    description: Mapped[str | None] = mapped_column(Text)
    definition_version: Mapped[int] = mapped_column(Integer)
    definition: Mapped[dict[str, object]] = mapped_column(JSON)
    purpose: Mapped[str | None] = mapped_column(Text)
    guidance_json: Mapped[dict[str, object] | None] = mapped_column(JSON)
    load_estimate_json: Mapped[dict[str, object] | None] = mapped_column(JSON)
    validation_report_json: Mapped[dict[str, object] | None] = mapped_column(JSON)
    generation_context_json: Mapped[dict[str, object] | None] = mapped_column(JSON)
    source_type: Mapped[str] = mapped_column(String(30), default="manual")
    generator_version: Mapped[str | None] = mapped_column(String(100))
    template_id: Mapped[str | None] = mapped_column(String(100))
    template_version: Mapped[str | None] = mapped_column(String(100))
    rule_set_version: Mapped[str | None] = mapped_column(String(100))
    knowledge_base_version: Mapped[str | None] = mapped_column(String(100))
    model_provider: Mapped[str | None] = mapped_column(String(100))
    model_id: Mapped[str | None] = mapped_column(String(200))
    prompt_template_version: Mapped[str | None] = mapped_column(String(100))
    content_hash: Mapped[str] = mapped_column(String(64))
    edit_source: Mapped[str] = mapped_column(String(30), default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    workout: Mapped[Workout] = relationship(back_populates="revisions", foreign_keys=[workout_id])
    validation_runs: Mapped[list["WorkoutValidationRun"]] = relationship(
        back_populates="revision", cascade="all, delete-orphan"
    )

    @property
    def definition_model(self):
        from app.services.planning.workout_definition import parse_definition

        return parse_definition(self.definition, self.definition_version)

    @property
    def step_count(self) -> int:
        from app.services.planning.workout_definition import workout_metrics

        return workout_metrics(self.definition_model).step_count


@event.listens_for(WorkoutRevision, "before_update")
def _prevent_revision_update(_mapper: Any, _connection: Any, _target: WorkoutRevision) -> None:
    raise ValueError("Workout revisions are immutable")


event.listen(
    WorkoutRevision.__table__,
    "after_create",
    DDL(
        "CREATE TRIGGER prevent_workout_revision_update "
        "BEFORE UPDATE ON workout_revisions "
        "BEGIN SELECT RAISE(ABORT, 'Workout revisions are immutable'); END"
    ).execute_if(dialect="sqlite"),
)


class WorkoutValidationRun(Base):
    __tablename__ = "workout_validation_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["revision_id", "workout_id"],
            ["workout_revisions.id", "workout_revisions.workout_id"],
            name="fk_workout_validation_runs_revision_same_workout",
            ondelete="CASCADE",
        ),
        Index(
            "ix_workout_validation_runs_revision_kind_evaluated",
            "revision_id",
            "validation_kind",
            "evaluated_at",
        ),
        Index("ix_workout_validation_runs_context_fingerprint", "context_fingerprint"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workout_id: Mapped[int] = mapped_column(Integer, index=True)
    revision_id: Mapped[int] = mapped_column(Integer, index=True)
    validation_kind: Mapped[str] = mapped_column(String(30))
    rule_set_version: Mapped[str] = mapped_column(String(100))
    context_fingerprint: Mapped[str] = mapped_column(String(64))
    feedback_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    valid: Mapped[bool] = mapped_column(Boolean)
    report_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)

    revision: Mapped[WorkoutRevision] = relationship(
        back_populates="validation_runs",
        foreign_keys=[revision_id, workout_id],
    )


class WorkoutEvent(Base):
    __tablename__ = "workout_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workout_id", "owner_user_id"],
            ["workouts.id", "workouts.user_id"],
            name="fk_workout_events_workout_owner",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["revision_id", "workout_id"],
            ["workout_revisions.id", "workout_revisions.workout_id"],
            name="fk_workout_events_revision_same_workout",
        ),
        CheckConstraint(
            "idempotency_key IS NULL OR length(idempotency_key) > 0",
            name="ck_workout_events_idempotency_key_nonempty",
        ),
        Index("ix_workout_events_workout_id", "workout_id"),
        Index("ix_workout_events_revision_id", "revision_id"),
        Index("ix_workout_events_owner_user_id", "owner_user_id"),
        Index("ix_workout_events_created_at", "created_at"),
        Index(
            "uq_workout_events_owner_action_idempotency_key",
            "owner_user_id",
            "action",
            "idempotency_key",
            unique=True,
            sqlite_where=text("idempotency_key IS NOT NULL AND idempotency_key <> ''"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workout_id: Mapped[int] = mapped_column(Integer)
    revision_id: Mapped[int | None] = mapped_column(Integer)
    owner_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    actor_type: Mapped[str] = mapped_column(String(30))
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(30))
    request_id: Mapped[str | None] = mapped_column(String(100))
    idempotency_key: Mapped[str | None] = mapped_column(String(200))
    safe_metadata_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class WorkoutGarminBinding(Base):
    __tablename__ = "workout_garmin_bindings"
    __table_args__ = (
        UniqueConstraint("workout_id", name="uq_workout_garmin_bindings_workout_id"),
        UniqueConstraint("id", "workout_id", name="uq_workout_garmin_bindings_id_workout_id"),
        ForeignKeyConstraint(
            ["active_remote_identity_id", "id"],
            ["workout_garmin_remote_identities.id", "workout_garmin_remote_identities.binding_id"],
            name="fk_workout_garmin_bindings_active_identity",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement="ignore_fk")
    workout_id: Mapped[int] = mapped_column(
        ForeignKey("workouts.id", ondelete="CASCADE"), index=True
    )
    active_remote_identity_id: Mapped[int | None] = mapped_column(Integer, index=True)
    content_status: Mapped[str] = mapped_column(String(30), default="not_requested")
    calendar_status: Mapped[str] = mapped_column(String(30), default="not_requested")
    device_status: Mapped[str] = mapped_column(String(30), default="not_requested")
    remote_scheduled_for: Mapped[date | None] = mapped_column(Date)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_message: Mapped[str | None] = mapped_column(String(1000))

    workout: Mapped[Workout] = relationship(back_populates="garmin_binding")
    remote_identities: Mapped[list["WorkoutGarminRemoteIdentity"]] = relationship(
        back_populates="binding",
        cascade="all, delete-orphan",
        foreign_keys="WorkoutGarminRemoteIdentity.binding_id",
    )
    operations: Mapped[list["WorkoutGarminOperation"]] = relationship(
        back_populates="binding", cascade="all, delete-orphan"
    )


class WorkoutGarminRemoteIdentity(Base):
    __tablename__ = "workout_garmin_remote_identities"
    __table_args__ = (
        UniqueConstraint(
            "garmin_account_id",
            "garmin_workout_id",
            name="uq_workout_garmin_remote_identity_account_workout",
        ),
        UniqueConstraint("id", "binding_id", name="uq_workout_garmin_remote_identity_id_binding"),
        CheckConstraint(
            "status IN ('active', 'removed')",
            name="ck_workout_garmin_remote_identity_status",
        ),
        CheckConstraint(
            "(status = 'active' AND removed_at IS NULL) OR "
            "(status = 'removed' AND removed_at IS NOT NULL)",
            name="ck_workout_garmin_remote_identity_removed_at",
        ),
        Index(
            "uq_workout_garmin_remote_identity_active_binding",
            "binding_id",
            unique=True,
            sqlite_where=text("status = 'active'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    binding_id: Mapped[int] = mapped_column(
        ForeignKey("workout_garmin_bindings.id", ondelete="CASCADE"), index=True
    )
    garmin_account_id: Mapped[int] = mapped_column(
        ForeignKey("garmin_accounts.id", ondelete="CASCADE"), index=True
    )
    garmin_workout_id: Mapped[str] = mapped_column(String(100))
    principal_fingerprint: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime)

    binding: Mapped[WorkoutGarminBinding] = relationship(
        back_populates="remote_identities", foreign_keys=[binding_id]
    )


class WorkoutGarminOperation(Base):
    __tablename__ = "workout_garmin_operations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["binding_id", "workout_id"],
            ["workout_garmin_bindings.id", "workout_garmin_bindings.workout_id"],
            name="fk_workout_garmin_operations_binding_same_workout",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["revision_id", "workout_id"],
            ["workout_revisions.id", "workout_revisions.workout_id"],
            name="fk_workout_garmin_operations_revision_same_workout",
        ),
        ForeignKeyConstraint(
            ["remote_identity_id", "binding_id"],
            ["workout_garmin_remote_identities.id", "workout_garmin_remote_identities.binding_id"],
            name="fk_workout_garmin_operations_identity_same_binding",
        ),
        CheckConstraint(
            "operation_type IN ('upload', 'update', 'schedule', 'unschedule', 'push', 'delete')",
            name="ck_workout_garmin_operations_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'succeeded', 'retryable', 'unknown', 'failed_final')",
            name="ck_workout_garmin_operations_status",
        ),
        CheckConstraint(
            "(operation_type = 'upload' AND remote_identity_id IS NULL) OR "
            "(operation_type <> 'upload' AND remote_identity_id IS NOT NULL)",
            name="ck_workout_garmin_operations_identity_required",
        ),
        CheckConstraint("length(idempotency_key) > 0", name="ck_workout_garmin_operations_key"),
        UniqueConstraint("idempotency_key", name="uq_workout_garmin_operations_key"),
        Index("ix_workout_garmin_operations_binding_status", "binding_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workout_id: Mapped[int] = mapped_column(Integer, index=True)
    binding_id: Mapped[int] = mapped_column(Integer, index=True)
    operation_type: Mapped[str] = mapped_column(String(30))
    revision_id: Mapped[int] = mapped_column(Integer, index=True)
    remote_identity_id: Mapped[int | None] = mapped_column(Integer, index=True)
    scheduled_for: Mapped[date | None] = mapped_column(Date)
    idempotency_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    remote_reference: Mapped[str | None] = mapped_column(String(200))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    binding: Mapped[WorkoutGarminBinding] = relationship(back_populates="operations")
    attempts: Mapped[list["WorkoutGarminAttempt"]] = relationship(
        back_populates="operation",
        cascade="all, delete-orphan",
        order_by="WorkoutGarminAttempt.attempt_number",
    )


class WorkoutGarminAttempt(Base):
    __tablename__ = "workout_garmin_attempts"
    __table_args__ = (
        UniqueConstraint(
            "operation_id", "attempt_number", name="uq_workout_garmin_attempts_number"
        ),
        CheckConstraint(
            "attempt_kind IN ('execute', 'reconcile')",
            name="ck_workout_garmin_attempts_kind",
        ),
        CheckConstraint(
            "status IN ('pending', 'succeeded', 'retryable', 'unknown', 'failed')",
            name="ck_workout_garmin_attempts_status",
        ),
        CheckConstraint("attempt_number >= 1", name="ck_workout_garmin_attempts_number"),
        Index("ix_workout_garmin_attempts_operation_status", "operation_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    operation_id: Mapped[int] = mapped_column(
        ForeignKey("workout_garmin_operations.id", ondelete="CASCADE"), index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer)
    attempt_kind: Mapped[str] = mapped_column(String(20), default="execute")
    status: Mapped[str] = mapped_column(String(30), default="pending")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(String(1000))

    operation: Mapped[WorkoutGarminOperation] = relationship(back_populates="attempts")

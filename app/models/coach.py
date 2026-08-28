from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.user import utcnow

if TYPE_CHECKING:
    from app.models.user import User
    from app.models.workout import Workout


class CoachConversation(Base):
    __tablename__ = "coach_conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(160), default="Neuer Chat")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    user: Mapped["User"] = relationship(back_populates="coach_conversations")
    messages: Mapped[list["CoachMessage"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="CoachMessage.id",
    )
    assistant_runs: Mapped[list["CoachAssistantRun"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="CoachAssistantRun.id",
    )


class CoachMessage(Base):
    __tablename__ = "coach_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("coach_conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="completed")
    model_id: Mapped[str | None] = mapped_column(String(200))
    request_id: Mapped[str | None] = mapped_column(String(100))
    prompt_template_version: Mapped[str | None] = mapped_column(String(100))
    operation_contract_version: Mapped[str | None] = mapped_column(String(100))
    failure_category: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)

    conversation: Mapped[CoachConversation] = relationship(back_populates="messages")
    tool_calls: Mapped[list["CoachToolCall"]] = relationship(
        back_populates="message",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="CoachToolCall.id",
    )
    generated_run: Mapped["CoachAssistantRun | None"] = relationship(
        back_populates="assistant_message",
        foreign_keys="CoachAssistantRun.assistant_message_id",
        uselist=False,
        passive_deletes=True,
    )


class CoachAssistantRun(Base):
    __tablename__ = "coach_assistant_runs"
    __table_args__ = (
        UniqueConstraint("assistant_message_id"),
        UniqueConstraint("workout_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("coach_conversations.id", ondelete="CASCADE"), index=True
    )
    user_message_id: Mapped[int] = mapped_column(
        ForeignKey("coach_messages.id", ondelete="CASCADE"), index=True
    )
    assistant_message_id: Mapped[int] = mapped_column(
        ForeignKey("coach_messages.id", ondelete="CASCADE"), index=True
    )
    workout_id: Mapped[int | None] = mapped_column(
        ForeignKey("workouts.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(20), default="streaming")
    model_id: Mapped[str | None] = mapped_column(String(200))
    request_id: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)

    conversation: Mapped[CoachConversation] = relationship(back_populates="assistant_runs")
    user_message: Mapped[CoachMessage] = relationship(foreign_keys=[user_message_id])
    assistant_message: Mapped[CoachMessage] = relationship(
        back_populates="generated_run", foreign_keys=[assistant_message_id]
    )
    workout: Mapped["Workout | None"] = relationship()


class CoachToolCall(Base):
    __tablename__ = "coach_tool_calls"
    __table_args__ = (UniqueConstraint("message_id", "call_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("coach_messages.id", ondelete="CASCADE"), index=True
    )
    call_id: Mapped[str] = mapped_column(String(100))
    tool_name: Mapped[str] = mapped_column(String(100))
    label: Mapped[str] = mapped_column(String(200))
    input_summary: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    error_message: Mapped[str | None] = mapped_column(String(500))

    message: Mapped[CoachMessage] = relationship(back_populates="tool_calls")

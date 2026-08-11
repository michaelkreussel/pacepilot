from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import CoachConversation, CoachMessage, CoachToolCall
from app.models.user import utcnow


def list_conversations(
    session: Session, user_id: int, *, limit: int = 50
) -> list[CoachConversation]:
    return list(
        session.scalars(
            select(CoachConversation)
            .where(CoachConversation.user_id == user_id)
            .order_by(CoachConversation.updated_at.desc(), CoachConversation.id.desc())
            .limit(limit)
        )
    )


def create_conversation(
    session: Session, user_id: int, *, title: str = "Neuer Chat"
) -> CoachConversation:
    conversation = CoachConversation(user_id=user_id, title=title)
    session.add(conversation)
    session.flush()
    return conversation


def find_conversation(
    session: Session, user_id: int, conversation_id: int
) -> CoachConversation | None:
    return session.scalar(
        select(CoachConversation).where(
            CoachConversation.id == conversation_id,
            CoachConversation.user_id == user_id,
        )
    )


def conversation_messages(
    session: Session, user_id: int, conversation_id: int
) -> list[CoachMessage] | None:
    conversation = session.scalar(
        select(CoachConversation)
        .options(selectinload(CoachConversation.messages).selectinload(CoachMessage.tool_calls))
        .where(
            CoachConversation.id == conversation_id,
            CoachConversation.user_id == user_id,
        )
    )
    return list(conversation.messages) if conversation is not None else None


def create_message(
    session: Session,
    conversation: CoachConversation,
    *,
    role: str,
    content: str = "",
    status: str = "completed",
    model_id: str | None = None,
) -> CoachMessage:
    message = CoachMessage(
        conversation=conversation,
        role=role,
        content=content,
        status=status,
        model_id=model_id,
        completed_at=utcnow() if status == "completed" else None,
    )
    conversation.updated_at = utcnow()
    session.add(message)
    session.flush()
    return message


def complete_message(session: Session, message_id: int, content: str) -> None:
    message = session.get(CoachMessage, message_id)
    if message is None:
        return
    message.content = content
    message.status = "completed"
    completed_at = utcnow()
    message.completed_at = completed_at
    message.conversation.updated_at = completed_at


def fail_message(session: Session, message_id: int, *, interrupted: bool = False) -> None:
    message = session.get(CoachMessage, message_id)
    if message is None:
        return
    message.status = "interrupted" if interrupted else "failed"
    completed_at = utcnow()
    message.completed_at = completed_at
    message.conversation.updated_at = completed_at


def start_tool_call(
    session: Session,
    message_id: int,
    *,
    call_id: str,
    tool_name: str,
    label: str,
    input_summary: str | None,
) -> None:
    existing = session.scalar(
        select(CoachToolCall).where(
            CoachToolCall.message_id == message_id,
            CoachToolCall.call_id == call_id,
        )
    )
    if existing is not None:
        return
    session.add(
        CoachToolCall(
            message_id=message_id,
            call_id=call_id,
            tool_name=tool_name,
            label=label,
            input_summary=input_summary,
        )
    )


def finish_tool_call(
    session: Session,
    message_id: int,
    call_id: str,
    *,
    error_message: str | None = None,
) -> None:
    tool_call = session.scalar(
        select(CoachToolCall).where(
            CoachToolCall.message_id == message_id,
            CoachToolCall.call_id == call_id,
        )
    )
    if tool_call is None:
        return
    tool_call.status = "failed" if error_message else "completed"
    tool_call.completed_at = utcnow()
    tool_call.error_message = error_message

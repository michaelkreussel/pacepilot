from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import CoachConversation, CoachMessage, CoachToolCall
from app.models.user import utcnow

CoachFailureCategory = Literal[
    "provider_error", "missing_final_answer", "interrupted", "internal_error"
]
RENDERED_MESSAGE_PAGE_SIZE = 40


@dataclass(frozen=True)
class CoachMessagePage:
    messages: tuple[CoachMessage, ...]
    older_before: int | None


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


def conversation_message_page(
    session: Session,
    user_id: int,
    conversation_id: int,
    *,
    before: int | None = None,
) -> CoachMessagePage:
    statement = (
        select(CoachMessage)
        .join(CoachConversation)
        .options(selectinload(CoachMessage.tool_calls))
        .where(
            CoachMessage.conversation_id == conversation_id,
            CoachConversation.user_id == user_id,
        )
        .order_by(CoachMessage.id.desc())
        .limit(RENDERED_MESSAGE_PAGE_SIZE + 1)
    )
    if before is not None:
        statement = statement.where(CoachMessage.id < before)
    newest_first = list(session.scalars(statement))
    has_older = len(newest_first) > RENDERED_MESSAGE_PAGE_SIZE
    messages = tuple(reversed(newest_first[:RENDERED_MESSAGE_PAGE_SIZE]))
    return CoachMessagePage(
        messages=messages,
        older_before=messages[0].id if has_older else None,
    )


def find_assistant_message(
    session: Session, user_id: int, conversation_id: int, message_id: int
) -> CoachMessage | None:
    return session.scalar(
        select(CoachMessage)
        .join(CoachConversation)
        .where(
            CoachMessage.id == message_id,
            CoachMessage.conversation_id == conversation_id,
            CoachMessage.role == "assistant",
            CoachConversation.user_id == user_id,
        )
    )


def create_message(
    session: Session,
    conversation: CoachConversation,
    *,
    role: str,
    content: str = "",
    status: str = "completed",
    model_id: str | None = None,
    request_id: str | None = None,
    prompt_template_version: str | None = None,
    operation_contract_version: str | None = None,
) -> CoachMessage:
    message = CoachMessage(
        conversation=conversation,
        role=role,
        content=content,
        status=status,
        model_id=model_id,
        request_id=request_id,
        prompt_template_version=prompt_template_version,
        operation_contract_version=operation_contract_version,
        completed_at=utcnow() if status == "completed" else None,
    )
    conversation.updated_at = utcnow()
    session.add(message)
    session.flush()
    return message


def claim_response(
    session: Session,
    conversation: CoachConversation,
    *,
    title: str,
    question: str,
    model_id: str,
    request_id: str | None,
    prompt_template_version: str,
    operation_contract_version: str,
) -> tuple[CoachMessage, CoachMessage]:
    with session.begin_nested():
        conversation.title = title
        user_message = create_message(session, conversation, role="user", content=question)
        assistant_message = create_message(
            session,
            conversation,
            role="assistant",
            status="streaming",
            model_id=model_id,
            request_id=request_id,
            prompt_template_version=prompt_template_version,
            operation_contract_version=operation_contract_version,
        )
    return user_message, assistant_message


def interrupt_stale_responses(
    session: Session,
    user_id: int,
    conversation_id: int,
    *,
    stale_before: datetime,
) -> int:
    stale_messages = list(
        session.scalars(
            select(CoachMessage)
            .join(CoachConversation)
            .where(
                CoachConversation.user_id == user_id,
                CoachMessage.conversation_id == conversation_id,
                CoachMessage.role == "assistant",
                CoachMessage.status == "streaming",
                CoachMessage.created_at < stale_before,
            )
        )
    )
    if not stale_messages:
        return 0

    completed_at = utcnow()
    for message in stale_messages:
        message.content = ""
        message.status = "interrupted"
        message.failure_category = "interrupted"
        message.completed_at = completed_at
        message.conversation.updated_at = completed_at
    session.flush()
    return len(stale_messages)


def complete_message(session: Session, message_id: int, content: str) -> None:
    message = session.get(CoachMessage, message_id)
    if message is None:
        return
    message.content = content
    message.status = "completed"
    completed_at = utcnow()
    message.completed_at = completed_at
    message.conversation.updated_at = completed_at


def fail_message(
    session: Session, message_id: int, *, failure_category: CoachFailureCategory
) -> None:
    message = session.get(CoachMessage, message_id)
    if message is None:
        return
    message.content = ""
    message.status = "interrupted" if failure_category == "interrupted" else "failed"
    message.failure_category = failure_category
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

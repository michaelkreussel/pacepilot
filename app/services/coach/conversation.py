from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal

from sqlalchemy.orm import Session, sessionmaker

from app.models import CoachConversation, CoachMessage
from app.repositories.coach import create_message

MAX_HISTORY_MESSAGES = 20
MAX_HISTORY_CHARACTERS = 12_000


@dataclass(frozen=True)
class CoachHistoryMessage:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class CoachRuntimeContext:
    user_id: int
    as_of: date
    session_factory: sessionmaker[Session]
    request_id: str | None = None
    conversation_id: int | None = None
    user_message_id: int | None = None
    assistant_message_id: int | None = None


@dataclass(frozen=True)
class CoachExecutionPreparation:
    history: tuple[CoachHistoryMessage, ...]
    runtime: CoachRuntimeContext
    assistant_message_id: int


def bounded_history(messages: Sequence[CoachMessage]) -> list[CoachHistoryMessage]:
    history: list[CoachHistoryMessage] = []
    characters = 0
    for message in reversed(messages):
        if message.status != "completed" or message.role not in {"user", "assistant"}:
            continue
        if len(history) >= MAX_HISTORY_MESSAGES:
            break
        remaining = MAX_HISTORY_CHARACTERS - characters
        if remaining <= 0:
            break
        content = message.content[-remaining:]
        history.append(CoachHistoryMessage(message.role, content))
        characters += len(content)
    return list(reversed(history))


def conversation_title(current_title: str, question: str) -> str:
    if current_title != "Neuer Chat":
        return current_title
    return question[:157] + ("..." if len(question) > 157 else "")


def prepare_execution(
    session: Session,
    conversation: CoachConversation,
    prior_messages: Sequence[CoachMessage],
    *,
    user_id: int,
    question: str,
    model_id: str,
    request_id: str | None,
    prompt_template_version: str,
    operation_contract_version: str,
    as_of: date,
) -> CoachExecutionPreparation:
    conversation.title = conversation_title(conversation.title, question)
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
    factory = sessionmaker(bind=session.get_bind(), autoflush=False, expire_on_commit=False)
    runtime = CoachRuntimeContext(
        user_id=user_id,
        as_of=as_of,
        session_factory=factory,
        request_id=request_id,
        conversation_id=conversation.id,
        user_message_id=user_message.id,
        assistant_message_id=assistant_message.id,
    )
    history = (*bounded_history(prior_messages), CoachHistoryMessage("user", question))
    return CoachExecutionPreparation(history, runtime, assistant_message.id)

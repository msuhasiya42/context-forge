"""The request working brief at the start of a run, and the request Message it starts from."""

from contextforge.core.entities.message import RequestMessage
from contextforge.core.repositories.base import MessageRepository
from contextforge.core.state import RequestState


def load_request(messages: MessageRepository, chat_id: str, message_id: str) -> RequestMessage:
    """The request Message; ``LookupError`` when the id names nothing or another kind."""
    message = messages.get(chat_id, message_id)
    if not isinstance(message, RequestMessage):
        raise LookupError(f"no request message {message_id} in chat {chat_id}")
    return message


def initial_brief(request: RequestMessage) -> RequestState:
    """Identity from ``request``; every brief field empty until the plan stage fills it."""
    return RequestState(
        request_message_id=request.message_id,
        created_at=request.created_at,
        chat_id=request.chat_id,
        user_id=request.user_id,
        goal="",
        constraints=[],
        plan=[],
        facts=[],
        open_questions=[],
        next_action="",
    )

"""Submit a request (implementation plan section 7.5): idempotent on the client's key."""

from contextforge.core.entities.message import (
    MessageKind,
    RequestMessage,
    RequestStatus,
    Sender,
)
from contextforge.core.ids import new_ulid, now_iso
from contextforge.core.repositories.memory import Repositories


def submit_request(
    repos: Repositories, *, client_key: str, chat_id: str, user_id: str, content: str
) -> RequestMessage:
    """Create the request Message at ACCEPTED; a replayed ``client_key`` returns the first one."""
    now = now_iso()
    message = RequestMessage(
        kind=MessageKind.REQUEST,
        message_id=new_ulid(),
        chat_id=chat_id,
        user_id=user_id,
        sender=Sender.USER,
        content=content,
        status=RequestStatus.ACCEPTED,
        created_at=now,
        updated_at=now,
    )
    return repos.messages.create_request(client_key, message)

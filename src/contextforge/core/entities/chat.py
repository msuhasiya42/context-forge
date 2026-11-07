"""Chat entity (implementation plan section 3.2)."""

from enum import StrEnum

from contextforge.core.entities import Entity
from contextforge.core.entities.message import RequestStatus


class ChatStatus(StrEnum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class Chat(Entity):
    chat_id: str
    user_id: str
    title: str
    status: ChatStatus
    created_at: str
    updated_at: str
    # Mirrors the newest request Message's status so the chat list needs no Message read.
    last_request_status: RequestStatus | None = None

"""User entity (implementation plan section 3.1)."""

from enum import StrEnum

from pydantic import Field

from contextforge.core.entities import Entity

INSTRUCTIONS_MAX_CHARS = 2000


class UserStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class User(Entity):
    user_id: str
    email: str
    display_name: str | None = None
    # Spec shape: { currency, timezone, locale }. Kept small on purpose.
    preferences: dict[str, str] | None = None
    # Standing instructions injected into every call's memory section; capped so they stay cheap.
    instructions: str | None = Field(default=None, max_length=INSTRUCTIONS_MAX_CHARS)
    status: UserStatus
    created_at: str
    updated_at: str

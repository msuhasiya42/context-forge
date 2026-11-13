"""Request bodies the API accepts. Unknown fields are refused, so a typo is a 400, not a no-op."""

from pydantic import BaseModel, ConfigDict, Field


class NewChatBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=200)


class SubmitBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=10000)


class RenameBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)


class ReplyBody(SubmitBody):
    inReplyToMessageId: str


class MePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instructions: str | None = Field(default=None, max_length=2000)
    preferences: dict[str, str] | None = None

"""Tool Protocol and the result envelope every tool returns, failures included."""

from typing import Any, Literal, Protocol

from contextforge.core.entities import Entity
from contextforge.core.models.base import ToolSpec

ToolPermission = Literal["READ", "WRITE", "DESTRUCTIVE"]


class ToolContext(Entity):
    """Who and what a tool call runs for; tools scope every lookup by ``chat_id``."""

    chat_id: str
    user_id: str
    request_message_id: str
    task_id: str
    tool_call_id: str


class ToolResult(Entity):
    """A tool outcome. A failure is a result the model sees, not an exception.

    ``error_class`` says whose fault it was: ``code`` means the model's arguments (fix them
    and call again), ``system`` means the tool or its backend. ``not_executed`` means the
    runtime refused the call, for example past the tool-call cap.
    """

    status: Literal["ok", "error", "not_executed"] = "ok"
    rows: list[dict[str, Any]] | None = None
    text: str | None = None
    error: str | None = None
    error_class: Literal["code", "system"] | None = None
    retryable: bool = False
    artifact_ids: list[str] = []


class Tool(Protocol):
    """A tool the execute stage can offer the model.

    Implementations declare ``spec`` and ``permission`` as annotated class attributes
    (``spec: ToolSpec = ToolSpec(...)``) so they are readable without an instance.
    """

    spec: ToolSpec
    permission: ToolPermission

    def run(self, arguments: dict[str, Any], tool_context: ToolContext) -> ToolResult: ...

"""Provider-neutral model boundary: what a stage sends to an LLM and what it gets back."""

from typing import Any, Literal, Protocol

from langchain_core.messages import AnyMessage
from pydantic import BaseModel, ConfigDict

from contextforge.core.entities import Entity
from contextforge.core.entities.model_call import Stage
from contextforge.core.values import Usage


class ToolSpec(Entity):
    """A tool definition as offered to the model; ``input_schema`` is JSON Schema."""

    name: str
    description: str
    input_schema: dict[str, Any]
    strict: bool = True


class ToolCall(Entity):
    """One tool invocation the model asked for; ``id`` pairs it with its ToolMessage."""

    id: str
    name: str
    arguments: dict[str, Any]


class ModelRequest(Entity):
    # Messages are LangChain objects passed through as-is. They validate natively in
    # langchain-core 1.x; the flag keeps them accepted if a message type stops being Pydantic.
    model_config = ConfigDict(arbitrary_types_allowed=True)

    stage: Stage
    model: str
    # System blocks in order, stable prefix first so a provider can cache it.
    system: list[str]
    messages: list[AnyMessage]
    tools: list[ToolSpec] = []
    # Schema the final answer must follow; the client returns it parsed in ``structured``.
    output_schema: type[BaseModel] | None = None
    max_tokens: int = 4000
    task_id: str | None = None


class ModelResponse(Entity):
    text: str | None = None
    # Structured output as a plain dict; each stage parses it into its own schema.
    structured: dict[str, Any] | None = None
    tool_calls: list[ToolCall] = []
    # None when the provider did not report usage: unknown is never zero.
    usage: Usage | None = None
    stop_reason: Literal["end_turn", "tool_use", "max_tokens"] = "end_turn"
    # True when served from a stored earlier result rather than a provider call, so it is
    # not recorded as a new ModelCall.
    replayed: bool = False


class ModelClient(Protocol):
    def invoke(self, request: ModelRequest) -> ModelResponse: ...

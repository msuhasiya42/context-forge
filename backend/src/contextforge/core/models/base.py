"""Provider-neutral model boundary: what a stage sends to an LLM and what it gets back."""

from typing import Any, Literal, Protocol

from langchain_core.messages import AnyMessage
from pydantic import BaseModel, ConfigDict, Field

from contextforge.core.entities import Entity
from contextforge.core.entities.model_call import CallMetadata, Stage
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
    # Provider fields the call must be sent back with on the next turn, such as Gemini 3's
    # thought signature; the stage keeps them on its AIMessage (``PROVIDER_DATA_KEY``). Opaque,
    # so never dumped to the wire, traces or transcripts.
    provider_data: dict[str, Any] | None = Field(default=None, exclude=True)


# The AIMessage ``additional_kwargs`` key holding each tool call's ``provider_data`` by call id,
# so it survives the checkpointed transcript to the next turn.
PROVIDER_DATA_KEY = "tool_call_provider_data"


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
    request_message_id: str | None = None
    # The stage's model when a fallback serves this call in its place (``worker.routing``).
    requested_model: str | None = None


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
    langsmith_run_id: str | None = None
    # Recorded on the call's ModelCall (``CallMetadata``), such as how the answer was read.
    metadata: CallMetadata = {}
    # The ModelCall that recorded this response, so a stage can add what it learns on reading it.
    model_call_id: str | None = None


class ModelClient(Protocol):
    def invoke(self, request: ModelRequest) -> ModelResponse: ...

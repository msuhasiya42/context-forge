"""Google Gemini through its OpenAI-compatible chat completions API: every research stage while
the Gemini profile of config/models.yaml is active (F17), and the offline judge.

A tool loop sends each assistant turn back with its tool calls, and each tool result as a
``tool`` message with the call's id. Gemini 3 also returns a thought signature on a function
call (``extra_content``) that must come back with that call on the next turn; it travels as the
ToolCall's ``provider_data``. The structured result is the Bedrock adapter's: a
``return_result`` tool the call is forced to use, read back as ``ModelResponse.structured``.
Failures map to the provider-neutral model errors so ``worker.routing`` moves to the next model
while Gemini is throttled or down.
Neither the API key nor a request body is ever logged or put in an error.
"""

import json
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from pydantic import SecretStr

from contextforge.core.entities.model_call import Stage
from contextforge.core.models.base import (
    PROVIDER_DATA_KEY,
    ModelRequest,
    ModelResponse,
    ToolCall,
)
from contextforge.core.models.errors import (
    ModelAccessDenied,
    ModelError,
    ModelRejected,
    ModelThrottled,
    ModelUnavailable,
    ModelUnreachable,
)
from contextforge.core.values import Usage
from contextforge.infra.bedrock import (
    RESULT_TOOL,
    RESULT_TOOL_DESCRIPTION,
    history_block,
    json_object,
    nova_schema,
)

_MODEL_PREFIX = "gemini-"
# Google's stand-in for a function call whose thought signature was not kept (a turn another
# model wrote, or a reply without one): Gemini 3 refuses a function call in the current turn
# that carries none. The value is from Google's thought-signature guide as recalled at F17; the
# live run confirms it (a refusal is ModelRejected, HTTP 400).
_SKIP_SIGNATURE = {"google": {"thought_signature": "skip_thought_signature_validator"}}


def is_gemini_model(model_id: str) -> bool:
    """Gemini model ids are served by ``GeminiModel``; every other id by Bedrock."""
    return model_id.startswith(_MODEL_PREFIX)


class RequestPacer:
    """Spaces calls at least ``min_interval`` seconds apart across threads, for the free tier's
    per-minute request limit. Each caller books its slot under the lock and waits outside it,
    so a waiting call never blocks another from booking the slot after it."""

    def __init__(
        self,
        min_interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._next_slot = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self._min_interval
        if slot > now:
            self._sleep(slot - now)


class GeminiModel:
    """A ModelClient over ``POST {base_url}/chat/completions`` with a bearer key."""

    def __init__(
        self,
        http: httpx.Client,
        api_key: SecretStr,
        *,
        base_url: str,
        pacer: RequestPacer | None = None,
        efforts: Mapping[Stage, str] | None = None,
    ) -> None:
        self._http = http
        self._api_key = api_key
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._pacer = pacer
        # Each stage's config/models.yaml effort, sent as ``reasoning_effort``. Gemini 3 always
        # thinks, and its thinking counts against max_tokens; a low effort keeps the token cap
        # for the answer.
        self._efforts = efforts or {}

    def invoke(self, request: ModelRequest) -> ModelResponse:
        try:
            payload = chat_payload(request, self._efforts.get(request.stage))
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelError(
                f"Could not build the Gemini request for {request.stage}: {exc}"
            ) from exc
        if self._pacer is not None:
            self._pacer.wait()
        headers = {"Authorization": f"Bearer {self._api_key.get_secret_value()}"}
        try:
            response = self._http.post(self._url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            # A timeout too is unreachable, not ModelTimeout, so routing moves to the fallback;
            # on the free tier an unanswered call costs nothing either way. Only the exception
            # type is kept: its text could quote the request.
            raise ModelUnreachable(f"Gemini could not be reached ({type(exc).__name__}).") from exc
        if response.status_code != 200:
            raise http_error(response)
        try:
            raw = response.json()
        except ValueError as exc:
            raise ModelError("Gemini returned a response that is not JSON.") from exc
        return parse_chat_response(raw, expects_result=request.output_schema is not None)


def chat_payload(request: ModelRequest, reasoning_effort: str | None = None) -> dict[str, Any]:
    """The chat completions body. A call that asks for a result and offers no tools of its own
    must call the result tool (``tool_choice: required`` with that tool alone, the standard
    OpenAI value; the compatibility docs show only ``auto`` and do not document naming one).
    No temperature is sent: Google advises keeping Gemini 3 at its default."""
    tools = [
        _function(t.name, t.description, t.input_schema)
        for t in sorted(request.tools, key=lambda t: t.name)
    ]
    if request.output_schema is not None:
        schema = request.output_schema.model_json_schema(by_alias=True)
        tools.append(_function(RESULT_TOOL, RESULT_TOOL_DESCRIPTION, schema))
    system = [{"role": "system", "content": text} for text in request.system if text]
    payload: dict[str, Any] = {
        "model": request.model,
        "messages": system + [_message(m) for m in request.messages],
        "max_tokens": request.max_tokens,
    }
    if tools:
        payload["tools"] = tools
        if request.output_schema is not None and not request.tools:
            payload["tool_choice"] = "required"
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort
    return payload


def _function(name: str, description: str, schema: dict[str, Any]) -> dict[str, Any]:
    # Local refs inlined, as for Bedrock: the compatibility layer's schema support is narrower
    # than OpenAI's and undocumented.
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": nova_schema(schema)},
    }


def _message(message: BaseMessage) -> dict[str, Any]:
    text = message.content if isinstance(message.content, str) else json.dumps(message.content)
    if isinstance(message, ToolMessage):
        return {"role": "tool", "tool_call_id": message.tool_call_id, "content": text or "(empty)"}
    if isinstance(message, AIMessage):
        turn: dict[str, Any] = {"role": "assistant", "content": text or None}
        if message.tool_calls:
            turn["tool_calls"] = _assistant_calls(message)
        return turn
    if isinstance(message, HumanMessage):
        return {"role": "user", "content": text or "(empty)"}
    return {"role": "user", "content": history_block(text)}


def _assistant_calls(message: AIMessage) -> list[dict[str, Any]]:
    """An assistant turn's tool calls as sent back, each with the ``extra_content`` it came
    with. Only the first call of a turn carries a signature, so the stand-in goes on the first
    call alone when the turn kept none."""
    kept: dict[str, Any] = message.additional_kwargs.get(PROVIDER_DATA_KEY) or {}
    calls: list[dict[str, Any]] = []
    for call in message.tool_calls:
        sent: dict[str, Any] = {
            "id": call["id"],
            "type": "function",
            "function": {"name": call["name"], "arguments": json.dumps(call["args"])},
        }
        if call["id"] in kept:
            sent.update(kept[call["id"]])
        calls.append(sent)
    if not kept:
        calls[0]["extra_content"] = _SKIP_SIGNATURE
    return calls


def http_error(response: httpx.Response) -> ModelError:
    """The model error for a non-200 answer: throttled and unavailable ones route to the next
    model (as the rag-system client falls back on 404/429/5xx), as does a key Google refuses."""
    status = response.status_code
    detail = f"Gemini answered HTTP {status}: {_error_message(response)}"
    if status == 429:
        return ModelThrottled(detail)
    if status == 404 or status >= 500:
        return ModelUnavailable(detail)
    if status in {401, 403}:
        return ModelAccessDenied(f"{detail} (check CONTEXTFORGE_GEMINI_API_KEY)")
    if status == 400:
        return ModelRejected(detail)
    return ModelError(detail)


def _error_message(response: httpx.Response) -> str:
    """Google's error message, first line and cut short; never the request."""
    try:
        body = response.json()
    except ValueError:
        return "(no error body)"
    if isinstance(body, list) and body:  # Google sometimes wraps the error in a list
        body = body[0]
    error = body.get("error") if isinstance(body, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    return str(message).splitlines()[0][:200] if message else "(no error message)"


def parse_chat_response(raw: Any, *, expects_result: bool) -> ModelResponse:
    """The first choice as a ModelResponse. A content-filter stop raises ``ModelRejected``; a
    body without a readable choice raises ``ModelError``."""
    try:
        choice = raw["choices"][0]
        if choice.get("finish_reason") == "content_filter":
            raise ModelRejected("Gemini stopped the response: content_filter.")
        return _response(choice, raw.get("usage"), expects_result=expects_result)
    except (AttributeError, KeyError, IndexError, TypeError, ValueError) as exc:
        reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        raise ModelError(f"Gemini returned a response that cannot be read: {reason}") from exc


def _response(
    choice: dict[str, Any], usage: dict[str, Any] | None, *, expects_result: bool
) -> ModelResponse:
    message = choice["message"]
    text = message.get("content") or None
    calls: list[ToolCall] = []
    structured = None
    for index, call in enumerate(message.get("tool_calls") or []):
        function = call["function"]
        arguments = _arguments(function)
        if function["name"] == RESULT_TOOL:
            structured = arguments
        else:
            # An id is how the tool result is paired with its call; one Gemini left out is
            # made up, and sent back with the result the same way.
            call_id = call.get("id") or f"call_{index}"
            extra = call.get("extra_content")
            calls.append(
                ToolCall(
                    id=call_id,
                    name=function["name"],
                    arguments=arguments,
                    provider_data={"extra_content": extra} if extra else None,
                )
            )
    if structured is not None:
        calls = []  # the result tool wins, as in the Bedrock adapter
    elif expects_result and not calls and text:
        structured = json_object(text)
    if calls:
        stop_reason = "tool_use"
    else:
        stop_reason = "max_tokens" if choice.get("finish_reason") == "length" else "end_turn"
    return ModelResponse(
        text=text,
        structured=structured,
        tool_calls=calls,
        usage=_usage(usage),
        stop_reason=stop_reason,
        metadata={"output_path": "tool" if message.get("tool_calls") else "text"},
    )


def _arguments(function: dict[str, Any]) -> dict[str, Any]:
    """A tool call's arguments, sent as a JSON string. ``ValueError`` unless an object."""
    value = function.get("arguments")
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError(f"the arguments of tool {function.get('name')} are not a JSON object")
    return value


def _usage(raw: dict[str, Any] | None) -> Usage | None:
    """Usage when both token counts were reported; missing or partial usage stays unknown."""
    if not raw or "prompt_tokens" not in raw or "completion_tokens" not in raw:
        return None
    return Usage(input_tokens=raw["prompt_tokens"], output_tokens=raw["completion_tokens"])

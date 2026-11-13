"""One ModelCall's transcript: the request the model saw and what it answered, as JSON.

``RecordingModelClient`` writes it to the blob store beside each ModelCall; the request trace
reads it back to show the exact prompt and output, and to derive tool calls from it: a tool
call is a tool-use block in one call's response, and its result is the ToolMessage with the
same id in a later call's request. The format is defined here only, for writer and reader.
"""

import json
import logging
from collections.abc import Iterable
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from contextforge.core.llm.base import ModelRequest, ModelResponse
from contextforge.core.storage import BlobStore, transcript_key

logger = logging.getLogger(__name__)

REDACTED = "[redacted]"
Transcript = dict[str, Any]
# Wire role names; LangChain names messages by type.
_ROLES = {"human": "user", "ai": "assistant", "system": "system", "tool": "tool"}


def transcript(
    request: ModelRequest,
    model: str,
    *,
    response: ModelResponse | None = None,
    error: str | None = None,
) -> Transcript:
    """The transcript of one call: ``response`` when it answered, ``error`` when it failed.
    ``model`` is the recorded ``<provider>/<model id>``."""
    schema = request.output_schema
    return {
        "request": {
            "stage": request.stage.value,
            "model": model,
            "taskId": request.task_id,
            "system": request.system,
            "messages": [message_record(m) for m in request.messages],
            "tools": [spec.model_dump(mode="json", by_alias=True) for spec in request.tools],
            "outputSchema": schema.model_json_schema() if schema is not None else None,
            "maxTokens": request.max_tokens,
        },
        "response": _response_record(response) if response is not None else None,
        "error": error,
    }


def message_record(message: BaseMessage) -> dict[str, Any]:
    """A message as the model saw it: role and content, plus the fields that pair a tool call
    with its result. Nothing else is kept, so provider metadata (response headers) never is."""
    record: dict[str, Any] = {
        "role": _ROLES.get(message.type, message.type),
        "content": message.content,
    }
    if isinstance(message, AIMessage) and message.tool_calls:
        record["toolCalls"] = [
            {"id": call["id"], "name": call["name"], "arguments": call["args"]}
            for call in message.tool_calls
        ]
    if isinstance(message, ToolMessage):
        meta = message.additional_kwargs
        record |= {
            "toolCallId": message.tool_call_id,
            "status": meta.get("status"),
            "rowCount": meta.get("row_count"),
            "artifactId": meta.get("artifact_id"),
        }
    return record


def _response_record(response: ModelResponse) -> dict[str, Any]:
    usage = response.usage
    return {
        "text": response.text,
        "toolCalls": [call.model_dump(mode="json", by_alias=True) for call in response.tool_calls],
        "structured": response.structured,
        "stopReason": response.stop_reason,
        "usage": usage.model_dump(mode="json", by_alias=True) if usage is not None else None,
    }


def tool_uses(record: Transcript) -> list[dict[str, Any]]:
    """The tool calls the model asked for in this call's response."""
    return list((record.get("response") or {}).get("toolCalls") or [])


def tool_results(record: Transcript) -> list[dict[str, Any]]:
    """The tool results (ToolMessages) this call's request carried."""
    messages = record.get("request", {}).get("messages") or []
    return [m for m in messages if m.get("role") == "tool"]


def content_text(content: Any) -> str:
    """A message's content as plain text: a string as is, text blocks joined, else JSON."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block if isinstance(block, str) else str(block.get("text", json.dumps(block)))
            for block in content
        )
    return json.dumps(content)


class TranscriptWriter:
    """Stores transcripts in the blob store with every known secret value redacted.

    A transcript is an observation of a call that already happened, so failing to store one
    never fails the call: it is logged, and the ModelCall records no transcript.
    """

    def __init__(self, blobs: BlobStore, secrets: Iterable[str] = ()) -> None:
        self._blobs = blobs
        # As they appear inside a JSON string, so a value with escapes is still found.
        self._secrets = [json.dumps(s, ensure_ascii=False)[1:-1] for s in secrets if s]

    def write(self, request_message_id: str, model_call_id: str, record: Transcript) -> str | None:
        """Store ``record``; its storage ref, or None when it could not be stored."""
        try:
            text = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
            for secret in self._secrets:
                text = text.replace(secret, REDACTED)
            key = transcript_key(request_message_id, model_call_id)
            return self._blobs.put(key, text.encode("utf-8"))
        except Exception:
            logger.warning(
                "transcript of model call %s was not stored", model_call_id, exc_info=True
            )
            return None

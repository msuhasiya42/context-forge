"""Compaction of a loop's ``messages`` (implementation plan 8.6), lightest level first.

Level 1 stubs old tool results with no model call; level 3 summarizes all but the last
messages with a ``stage=compress`` call. Level 2 (task subgraphs) is structural, not here.
The budget and the number of messages kept come from the caller, read from ``Budgets``.
"""

import json
from typing import Any, TypeGuard

from langchain_core.messages import AIMessage, AnyMessage, SystemMessage, ToolMessage

from contextforge.core.context.builder import message_text
from contextforge.core.context.prompts import load_prompt
from contextforge.core.context.tokens import estimate_tokens
from contextforge.core.entities.model_call import Stage
from contextforge.core.models.base import ModelClient, ModelRequest

# Every stub starts with this; it is how level 1 recognises a result it already stubbed.
_STUB_PREFIX = "[tool "
_SUMMARY_PREFIX = "Summary of earlier work: "
_SUMMARY_MAX_TOKENS = 1000


def messages_tokens(messages: list[AnyMessage]) -> int:
    """Estimated tokens of ``messages``, measured as the Context Builder measures them."""
    return sum(estimate_tokens(message_text(m)) for m in messages)


def compact(
    messages: list[AnyMessage],
    *,
    budget_tokens: int,
    keep_last: int,
    model: ModelClient,
    memory: str,
    model_id: str = "scripted",
) -> list[AnyMessage]:
    """Fit ``messages`` into ``budget_tokens``: unchanged if they fit, else level 1, else 3.

    The model is called only when stubbing is not enough. One pass: a level 3 result still
    over budget is returned as is, and the next call compacts again.
    """
    if messages_tokens(messages) <= budget_tokens:
        return list(messages)
    stubbed = stub_old_tool_results(messages)
    if messages_tokens(stubbed) <= budget_tokens:
        return stubbed
    return summarize_messages(
        stubbed, keep_last=keep_last, model=model, memory=memory, model_id=model_id
    )


def stub_old_tool_results(messages: list[AnyMessage], keep_turns: int = 2) -> list[AnyMessage]:
    """Level 1: replace each tool result older than the last ``keep_turns`` AI turns by a stub.

    The stub keeps the call (tool, arguments, row count, artifact) and drops the rows; the
    artifact handle lets a later call read them back. Messages are never mutated: stubbed ones
    are new objects, the rest are reused. Stubs are left alone, so a second pass changes nothing.
    """
    verbatim_from = _recent_turns_start(messages, keep_turns)
    return [
        _stub(m) if i < verbatim_from and _is_unstubbed_result(m) else m
        for i, m in enumerate(messages)
    ]


def summarize_messages(
    messages: list[AnyMessage],
    *,
    keep_last: int,
    model: ModelClient,
    memory: str,
    model_id: str = "scripted",
) -> list[AnyMessage]:
    """Level 3: replace everything before the last ``keep_last`` messages by a model summary.

    Makes no model call, and returns the messages unchanged, when nothing lies before the
    boundary.
    """
    boundary = _summary_boundary(messages, keep_last)
    if boundary == 0:
        return list(messages)
    response = model.invoke(
        ModelRequest(
            stage=Stage.COMPRESS,
            model=model_id,
            system=[memory, load_prompt("compress.1")],
            messages=messages[:boundary],
            max_tokens=_SUMMARY_MAX_TOKENS,
        )
    )
    summary = SystemMessage(content=_SUMMARY_PREFIX + (response.text or ""))
    return [summary, *messages[boundary:]]


def _recent_turns_start(messages: list[AnyMessage], keep_turns: int) -> int:
    """Index of the first of the last ``keep_turns`` AI turns: it and everything after stay."""
    if keep_turns <= 0:
        return len(messages)
    ai_turns = [i for i, m in enumerate(messages) if isinstance(m, AIMessage)]
    # Fewer turns than kept: every message is recent.
    return ai_turns[-keep_turns] if len(ai_turns) >= keep_turns else 0


def _is_unstubbed_result(message: AnyMessage) -> TypeGuard[ToolMessage]:
    if not isinstance(message, ToolMessage):
        return False
    content = message.content
    return not (isinstance(content, str) and content.startswith(_STUB_PREFIX))


def _stub(message: ToolMessage) -> ToolMessage:
    # A copy keeps tool_call_id (which pairs the result with its call), id and metadata.
    return message.model_copy(update={"content": _stub_text(message.additional_kwargs)})


def _stub_text(meta: dict[str, Any]) -> str:
    """The one-line stub, from the metadata the execute node stores on each result.

    Missing keys fall back to defaults: a result without metadata still compacts, never raises.
    """
    args = json.dumps(meta.get("arguments", {}), sort_keys=True)
    return (
        f"{_STUB_PREFIX}{meta.get('tool_name', 'unknown')} args={args} "
        f"outcome={meta.get('status', 'ok')} rows={meta.get('row_count', 0)} "
        f"artifact={meta.get('artifact_id') or '-'}]"
    )


def _summary_boundary(messages: list[AnyMessage], keep_last: int) -> int:
    """Where the kept tail starts, moved earlier so a tool result never starts it.

    Providers reject a tool result whose call is missing, so a call and its result are kept
    or summarized together.
    """
    boundary = max(0, len(messages) - keep_last)
    # The length check guards keep_last=0, where the boundary is past the last message.
    while 0 < boundary < len(messages) and isinstance(messages[boundary], ToolMessage):
        boundary -= 1
    return boundary

"""Repository rules both backends share, so the in-memory and DynamoDB repositories differ only
in how they read and write, never in what an answer means."""

from collections.abc import Iterable

from contextforge.core.entities.message import (
    ActionResponseMessage,
    ClarificationMessage,
    ClarificationResponseMessage,
    Message,
    ProposedActionMessage,
)
from contextforge.core.values import Usage


def unanswered(in_chat: Iterable[Message], request_message_id: str) -> list[Message]:
    """The request's clarification and proposed_action entries that no reply names yet.

    A reply is a clarification_response or action_response whose ``in_reply_to_message_id`` is
    the entry's id. ``in_chat`` order is kept, so a chat listed by ``message_id`` stays ordered.
    """
    messages = list(in_chat)
    answered = {
        m.in_reply_to_message_id
        for m in messages
        if isinstance(m, ClarificationResponseMessage | ActionResponseMessage)
    }
    return [
        m
        for m in messages
        if isinstance(m, ClarificationMessage | ProposedActionMessage)
        and m.request_message_id == request_message_id
        and m.message_id not in answered
    ]


def sum_known_usage(usages: Iterable[Usage | None]) -> Usage | None:
    """Sum the usages that were reported; None when none were, because unknown is not zero."""
    known = [u for u in usages if u is not None]
    if not known:
        return None
    return Usage(
        input_tokens=sum(u.input_tokens for u in known),
        output_tokens=sum(u.output_tokens for u in known),
        cache_read_tokens=_sum_reported([u.cache_read_tokens for u in known]),
        cache_write_tokens=_sum_reported([u.cache_write_tokens for u in known]),
        thinking_tokens=_sum_reported([u.thinking_tokens for u in known]),
    )


def _sum_reported(values: list[int | None]) -> int | None:
    reported = [v for v in values if v is not None]
    return sum(reported) if reported else None

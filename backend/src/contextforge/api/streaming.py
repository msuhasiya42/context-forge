"""Server-sent events for one request's progress (spec 11.5)."""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from contextforge.core.entities import wire
from contextforge.core.entities.message import RequestMessage, RequestStatus
from contextforge.core.services.application import TERMINAL, Application, RequestView

# A heartbeat comment every 15 seconds, and a stream never outlives 15 minutes, so a stalled
# worker cannot hold a connection; the client refetches and reopens.
HEARTBEAT_SECONDS = 15.0
STREAM_MAX_SECONDS = 900.0


async def event_stream(
    service: Application,
    opened: RequestMessage,
    poll_seconds: float,
    *,
    heartbeat_seconds: float,
    max_seconds: float,
) -> AsyncIterator[str]:
    """Each record whose content changed as an event, transient activity, and a heartbeat
    comment every ``heartbeat_seconds`` of wall time.

    A request already finished when the stream opens gets its one ``request`` event. Otherwise
    the stream closes once the request ends or waits on the user with the question sent, or
    after ``max_seconds`` of wall time.
    """
    if opened.status in TERMINAL:
        yield _frame("request", wire(opened))
        return
    sent: dict[str, str] = {}
    activity_after = 0
    started = last_beat = time.monotonic()
    while time.monotonic() - started < max_seconds:
        view = await asyncio.to_thread(service.request_view, opened.chat_id, opened.message_id)
        for frame in _changed_frames(view, sent):
            yield frame
        for sequence, activity in service.activity_since(opened.message_id, activity_after):
            yield _frame("activity", activity)
            activity_after = sequence
        if view.closed:
            return
        if time.monotonic() - last_beat >= heartbeat_seconds:
            yield ": heartbeat\n\n"
            last_beat = time.monotonic()
        await asyncio.sleep(poll_seconds)


def _changed_frames(view: RequestView, sent: dict[str, str]) -> list[str]:
    """Frames for the view's records that differ from the last ``sent`` copy, which is updated.

    Tasks and messages come before the request, so a closing status arrives last. WAITING_USER
    is held back until the question is visible too: a client closes the stream on it.
    """
    records = [
        *(("task", task) for task in view.tasks),
        *(("message", message) for message in view.messages),
    ]
    if view.closed or view.request["status"] != RequestStatus.WAITING_USER:
        records.append(("request", view.request))
    frames = []
    for kind, record in records:
        frame = _frame(kind, record)
        key = f"{kind}:{record.get('messageId', record.get('taskId', ''))}"
        if sent.get(key) != frame:
            frames.append(frame)
            sent[key] = frame
    return frames


def _frame(kind: str, record: dict[str, Any]) -> str:
    return f"event: {kind}\ndata: {json.dumps(record, ensure_ascii=False)}\n\n"

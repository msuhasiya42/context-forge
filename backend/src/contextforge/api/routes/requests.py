"""Research requests in a chat: submit, follow, cancel, reply, and inspect their trace."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Response
from fastapi.responses import StreamingResponse

from contextforge.api.deps import ServiceDep, SettingsDep, authorized
from contextforge.api.schemas import ReplyBody, SubmitBody
from contextforge.api.streaming import HEARTBEAT_SECONDS, STREAM_MAX_SECONDS, event_stream
from contextforge.core.entities import wire

router = APIRouter(
    prefix="/chats/{chat_id}/requests", tags=["requests"], dependencies=[Depends(authorized)]
)


@router.post("", status_code=202)
def submit(
    chat_id: str,
    body: SubmitBody,
    service: ServiceDep,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
) -> dict[str, Any]:
    return wire(service.submit(chat_id, body.content, idempotency_key))


@router.get("/{message_id}")
def detail(chat_id: str, message_id: str, service: ServiceDep) -> dict[str, Any]:
    return service.detail(chat_id, message_id)


@router.post("/{message_id}/cancel")
def cancel(chat_id: str, message_id: str, service: ServiceDep) -> dict[str, Any]:
    return wire(service.cancel(chat_id, message_id))


@router.post("/{message_id}/replies", status_code=201)
def reply(chat_id: str, message_id: str, body: ReplyBody, service: ServiceDep) -> dict[str, Any]:
    return wire(service.reply(chat_id, message_id, body.inReplyToMessageId, body.content))


@router.get("/{message_id}/trace")
def trace(chat_id: str, message_id: str, service: ServiceDep) -> dict[str, Any]:
    return service.trace(chat_id, message_id)


@router.get("/{message_id}/model-calls/{model_call_id}/transcript")
def transcript(chat_id: str, message_id: str, model_call_id: str, service: ServiceDep) -> Response:
    return Response(
        service.transcript(chat_id, message_id, model_call_id),
        media_type="application/json",
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.get("/{message_id}/events")
def events(
    chat_id: str, message_id: str, service: ServiceDep, settings: SettingsDep
) -> StreamingResponse:
    opened = service.request(chat_id, message_id)  # a 404 here, never an empty stream
    stream = event_stream(
        service,
        opened,
        settings.sse_poll_seconds,
        heartbeat_seconds=HEARTBEAT_SECONDS,
        max_seconds=STREAM_MAX_SECONDS,
    )
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

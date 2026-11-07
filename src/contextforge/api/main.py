"""Thin FastAPI boundary for the web-first research slice; the rules live in ``Application``."""

import asyncio
import hmac
import json
import logging
import os
import re
import sqlite3
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from contextforge.core.entities import wire
from contextforge.core.entities.message import RequestMessage, RequestStatus
from contextforge.core.logging import configure as configure_logging
from contextforge.core.services.application import (
    TERMINAL,
    Application,
    RequestView,
    ServiceError,
    page,
)
from contextforge.core.settings import Settings
from contextforge.infra.runtime import build_worker
from contextforge.worker.consumer import QueueConsumer
from contextforge.worker.dependencies import WorkerDependencies
from contextforge.worker.runner import RequestRunner

# Spec 11.5: a heartbeat comment every 15 seconds, and a stream never outlives 15 minutes, so a
# stalled worker cannot hold a connection; the client refetches and reopens.
HEARTBEAT_SECONDS = 15.0
STREAM_MAX_SECONDS = 900.0
INTERNAL_MESSAGE = "Something went wrong. Please try again."

logger = logging.getLogger(__name__)


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


def create_app(
    settings: Settings | None = None, *, worker_dependencies: WorkerDependencies | None = None
) -> FastAPI:
    config = settings or Settings()
    if worker_dependencies is None:
        worker_dependencies = build_worker(config)
    config.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(config.checkpoint_path, check_same_thread=False)
    runner = RequestRunner(worker_dependencies, SqliteSaver(connection))
    service = Application(worker_dependencies, config.dev_user_id)
    web_tool_limit = str(worker_dependencies.budgets.web_tool_call_budget)
    consumer = QueueConsumer(worker_dependencies, runner)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        service.me()
        if config.run_worker:
            consumer.start()
        yield
        consumer.stop()
        if not consumer.running:
            connection.close()

    app = FastAPI(title="ContextForge", lifespan=lifespan)
    app.state.service, app.state.consumer = service, consumer
    # Added first, so it sits inside CORS: the 500 envelope gets the CORS headers too.
    app.add_middleware(InternalErrorEnvelope)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "Idempotency-Key", "Authorization"],
    )
    _install_error_handlers(app)

    def authorized(authorization: Annotated[str | None, Header()] = None) -> None:
        if config.dev_token is not None and not hmac.compare_digest(
            authorization or "", f"Bearer {config.dev_token.get_secret_value()}"
        ):
            raise ServiceError(401, "unauthenticated", "A valid development token is required.")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {
            "status": "ok",
            "build": "web-research-v3",
            "processId": str(os.getpid()),
            "webToolCallLimit": web_tool_limit,
            "backend": config.backend,
            "model": config.model_backend,
            "search": config.search_provider,
        }

    @app.get("/me", dependencies=[Depends(authorized)])
    def get_me() -> dict[str, Any]:
        return wire(service.me())

    @app.patch("/me", dependencies=[Depends(authorized)])
    def patch_me(body: MePatch) -> dict[str, Any]:
        return wire(service.update_me(body.model_dump(exclude_unset=True)))

    @app.post("/chats", status_code=201, dependencies=[Depends(authorized)])
    def new_chat(body: NewChatBody | None = None) -> dict[str, Any]:
        return wire(service.create_chat(body.title if body else None))

    @app.get("/chats", dependencies=[Depends(authorized)])
    def list_chats(
        limit: int = Query(50, ge=1, le=100), cursor: str | None = None
    ) -> dict[str, Any]:
        return page(service.chats(), f"chats:{service.user_id}", limit, cursor)

    @app.patch("/chats/{chat_id}", dependencies=[Depends(authorized)])
    def rename_chat(chat_id: str, body: RenameBody) -> dict[str, Any]:
        return wire(service.update_chat(chat_id, title=body.title))

    @app.delete("/chats/{chat_id}", status_code=204, dependencies=[Depends(authorized)])
    def delete_chat(chat_id: str) -> Response:
        service.update_chat(chat_id, archive=True)
        return Response(status_code=204)

    @app.get("/chats/{chat_id}/messages", dependencies=[Depends(authorized)])
    def messages(
        chat_id: str, limit: int = Query(50, ge=1, le=100), cursor: str | None = None
    ) -> dict[str, Any]:
        return page(service.messages(chat_id), f"messages:{chat_id}", limit, cursor, newest=True)

    @app.post("/chats/{chat_id}/requests", status_code=202, dependencies=[Depends(authorized)])
    def submit(
        chat_id: str,
        body: SubmitBody,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ) -> dict[str, Any]:
        return wire(service.submit(chat_id, body.content, idempotency_key))

    @app.get("/chats/{chat_id}/requests/{message_id}", dependencies=[Depends(authorized)])
    def detail(chat_id: str, message_id: str) -> dict[str, Any]:
        return service.detail(chat_id, message_id)

    @app.post("/chats/{chat_id}/requests/{message_id}/cancel", dependencies=[Depends(authorized)])
    def cancel(chat_id: str, message_id: str) -> dict[str, Any]:
        return wire(service.cancel(chat_id, message_id))

    @app.post(
        "/chats/{chat_id}/requests/{message_id}/replies",
        status_code=201,
        dependencies=[Depends(authorized)],
    )
    def reply(chat_id: str, message_id: str, body: ReplyBody) -> dict[str, Any]:
        return wire(service.reply(chat_id, message_id, body.inReplyToMessageId, body.content))

    @app.get("/chats/{chat_id}/requests/{message_id}/trace", dependencies=[Depends(authorized)])
    def trace(chat_id: str, message_id: str) -> dict[str, Any]:
        return service.trace(chat_id, message_id)

    @app.get(
        "/chats/{chat_id}/requests/{message_id}/model-calls/{model_call_id}/transcript",
        dependencies=[Depends(authorized)],
    )
    def transcript(chat_id: str, message_id: str, model_call_id: str) -> Response:
        return Response(
            service.transcript(chat_id, message_id, model_call_id),
            media_type="application/json",
            headers={"X-Content-Type-Options": "nosniff"},
        )

    @app.get("/chats/{chat_id}/requests/{message_id}/events", dependencies=[Depends(authorized)])
    def events(chat_id: str, message_id: str) -> StreamingResponse:
        opened = service.request(chat_id, message_id)  # a 404 here, never an empty stream
        stream = event_stream(
            service,
            opened,
            config.sse_poll_seconds,
            heartbeat_seconds=HEARTBEAT_SECONDS,
            max_seconds=STREAM_MAX_SECONDS,
        )
        return StreamingResponse(
            stream,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/chats/{chat_id}/artifacts", dependencies=[Depends(authorized)])
    def artifacts(
        chat_id: str, limit: int = Query(50, ge=1, le=100), cursor: str | None = None
    ) -> dict[str, Any]:
        return page(service.artifacts(chat_id), f"artifacts:{chat_id}", limit, cursor)

    @app.get("/chats/{chat_id}/artifacts/{artifact_id}/content", dependencies=[Depends(authorized)])
    def artifact_content(chat_id: str, artifact_id: str) -> Response:
        artifact, data = service.artifact_content(chat_id, artifact_id)
        # attachment and nosniff: a snapshot holds page text, which must never be rendered as
        # a page of this origin if the URL is opened directly.
        filename = re.sub(r"[^A-Za-z0-9._-]", "_", artifact.name)
        return Response(
            data,
            media_type=artifact.media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    return app


def _install_error_handlers(app: FastAPI) -> None:
    """One error shape everywhere (spec 11.2): ``{"error": {"code", "message"}}``, no traces."""

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError) -> JSONResponse:
        return _error(exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error(400, "validation", "Invalid request fields.")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            return _error(404, "not_found", "Not found.")
        return _error(exc.status_code, "validation", str(exc.detail))


class InternalErrorEnvelope:
    """Answers an unhandled exception with the 500 envelope and logs its trace.

    Not an ``exception_handler(Exception)``: Starlette runs that one outside every middleware,
    so its response would carry no CORS headers and a browser could not read it. An error after
    the response has started cannot be answered and is re-raised to the server.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracked(message: Message) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, receive, tracked)
        except Exception:
            if started:
                raise
            logger.exception("Unhandled error on %s %s", scope["method"], scope["path"])
            await _error(500, "internal", INTERNAL_MESSAGE)(scope, receive, send)


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


async def event_stream(
    service: Application,
    opened: RequestMessage,
    poll_seconds: float,
    *,
    heartbeat_seconds: float,
    max_seconds: float,
) -> AsyncIterator[str]:
    """Spec 11.5: each record whose content changed as an event, transient activity, and a
    heartbeat comment every ``heartbeat_seconds`` of wall time.

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


def main() -> None:
    import uvicorn

    # The worker runs in this process; without this its INFO records (plan, task, report
    # progress, each with its request and task id) were dropped.
    settings = Settings()
    secrets = tuple(
        value.get_secret_value()
        for value in (settings.dev_token, settings.serper_api_key, settings.langsmith_api_key)
        if value is not None
    )
    configure_logging(settings.backend_log_path, secrets=secrets)
    logging.getLogger(__name__).info("starting API; build=web-research-v3")
    uvicorn.run(create_app(settings), host="127.0.0.1", port=8000, log_config=None)

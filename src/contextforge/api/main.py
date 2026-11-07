"""Thin FastAPI boundary for the web-first research slice."""

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from contextforge.core.entities.chat import ChatStatus
from contextforge.core.entities.message import RequestStatus
from contextforge.core.services.application import TERMINAL, Application, ServiceError, page, wire
from contextforge.core.settings import Settings
from contextforge.worker.consumer import QueueConsumer
from contextforge.worker.deps import WorkerDeps
from contextforge.worker.runner import RequestRunner


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


def create_app(settings: Settings | None = None, *, deps: WorkerDeps | None = None) -> FastAPI:
    config = settings or Settings()
    if deps is None:
        from contextforge.infra.runtime import build_worker

        deps = build_worker(config)
    config.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(config.checkpoint_path, check_same_thread=False)
    runner = RequestRunner(deps, SqliteSaver(connection))
    service = Application(deps, runner, config.dev_user_id)
    consumer = QueueConsumer(deps, runner)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        service.me()
        if config.run_worker:
            consumer.start()
        yield
        if config.run_worker:
            consumer.stop()
        if not consumer.thread.is_alive():
            connection.close()

    app = FastAPI(title="ContextForge", lifespan=lifespan)
    app.state.service, app.state.consumer = service, consumer
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "Idempotency-Key", "Authorization"],
    )

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message}}
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "validation", "message": "Invalid request fields."}},
        )

    def authorized(authorization: Annotated[str | None, Header()] = None) -> None:
        if config.dev_token is not None:
            import hmac

            if not hmac.compare_digest(
                authorization or "", f"Bearer {config.dev_token.get_secret_value()}"
            ):
                raise ServiceError(401, "authentication", "A valid development token is required.")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {
            "status": "ok",
            "backend": config.backend,
            "model": config.model_backend,
            "search": config.search_provider,
        }

    @app.get("/me", dependencies=[Depends(authorized)])
    def get_me() -> dict[str, Any]:
        return wire(service.me())

    @app.patch("/me", dependencies=[Depends(authorized)])
    def patch_me(body: MePatch) -> dict[str, Any]:
        try:
            return wire(service.update_me(body.model_dump(exclude_unset=True)))
        except ValidationError as exc:
            raise ServiceError(400, "validation", "Invalid preferences or instructions.") from exc

    @app.post("/chats", status_code=201, dependencies=[Depends(authorized)])
    def new_chat() -> dict[str, Any]:
        return wire(service.create_chat())

    @app.get("/chats", dependencies=[Depends(authorized)])
    def list_chats(
        limit: int = Query(50, ge=1, le=100), cursor: str | None = None
    ) -> dict[str, Any]:
        return page(
            [
                c
                for c in service.deps.repos.chats.list_by_user(service.user_id)
                if c.status == ChatStatus.ACTIVE
            ],
            f"chats:{service.user_id}",
            limit,
            cursor,
        )

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
        if not body.content.strip():
            raise ServiceError(400, "validation", "Question cannot be blank.")
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

    @app.get("/chats/{chat_id}/requests/{message_id}/events", dependencies=[Depends(authorized)])
    def events(chat_id: str, message_id: str) -> StreamingResponse:
        service.request(chat_id, message_id)
        return StreamingResponse(
            event_stream(service, chat_id, message_id, config.sse_poll_seconds),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/chats/{chat_id}/artifacts", dependencies=[Depends(authorized)])
    def artifacts(
        chat_id: str, limit: int = Query(50, ge=1, le=100), cursor: str | None = None
    ) -> dict[str, Any]:
        service.chat(chat_id)
        return page(
            service.deps.repos.artifacts.list_by_chat(chat_id),
            f"artifacts:{chat_id}",
            limit,
            cursor,
        )

    return app


async def event_stream(
    service: Application, chat_id: str, message_id: str, interval: float
) -> AsyncIterator[str]:
    seen: dict[str, str] = {}
    activity_cursor = 0
    for _ in range(max(1, int(900 / interval))):
        detail = await asyncio.to_thread(service.detail, chat_id, message_id)
        messages = await asyncio.to_thread(service.messages, chat_id)
        records = [("task", t) for t in detail["tasks"]]
        records += [
            ("message", wire(m))
            for m in messages
            if m.sender != "user" and getattr(m, "request_message_id", None) == message_id
        ]
        records.append(("request", detail["request"]))
        for kind, record in records:
            encoded = json.dumps(record, ensure_ascii=False)
            key = kind + ":" + record.get("messageId", record.get("taskId", ""))
            if seen.get(key) != encoded:
                yield f"event: {kind}\ndata: {encoded}\n\n"
                seen[key] = encoded
        for sequence, activity in service.deps.activity.since(message_id, activity_cursor):
            yield f"event: activity\ndata: {json.dumps(activity)}\n\n"
            activity_cursor = sequence
        status = detail["request"]["status"]
        if status in TERMINAL or (
            status == RequestStatus.WAITING_USER
            and any(k == "message" and r.get("kind") == "clarification" for k, r in records)
        ):
            return
        yield ": heartbeat\n\n"
        await asyncio.sleep(interval)


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8000)

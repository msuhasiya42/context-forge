"""One error shape everywhere (spec 11.2): ``{"error": {"code", "message"}}``, no traces."""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from contextforge.core.services.application import ServiceError

INTERNAL_MESSAGE = "Something went wrong. Please try again."

logger = logging.getLogger(__name__)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError) -> JSONResponse:
        return error_response(exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(400, "validation", "Invalid request fields.")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            return error_response(404, "not_found", "Not found.")
        return error_response(exc.status_code, "validation", str(exc.detail))


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
            await error_response(500, "internal", INTERNAL_MESSAGE)(scope, receive, send)


def error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})

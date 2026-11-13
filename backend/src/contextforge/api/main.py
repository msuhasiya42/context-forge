"""Builds the FastAPI app: wiring, middleware, and routers. Routes live in ``api.routes``."""

import logging
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from langgraph.checkpoint.sqlite import SqliteSaver

from contextforge.api.errors import InternalErrorEnvelope, install_error_handlers
from contextforge.api.routes import artifacts, chats, health, me, requests
from contextforge.core.logging import configure as configure_logging
from contextforge.core.services.application import Application
from contextforge.core.settings import Settings
from contextforge.infra.runtime import build_worker, research_providers
from contextforge.worker.consumer import QueueConsumer
from contextforge.worker.dependencies import WorkerDependencies
from contextforge.worker.runner import RequestRunner

BUILD = "web-research-v3"


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
    app.state.service, app.state.consumer, app.state.settings = service, consumer, config
    app.state.build_info = _build_info(config, worker_dependencies)
    # Added first, so it sits inside CORS: the 500 envelope gets the CORS headers too.
    app.add_middleware(InternalErrorEnvelope)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "Idempotency-Key", "Authorization"],
    )
    install_error_handlers(app)
    for module in (health, me, chats, requests, artifacts):
        app.include_router(module.router)
    return app


def _build_info(config: Settings, worker_dependencies: WorkerDependencies) -> dict[str, str]:
    """What ``/health`` reports about this process besides its pid."""
    # The provider the research stages call ("gemini", "bedrock", or both joined by "+"), or
    # "scripted" when no real model is used.
    model_label = (
        "+".join(sorted(research_providers(worker_dependencies.routes)))
        if config.model_backend != "scripted"
        else "scripted"
    )
    return {
        "build": BUILD,
        "webToolCallLimit": str(worker_dependencies.budgets.web_tool_call_budget),
        "backend": config.backend,
        "model": model_label,
        "search": config.search_provider,
    }


def main() -> None:
    import uvicorn

    # The worker runs in this process; without this its INFO records (plan, task, report
    # progress, each with its request and task id) were dropped.
    settings = Settings()
    secrets = tuple(
        value.get_secret_value()
        for value in (
            settings.dev_token,
            settings.serper_api_key,
            settings.tavily_api_key,
            settings.langsmith_api_key,
        )
        if value is not None
    )
    configure_logging(settings.backend_log_path, secrets=secrets)
    logging.getLogger(__name__).info("starting API; build=%s", BUILD)
    uvicorn.run(create_app(settings), host="127.0.0.1", port=8000, log_config=None)

"""Optional LangSmith model spans. Telemetry failure never repeats a provider call."""

import logging
import sys
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any

from langsmith import Client, trace, tracing_context

from contextforge.core.models.base import ModelClient, ModelRequest, ModelResponse

logger = logging.getLogger(__name__)


class TracedModel:
    def __init__(self, inner: ModelClient, api_key: str, project: str) -> None:
        self.inner = inner
        self.client = Client(api_key=api_key)
        self.project = project

    def invoke(self, request: ModelRequest) -> ModelResponse:
        with self._span(request) as run:
            response = self.inner.invoke(request)
            if run is None:
                return response
            try:
                run.end(
                    outputs={
                        "stopReason": response.stop_reason,
                        "usage": response.usage.model_dump() if response.usage else None,
                    }
                )
            except Exception:
                logger.warning("Could not finish model telemetry.")
            return response.model_copy(update={"langsmith_run_id": str(run.id)})

    @contextmanager
    def _span(self, request: ModelRequest) -> Iterator[Any]:
        metadata = {
            "stage": request.stage.value,
            "model": request.model,
            "taskId": request.task_id,
            "requestMessageId": request.request_message_id,
        }
        stack = ExitStack()
        try:
            stack.enter_context(
                tracing_context(enabled=True, client=self.client, project_name=self.project)
            )
            run = stack.enter_context(
                trace(
                    name=f"contextforge.{request.stage.value}",
                    run_type="llm",
                    inputs=metadata,
                    metadata=metadata,
                )
            )
        except Exception:
            _close(stack)
            logger.warning("Model telemetry unavailable; continuing without it.")
            yield None
            return
        try:
            yield run
        except BaseException:
            try:
                stack.__exit__(*sys.exc_info())
            except Exception:
                logger.warning("Could not record failed model telemetry.")
            raise
        else:
            _close(stack)


def _close(stack: ExitStack) -> None:
    try:
        stack.close()
    except Exception:
        logger.warning("Could not close model telemetry.")

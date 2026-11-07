"""Structured logging: every record carries the request and task it was emitted for.

The ids live in context variables set by ``request_context``, so code deep in a stage logs
normally and still gets them. The formatter reads them when it formats, which is in the
emitting thread for the synchronous stream handler ``configure`` installs.
"""

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

_request_message_id: ContextVar[str | None] = ContextVar("request_message_id", default=None)
_task_id: ContextVar[str | None] = ContextVar("task_id", default=None)


@contextmanager
def request_context(request_message_id: str, task_id: str | None = None) -> Iterator[None]:
    """Tag every log record emitted inside the block with these ids; restores the outer ones."""
    request_token = _request_message_id.set(request_message_id)
    task_token = _task_id.set(task_id)
    try:
        yield
    finally:
        _task_id.reset(task_token)
        _request_message_id.reset(request_token)


class JsonFormatter(logging.Formatter):
    """One JSON object per record; the ids are null outside a ``request_context``."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, str | None] = {
            "ts": datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_message_id": _request_message_id.get(),
            "task_id": _task_id.get(),
        }
        # Present only when the record carries one, so logger.exception keeps its traceback.
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def configure() -> None:
    """Send root-logger records at INFO and above to stderr as JSON. Safe to call twice."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if any(isinstance(handler.formatter, JsonFormatter) for handler in root.handlers):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)

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
from logging.handlers import RotatingFileHandler
from pathlib import Path

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

    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        super().__init__()
        self.secrets = tuple(secret for secret in secrets if secret)

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
        # Redact before JSON encoding so quotes/backslashes in a known secret cannot
        # defeat replacement. Normal logs never intentionally include credentials.
        for key, value in entry.items():
            if isinstance(value, str):
                for secret in self.secrets:
                    value = value.replace(secret, "[redacted]")
                entry[key] = value
        return json.dumps(entry)


def configure(log_path: Path | None = None, *, secrets: tuple[str, ...] = ()) -> None:
    """JSON logs to stderr and an optional rotating file; repeated calls do not duplicate."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formatter = JsonFormatter(secrets)
    handlers = [h for h in root.handlers if isinstance(h.formatter, JsonFormatter)]
    if not any(not isinstance(h, logging.FileHandler) for h in handlers):
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        root.addHandler(stream)
    for handler in handlers:
        handler.setFormatter(formatter)
    if log_path is not None:
        resolved = log_path.resolve()
        if not any(
            isinstance(h, RotatingFileHandler) and h.baseFilename == str(resolved) for h in handlers
        ):
            resolved.parent.mkdir(parents=True, exist_ok=True)
            file = RotatingFileHandler(
                resolved, maxBytes=10_000_000, backupCount=3, encoding="utf-8"
            )
            file.setFormatter(formatter)
            root.addHandler(file)

"""The trace hooks the worker opens: one per request segment, stage and task.

The worker depends on this protocol only (system design section 12: worker imports core, never
infra). ``NullTracer`` traces nothing and is the default; the LangSmith implementation is
``infra.tracing.RequestTracer``, which the runtime passes in when tracing is on.
"""

from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from typing import Any, Protocol

from contextforge.core.state import RequestState


class Tracer(Protocol):
    def request(
        self,
        request_message_id: str,
        chat_id: str,
        summary: Callable[[], Mapping[str, Any]],
    ) -> AbstractContextManager[None]:
        """The root of one segment; ``summary()`` (status, attempt) is recorded as it ends."""
        ...

    def stage(
        self, request_message_id: str, name: str, metadata: Mapping[str, Any] | None = None
    ) -> AbstractContextManager[None]:
        """A stage (plan, a research round, a lead review, cite, report) under the innermost
        stage open for the request, else its root."""
        ...

    def task(self, request_state: RequestState, task_id: str) -> AbstractContextManager[None]:
        """One task's execution under the innermost open stage (its round), else the root."""
        ...

    def annotate(self, request_message_id: str, metadata: Mapping[str, Any]) -> None:
        """Add ``metadata`` to the innermost stage open for the request: a decision and its
        reason, or which path a stage took."""
        ...


class NullTracer:
    """Traces nothing: every hook is an empty block."""

    @contextmanager
    def request(
        self,
        request_message_id: str,
        chat_id: str,
        summary: Callable[[], Mapping[str, Any]],
    ) -> Iterator[None]:
        yield

    @contextmanager
    def stage(
        self, request_message_id: str, name: str, metadata: Mapping[str, Any] | None = None
    ) -> Iterator[None]:
        yield

    @contextmanager
    def task(self, request_state: RequestState, task_id: str) -> Iterator[None]:
        yield

    def annotate(self, request_message_id: str, metadata: Mapping[str, Any]) -> None:
        return None

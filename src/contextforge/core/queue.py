"""Work queue boundary between the API and the worker: one message per run to start or resume."""

from collections import deque
from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class WorkMessage:
    kind: Literal["start", "resume"]
    chat_id: str
    # The request Message the run belongs to.
    message_id: str
    # On ``resume``, the clarification_response or action_response that unblocked the request.
    reply_message_id: str | None = None


class Queue(Protocol):
    def send(self, msg: WorkMessage) -> None: ...

    def receive(self) -> WorkMessage | None:
        """Return the oldest message, or None when the queue is empty."""
        ...


class InProcessQueue:
    """FIFO queue for one process; deque append and popleft are thread-safe."""

    def __init__(self) -> None:
        self._items: deque[WorkMessage] = deque()

    def send(self, msg: WorkMessage) -> None:
        self._items.append(msg)

    def receive(self) -> WorkMessage | None:
        # popleft on an empty deque raises; catching it avoids a check-then-pop race.
        try:
            return self._items.popleft()
        except IndexError:
            return None

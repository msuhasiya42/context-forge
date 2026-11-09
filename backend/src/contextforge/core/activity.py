"""Bounded, transient tool activity for local SSE; no user/tool payloads."""

import threading
from collections import deque


class ActivityBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: deque[tuple[int, str, dict[str, str]]] = deque(maxlen=500)
        self._sequence = 0

    def emit(self, request_id: str, task_id: str, text: str) -> None:
        with self._lock:
            self._sequence += 1
            self._events.append((self._sequence, request_id, {"taskId": task_id, "text": text}))

    def since(self, request_id: str, after: int) -> list[tuple[int, dict[str, str]]]:
        with self._lock:
            return [
                (seq, data) for seq, req, data in self._events if req == request_id and seq > after
            ]

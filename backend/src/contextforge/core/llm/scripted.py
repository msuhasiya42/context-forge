"""Deterministic stand-in for an LLM: returns prepared responses per stage, in order."""

import threading
from collections import deque
from collections.abc import Callable

from contextforge.core.llm.base import ModelRequest, ModelResponse
from contextforge.core.llm.errors import ModelError


class ScriptExhausted(ModelError):
    """The script has no response left for the requested stage."""


class ScriptedModel:
    """A ``ModelClient`` that pops the next scripted response for the request's stage.

    ``script`` is keyed by stage value (``"plan"``, ``"execute"``, ``"compress"``). The lists
    are copied, so the caller's script is never consumed. ``invoke`` is safe to call from
    several threads sharing one script.
    """

    def __init__(self, script: dict[str, list[ModelResponse]]) -> None:
        self._script = {stage: deque(responses) for stage, responses in script.items()}
        self._lock = threading.Lock()
        # Test hook: called with each request before its response is returned.
        self.on_request: Callable[[ModelRequest], None] | None = None

    def invoke(self, request: ModelRequest) -> ModelResponse:
        if self.on_request is not None:
            self.on_request(request)
        stage = request.stage.value
        with self._lock:
            responses = self._script.get(stage)
            if not responses:
                raise ScriptExhausted(f"no scripted response left for stage {stage}")
            return responses.popleft()

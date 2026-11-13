"""Per-stage model routing: a call goes to its model and, while that model is down, to the next
of its stage's fallbacks (config/models.yaml ``fallbacks``).

Only availability routes a call: the provider is down or unreachable, throttles after the
bounded retry, or the account may not invoke the model. A refused answer, a schema failure or
an exhausted budget is the same on any model, so it is raised as it is.
"""

import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence

from contextforge.core.entities.model_call import Stage
from contextforge.core.llm.base import ModelClient, ModelRequest, ModelResponse
from contextforge.core.llm.config import ModelRoute
from contextforge.core.llm.errors import ModelAccessDenied, ModelThrottled, ModelUnavailable

logger = logging.getLogger(__name__)

# ModelUnreachable is a ModelUnavailable.
ROUTED_FAILURES = (ModelUnavailable, ModelThrottled, ModelAccessDenied)
COOLDOWN_SECONDS = 60.0


class ModelCooldown:
    """The models that were down lately, for the whole process. A model is tried after the
    others for ``seconds`` after it failed, so a model that is down is not the first call of
    every request; it is still tried when every other model has failed."""

    def __init__(
        self, seconds: float = COOLDOWN_SECONDS, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._seconds = seconds
        self._clock = clock
        self._until: dict[str, float] = {}
        self._lock = threading.Lock()

    def mark(self, model: str) -> None:
        with self._lock:
            self._until[model] = self._clock() + self._seconds

    def order(self, models: Sequence[str]) -> list[str]:
        """``models`` with those cooling down moved last, each group in its given order."""
        now = self._clock()
        with self._lock:
            cooling = {model for model in models if self._until.get(model, 0.0) > now}
        return [m for m in models if m not in cooling] + [m for m in models if m in cooling]


class RoutedModel:
    """Sends each call to the request's model, then to its stage's fallbacks in turn while the
    model before is down. ``inner`` admits, retries and records each attempt, so every model
    tried is its own ModelCall and the one that answered is the call's ``model``."""

    def __init__(
        self, inner: ModelClient, routes: Mapping[Stage, ModelRoute], cooldown: ModelCooldown
    ) -> None:
        self._inner = inner
        self._routes = routes
        self._cooldown = cooldown

    def invoke(self, request: ModelRequest) -> ModelResponse:
        *others, last = self._cooldown.order(self._candidates(request))
        for model in others:
            try:
                return self._inner.invoke(_served_by(request, model))
            except ROUTED_FAILURES as exc:
                self._cooldown.mark(model)
                logger.warning(
                    "%s is unavailable for %s (%s); trying the next model",
                    model,
                    request.stage.value,
                    type(exc).__name__,
                )
        try:
            return self._inner.invoke(_served_by(request, last))
        except ROUTED_FAILURES:
            self._cooldown.mark(last)
            raise

    def _candidates(self, request: ModelRequest) -> list[str]:
        """The request's model, then its stage's fallbacks, each once."""
        route = self._routes.get(request.stage)
        fallbacks = route.fallbacks if route is not None else ()
        return list(dict.fromkeys([request.model, *fallbacks]))


def _served_by(request: ModelRequest, model: str) -> ModelRequest:
    if model == request.model:
        return request
    return request.model_copy(update={"model": model, "requested_model": request.model})

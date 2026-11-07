"""Atomic in-process call admission, spend reservations and bounded retries.

``BudgetedModel._reserve`` is the one place a request's call budget, spend cap and deadline are
enforced, before every attempt.
"""

import math
import random
import threading
import time

from contextforge.core.models.base import ModelClient, ModelRequest, ModelResponse
from contextforge.core.models.errors import ModelError
from contextforge.core.repositories.base import ModelCallRepository


class BudgetedModel:
    """Admits each attempt against the request's limits, then retries a retryable failure.

    A finished call is charged its recorded ``estimated_cost_usd``; a call still in flight is
    charged ``reserve_usd``, so concurrent calls cannot overrun the cap between them.
    """

    def __init__(
        self,
        inner: ModelClient,
        calls: ModelCallRepository,
        request_id: str,
        *,
        call_budget: int,
        max_usd: float = math.inf,
        reserve_usd: float = 0.0,
        retries: int = 0,
        lock: "threading.RLock | None" = None,
        pending: dict[str, int] | None = None,
        deadline: float | None = None,
    ) -> None:
        self.inner, self.calls, self.request_id = inner, calls, request_id
        self.call_budget, self.max_usd, self.reserve_usd = call_budget, max_usd, reserve_usd
        self.retries, self.lock = retries, lock or threading.RLock()
        self.pending = pending if pending is not None else {}
        # A time.monotonic() value; None means no deadline.
        self.deadline = deadline

    def invoke(self, request: ModelRequest) -> ModelResponse:
        attempt = 0
        while True:
            self._reserve()
            try:
                return self.inner.invoke(request)
            except ModelError as exc:
                if not exc.retryable or attempt >= self.retries:
                    raise
            finally:
                with self.lock:
                    self.pending[self.request_id] -= 1
            time.sleep(random.uniform(0, min(2**attempt, 4)))
            attempt += 1

    def _reserve(self) -> None:
        with self.lock:
            records = self.calls.list_by_request(self.request_id)
            inflight = self.pending.get(self.request_id, 0)
            if len(records) + inflight >= self.call_budget:
                raise ModelError("Request model-call budget exhausted.")
            if self.deadline is not None and time.monotonic() >= self.deadline:
                raise ModelError("Request deadline exceeded.")
            # A record without a cost (written before calls were priced) is charged the reserve.
            spent = sum(
                c.estimated_cost_usd if c.estimated_cost_usd is not None else self.reserve_usd
                for c in records
            )
            if spent + (inflight + 1) * self.reserve_usd > self.max_usd + 1e-9:
                raise ModelError("Request spend budget exhausted.")
            self.pending[self.request_id] = inflight + 1

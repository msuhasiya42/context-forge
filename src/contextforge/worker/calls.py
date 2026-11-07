"""Atomic in-process call admission, conservative spend reservations and bounded retries."""

import random
import threading
import time
from typing import Any

from contextforge.core.models.base import ModelClient, ModelRequest, ModelResponse
from contextforge.core.models.errors import ModelError, ModelTimeout, ModelUnavailable
from contextforge.core.repositories.base import ModelCallRepository


class BudgetedModel:
    def __init__(
        self,
        inner: ModelClient,
        calls: ModelCallRepository,
        request_id: str,
        *,
        call_budget: int,
        max_usd: float,
        reserve_usd: float,
        retries: int = 0,
        lock: Any = None,
        pending: dict[str, int] | None = None,
        deadline: float | None = None,
    ) -> None:
        self.inner, self.calls, self.request_id = inner, calls, request_id
        self.call_budget, self.max_usd, self.reserve_usd = call_budget, max_usd, reserve_usd
        self.retries, self.lock = retries, lock or threading.RLock()
        self.pending = pending if pending is not None else {}
        self.deadline = deadline

    def invoke(self, request: ModelRequest) -> ModelResponse:
        for attempt in range(self.retries + 1):
            self._reserve()
            try:
                return self.inner.invoke(request)
            except (ModelTimeout, ModelUnavailable):
                if attempt == self.retries:
                    raise
            finally:
                with self.lock:
                    self.pending[self.request_id] -= 1
            time.sleep(random.uniform(0, min(2**attempt, 4)))
        raise ModelError("Model attempts exhausted.")

    def _reserve(self) -> None:
        with self.lock:
            records = self.calls.list_by_request(self.request_id)
            inflight = self.pending.get(self.request_id, 0)
            if len(records) + inflight >= self.call_budget:
                raise ModelError("Request model-call budget exhausted.")
            if self.deadline is not None and time.monotonic() >= self.deadline:
                raise ModelError("Request deadline exceeded.")
            spent = sum(
                c.estimated_cost_usd if c.estimated_cost_usd is not None else self.reserve_usd
                for c in records
            )
            if spent + (inflight + 1) * self.reserve_usd > self.max_usd + 1e-9:
                raise ModelError("Request conservative spend budget exhausted.")
            self.pending[self.request_id] = inflight + 1

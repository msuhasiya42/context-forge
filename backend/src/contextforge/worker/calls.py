"""Atomic in-process call admission, spend reservations and bounded retries.

``BudgetedModel._reserve`` is the one place a request's call budgets, spend cap and deadline
are enforced, before every attempt.
"""

import math
import random
import threading
import time
from collections.abc import Callable, Sequence

from contextforge.core.entities.model_call import ModelCall
from contextforge.core.models.base import ModelClient, ModelRequest, ModelResponse
from contextforge.core.models.errors import ModelError
from contextforge.core.models.recording import (
    PriceTable,
    refused_before_generation,
    worst_case_usd,
)
from contextforge.core.repositories.base import ModelCallRepository

CALL_BUDGET_EXHAUSTED = "Request model-call budget exhausted."
CALLS_RESERVED = "Request model calls left are reserved for cite and report."
REFUSED_BUDGET_EXHAUSTED = "Request refused-call budget exhausted: the providers keep refusing."


def calls_used(records: Sequence[ModelCall]) -> int:
    """How many of ``records`` count toward the call budget: all but the attempts refused
    before generating."""
    return sum(1 for c in records if not refused_before_generation(c))


class BudgetedModel:
    """Admits each attempt against the request's limits, then retries a retryable failure.

    A finished call is charged its recorded ``estimated_cost_usd``. A call in flight holds a
    reservation of its own worst case: the context cap in and its ``max_tokens`` out at its
    model's ``prices`` (``reserve_usd`` for a model without a price). So concurrent calls cannot
    overrun the cap between them, yet a cheap call does not hold a planner-sized reservation.
    A zero-priced model reserves $0. ``RoutedModel`` admits each fallback as its own attempt, so
    the model that actually runs is the one reserved for, at its own prices.
    ``pending`` maps each request to the reservations of its calls in flight.

    An attempt the provider refused before generating (``refused_before_generation``) does not
    count toward ``call_budget`` when ``refused_call_budget`` is set; those attempts have that
    limit of their own instead. ``keep_calls`` is how many calls of ``call_budget`` this client
    may not use: a researcher's client leaves them to the lead, cite and report.
    """

    def __init__(
        self,
        inner: ModelClient,
        calls: ModelCallRepository,
        request_id: str,
        *,
        call_budget: int,
        refused_call_budget: int | None = None,
        keep_calls: int = 0,
        max_usd: float = math.inf,
        reserve_usd: float = 0.0,
        prices: PriceTable | None = None,
        cap_input_tokens: int = 0,
        retries: int = 0,
        lock: "threading.RLock | None" = None,
        pending: dict[str, list[float]] | None = None,
        deadline: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.inner, self.calls, self.request_id = inner, calls, request_id
        self.call_budget, self.max_usd, self.reserve_usd = call_budget, max_usd, reserve_usd
        self.refused_call_budget, self.keep_calls = refused_call_budget, keep_calls
        self.prices, self.cap_input_tokens = prices or {}, cap_input_tokens
        self.retries, self.lock = retries, lock or threading.RLock()
        self.pending = pending if pending is not None else {}
        # A ``clock()`` value; None means no deadline.
        self.deadline, self.clock = deadline, clock

    def invoke(self, request: ModelRequest) -> ModelResponse:
        attempt = 0
        while True:
            reserved = self._reserve(request)
            try:
                return self.inner.invoke(request)
            except ModelError as exc:
                if not exc.retryable or attempt >= self.retries:
                    raise
            finally:
                with self.lock:
                    self.pending[self.request_id].remove(reserved)
            time.sleep(random.uniform(0, min(2**attempt, 4)))
            attempt += 1

    def _reserve(self, request: ModelRequest) -> float:
        """Admit one attempt of ``request`` and hold its reservation; returns the amount held."""
        worst = worst_case_usd(self.prices, request, self.cap_input_tokens)
        reservation = self.reserve_usd if worst is None else worst
        with self.lock:
            records = self.calls.list_by_request(self.request_id)
            inflight = self.pending.setdefault(self.request_id, [])
            counted = len(records)
            if self.refused_call_budget is not None:
                counted = calls_used(records)
                if len(records) - counted >= self.refused_call_budget:
                    raise ModelError(REFUSED_BUDGET_EXHAUSTED)
            if counted + len(inflight) >= self.call_budget:
                raise ModelError(CALL_BUDGET_EXHAUSTED)
            if counted + len(inflight) + self.keep_calls >= self.call_budget:
                raise ModelError(CALLS_RESERVED)
            if self.deadline is not None and self.clock() >= self.deadline:
                raise ModelError("Request deadline exceeded.")
            # A record without a cost (written before calls were priced) is charged the reserve.
            spent = sum(
                c.estimated_cost_usd if c.estimated_cost_usd is not None else self.reserve_usd
                for c in records
            )
            if spent + sum(inflight) + reservation > self.max_usd + 1e-9:
                raise ModelError("Request spend budget exhausted.")
            inflight.append(reservation)
            return reservation

"""The dependency bundle every worker stage receives: storage, the model, limits and tools."""

import math
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from contextforge.core.activity import ActivityBus
from contextforge.core.context.budgets import Budgets
from contextforge.core.context.prompts import MEMORY_VERSION
from contextforge.core.entities.message import (
    ActionResponseMessage,
    ClarificationResponseMessage,
    RequestMessage,
)
from contextforge.core.entities.model_call import Stage
from contextforge.core.models.base import ModelClient
from contextforge.core.models.config import ModelRoute
from contextforge.core.models.recording import PriceTable, RecordingModelClient
from contextforge.core.models.transcript import TranscriptWriter
from contextforge.core.queue import Queue
from contextforge.core.repositories.memory import Repositories
from contextforge.core.settings import Settings
from contextforge.core.storage import BlobStore
from contextforge.core.tools.base import Tool
from contextforge.core.tracing import NullTracer, Tracer
from contextforge.worker.calls import BudgetedModel
from contextforge.worker.routing import ModelCooldown, RoutedModel


@dataclass
class WorkerDependencies:
    """``model`` is the provider client (traced when tracing is on); stages call it only through
    ``model_for`` so every call is admitted and recorded as a ModelCall of its request."""

    repositories: Repositories
    blobs: BlobStore
    model: ModelClient
    budgets: Budgets
    tools: dict[str, Tool]
    queue: Queue
    # Written on every ModelCall so a later price change never rewrites past costs.
    price_version: str
    # The model each stage calls and its fallbacks (config/models.yaml with the settings'
    # overrides); a stage without a route calls "scripted".
    routes: dict[Stage, ModelRoute] = field(default_factory=dict)
    # config/models.yaml prices; each ModelCall's cost is computed from its usage with them.
    prices: PriceTable = field(default_factory=dict)
    # Prefix of ModelCall.model (spec 3.5): every real model of this project is on Bedrock.
    model_provider: str = "bedrock"
    # Caps the tasks executing at once across the worker at ``budgets.tasks_in_flight``.
    semaphore: threading.BoundedSemaphore = field(init=False)
    settings: Settings | None = None
    budget_lock: threading.RLock = field(default_factory=threading.RLock)
    # The reservations of each request's model calls in flight (``BudgetedModel``).
    pending_calls: dict[str, list[float]] = field(default_factory=dict)
    activity: ActivityBus = field(default_factory=ActivityBus)
    # The monotonic clock deadlines are measured on; a test injects its own.
    clock: Callable[[], float] = time.monotonic
    # ``clock()`` at the start of each request's current segment (see ``segment``).
    segment_started: dict[str, float] = field(default_factory=dict)
    # Opens the trace runs; the default traces nothing. The runtime's LangSmith tracer also
    # wraps ``model`` and ``tools`` before they get here (infra.tracing.RequestTracer).
    tracer: Tracer = field(default_factory=NullTracer)
    # Shared by every request of the process: a model that was just down is tried last.
    cooldown: ModelCooldown = field(default_factory=ModelCooldown)

    def __post_init__(self) -> None:
        self.semaphore = threading.BoundedSemaphore(self.budgets.tasks_in_flight)

    def model_id(self, stage: Stage) -> str:
        """The model ``stage`` asks for first."""
        route = self.routes.get(stage)
        return route.primary if route is not None else "scripted"

    def model_for(
        self, request_message_id: str, *, memory_version: str | None = None
    ) -> ModelClient:
        """The model client for one request. A call goes to its stage's fallbacks while its model
        is down (``worker.routing``). Every attempt is admitted against the request's call budget
        (with settings, also its spend cap and deadline), retried when transient, and saved as a
        ModelCall priced from its usage, with its transcript unless settings turn transcripts off.

        The deadline counts from the start of the request's current segment, never from the
        request's creation (A-C1), so time spent waiting on the user is not counted.
        """
        settings = self.settings
        recorded = RecordingModelClient(
            self.model,
            self.repositories.model_calls,
            request_message_id=request_message_id,
            memory_version=memory_version or MEMORY_VERSION,
            price_version=self.price_version,
            prices=self.prices,
            unknown_cost_usd=settings.call_reserve_usd if settings else None,
            provider=self.model_provider,
            transcripts=self._transcript_writer(),
        )
        deadline = None
        if settings is not None:
            # A stage run outside a segment (a direct call in a test) gets the whole deadline.
            started = self.segment_started.get(request_message_id, self.clock())
            deadline = started + settings.request_deadline_seconds
        # Without settings (tests, offline tools) the call budget is the only limit.
        budgeted = BudgetedModel(
            recorded,
            self.repositories.model_calls,
            request_message_id,
            call_budget=self.budgets.call_budget,
            max_usd=settings.request_max_usd if settings else math.inf,
            reserve_usd=settings.call_reserve_usd if settings else 0.0,
            prices=self.prices,
            cap_input_tokens=self.budgets.cap_input_tokens,
            retries=settings.model_retries if settings else 0,
            lock=self.budget_lock,
            pending=self.pending_calls,
            deadline=deadline,
            clock=self.clock,
        )
        return RoutedModel(budgeted, self.routes, self.cooldown)

    def _transcript_writer(self) -> TranscriptWriter | None:
        """Writes transcripts to the blob store, redacting the configured secrets; None when
        settings turn transcripts off. Without settings (tests) they are recorded."""
        settings = self.settings
        if settings is None:
            return TranscriptWriter(self.blobs)
        if not settings.record_transcripts:
            return None
        return TranscriptWriter(self.blobs, secret_values(settings))

    @contextmanager
    def segment(self, chat_id: str, request_message_id: str) -> Iterator[None]:
        """One run of a request: its start or a resume, from claim until it ends or pauses.

        The request deadline counts from the segment's start, so the time a request waits on
        the user, between segments, never counts against it. The segment is one trace root.
        """
        self.segment_started[request_message_id] = self.clock()
        try:
            with self.tracer.request(
                request_message_id,
                chat_id,
                lambda: self._segment_summary(chat_id, request_message_id),
            ):
                yield
        finally:
            self.segment_started.pop(request_message_id, None)

    def _segment_summary(self, chat_id: str, request_message_id: str) -> dict[str, Any]:
        """The request's status as a segment ends, and which segment it was: 1 for the first
        run, n + 1 for the run after the user's n-th answer."""
        messages = self.repositories.messages.list_by_chat(chat_id)
        answers = [
            m
            for m in messages
            if isinstance(m, ClarificationResponseMessage | ActionResponseMessage)
            and m.request_message_id == request_message_id
        ]
        request = next(
            (
                m
                for m in messages
                if isinstance(m, RequestMessage) and m.message_id == request_message_id
            ),
            None,
        )
        return {"status": request.status if request else None, "attempt": len(answers) + 1}

    def touch_chat(self, chat_id: str, user_id: str) -> None:
        chat = self.repositories.chats.get(user_id, chat_id)
        if chat is None:
            return
        requests = [
            m
            for m in self.repositories.messages.list_by_chat(chat_id)
            if isinstance(m, RequestMessage)
        ]
        latest = max(requests, key=lambda m: (m.created_at, m.message_id), default=None)
        self.repositories.chats.touch(user_id, chat_id, latest.status if latest else None)


def secret_values(settings: Settings) -> list[str]:
    """Every configured secret, for ``TranscriptWriter`` to redact from what it stores."""
    keys = (
        settings.serper_api_key,
        settings.tavily_api_key,
        settings.langsmith_api_key,
        settings.dev_token,
        settings.gemini_api_key,
    )
    return [k.get_secret_value() for k in keys if k is not None]

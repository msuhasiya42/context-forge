"""The dependency bundle every worker stage receives: storage, the model, limits and tools."""

import math
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from contextforge.core.activity import ActivityBus
from contextforge.core.context.budgets import Budgets
from contextforge.core.context.prompts import MEMORY_VERSION
from contextforge.core.models.base import ModelClient
from contextforge.core.models.recording import PriceTable, RecordingModelClient
from contextforge.core.queue import Queue
from contextforge.core.repositories.memory import Repositories
from contextforge.core.settings import Settings
from contextforge.core.storage import BlobStore
from contextforge.core.tools.base import Tool
from contextforge.worker.calls import BudgetedModel


@dataclass
class WorkerDeps:
    """``model`` is the bare client; stages call it only through ``model_for`` so every call is
    recorded as a ModelCall of its request."""

    repos: Repositories
    blobs: BlobStore
    model: ModelClient
    budgets: Budgets
    tools: dict[str, Tool]
    queue: Queue
    # Written on every ModelCall so a later price change never rewrites past costs.
    price_version: str
    planner_model: str = "scripted"
    executor_model: str = "scripted"
    # config/models.yaml prices; each ModelCall's cost is computed from its usage with them.
    prices: PriceTable = field(default_factory=dict)
    # Prefix of ModelCall.model (spec 3.5): every real model of this project is on Bedrock.
    model_provider: str = "bedrock"
    # Caps the tasks executing at once across the worker at ``budgets.tasks_in_flight``.
    semaphore: threading.BoundedSemaphore = field(init=False)
    settings: Settings | None = None
    budget_lock: threading.RLock = field(default_factory=threading.RLock)
    pending_calls: dict[str, int] = field(default_factory=dict)
    activity: ActivityBus = field(default_factory=ActivityBus)

    def __post_init__(self) -> None:
        self.semaphore = threading.BoundedSemaphore(self.budgets.tasks_in_flight)

    def model_for(
        self,
        request_message_id: str,
        *,
        memory_version: str | None = None,
        created_at: str | None = None,
    ) -> ModelClient:
        """The model client for one request. Every attempt is admitted against the request's call
        budget (with settings, also its spend cap and deadline), retried when transient, and
        saved as a ModelCall priced from its usage.
        """
        settings = self.settings
        recorded = RecordingModelClient(
            self.model,
            self.repos.model_calls,
            request_message_id=request_message_id,
            memory_version=memory_version or MEMORY_VERSION,
            price_version=self.price_version,
            prices=self.prices,
            unknown_cost_usd=settings.call_reserve_usd if settings else None,
            provider=self.model_provider,
        )
        deadline = None
        if settings is not None:
            remaining = float(settings.request_deadline_seconds)
            if created_at is not None:
                age = (datetime.now(UTC) - datetime.fromisoformat(created_at)).total_seconds()
                remaining = max(0, remaining - age)
            deadline = time.monotonic() + remaining
        # Without settings (tests, offline tools) the call budget is the only limit.
        return BudgetedModel(
            recorded,
            self.repos.model_calls,
            request_message_id,
            call_budget=self.budgets.call_budget,
            max_usd=settings.request_max_usd if settings else math.inf,
            reserve_usd=settings.call_reserve_usd if settings else 0.0,
            retries=settings.model_retries if settings else 0,
            lock=self.budget_lock,
            pending=self.pending_calls,
            deadline=deadline,
        )

    def touch_chat(self, chat_id: str, user_id: str) -> None:
        from contextforge.core.entities.message import RequestMessage

        chat = self.repos.chats.get(user_id, chat_id)
        if chat is None:
            return
        requests = [
            m for m in self.repos.messages.list_by_chat(chat_id) if isinstance(m, RequestMessage)
        ]
        latest = max(requests, key=lambda m: (m.created_at, m.message_id), default=None)
        self.repos.chats.touch(user_id, chat_id, latest.status if latest else None)

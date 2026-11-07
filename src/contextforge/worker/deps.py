"""The dependency bundle every worker stage receives: storage, the model, limits and tools."""

import threading
from dataclasses import dataclass, field

from contextforge.core.context.budgets import Budgets
from contextforge.core.context.prompts import MEMORY_VERSION
from contextforge.core.models.base import ModelClient
from contextforge.core.models.recording import RecordingModelClient
from contextforge.core.queue import Queue
from contextforge.core.repositories.memory import Repositories
from contextforge.core.storage import BlobStore
from contextforge.core.tools.base import Tool


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
    # Caps the tasks executing at once across the worker at ``budgets.tasks_in_flight``.
    semaphore: threading.BoundedSemaphore = field(init=False)

    def __post_init__(self) -> None:
        self.semaphore = threading.BoundedSemaphore(self.budgets.tasks_in_flight)

    def model_for(self, request_message_id: str) -> ModelClient:
        """The model client for one request: each call it makes is saved as a ModelCall."""
        return RecordingModelClient(
            self.model,
            self.repos.model_calls,
            request_message_id=request_message_id,
            memory_version=MEMORY_VERSION,
            price_version=self.price_version,
        )

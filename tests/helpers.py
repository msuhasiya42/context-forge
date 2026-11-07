"""Builders shared by the worker tests: dependencies, a seeded chat and an empty brief."""

import dataclasses
from pathlib import Path

from contextforge.core.context.budgets import Budgets, load_budgets
from contextforge.core.entities.artifact import Artifact, ArtifactKind, ArtifactStatus, IndexStatus
from contextforge.core.entities.chat import Chat, ChatStatus
from contextforge.core.entities.message import MessageKind, RequestMessage, RequestStatus, Sender
from contextforge.core.ids import new_ulid, now_iso
from contextforge.core.models.base import ModelClient
from contextforge.core.models.config import load_models_config
from contextforge.core.queue import InProcessQueue
from contextforge.core.repositories.memory import Repositories
from contextforge.core.storage import MemoryBlobStore, dataset_key
from contextforge.worker.brief import initial_brief as initial_brief
from contextforge.worker.deps import WorkerDeps

# Resolved from this file, not the CWD, so the helpers work wherever pytest is launched.
REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_CSV = REPO_ROOT / "fixtures" / "transactions_sep_oct.csv"
FIXTURE_REQUEST = REPO_ROOT / "fixtures" / "request.txt"


def make_deps(
    model: ModelClient, *, call_budget: int | None = None, tasks_in_flight: int | None = None
) -> WorkerDeps:
    """In-memory WorkerDeps over ``model``, with the configured budgets and per-stage models."""
    models = load_models_config(REPO_ROOT / "config" / "models.yaml")
    return WorkerDeps(
        repos=Repositories.in_memory(),
        blobs=MemoryBlobStore(),
        model=model,
        budgets=_budgets(call_budget=call_budget, tasks_in_flight=tasks_in_flight),
        tools={},
        queue=InProcessQueue(),
        price_version=models.price_version,
        planner_model=models.stages["plan"].model,
        executor_model=models.stages["execute"].model,
    )


def _budgets(**overrides: int | None) -> Budgets:
    """``config/budgets.yaml`` with every override that is not None applied."""
    given = {name: value for name, value in overrides.items() if value is not None}
    return dataclasses.replace(load_budgets(), **given)


def seed_chat_with_dataset(
    deps: WorkerDeps, *, chat_id: str, user_id: str, dataset_id: str
) -> RequestMessage:
    """Save a chat holding the fixture CSV as an AVAILABLE dataset and an ACCEPTED request."""
    now = now_iso()
    deps.repos.chats.save(
        Chat(
            chat_id=chat_id,
            user_id=user_id,
            title="Spending",
            status=ChatStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
    )
    deps.repos.artifacts.save(_fixture_dataset(deps, chat_id, user_id, dataset_id, now))
    request = RequestMessage(
        message_id=new_ulid(),
        chat_id=chat_id,
        user_id=user_id,
        sender=Sender.USER,
        kind=MessageKind.REQUEST,
        content=FIXTURE_REQUEST.read_text(encoding="utf-8").strip(),
        status=RequestStatus.ACCEPTED,
        created_at=now,
        updated_at=now,
    )
    deps.repos.messages.save(request)
    return request


def _fixture_dataset(
    deps: WorkerDeps, chat_id: str, user_id: str, dataset_id: str, now: str
) -> Artifact:
    """The fixture CSV copied into the blob store, as the dataset Artifact that points at it."""
    data = FIXTURE_CSV.read_bytes()
    return Artifact(
        artifact_id=dataset_id,
        user_id=user_id,
        chat_id=chat_id,
        kind=ArtifactKind.DATASET,
        name=FIXTURE_CSV.name,
        media_type="text/csv",
        size_bytes=len(data),
        storage_ref=deps.blobs.put(dataset_key(chat_id, dataset_id), data),
        status=ArtifactStatus.AVAILABLE,
        index_status=IndexStatus.NOT_INDEXED,
        created_at=now,
    )

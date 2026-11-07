"""Builders shared by the worker tests: dependencies, a seeded chat, an empty brief, a plan."""

import dataclasses
from pathlib import Path
from typing import Any

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
from contextforge.worker.tools.sql_query import SqlQueryTool

# Resolved from this file, not the CWD, so the helpers work wherever pytest is launched.
REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_CSV = REPO_ROOT / "fixtures" / "transactions_sep_oct.csv"
FIXTURE_REQUEST = REPO_ROOT / "fixtures" / "request.txt"


def make_deps(
    model: ModelClient, *, call_budget: int | None = None, tasks_in_flight: int | None = None
) -> WorkerDeps:
    """In-memory WorkerDeps over ``model``, with the configured budgets and per-stage models,
    and the real ``sql_query`` tool over the same repositories and blob store."""
    models = load_models_config(REPO_ROOT / "config" / "models.yaml")
    repos = Repositories.in_memory()
    blobs = MemoryBlobStore()
    return WorkerDeps(
        repos=repos,
        blobs=blobs,
        model=model,
        budgets=_budgets(call_budget=call_budget, tasks_in_flight=tasks_in_flight),
        tools={"sql_query": SqlQueryTool(repos.artifacts, blobs)},
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


def six_task_plan() -> dict[str, Any]:
    """The spec 9.1 plan shape on the fixture chat: three sql tasks on a2 (t2 after t1), one
    pdf_lookup, one source_search and a reconcile after the three sql tasks. Camel-case, as
    the planner returns it; a fresh dict on every call, so a test may edit its copy."""
    return {
        "kind": "plan",
        "goal": "Explain how October 2026 spending differs from September 2026.",
        "constraints": [
            "Exclude transfers between the user's own accounts from spending.",
            "Count a card purchase once; do not also count the bill payment that settles it.",
        ],
        "openQuestions": ["Which account_ids belong to the user."],
        "tasks": [
            _planned("t1", "sql_analysis", "Identify transfers between accounts in a2.", ["a2"]),
            _planned("t2", "sql_analysis", "Monthly spending per category.", ["a2"], ["t1"]),
            _planned("t3", "sql_analysis", "Recurring merchants charged in both months.", ["a2"]),
            _planned("t4", "pdf_lookup", "Read each October card statement.", []),
            _planned("t5", "source_search", "Find refunds and reversals.", ["a2"]),
            _planned("t6", "reconcile", "Match card bills to payments.", [], ["t1", "t2", "t3"]),
        ],
        "nextAction": "Run t1, t3, t4 and t5, then t2 after t1.",
    }


def clarification(question: str) -> dict[str, Any]:
    """A planner output asking the user ``question`` instead of planning; camel-case, as the
    planner returns it."""
    return {
        "kind": "clarification",
        "goal": "g",
        "constraints": [],
        "openQuestions": [],
        "question": question,
        "nextAction": "wait for the answer",
    }


def _planned(
    ref: str, kind: str, goal: str, inputs: list[str], depends_on: list[str] | None = None
) -> dict[str, Any]:
    return {
        "ref": ref,
        "kind": kind,
        "goal": goal,
        "inputArtifactIds": inputs,
        "dependsOn": depends_on or [],
    }


def t1_result() -> dict[str, Any]:
    """The spec 9.2 t1 result trimmed to the fixture: two transfer pairs, the card settlement
    pair, two evidence refs on a2, two facts citing them and one uncertainty."""
    transfers = {"artifactId": "a2", "locator": {"kind": "row", "value": "12,13,40,41"}}
    settlement = {"artifactId": "a2", "locator": {"kind": "row", "value": "50,51"}}
    return {
        "summary": "Two own-account transfers between ACC-SAV-1 and ACC-CUR-2 totalling "
        "INR 40,000; one pair is an HDFC card bill settlement, not a transfer.",
        "facts": [
            {
                "text": "Rows 12/13 and 40/41 are transfers from ACC-SAV-1 to ACC-CUR-2 "
                "totalling INR 40,000.",
                "evidenceRefs": [transfers],
            },
            {
                "text": "Rows 50/51 settle the HDFC card bill of INR 4,312.",
                "evidenceRefs": [settlement],
            },
        ],
        "structuredData": {
            "transferPairs": [
                {"debitRow": 12, "creditRow": 13, "amount": 25000.00, "date": "2026-09-03"},
                {"debitRow": 40, "creditRow": 41, "amount": 15000.00, "date": "2026-10-02"},
            ],
            "cardSettlementPairs": [
                {
                    "debitRow": 50,
                    "creditRow": 51,
                    "amount": 4312.00,
                    "card": "ACC-CARD-HDFC",
                    "date": "2026-10-09",
                }
            ],
        },
        "evidenceRefs": [transfers, settlement],
        "artifactRefs": [],
        "uncertainties": [
            "Account ownership is inferred from self-referencing descriptions. "
            "Not confirmed by the user."
        ],
        "coverage": {"period": "2026-09-01 to 2026-10-31"},
    }

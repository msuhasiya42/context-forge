"""Runs one request's workflow on its own checkpointed thread: start, resume and restart.

The thread id is the request message id, so a later process (a new runner on the same
checkpoint store) finds the thread again. Every start and resume first claims the request with
a conditional status update, so a duplicate delivery never runs the workflow twice. The queue
is not used in phase 1; the API service delivers through it from phase 4.
"""

import logging
from dataclasses import dataclass
from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command

from contextforge.core.entities.message import (
    ActionResponseMessage,
    ClarificationResponseMessage,
    RequestStatus,
)
from contextforge.core.logging import request_context
from contextforge.core.repositories.base import StaleStatusError
from contextforge.core.state import PendingInterrupt
from contextforge.worker.brief import load_request
from contextforge.worker.deps import WorkerDeps
from contextforge.worker.workflow import build_workflow

logger = logging.getLogger(__name__)

ALREADY_STARTED = "already started"
NOT_THE_PENDING_INTERRUPT = "reply does not target the pending interrupt"

Reply = ClarificationResponseMessage | ActionResponseMessage


@dataclass(frozen=True)
class RunOutcome:
    """``status`` is the request's final status; None when the run paused on a question.
    ``reason`` says why a delivery did not run the workflow."""

    started: bool
    status: RequestStatus | None = None
    reason: str | None = None


class RequestRunner:
    """Holds one workflow over ``deps`` that checkpoints to ``checkpointer``."""

    def __init__(self, deps: WorkerDeps, checkpointer: BaseCheckpointSaver[Any]) -> None:
        self._deps = deps
        self._checkpointer = checkpointer
        self._workflow = build_workflow(deps, checkpointer)

    def start(self, chat_id: str, message_id: str) -> RunOutcome:
        """Claim the ACCEPTED request and run it until it ends or asks the user.

        A thread that already has a checkpoint (an operator requeue after a crash) continues
        with its finished tasks restored. Exceptions from the run propagate, leaving the request
        RUNNING and the checkpoint in place for a requeue.
        """
        with request_context(message_id):
            if not self._claim(chat_id, message_id, RequestStatus.ACCEPTED):
                return RunOutcome(started=False, reason=ALREADY_STARTED)
            return self._run(self._start_input(chat_id, message_id), message_id)

    def resume(self, chat_id: str, message_id: str, reply: Reply) -> RunOutcome:
        """Answer the pending interrupt with ``reply`` and run on until the next end or question.

        Refused, saving nothing and changing no status, unless the request is WAITING_USER and
        ``reply`` answers its pending interrupt. The entrypoint re-runs from its first line: the
        finished tasks are restored and the interrupt returns the answer.
        """
        with request_context(message_id):
            if not self._accept(chat_id, message_id, reply):
                return RunOutcome(started=False, reason=NOT_THE_PENDING_INTERRUPT)
            answer = {"message_id": reply.message_id, "content": reply.content}
            return self._run(Command(resume=answer), message_id)

    def pending_interrupt(self, message_id: str) -> PendingInterrupt | None:
        """What the request's thread is waiting on, or None when it waits on nothing."""
        snapshot = self._workflow.get_state(_thread(message_id))
        # Older LangGraph releases expose interrupts only per task.
        interrupts = getattr(snapshot, "interrupts", ()) or [
            pending for task in snapshot.tasks for pending in task.interrupts
        ]
        return cast(PendingInterrupt, interrupts[0].value) if interrupts else None

    def _claim(self, chat_id: str, message_id: str, expected: RequestStatus) -> bool:
        """Move the request ``expected`` -> RUNNING; False when another delivery moved it first."""
        try:
            self._deps.repos.messages.update_request_status(
                chat_id, message_id, expected, RequestStatus.RUNNING
            )
        except StaleStatusError as stale:
            logger.info(
                "request is %s, not %s; this delivery does not run it", stale.actual, expected
            )
            return False
        return True

    def _accept(self, chat_id: str, message_id: str, reply: Reply) -> bool:
        """Save ``reply`` and claim the request when the reply answers its pending interrupt."""
        if not self._awaits(chat_id, message_id, reply.in_reply_to_message_id):
            logger.warning(
                "reply %s refused: it does not answer the pending interrupt", reply.message_id
            )
            return False
        # Saved before the claim, so a RUNNING request always has its answer in the chat. A
        # delivery that then loses the claim to a concurrent one has saved its reply, no more.
        self._deps.repos.messages.save(reply)
        return self._claim(chat_id, message_id, RequestStatus.WAITING_USER)

    def _awaits(self, chat_id: str, message_id: str, target_message_id: str) -> bool:
        """Whether the request is WAITING_USER on the interrupt for ``target_message_id``."""
        request = load_request(self._deps.repos.messages, chat_id, message_id)
        if request.status != RequestStatus.WAITING_USER:
            return False
        pending = self.pending_interrupt(message_id)
        return pending is not None and pending["message_id"] == target_message_id

    def _start_input(self, chat_id: str, message_id: str) -> dict[str, str] | None:
        """The entrypoint input of a new thread; None continues a thread from its checkpoint.

        Asks the checkpointer, not ``get_state(...).values``: a thread stopped mid-run has a
        checkpoint whose values are None, as falsy as a missing thread's empty values.
        """
        if self._checkpointer.get_tuple(_thread(message_id)) is None:
            return {"chat_id": chat_id, "message_id": message_id}
        logger.info("checkpoint found; continuing the thread with finished tasks restored")
        return None

    def _run(self, payload: dict[str, str] | Command[Any] | None, message_id: str) -> RunOutcome:
        """Invoke the thread; a run paused on an interrupt returns no status."""
        result = self._workflow.invoke(payload, _thread(message_id))
        status = result.get("status")
        logger.info("run returned %s", status or "on an interrupt")
        return RunOutcome(started=True, status=None if status is None else RequestStatus(status))


def _thread(message_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": message_id}}

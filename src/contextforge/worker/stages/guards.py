"""The cooperative cancel check a stage runs before every model call, and the one way a stage
moves the request out of RUNNING, which honours a cancel.

The request's call budget, spend cap and deadline are enforced in one place, ``BudgetedModel``
(``worker/calls.py``), which every model call goes through via ``WorkerDependencies.model_for``."""

from contextforge.core.entities.message import RequestStatus
from contextforge.core.repositories.base import StaleStatusError
from contextforge.worker.dependencies import WorkerDependencies
from contextforge.worker.request_state import load_request


class Cancelled(Exception):
    """The user cancelled the request; the run stops before its next model call or wave."""


def check_not_cancelled(
    worker_dependencies: WorkerDependencies, chat_id: str, request_message_id: str
) -> None:
    """Raise ``Cancelled`` when the API has moved the request to CANCELLED."""
    request = load_request(worker_dependencies.repositories.messages, chat_id, request_message_id)
    if request.status == RequestStatus.CANCELLED:
        raise Cancelled(f"request {request_message_id} was cancelled")


def leave_running(
    worker_dependencies: WorkerDependencies,
    chat_id: str,
    request_message_id: str,
    new: RequestStatus,
    failure_reason: str | None = None,
) -> None:
    """Move the request RUNNING -> ``new``; a re-run that finds it already ``new`` changes nothing.

    Raises ``Cancelled`` when the user cancelled meanwhile and ``StaleStatusError`` for any other
    unexpected status.
    """
    try:
        worker_dependencies.repositories.messages.update_request_status(
            chat_id, request_message_id, RequestStatus.RUNNING, new, failure_reason
        )
    except StaleStatusError as exc:
        if exc.actual == RequestStatus.CANCELLED:
            raise Cancelled(f"request {request_message_id} was cancelled") from exc
        if exc.actual != new:
            raise

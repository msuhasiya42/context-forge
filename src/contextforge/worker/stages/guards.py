"""The cooperative cancel check a stage runs before every model call, and the one way a stage
moves the request out of RUNNING, which honours a cancel.

The request's call budget, spend cap and deadline are enforced in one place, ``BudgetedModel``
(``worker/calls.py``), which every model call goes through via ``WorkerDeps.model_for``."""

from contextforge.core.entities.message import RequestStatus
from contextforge.core.repositories.base import StaleStatusError
from contextforge.worker.brief import load_request
from contextforge.worker.deps import WorkerDeps


class Cancelled(Exception):
    """The user cancelled the request; the run stops before its next model call or wave."""


def check_not_cancelled(deps: WorkerDeps, chat_id: str, request_message_id: str) -> None:
    """Raise ``Cancelled`` when the API has moved the request to CANCELLED."""
    request = load_request(deps.repos.messages, chat_id, request_message_id)
    if request.status == RequestStatus.CANCELLED:
        raise Cancelled(f"request {request_message_id} was cancelled")


def leave_running(
    deps: WorkerDeps,
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
        deps.repos.messages.update_request_status(
            chat_id, request_message_id, RequestStatus.RUNNING, new, failure_reason
        )
    except StaleStatusError as exc:
        if exc.actual == RequestStatus.CANCELLED:
            raise Cancelled(f"request {request_message_id} was cancelled") from exc
        if exc.actual != new:
            raise

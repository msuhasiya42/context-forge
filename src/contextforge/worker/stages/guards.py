"""Checks a stage runs before every model call (cooperative cancel and the request call budget),
and the one way a stage moves the request out of RUNNING, which honours a cancel."""

from contextforge.core.entities.message import RequestStatus
from contextforge.core.repositories.base import StaleStatusError
from contextforge.worker.brief import load_request
from contextforge.worker.deps import WorkerDeps


class Cancelled(Exception):
    """The user cancelled the request; the run stops before its next model call or wave."""


class CallBudgetExceeded(Exception):
    """The request has made as many model calls as ``budgets.call_budget`` allows."""


def check_not_cancelled(deps: WorkerDeps, chat_id: str, request_message_id: str) -> None:
    """Raise ``Cancelled`` when the API has moved the request to CANCELLED."""
    request = load_request(deps.repos.messages, chat_id, request_message_id)
    if request.status == RequestStatus.CANCELLED:
        raise Cancelled(f"request {request_message_id} was cancelled")


def check_call_budget(deps: WorkerDeps, request_message_id: str) -> None:
    """Raise ``CallBudgetExceeded`` when one more call would pass the budget.

    Failed calls count too: they were recorded, and a provider may still bill them.
    """
    made = len(deps.repos.model_calls.list_by_request(request_message_id))
    if made >= deps.budgets.call_budget:
        raise CallBudgetExceeded(
            f"request {request_message_id} has made {made} of {deps.budgets.call_budget} "
            "model calls"
        )


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

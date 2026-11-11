"""The request workflow (LangGraph Functional API): plan, then research rounds (gate, task
waves, lead review), then cite and report.

On resume the entrypoint re-runs from its first line and every finished task's result is
restored by name and call order. So every model call and repository write lives in a task,
``interrupt`` is called only here, and the code between tasks is deterministic given their
results: no clock, no randomness, task futures created and awaited in task_id order.
"""

import logging
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass
from functools import partial
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.func import entrypoint, task
from langgraph.pregel import Pregel
from langgraph.types import interrupt

from contextforge.core.context.builder import newest_facts
from contextforge.core.entities.message import RequestStatus
from contextforge.core.logging import request_context
from contextforge.core.services.memory import memory_snapshot
from contextforge.core.state import OpenQuestion, PlanEntry, RequestState
from contextforge.worker.dependencies import WorkerDependencies
from contextforge.worker.request_state import create_initial_request_state, load_request
from contextforge.worker.stages.citation_checks import (
    CitationCheckResult,
    merge_duplicate_findings,
)
from contextforge.worker.stages.execute import TaskOutcome, execute_task
from contextforge.worker.stages.guards import Cancelled, check_not_cancelled
from contextforge.worker.stages.lead import LeadReview, lead_review
from contextforge.worker.stages.plan import (
    PlanFailed,
    PlanResult,
    answer_constraint,
    create_research_plan,
    validate_and_save_tasks,
)
from contextforge.worker.stages.report import fail_request, write_report
from contextforge.worker.stages.review import validate_research_findings
from contextforge.worker.stages.round_budget import research_reserve

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _WorkflowStages:
    """Each stage bound to the worker's worker_dependencies and wrapped once as a named LangGraph
    task."""

    load_request_state: Callable[..., Future[RequestState]]
    create_plan: Callable[..., Future[PlanResult]]
    validate_and_save_tasks: Callable[..., Future[list[PlanEntry]]]
    execute_task: Callable[..., Future[TaskOutcome]]
    lead_review: Callable[..., Future[LeadReview]]
    validate_findings: Callable[..., Future[CitationCheckResult]]
    write_report: Callable[..., Future[str]]
    fail_request: Callable[..., Future[None]]

    @classmethod
    def create(cls, worker_dependencies: WorkerDependencies) -> "_WorkflowStages":
        # Stored task labels stay stable so paused workflows can restore earlier results.
        return cls(
            load_request_state=_as_task(
                "load_brief", partial(load_initial_request_state, worker_dependencies)
            ),
            create_plan=_as_task(
                "plan", partial(create_research_plan, worker_dependencies=worker_dependencies)
            ),
            validate_and_save_tasks=_as_task(
                "gate", partial(validate_and_save_tasks, worker_dependencies=worker_dependencies)
            ),
            execute_task=_as_task(
                "execute", partial(execute_task, worker_dependencies=worker_dependencies)
            ),
            lead_review=_as_task(
                "lead", partial(lead_review, worker_dependencies=worker_dependencies)
            ),
            validate_findings=_as_task(
                "cite", partial(validate_research_findings, worker_dependencies=worker_dependencies)
            ),
            write_report=_as_task(
                "report", partial(write_report, worker_dependencies=worker_dependencies)
            ),
            fail_request=_as_task(
                "fail_request", partial(fail_request, worker_dependencies=worker_dependencies)
            ),
        )


def build_research_workflow(
    worker_dependencies: WorkerDependencies, checkpointer: BaseCheckpointSaver[Any] | None
) -> Pregel[Any, Any, Any, Any]:
    """The workflow over ``worker_dependencies``. Invoke it with ``{"chat_id", "message_id"}`` on
    the thread
    ``message_id``; resume a question with ``Command(resume={"message_id", "content"})``."""
    workflow_stages = _WorkflowStages.create(worker_dependencies)

    def run_request(inputs: dict[str, Any]) -> dict[str, str]:
        return _run_request_with_cancellation(inputs, workflow_stages, worker_dependencies)

    return entrypoint(checkpointer=checkpointer)(run_request)


def load_initial_request_state(
    worker_dependencies: WorkerDependencies, chat_id: str, message_id: str
) -> RequestState:
    """The empty request state of request ``message_id`` with its user's memory as of now, read from
    the repositories."""
    request = load_request(worker_dependencies.repositories.messages, chat_id, message_id)
    return create_initial_request_state(
        request, memory_snapshot(worker_dependencies.repositories.users.get(request.user_id))
    )


def _as_task[T](name: str, stage: Callable[..., T]) -> Callable[..., Future[T]]:
    return task(name=name)(stage)


def _run_request_with_cancellation(
    inputs: dict[str, Any],
    workflow_stages: _WorkflowStages,
    worker_dependencies: WorkerDependencies,
) -> dict[str, str]:
    """The entrypoint body. A cancelled request returns without touching its status, which the
    API already set; any other exception propagates so the checkpoint keeps finished tasks."""
    with request_context(inputs["message_id"]):
        try:
            return {"status": _run_research_request(inputs, workflow_stages, worker_dependencies)}
        except Cancelled:
            logger.info("request cancelled; the run stops here")
            return {"status": RequestStatus.CANCELLED.value}


def _run_research_request(
    inputs: dict[str, Any],
    workflow_stages: _WorkflowStages,
    worker_dependencies: WorkerDependencies,
) -> str:
    """Plan, run the research rounds, cite and report; the request's final status."""
    request_state = workflow_stages.load_request_state(
        inputs["chat_id"], inputs["message_id"]
    ).result()
    try:
        with worker_dependencies.tracer.stage(inputs["message_id"], "plan"):
            request_state = _plan_research_tasks(request_state, workflow_stages)
    except PlanFailed as failed:
        workflow_stages.fail_request(request_state, failed.reason).result()
        return RequestStatus.FAILED.value
    request_state, task_outcomes = _research_rounds(
        request_state, workflow_stages, worker_dependencies
    )
    # The request state is a bounded context aid; task results are the complete factual record.
    # Restore every task's findings for final verification instead of silently losing early
    # subjects when the request state's recency cap was reached.
    request_state = {
        **request_state,
        "facts": merge_duplicate_findings([fact for o in task_outcomes for fact in o["facts"]]),
    }
    with worker_dependencies.tracer.stage(inputs["message_id"], "cite"):
        cited = workflow_stages.validate_findings(request_state).result()
    request_state = {
        **request_state,
        "facts": cited["facts"],
        "verification": cited.get("review", {}),
    }
    with worker_dependencies.tracer.stage(inputs["message_id"], "report"):
        return workflow_stages.write_report(
            request_state, task_outcomes, cited["uncertainties"]
        ).result()


def _plan_research_tasks(
    request_state: RequestState, workflow_stages: _WorkflowStages
) -> RequestState:
    """Plan, interrupting for each question the planner asks, then gate the plan into Tasks.

    Each answer joins the constraints, so the next attempt plans with it.
    """
    attempt = 0
    planned = workflow_stages.create_plan(request_state, attempt).result()
    while (pending := planned["pending"]) is not None:
        answer = interrupt(pending)
        request_state = _with_answer(request_state, pending["question"], answer)
        attempt += 1
        planned = workflow_stages.create_plan(request_state, attempt).result()
    request_state = _with_plan(request_state, planned)
    return {
        **request_state,
        "plan": workflow_stages.validate_and_save_tasks(request_state, planned).result(),
    }


def _with_answer(
    request_state: RequestState, question: str, answer: dict[str, str]
) -> RequestState:
    """The request state with the user's answer as a constraint; an answer holding a credential is
    left
    out (the report says so)."""
    constraint = answer_constraint(question, answer["content"])
    if constraint is None:
        logger.warning("answer %s looks like a credential; not used", answer["message_id"])
        return request_state
    return _with_constraints(request_state, [constraint])


def _with_constraints(request_state: RequestState, added: Iterable[str]) -> RequestState:
    return {**request_state, "constraints": _unique([*request_state["constraints"], *added])}


def _with_plan(request_state: RequestState, planned: PlanResult) -> RequestState:
    """The planner's goal and next action; its constraints and open questions are added to the
    request state's own (clarification answers among them), never put in their place."""
    asked = {question["text"] for question in request_state["open_questions"]}
    new_questions = [
        OpenQuestion(text=text, resolved_by=None)
        for text in _unique(planned["open_questions"])
        if text not in asked
    ]
    constraints = list(planned["constraints"])
    period = planned["period"]
    if period is not None:
        constraints.append(f"Period: {period['start']} to {period['end']}")
    assumption = planned.get("assumption")
    assumed = [assumption] if assumption else []
    return {
        **_with_constraints(request_state, constraints),
        "period": period,
        "assumptions": [*request_state.get("assumptions", []), *assumed],
        "goal": planned["goal"],
        # A plan checkpointed before F16 has no brief.
        "brief": planned.get("brief", ""),
        "open_questions": [*request_state["open_questions"], *new_questions],
        "next_action": planned["next_action"],
    }


def _unique(items: Iterable[str]) -> list[str]:
    """``items`` without repeats, first occurrence first."""
    return list(dict.fromkeys(items))


def _research_rounds(
    request_state: RequestState,
    workflow_stages: _WorkflowStages,
    worker_dependencies: WorkerDependencies,
) -> tuple[RequestState, list[TaskOutcome]]:
    """Run the plan's researchers, then, while rounds remain, let the lead review every
    finding so far and gate and run the researchers it starts. Ends when the lead completes
    (its review also completes when a task or budget cap leaves no room) or after
    ``budgets.research_rounds`` rounds; with 1 the lead is never called. Returns every round's
    outcomes."""
    finished: dict[str, TaskOutcome] = {}
    research_round = 1
    while True:
        request_state = _execute_planned_tasks(
            request_state, finished, workflow_stages, worker_dependencies
        )
        if research_round >= worker_dependencies.budgets.research_rounds:
            return request_state, list(finished.values())
        review = workflow_stages.lead_review(
            request_state, list(finished.values()), research_round
        ).result()
        if review["brief"]:
            request_state = {**request_state, "brief": review["brief"]}
        if review["action"] == "complete_task":
            return request_state, list(finished.values())
        research_round += 1
        try:
            entries = workflow_stages.validate_and_save_tasks(
                request_state, review, research_round=research_round
            ).result()
        except PlanFailed as rejected:  # the report is written from the rounds already run
            logger.warning("round %d rejected by the gate: %s", research_round, rejected.reason)
            worker_dependencies.activity.emit(
                request_state["request_message_id"],
                "",
                f"Research stopped: round {research_round} rejected: {rejected.reason}",
            )
            return request_state, list(finished.values())
        request_state = {**request_state, "plan": [*request_state["plan"], *entries]}


def _execute_planned_tasks(
    request_state: RequestState,
    finished_task_outcomes: dict[str, TaskOutcome],
    workflow_stages: _WorkflowStages,
    worker_dependencies: WorkerDependencies,
) -> RequestState:
    """Execute the plan's unfinished entries in dependency waves, adding each outcome to
    ``finished_task_outcomes``; a wave's facts join the request state before the next.

    Each wave runs with the calls the lead, cite and report still need kept from it
    (``research_reserve`` over the facts found and the researchers yet to finish), so a
    researcher that would use them ends instead, and the report is written from what research
    found."""
    while len(finished_task_outcomes) < len(request_state["plan"]):
        check_not_cancelled(
            worker_dependencies, request_state["chat_id"], request_state["request_message_id"]
        )
        ready_tasks = _find_ready_tasks(
            request_state["plan"],
            finished_task_outcomes,
            worker_dependencies.budgets.tasks_in_flight,
        )
        reserve = research_reserve(
            sum(len(outcome["facts"]) for outcome in finished_task_outcomes.values()),
            len(request_state["plan"]) - len(finished_task_outcomes),
        )
        wave_state: RequestState = {**request_state, "call_reserve": reserve}
        futures = [
            workflow_stages.execute_task(wave_state, entry["task_id"]) for entry in ready_tasks
        ]
        task_outcomes = [future.result() for future in futures]
        finished_task_outcomes.update((outcome["task_id"], outcome) for outcome in task_outcomes)
        request_state = _update_request_state_with_results(
            request_state, task_outcomes, worker_dependencies.budgets.facts_cap
        )
    return request_state


def _find_ready_tasks(
    plan: list[PlanEntry], finished_task_outcomes: Mapping[str, TaskOutcome], limit: int
) -> list[PlanEntry]:
    """Up to ``limit`` unfinished entries whose dependencies are all done, in task_id order.

    Capped here as well as by ``worker_dependencies.semaphore``: the semaphore bounds the tasks in
    flight, but
    task threads reach it in no fixed order, so a wave larger than the cap would start its tasks
    in a different order on every run.
    """
    ready = [
        entry
        for entry in sorted(plan, key=lambda entry: entry["task_id"])
        if entry["task_id"] not in finished_task_outcomes
        and all(d in finished_task_outcomes for d in entry["depends_on"])
    ]
    if not ready:
        raise ValueError("no planned task can start: a dependency is outside the plan")
    return ready[:limit]


def _update_request_state_with_results(
    request_state: RequestState, task_outcomes: list[TaskOutcome], facts_cap: int
) -> RequestState:
    """The request state with the wave's facts appended (the newest ``facts_cap`` kept) and its plan
    entries showing the wave's statuses."""
    statuses = {outcome["task_id"]: outcome["status"] for outcome in task_outcomes}
    facts = [
        *request_state["facts"],
        *(fact for outcome in task_outcomes for fact in outcome["facts"]),
    ]
    plan: list[PlanEntry] = [
        {**entry, "status": statuses.get(entry["task_id"], entry["status"])}
        for entry in request_state["plan"]
    ]
    return {**request_state, "facts": newest_facts(facts, facts_cap), "plan": plan}

"""The request workflow (LangGraph Functional API): plan, gate, task waves, cite, report.

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
from contextforge.worker.brief import initial_brief, load_request
from contextforge.worker.deps import WorkerDeps
from contextforge.worker.stages.cite import Citations, check_citations
from contextforge.worker.stages.execute import TaskOutcome, execute_stage
from contextforge.worker.stages.guards import Cancelled, check_not_cancelled
from contextforge.worker.stages.plan import (
    PlanFailed,
    PlanResult,
    answer_constraint,
    plan_gate,
    plan_stage,
)
from contextforge.worker.stages.report import fail_request, write_report

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _StageTasks:
    """Each stage bound to the worker's deps and wrapped once as a named LangGraph task."""

    load_brief: Callable[..., Future[RequestState]]
    plan: Callable[..., Future[PlanResult]]
    gate: Callable[..., Future[list[PlanEntry]]]
    execute: Callable[..., Future[TaskOutcome]]
    cite: Callable[..., Future[Citations]]
    report: Callable[..., Future[str]]
    fail: Callable[..., Future[None]]

    @classmethod
    def bound_to(cls, deps: WorkerDeps) -> "_StageTasks":
        return cls(
            load_brief=_as_task("load_brief", partial(initial_brief_from_repo, deps)),
            plan=_as_task("plan", partial(plan_stage, deps=deps)),
            gate=_as_task("gate", partial(plan_gate, deps=deps)),
            execute=_as_task("execute", partial(execute_stage, deps=deps)),
            cite=_as_task("cite", partial(check_citations, deps=deps)),
            report=_as_task("report", partial(write_report, deps=deps)),
            fail=_as_task("fail_request", partial(fail_request, deps=deps)),
        )


def build_workflow(
    deps: WorkerDeps, checkpointer: BaseCheckpointSaver[Any] | None
) -> Pregel[Any, Any, Any, Any]:
    """The workflow over ``deps``. Invoke it with ``{"chat_id", "message_id"}`` on the thread
    ``message_id``; resume a question with ``Command(resume={"message_id", "content"})``."""
    stages = _StageTasks.bound_to(deps)

    def run_request(inputs: dict[str, Any]) -> dict[str, str]:
        return _run_request(inputs, stages, deps)

    return entrypoint(checkpointer=checkpointer)(run_request)


def initial_brief_from_repo(deps: WorkerDeps, chat_id: str, message_id: str) -> RequestState:
    """The empty brief of request ``message_id``, read from the repository."""
    brief = initial_brief(load_request(deps.repos.messages, chat_id, message_id))
    brief["memory"] = memory_snapshot(deps.repos.users.get(brief["user_id"]))
    return brief


def _as_task[T](name: str, stage: Callable[..., T]) -> Callable[..., Future[T]]:
    return task(name=name)(stage)


def _run_request(inputs: dict[str, Any], stages: _StageTasks, deps: WorkerDeps) -> dict[str, str]:
    """The entrypoint body. A cancelled request returns without touching its status, which the
    API already set; any other exception propagates so the checkpoint keeps finished tasks."""
    with request_context(inputs["message_id"]):
        try:
            return {"status": _run_to_status(inputs, stages, deps)}
        except Cancelled:
            logger.info("request cancelled; the run stops here")
            return {"status": RequestStatus.CANCELLED.value}


def _run_to_status(inputs: dict[str, Any], stages: _StageTasks, deps: WorkerDeps) -> str:
    """Plan, run the waves, cite and report; the request's final status."""
    brief = stages.load_brief(inputs["chat_id"], inputs["message_id"]).result()
    try:
        with deps.tracer.stage(inputs["message_id"], "plan"):
            brief = _plan(brief, stages)
    except PlanFailed as failed:
        stages.fail(brief, failed.reason).result()
        return RequestStatus.FAILED.value
    brief, outcomes = _run_waves(brief, stages, deps)
    with deps.tracer.stage(inputs["message_id"], "cite"):
        cited = stages.cite(brief).result()
    brief = {**brief, "facts": cited["facts"]}
    with deps.tracer.stage(inputs["message_id"], "report"):
        return stages.report(brief, outcomes, cited["uncertainties"]).result()


def _plan(brief: RequestState, stages: _StageTasks) -> RequestState:
    """Plan, interrupting for each question the planner asks, then gate the plan into Tasks.

    Each answer joins the constraints, so the next attempt plans with it.
    """
    attempt = 0
    planned = stages.plan(brief, attempt).result()
    while (pending := planned["pending"]) is not None:
        answer = interrupt(pending)
        brief = _with_answer(brief, pending["question"], answer)
        attempt += 1
        planned = stages.plan(brief, attempt).result()
    brief = _with_plan(brief, planned)
    return {**brief, "plan": stages.gate(brief, planned).result()}


def _with_answer(brief: RequestState, question: str, answer: dict[str, str]) -> RequestState:
    """The brief with the user's answer as a constraint; an answer holding a credential is left
    out (the report says so)."""
    constraint = answer_constraint(question, answer["content"])
    if constraint is None:
        logger.warning("answer %s looks like a credential; not used", answer["message_id"])
        return brief
    return _with_constraints(brief, [constraint])


def _with_constraints(brief: RequestState, added: Iterable[str]) -> RequestState:
    return {**brief, "constraints": _unique([*brief["constraints"], *added])}


def _with_plan(brief: RequestState, planned: PlanResult) -> RequestState:
    """The planner's goal and next action; its constraints and open questions are added to the
    brief's own (clarification answers among them), never put in their place."""
    asked = {question["text"] for question in brief["open_questions"]}
    new_questions = [
        OpenQuestion(text=text, resolved_by=None)
        for text in _unique(planned["open_questions"])
        if text not in asked
    ]
    constraints = list(planned["constraints"])
    period = planned["period"]
    if period is not None:
        constraints.append(f"Period: {period['start']} to {period['end']}")
    return {
        **_with_constraints(brief, constraints),
        "period": period,
        "goal": planned["goal"],
        "open_questions": [*brief["open_questions"], *new_questions],
        "next_action": planned["next_action"],
    }


def _unique(items: Iterable[str]) -> list[str]:
    """``items`` without repeats, first occurrence first."""
    return list(dict.fromkeys(items))


def _run_waves(
    brief: RequestState, stages: _StageTasks, deps: WorkerDeps
) -> tuple[RequestState, list[TaskOutcome]]:
    """Execute the plan in dependency waves; a wave's facts join the brief before the next."""
    done: dict[str, TaskOutcome] = {}
    while len(done) < len(brief["plan"]):
        check_not_cancelled(deps, brief["chat_id"], brief["request_message_id"])
        wave = _next_wave(brief["plan"], done, deps.budgets.tasks_in_flight)
        futures = [stages.execute(brief, entry["task_id"]) for entry in wave]
        outcomes = [future.result() for future in futures]
        done.update((outcome["task_id"], outcome) for outcome in outcomes)
        brief = _after_wave(brief, outcomes, deps.budgets.facts_cap)
    return brief, list(done.values())


def _next_wave(
    plan: list[PlanEntry], done: Mapping[str, TaskOutcome], limit: int
) -> list[PlanEntry]:
    """Up to ``limit`` unfinished entries whose dependencies are all done, in task_id order.

    Capped here as well as by ``deps.semaphore``: the semaphore bounds the tasks in flight, but
    task threads reach it in no fixed order, so a wave larger than the cap would start its tasks
    in a different order on every run.
    """
    ready = [
        entry
        for entry in sorted(plan, key=lambda entry: entry["task_id"])
        if entry["task_id"] not in done and all(d in done for d in entry["depends_on"])
    ]
    if not ready:
        raise ValueError("no planned task can start: a dependency is outside the plan")
    return ready[:limit]


def _after_wave(brief: RequestState, outcomes: list[TaskOutcome], facts_cap: int) -> RequestState:
    """The brief with the wave's facts appended (the newest ``facts_cap`` kept) and its plan
    entries showing the wave's statuses."""
    statuses = {outcome["task_id"]: outcome["status"] for outcome in outcomes}
    facts = [*brief["facts"], *(fact for outcome in outcomes for fact in outcome["facts"])]
    plan: list[PlanEntry] = [
        {**entry, "status": statuses.get(entry["task_id"], entry["status"])}
        for entry in brief["plan"]
    ]
    return {**brief, "facts": newest_facts(facts, facts_cap), "plan": plan}

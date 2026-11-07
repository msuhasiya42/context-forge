"""Plan stage and plan gate (spec 9.1): a request becomes a validated plan, or one question.

Both are plain functions the workflow wraps as tasks. They never call ``interrupt()``: the plan
stage returns the clarification as a ``pending`` payload and the workflow interrupts on it.
"""

import logging
from dataclasses import replace
from datetime import date, datetime
from typing import Any, TypedDict
from zoneinfo import ZoneInfo

from langchain_core.messages import HumanMessage
from pydantic import ValidationError

from contextforge.core.context.builder import ContextInputs, build_context
from contextforge.core.context.prompts import load_prompt, prompt_version
from contextforge.core.context.resolver import ResolvedContext, resolve_context
from contextforge.core.entities.artifact import Artifact, ArtifactStatus
from contextforge.core.entities.message import (
    ClarificationMessage,
    Message,
    MessageKind,
    RequestMessage,
    RequestStatus,
    Sender,
)
from contextforge.core.entities.model_call import Stage
from contextforge.core.entities.task import Task, TaskStatus
from contextforge.core.ids import derived_id, new_ulid, now_iso
from contextforge.core.logging import request_context
from contextforge.core.models.base import ModelRequest
from contextforge.core.models.scripted import ScriptExhausted
from contextforge.core.planner import PlannedTask, PlannerOutput
from contextforge.core.repositories.base import StaleStatusError
from contextforge.core.state import PendingInterrupt, PlanEntry, RequestState
from contextforge.worker.brief import load_request
from contextforge.worker.deps import WorkerDeps
from contextforge.worker.stages.guards import (
    CallBudgetExceeded,
    check_call_budget,
    check_not_cancelled,
)
from contextforge.worker.stages.sources import available_artifacts, source_line

logger = logging.getLogger(__name__)

# Phase 1 has no User record to read it from; phase 7 takes it from the user's settings.
USER_TIMEZONE = "Asia/Kolkata"
# Attempts 0 and 1 may each ask one question; from attempt 2 the planner must plan.
MAX_CLARIFICATIONS = 2
NO_MORE_QUESTIONS = "Do not ask another question; plan with stated assumptions"


class PlanFailed(Exception):
    """The request cannot be planned; ``reason`` becomes the request's failure reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PlanResult(TypedDict):
    """What ``plan_stage`` returns: plain JSON values, so the workflow can checkpoint it.

    ``tasks`` holds ``PlannedTask`` dumps with snake_case keys; ``pending`` is set only when the
    planner asked a question.
    """

    kind: str
    goal: str
    constraints: list[str]
    open_questions: list[str]
    next_action: str
    tasks: list[dict[str, Any]]
    pending: PendingInterrupt | None


def clarification_id(request_message_id: str, attempt: int) -> str:
    """The clarification Message id of one plan attempt, so a re-run overwrites, never adds."""
    return derived_id(f"{request_message_id}:clarification:{attempt}")


def plan_stage(
    brief: RequestState, attempt: int, deps: WorkerDeps, *, today: date | None = None
) -> PlanResult:
    """Ask the planner for a plan or one clarification; ``attempt`` counts questions answered.

    A clarification is saved as a Message and moves the request RUNNING -> WAITING_USER. Raises
    ``PlanFailed`` on a third question, a second invalid plan, an exhausted call budget or a
    failed model call; ``Cancelled`` propagates. ``today`` defaults to the user's local date.
    """
    with request_context(brief["request_message_id"]):
        inputs = _planner_inputs(brief, attempt, deps, today or _local_today())
        output = _plan_or_fail(brief, inputs, deps)
        logger.info("planner returned a %s", output.kind)
        pending = None if output.kind == "plan" else _ask_user(brief, attempt, output, deps)
        return PlanResult(
            kind=output.kind,
            goal=output.goal,
            constraints=output.constraints,
            open_questions=output.open_questions,
            next_action=output.next_action,
            tasks=[task.model_dump(mode="json") for task in output.tasks or []],
            pending=pending,
        )


def plan_gate(brief: RequestState, planned: PlanResult, deps: WorkerDeps) -> list[PlanEntry]:
    """Check the plan against the chat, then create one PLANNED Task per planned task.

    Every task is checked before any is saved, so a rejected plan creates nothing. Idempotent:
    when the request already has Tasks (a re-run after a crash before the checkpoint), returns
    their entries and creates none. Entries come back in plan order.
    """
    request_message_id = brief["request_message_id"]
    with request_context(request_message_id):
        existing = deps.repos.tasks.list_by_request(request_message_id)
        if existing:
            logger.info("plan gate found %d tasks already created", len(existing))
            return [_plan_entry(task) for task in existing]
        planned_tasks = [PlannedTask.model_validate(task) for task in planned["tasks"]]
        _check_plan(planned_tasks, deps.repos.artifacts.list_by_chat(brief["chat_id"]))
        tasks = _new_tasks(planned_tasks, request_message_id)
        for task in tasks:
            deps.repos.tasks.save(task)
        logger.info("plan gate created %d tasks", len(tasks))
        return [_plan_entry(task) for task in tasks]


def _local_today() -> date:
    return datetime.now(ZoneInfo(USER_TIMEZONE)).date()


def _planner_inputs(
    brief: RequestState, attempt: int, deps: WorkerDeps, today: date
) -> ContextInputs:
    """Section 8.2 for the planner: request, sources and prior messages; no tools or facts."""
    request = load_request(deps.repos.messages, brief["chat_id"], brief["request_message_id"])
    artifacts = available_artifacts(deps.repos.artifacts, brief["chat_id"])
    resolved = resolve_context(request.content, artifacts, today=today, timezone=USER_TIMEZONE)
    return ContextInputs(
        memory=load_prompt("agent-memory"),
        stage_prompt=load_prompt(prompt_version(Stage.PLAN)),
        chat_id=brief["chat_id"],
        request=request,
        # Phase 1 has no User repository; User.instructions arrive in phase 7.
        preferences={},
        constraints=_planner_constraints(brief, attempt, resolved),
        facts=[],
        open_questions=[],
        next_action="",
        dependency_summaries=[],
        evidence_slices=[],
        tools=[],
        sources=[source_line(artifact, deps.blobs) for artifact in artifacts],
        prior_messages=_prior_messages(deps, request),
        loop_messages=[],
        context_block=_context_block(resolved),
    )


def _planner_constraints(brief: RequestState, attempt: int, resolved: ResolvedContext) -> list[str]:
    constraints = list(brief["constraints"])
    if resolved.period is not None:
        constraints.append(f"Period: {resolved.period}")
    if attempt >= MAX_CLARIFICATIONS:
        constraints.append(NO_MORE_QUESTIONS)
    return constraints


def _context_block(resolved: ResolvedContext) -> str:
    """Today, the period and each source's index status; dates live here, never in system."""
    return "\n".join(
        [
            f"today: {resolved.today} ({resolved.timezone})",
            f"period: {resolved.period or 'not stated in the request'}",
            *(f"{s.artifact_id} index={s.index_status}" for s in resolved.sources),
        ]
    )


def _prior_messages(deps: WorkerDeps, request: RequestMessage) -> list[Message]:
    """The chat's other Messages, oldest first."""
    in_chat = deps.repos.messages.list_by_chat(request.chat_id)
    others = [m for m in in_chat if m.message_id != request.message_id]
    # Not message_id order alone: a clarification's derived id does not sort by time.
    return sorted(others, key=lambda m: (m.created_at, m.message_id))


def _plan_or_fail(brief: RequestState, inputs: ContextInputs, deps: WorkerDeps) -> PlannerOutput:
    """The planner's output; a call that cannot be made or answered becomes ``PlanFailed``."""
    try:
        return _plan_with_one_retry(brief, inputs, deps)
    except CallBudgetExceeded as exc:
        raise PlanFailed("model budget exhausted") from exc
    except ScriptExhausted as exc:
        raise PlanFailed(f"planner model call failed: {exc}") from exc


def _plan_with_one_retry(
    brief: RequestState, inputs: ContextInputs, deps: WorkerDeps
) -> PlannerOutput:
    """Call the planner; on a schema failure call once more, told what was wrong."""
    try:
        return _call_planner(brief, inputs, deps)
    except ValidationError as rejected:
        errors = _one_line(rejected)
        logger.warning("planner output rejected, retrying once: %s", errors)
        rejection = f"Previous plan was rejected: {errors}"
        retry_inputs = replace(inputs, constraints=[*inputs.constraints, rejection])
    try:
        return _call_planner(brief, retry_inputs, deps)
    except ValidationError as rejected_again:
        reason = f"planner returned an invalid plan twice: {_one_line(rejected_again)}"
        raise PlanFailed(reason) from rejected_again


def _call_planner(brief: RequestState, inputs: ContextInputs, deps: WorkerDeps) -> PlannerOutput:
    """One guarded, recorded planner call; ``ValidationError`` when the output breaks the schema."""
    check_not_cancelled(deps, brief["chat_id"], brief["request_message_id"])
    check_call_budget(deps, brief["request_message_id"])
    built = build_context(inputs, deps.budgets)
    request = ModelRequest(
        stage=Stage.PLAN,
        model=deps.planner_model,
        system=built.system,
        messages=[HumanMessage(built.user_text), *built.messages],
        output_schema=PlannerOutput,
    )
    response = deps.model_for(brief["request_message_id"]).invoke(request)
    return PlannerOutput.model_validate(response.structured)


def _one_line(error: ValidationError) -> str:
    """Each error as ``<field path>: <message>`` joined by "; ", for the retry's constraint."""
    return "; ".join(
        f"{'.'.join(str(part) for part in e['loc'])}: {e['msg']}" if e["loc"] else e["msg"]
        for e in error.errors()
    )


def _ask_user(
    brief: RequestState, attempt: int, output: PlannerOutput, deps: WorkerDeps
) -> PendingInterrupt:
    """Save the question as a Message, mark the request waiting, and return the payload."""
    if attempt >= MAX_CLARIFICATIONS:
        raise PlanFailed("planner asked a third question")
    # PlannerOutput's validator rejects a clarification without a question.
    assert output.question
    message_id = clarification_id(brief["request_message_id"], attempt)
    deps.repos.messages.save(_clarification(brief, message_id, output.question))
    _wait_for_user(brief, deps)
    return PendingInterrupt(
        kind=MessageKind.CLARIFICATION.value,
        message_id=message_id,
        question=output.question,
        next_action=output.next_action,
    )


def _clarification(brief: RequestState, message_id: str, question: str) -> ClarificationMessage:
    now = now_iso()
    return ClarificationMessage(
        message_id=message_id,
        chat_id=brief["chat_id"],
        user_id=brief["user_id"],
        sender=Sender.ASSISTANT,
        kind=MessageKind.CLARIFICATION,
        request_message_id=brief["request_message_id"],
        content=question,
        created_at=now,
        updated_at=now,
    )


def _wait_for_user(brief: RequestState, deps: WorkerDeps) -> None:
    """RUNNING -> WAITING_USER; a re-run that finds the request already waiting changes nothing."""
    try:
        deps.repos.messages.update_request_status(
            brief["chat_id"],
            brief["request_message_id"],
            RequestStatus.RUNNING,
            RequestStatus.WAITING_USER,
        )
    except StaleStatusError as exc:
        if exc.actual != RequestStatus.WAITING_USER:
            raise


def _check_plan(planned_tasks: list[PlannedTask], chat_artifacts: list[Artifact]) -> None:
    """Raise ``PlanFailed`` at the first input the chat cannot supply or unknown dependency."""
    artifacts = {artifact.artifact_id: artifact for artifact in chat_artifacts}
    refs = {task.ref for task in planned_tasks}
    for task in planned_tasks:
        for artifact_id in task.input_artifact_ids:
            _check_input(task.ref, artifact_id, artifacts.get(artifact_id))
        for dependency in task.depends_on:
            if dependency not in refs:
                raise PlanFailed(f"Planned task {task.ref} depends on unknown task {dependency}")


def _check_input(ref: str, artifact_id: str, artifact: Artifact | None) -> None:
    if artifact is None:
        raise PlanFailed(
            f"Planned task {ref} names artifact {artifact_id} that is not in this chat"
        )
    if artifact.status != ArtifactStatus.AVAILABLE:
        raise PlanFailed(
            f"Planned task {ref} names artifact {artifact_id} that is {artifact.status.value}, "
            "not AVAILABLE"
        )


def _new_tasks(planned_tasks: list[PlannedTask], request_message_id: str) -> list[Task]:
    """One PLANNED Task per planned task; ids are made in plan order, so they sort in it."""
    task_ids = {task.ref: new_ulid() for task in planned_tasks}
    now = now_iso()
    return [
        Task(
            task_id=task_ids[task.ref],
            request_message_id=request_message_id,
            kind=task.kind,
            goal=task.goal,
            input_artifact_ids=task.input_artifact_ids,
            depends_on_task_ids=[task_ids[ref] for ref in task.depends_on],
            status=TaskStatus.PLANNED,
            attempt=1,
            created_at=now,
            updated_at=now,
        )
        for task in planned_tasks
    ]


def _plan_entry(task: Task) -> PlanEntry:
    return PlanEntry(task_id=task.task_id, kind=task.kind.value, status=task.status.value)

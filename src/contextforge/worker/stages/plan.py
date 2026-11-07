"""Plan stage and plan gate (spec 9.1): a request becomes a validated plan, or one question.

Both are plain functions the workflow wraps as tasks. They never call ``interrupt()``: the plan
stage returns the clarification as a ``pending`` payload and the workflow interrupts on it.
"""

import logging
import re
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
    ReportMessage,
    RequestMessage,
    RequestStatus,
    Sender,
)
from contextforge.core.entities.model_call import Stage
from contextforge.core.entities.task import Task, TaskStatus
from contextforge.core.ids import derived_id, new_ulid, now_iso
from contextforge.core.logging import request_context
from contextforge.core.models.base import ModelRequest
from contextforge.core.models.errors import ModelError
from contextforge.core.planner import PlannedTask, PlannerOutput
from contextforge.core.services.credentials import contains_credential
from contextforge.core.services.memory import user_timezone
from contextforge.core.state import PendingInterrupt, PlanEntry, RequestState
from contextforge.worker.brief import load_request
from contextforge.worker.deps import WorkerDeps
from contextforge.worker.stages.errors import one_line
from contextforge.worker.stages.guards import check_not_cancelled, leave_running
from contextforge.worker.stages.sources import available_artifacts, source_line

logger = logging.getLogger(__name__)

# One question per request at most, and only about the chat's own sources: a public web
# question is planned on its most likely reading (live: the planner asked for "the period" of
# "this week", then asked it again after the answer).
MAX_CLARIFICATIONS = 1
NO_MORE_QUESTIONS = "Do not ask another question; plan with stated assumptions"
FORCED_PLAN = "You may not ask '{question}' or any other question. Plan on its most likely reading."
NOT_ASKED = "Not asked: '{question}'. Planned on its most likely reading."
NO_PERIOD = "none stated; use the most recent information available"
# A constraint naming these is the planner restating its own output rules (live finding L-2),
# not something the user stated; the report drops it so no later request inherits it.
_PLAN_MECHANICS = re.compile(
    r"\b(?:plan|task|tasks|kind|schema|inputArtifactIds|JSON|format)\b", re.IGNORECASE
)


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
    period: dict[str, str] | None
    # Set when the planner wanted a question it may not ask; the report lists it.
    assumption: str | None


def clarification_id(request_message_id: str, attempt: int) -> str:
    """The clarification Message id of one plan attempt, so a re-run overwrites, never adds."""
    return derived_id(f"{request_message_id}:clarification:{attempt}")


def plan_stage(
    brief: RequestState, attempt: int, deps: WorkerDeps, *, today: date | None = None
) -> PlanResult:
    """Ask the planner for a plan or one clarification; ``attempt`` counts questions answered.

    A clarification is saved as a Message and moves the request RUNNING -> WAITING_USER. A
    question the planner may not ask (``_may_ask``) is never put to the user: the planner is
    called once more, told to plan on the most likely reading, and the result's ``assumption``
    names the question. Raises ``PlanFailed`` when it asks even then, on a second invalid plan,
    an exhausted call budget or a failed model call; ``Cancelled`` propagates. ``today``
    defaults to the user's local date.
    """
    with request_context(brief["request_message_id"]):
        request = load_request(deps.repos.messages, brief["chat_id"], brief["request_message_id"])
        artifacts = available_artifacts(deps.repos.artifacts, brief["chat_id"])
        timezone = user_timezone(brief["memory"])
        resolved = resolve_context(
            request.content, artifacts, today=today or local_today(timezone), timezone=timezone
        )
        may_ask = _may_ask(attempt, artifacts)
        inputs = _planner_inputs(brief, may_ask, deps, request, artifacts, resolved)
        output = _plan_or_fail(brief, inputs, deps)
        logger.info("planner returned a %s", output.kind)
        assumption = None
        if output.kind == "clarification" and not may_ask:
            logger.warning("planner asked a question it may not ask; planning instead")
            assumption = NOT_ASKED.format(question=output.question)
            told = FORCED_PLAN.format(question=output.question)
            output = _plan_or_fail(
                brief, replace(inputs, constraints=[*inputs.constraints, told]), deps
            )
            if output.kind == "clarification":
                raise PlanFailed("planner asked a question after being told to plan")
        pending = None if output.kind == "plan" else _ask_user(brief, attempt, output, deps)
        return PlanResult(
            kind=output.kind,
            goal=output.goal,
            constraints=output.constraints,
            open_questions=output.open_questions,
            next_action=output.next_action,
            tasks=[task.model_dump(mode="json") for task in output.tasks or []],
            pending=pending,
            period={"start": resolved.period_start, "end": resolved.period_end}
            if resolved.period_start and resolved.period_end
            else None,
            assumption=assumption,
        )


def _may_ask(attempt: int, artifacts: list[Artifact]) -> bool:
    """Whether the planner may put a question to the user: only the first time, and only in a
    chat with sources of its own to ask about; a public web question is never asked about."""
    return attempt < MAX_CLARIFICATIONS and bool(artifacts)


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
        deps.repos.tasks.save_all(tasks)
        logger.info("plan gate created %d tasks", len(tasks))
        return [_plan_entry(task) for task in tasks]


def local_today(timezone: str) -> date:
    """Today in ``timezone``, the user's."""
    return datetime.now(ZoneInfo(timezone)).date()


def answer_constraint(question: str, answer: str) -> str | None:
    """The constraint a clarification answer adds to the brief; None when the answer holds a
    credential, which never enters the brief or a report (system design section 5)."""
    if contains_credential(answer):
        return None
    return f"User answered '{question}': {answer}"


def is_plan_mechanics(constraint: str) -> bool:
    """True when ``constraint`` is about the plan's format rather than the user's request."""
    return _PLAN_MECHANICS.search(constraint) is not None


def _planner_inputs(
    brief: RequestState,
    may_ask: bool,
    deps: WorkerDeps,
    request: RequestMessage,
    artifacts: list[Artifact],
    resolved: ResolvedContext,
) -> ContextInputs:
    """Section 8.2 for the planner: request, sources and prior messages; no tools or facts.
    The memory snapshot supplies the system prefix (with the user's instructions) and the
    preferences."""
    return ContextInputs(
        memory=brief["memory"]["text"],
        stage_prompt=load_prompt(prompt_version(Stage.PLAN)),
        chat_id=brief["chat_id"],
        request=request,
        preferences=brief["memory"]["preferences"],
        constraints=_planner_constraints(brief, may_ask, resolved),
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


def _planner_constraints(
    brief: RequestState, may_ask: bool, resolved: ResolvedContext
) -> list[str]:
    constraints = list(brief["constraints"])
    if resolved.period is not None:
        constraints.append(f"Period: {resolved.period}")
    if not may_ask:
        constraints.append(NO_MORE_QUESTIONS)
    return constraints


def _context_block(resolved: ResolvedContext) -> str:
    """Today, the period and each source's index status; dates live here, never in system."""
    return "\n".join(
        [
            f"today: {resolved.today} ({resolved.timezone})",
            f"period: {resolved.period or NO_PERIOD}",
            *(f"{s.artifact_id} index={s.index_status}" for s in resolved.sources),
        ]
    )


def _prior_messages(deps: WorkerDeps, request: RequestMessage) -> list[Message]:
    """The chat's two newest reports from before ``request``, oldest first, each after the
    request it answered: a short follow-up ("Try again.") needs the question, not only the
    claims."""
    in_chat = deps.repos.messages.list_by_chat(request.chat_id)
    reports = [
        m for m in in_chat if isinstance(m, ReportMessage) and m.created_at < request.created_at
    ]
    # Not message_id order alone: a clarification's derived id does not sort by time.
    newest = sorted(reports, key=lambda m: (m.created_at, m.message_id))[-2:]
    asked = {m.message_id: m for m in in_chat if isinstance(m, RequestMessage)}
    prior: list[Message] = []
    for report in newest:
        if report.request_message_id in asked:
            prior.append(asked[report.request_message_id])
        prior.append(report)
    return prior


def _plan_or_fail(brief: RequestState, inputs: ContextInputs, deps: WorkerDeps) -> PlannerOutput:
    """The planner's output; a call that cannot be made or answered becomes ``PlanFailed``."""
    try:
        return _plan_with_one_retry(brief, inputs, deps)
    except ModelError as exc:  # an exhausted budget or a passed deadline among them
        raise PlanFailed(f"planner model call failed: {exc}") from exc


def _plan_with_one_retry(
    brief: RequestState, inputs: ContextInputs, deps: WorkerDeps
) -> PlannerOutput:
    """Call the planner; on a schema failure call once more, told what was wrong."""
    try:
        return _call_planner(brief, inputs, deps)
    except ValidationError as rejected:
        errors = one_line(rejected)
        logger.warning("planner output rejected, retrying once: %s", errors)
        # ModelCall has no metadata field, so the retry is on record as activity (finding L-3).
        deps.activity.emit(brief["request_message_id"], "", "Plan was invalid; retry 1 of 1")
        rejection = f"Previous plan was rejected: {errors}"
        retry_inputs = replace(inputs, constraints=[*inputs.constraints, rejection])
    try:
        return _call_planner(brief, retry_inputs, deps)
    except ValidationError as rejected_again:
        reason = f"planner returned an invalid plan twice: {one_line(rejected_again)}"
        raise PlanFailed(reason) from rejected_again


def _call_planner(brief: RequestState, inputs: ContextInputs, deps: WorkerDeps) -> PlannerOutput:
    """One guarded, recorded planner call; ``ValidationError`` when the output breaks the schema."""
    check_not_cancelled(deps, brief["chat_id"], brief["request_message_id"])
    built = build_context(inputs, deps.budgets)
    request = ModelRequest(
        stage=Stage.PLAN,
        model=deps.planner_model,
        system=built.system,
        messages=[HumanMessage(built.user_text), *built.messages],
        output_schema=PlannerOutput,
    )
    response = deps.model_for(
        brief["request_message_id"], memory_version=brief["memory"]["version"]
    ).invoke(request)
    return PlannerOutput.model_validate(response.structured)


def _ask_user(
    brief: RequestState, attempt: int, output: PlannerOutput, deps: WorkerDeps
) -> PendingInterrupt:
    """Mark the request waiting, then save the question as a Message; return the payload.

    The status moves first so a request cancelled during the planner call never gets a question.
    A crash between the two heals on re-run: WAITING_USER is accepted and the id is derived.
    """
    if not output.question:
        raise PlanFailed("planner asked without a question")
    leave_running(deps, brief["chat_id"], brief["request_message_id"], RequestStatus.WAITING_USER)
    message_id = clarification_id(brief["request_message_id"], attempt)
    deps.repos.messages.save(_clarification(brief, message_id, output.question))
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
    return PlanEntry(
        task_id=task.task_id,
        kind=task.kind.value,
        status=task.status.value,
        depends_on=task.depends_on_task_ids,
    )

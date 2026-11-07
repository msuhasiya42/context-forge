"""Execute stage (spec 9.2): one Task run to its outcome; an sql task through its own subgraph.

The subgraph is the model/tool loop of one task over ``TaskState``. It is compiled without a
checkpointer, so its transcript is dropped when the task ends (spec 8.6 level 2): what outlives
it is the Task record and the outcome the workflow folds into the request state.
"""

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Any, NotRequired, TypedDict

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    ToolMessage,
)
from langchain_core.messages.tool import tool_call
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from pydantic import ValidationError

from contextforge.core.context.budgets import Budgets
from contextforge.core.context.builder import (
    BuiltContext,
    ContextInputs,
    SourceLine,
    build_context,
)
from contextforge.core.context.compaction import compact
from contextforge.core.context.prompts import load_prompt, prompt_version
from contextforge.core.context.tool_results import ProcessedRows, process_rows, raw_result_id
from contextforge.core.entities.model_call import Stage
from contextforge.core.entities.task import Fact as ResultFact
from contextforge.core.entities.task import (
    Task,
    TaskKind,
    TaskResult,
    TaskStatus,
    validate_structured_data,
)
from contextforge.core.ids import now_iso
from contextforge.core.logging import request_context
from contextforge.core.models.base import (
    ModelClient,
    ModelRequest,
    ModelResponse,
    ToolCall,
    ToolSpec,
)
from contextforge.core.models.errors import ModelError
from contextforge.core.repositories.base import StaleStatusError
from contextforge.core.state import Conclusion, Fact, RequestState, TaskState
from contextforge.core.tools.base import Tool, ToolContext, ToolResult
from contextforge.core.values import LocatorKind
from contextforge.worker.dependencies import WorkerDependencies
from contextforge.worker.request_state import load_request
from contextforge.worker.stages.errors import one_line
from contextforge.worker.stages.guards import check_not_cancelled
from contextforge.worker.stages.sources import available_artifacts, source_line
from contextforge.worker.stages.stubs import reconcile_stub, skipped_reason
from contextforge.worker.tools import TOOL_NAMES_BY_KIND

logger = logging.getLogger(__name__)

ONE_CALL_REMAINS = "[runtime] one tool call remains; return your structured result after it"
NO_CALLS_REMAIN = (
    "[runtime] no tool calls remain; return your structured result now with the facts you "
    "have, and say in uncertainties what you could not read"
)
NO_PROGRESS_LEFT = (
    "[runtime] {turns} turns in a row ran no new tool call; return your structured result now "
    "with the facts you have"
)
TEXT_IS_NOT_A_RESULT = (
    "[runtime] a text reply is not a result; call return_result with the task result now"
)
NO_FACTS = (
    "[runtime] your result has no facts, but your tools saved evidence in this task; return "
    "it again with facts that cite it, or say in uncertainties why none could be supported"
)
TOOL_BUDGET_EXHAUSTED = "tool call budget exhausted"
NO_PROGRESS = "no progress"
DEPENDENCY_FAILED = "dependency failed"
ALREADY_DONE = "[already done] "

_FINISHED = frozenset({TaskStatus.COMPLETED, TaskStatus.SKIPPED, TaskStatus.FAILED})
# The answer to a call made in a turn that ran past the cap.
_NOT_EXECUTED = ToolResult(status="not_executed", error=TOOL_BUDGET_EXHAUSTED)


class TaskOutcome(TypedDict):
    """What ``execute_task`` returns: plain JSON values, so the workflow can checkpoint it."""

    task_id: str
    # The part of the request the task was to answer; the report names it when unanswered.
    goal: str
    status: str  # a TaskStatus value
    summary: str | None
    facts: list[Fact]
    # Absent from an outcome checkpointed before conclusions existed (F10).
    conclusions: NotRequired[list[Conclusion]]
    uncertainties: list[str]
    # Why the task was SKIPPED or FAILED; the report lists it under uncertainties.
    failure_reason: str | None
    coverage: NotRequired[dict[str, Any]]


@dataclass(frozen=True)
class TaskContext:
    """What one subgraph run reads and never changes (its runtime context)."""

    task: Task
    request_state: RequestState
    sources: list[SourceLine]
    # Artifacts the facts of the task's dependencies cite: a comparison task may cite the
    # evidence it compares (cite still checks every excerpt against its snapshot).
    dependency_evidence: list[str] = field(default_factory=list)


@dataclass
class _Turn:
    """What one ``run_tools`` turn advances call by call; written back to state at its end."""

    made: int
    seen: dict[str, str]
    messages: list[ToolMessage] = field(default_factory=list)
    artifact_ids: list[str] = field(default_factory=list)


def execute_task(
    request_state: RequestState, task_id: str, worker_dependencies: WorkerDependencies
) -> TaskOutcome:
    """Run one PLANNED Task to COMPLETED, SKIPPED or FAILED, holding a
    ``worker_dependencies.semaphore`` slot.

    A re-run that finds the Task already finished (a crash between its write and the checkpoint)
    returns the stored outcome. One that finds it RUNNING runs it again as its next attempt: the
    runner's conditional claim lets one run hold a request, so that attempt died in a crash.
    ``Cancelled`` propagates and leaves the Task RUNNING.
    """
    request_message_id = request_state["request_message_id"]
    with (
        request_context(request_message_id, task_id),
        worker_dependencies.semaphore,
        worker_dependencies.tracer.task(request_state, task_id),
    ):
        try:
            task = worker_dependencies.repositories.tasks.update_status(
                request_message_id, task_id, TaskStatus.PLANNED, TaskStatus.RUNNING
            )
        except StaleStatusError as exc:
            if exc.actual in _FINISHED:
                logger.info("task already %s; returning its stored outcome", exc.actual)
                return _build_task_outcome(
                    _get_task(worker_dependencies, request_message_id, task_id)
                )
            if exc.actual != TaskStatus.RUNNING:
                raise
            task = _next_attempt(
                _get_task(worker_dependencies, request_message_id, task_id), worker_dependencies
            )
        logger.info("%s task started, attempt %d", task.kind, task.attempt)
        return _build_task_outcome(_execute_task_by_kind(task, request_state, worker_dependencies))


def build_task_graph(
    worker_dependencies: WorkerDependencies,
) -> CompiledStateGraph[TaskState, TaskContext, TaskState, TaskState]:
    """One task's loop: ``call_model``, then ``run_tools`` for as long as the model calls tools."""
    graph = StateGraph(TaskState, context_schema=TaskContext)
    graph.add_node("call_model", partial(call_model, worker_dependencies=worker_dependencies))
    graph.add_node("run_tools", partial(run_tools, worker_dependencies=worker_dependencies))
    graph.add_edge(START, "call_model")
    graph.add_conditional_edges("call_model", _next_step_after_model, ["run_tools", END])
    graph.add_edge("run_tools", "call_model")
    # False, not None: None would inherit the workflow's checkpointer and save the transcript.
    return graph.compile(checkpointer=False)


def call_model(
    task_state: TaskState, runtime: Runtime[TaskContext], *, worker_dependencies: WorkerDependencies
) -> dict[str, Any]:
    """One execute call: appends the model's tool calls, or sets ``result``, or sets ``error``.

    The loop messages are compacted first when they overrun their budget, and a compacted
    transcript replaces the one in state. ``Cancelled`` propagates; a failed call (the compress
    call included), which an exhausted budget or a passed deadline is, ends the run as
    ``error``.
    """
    check_not_cancelled(
        worker_dependencies, task_state["chat_id"], task_state["request_message_id"]
    )
    task_context = runtime.context
    limits = _task_budgets(task_context.task.kind, worker_dependencies)
    stop = _stop_reason(task_state, limits)
    if task_context.task.kind == TaskKind.WEB_SEARCH and _web_budget_spent(
        task_state, worker_dependencies
    ):
        stop = "request web-tool budget exhausted"
    try:
        model = worker_dependencies.model_for(
            task_state["request_message_id"],
            memory_version=task_context.request_state["memory"]["version"],
        )
        # F7 ruling 1: the call that ends an attempt offers no tools, so return_result is the
        # only tool left and the adapter forces it; the task returns what it has gathered.
        tools = {} if stop else _allowed_tools(task_context.task.kind, worker_dependencies)
        tool_definitions = [tool.spec for tool in tools.values()]
        model_context = build_context(
            _execute_inputs(task_state, task_context, tool_definitions, worker_dependencies),
            worker_dependencies.budgets,
        )
        messages = _compacted_messages(task_state, model_context, model, worker_dependencies)
        response = model.invoke(
            _build_model_request(
                task_state, model_context, messages, tool_definitions, worker_dependencies
            )
        )
    except ModelError as exc:
        logger.warning("execute call failed: %s", exc)
        return {"error": str(exc)}
    update = _handle_response(response, task_state, task_context, limits, stop)
    return _with_transcript(update, before=task_state["messages"], after=messages)


def run_tools(
    task_state: TaskState, runtime: Runtime[TaskContext], *, worker_dependencies: WorkerDependencies
) -> dict[str, Any]:
    """Answer every tool call of the last turn with a ToolMessage, failures included.

    A repeated call is answered from its earlier result and spends no slot; a call past the cap
    is not run. A turn that ran nothing new is a no-progress turn. A nudge follows the results
    as the cap nears, and when ``budgets.no_progress_turns`` of them in a row are reached; the
    call after the last slot or that limit may only return the result (``_stop_reason``).
    """
    turn = _Turn(made=task_state["tool_calls_made"], seen=dict(task_state["seen_calls"]))
    limits = _task_budgets(runtime.context.task.kind, worker_dependencies)
    for call in _requested_calls(task_state):
        turn.messages.append(
            _execute_tool_call(call, turn, task_state, runtime.context.task, worker_dependencies)
        )
    progressed = turn.made > task_state["tool_calls_made"]
    no_progress = 0 if progressed else task_state["no_progress_turns"] + 1
    if no_progress == worker_dependencies.budgets.no_progress_turns:
        logger.warning("%d turns in a row ran no new tool call", no_progress)
    return {
        "messages": [
            *turn.messages,
            *_nudges(task_state["tool_calls_made"], turn.made, no_progress, limits),
        ],
        "evidence_artifact_ids": list(
            dict.fromkeys([*task_state.get("evidence_artifact_ids", []), *turn.artifact_ids])
        ),
        "tool_calls_made": turn.made,
        "seen_calls": turn.seen,
        "no_progress_turns": no_progress,
    }


def _execute_task_by_kind(
    task: Task, request_state: RequestState, worker_dependencies: WorkerDependencies
) -> Task:
    """The Task as stored after its final transition: FAILED when it has dependencies and none
    completed, whatever the kind (I-2); otherwise what that is depends on its kind.

    F7 ruling 2: a task with at least one completed dependency runs, told which of the others
    did not complete and why, so it states the gap instead of the whole answer being lost.
    """
    results, gaps = _load_dependency_results(task, worker_dependencies)
    if gaps and not results:
        return _finish_task(
            worker_dependencies, task, TaskStatus.FAILED, failure_reason=DEPENDENCY_FAILED
        )
    if task.kind in (TaskKind.SQL_ANALYSIS, TaskKind.WEB_SEARCH):
        return _run_model_tool_loop(task, request_state, worker_dependencies, results, gaps)
    if task.kind == TaskKind.RECONCILE:
        if any(
            ref.locator.kind == LocatorKind.URL
            for result in results
            for fact in result.facts
            for ref in fact.evidence_refs
        ):
            # Public-web comparisons synthesize their dependencies through the same
            # validated model loop, with no tools granted to the reconcile task.
            return _run_model_tool_loop(task, request_state, worker_dependencies, results, gaps)
        result = reconcile_stub(task, results, gaps)
        return _finish_task(worker_dependencies, task, TaskStatus.COMPLETED, result=result)
    return _finish_task(
        worker_dependencies, task, TaskStatus.SKIPPED, failure_reason=skipped_reason(task.kind)
    )


def _run_model_tool_loop(
    task: Task,
    request_state: RequestState,
    worker_dependencies: WorkerDependencies,
    dependencies: list[TaskResult],
    gaps: list[str],
) -> Task:
    """Run the subgraph, again from a fresh state after an error; COMPLETED or FAILED.

    The completed dependencies' summaries and the ``gaps`` left by the others form its
    ``<dependencies>`` block.
    """
    subgraph = build_task_graph(worker_dependencies)
    task_context = TaskContext(
        task=task,
        request_state=request_state,
        sources=_task_sources(task, request_state["chat_id"], worker_dependencies),
        dependency_evidence=[
            ref.artifact_id
            for result in dependencies
            for f in result.facts
            for ref in f.evidence_refs
        ],
    )
    dependency_summaries = [*(result.summary for result in dependencies), *gaps]
    config: RunnableConfig = {
        "recursion_limit": _recursion_limit(_task_budgets(task.kind, worker_dependencies))
    }
    error = ""
    attempts = worker_dependencies.budgets.task_retries + 1
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            # ModelCall has no metadata field, so the retry is on record as activity (L-3).
            retry = f"Task retry {attempt - 1} of {attempts - 1}"
            worker_dependencies.activity.emit(task.request_message_id, task.task_id, retry)
        task_state = _create_initial_task_state(task, request_state, dependency_summaries)
        final = subgraph.invoke(task_state, config, context=task_context)
        if final["result"] is not None:
            result = TaskResult.model_validate(final["result"])
            return _finish_task(worker_dependencies, task, TaskStatus.COMPLETED, result=result)
        error = final["error"] or "the task ended without a result"
        logger.warning("attempt %d ended: %s", attempt, error)
    return _finish_task(worker_dependencies, task, TaskStatus.FAILED, failure_reason=error)


def _finish_task(
    worker_dependencies: WorkerDependencies,
    task: Task,
    status: TaskStatus,
    *,
    result: TaskResult | None = None,
    failure_reason: str | None = None,
) -> Task:
    """RUNNING -> ``status``: the Task's final transition."""
    logger.info("task %s %s", status.value, failure_reason or "")
    return worker_dependencies.repositories.tasks.update_status(
        task.request_message_id,
        task.task_id,
        TaskStatus.RUNNING,
        status,
        result=result,
        failure_reason=failure_reason,
    )


def _next_attempt(task: Task, worker_dependencies: WorkerDependencies) -> Task:
    """The RUNNING ``task`` saved with ``attempt`` counted up, so the retry is on record."""
    logger.warning("task found RUNNING after attempt %d died; running it again", task.attempt)
    retried = task.model_copy(update={"attempt": task.attempt + 1, "updated_at": now_iso()})
    worker_dependencies.repositories.tasks.save(retried)
    return retried


def _get_task(
    worker_dependencies: WorkerDependencies, request_message_id: str, task_id: str
) -> Task:
    task = worker_dependencies.repositories.tasks.get(request_message_id, task_id)
    if task is None:
        raise LookupError(f"no task {task_id} in request {request_message_id}")
    return task


def _load_dependency_results(
    task: Task, worker_dependencies: WorkerDependencies
) -> tuple[list[TaskResult], list[str]]:
    """The results of the task's completed dependencies, and a gap line for each one that did
    not complete (FAILED, SKIPPED or CANCELLED), both in ``depends_on`` order."""
    results: list[TaskResult] = []
    gaps: list[str] = []
    for task_id in task.depends_on_task_ids:
        dependency = _get_task(worker_dependencies, task.request_message_id, task_id)
        if dependency.status == TaskStatus.COMPLETED and dependency.result is not None:
            results.append(dependency.result)
        else:
            gaps.append(_gap(dependency))
    return results, gaps


def _gap(dependency: Task) -> str:
    """What a dependency that did not complete leaves out, and why, for the dependent's request
    state.
    Named by its goal, not its id: the dependent states the gap in words the user reads."""
    reason = dependency.failure_reason or dependency.status.value.lower()
    return f"A dependency did not complete ({reason}); not covered: {dependency.goal}"


def _task_sources(
    task: Task, chat_id: str, worker_dependencies: WorkerDependencies
) -> list[SourceLine]:
    """The ``<sources>`` lines of the task's input Artifacts, with each dataset's columns."""
    return [
        source_line(artifact, worker_dependencies.blobs)
        for artifact in available_artifacts(worker_dependencies.repositories.artifacts, chat_id)
        if artifact.artifact_id in task.input_artifact_ids
    ]


def _create_initial_task_state(
    task: Task, request_state: RequestState, dependency_summaries: list[str]
) -> TaskState:
    return TaskState(
        task_id=task.task_id,
        request_message_id=task.request_message_id,
        chat_id=request_state["chat_id"],
        user_id=request_state["user_id"],
        constraints=list(request_state["constraints"]),
        dependency_summaries=dependency_summaries,
        messages=[],
        result=None,
        error=None,
        tool_calls_made=0,
        seen_calls={},
        no_progress_turns=0,
    )


def _recursion_limit(budgets: Budgets) -> int:
    """Graph steps one run may take, two per turn.

    Until the attempt must stop, every turn runs a new call (at most the cap of them) or is one
    of at most ``no_progress_turns`` turns in a row that ran none. After that come at most two
    calls: the one that may only return the result, and one more after the no-facts reminder.
    """
    turns = budgets.tool_calls_per_task * budgets.no_progress_turns + 2
    return 2 * turns + 1


def _build_task_outcome(task: Task) -> TaskOutcome:
    """The outcome of a finished Task, read from its stored record."""
    result = task.result
    return TaskOutcome(
        task_id=task.task_id,
        goal=task.goal,
        status=task.status.value,
        summary=result.summary if result else None,
        facts=[_brief_fact(fact) for fact in result.facts] if result else [],
        conclusions=_brief_conclusions(result) if result else [],
        uncertainties=list(result.uncertainties) if result else [],
        failure_reason=task.failure_reason,
        coverage=result.coverage if result else {},
    )


def _brief_fact(fact: ResultFact) -> Fact:
    refs = [ref.model_dump(mode="json", exclude_none=True) for ref in fact.evidence_refs]
    return Fact(text=fact.text, evidence_refs=refs)


def _brief_conclusions(result: TaskResult) -> list[Conclusion]:
    """The result's conclusions, each naming the facts it rests on by their text: the report
    finds the claim a fact became by its text, as cite merges facts."""
    facts = result.facts
    return [
        Conclusion(
            text=conclusion.text,
            based_on=[facts[i].text for i in conclusion.supporting_facts if i < len(facts)],
            confidence=conclusion.confidence,
        )
        for conclusion in result.conclusions
    ]


def _next_step_after_model(task_state: TaskState) -> str:
    """To ``run_tools`` when the model asked for tools; END once a result or an error is set."""
    finished = task_state["result"] is not None or task_state["error"] is not None
    return END if finished else "run_tools"


def _compacted_messages(
    task_state: TaskState,
    model_context: BuiltContext,
    model: ModelClient,
    worker_dependencies: WorkerDependencies,
) -> list[AnyMessage]:
    """Section 8 fitted into the window that sections 1 to 7 leave (spec 8.6).

    Only the loop messages are passed, so request state fields cannot be lost to compaction. The
    summary call goes through the request's recording client: it is saved as a ``compress``
    ModelCall and counts against the call budget.
    """
    budgets = worker_dependencies.budgets
    return compact(
        task_state["messages"],
        budget_tokens=budgets.messages_budget(model_context.fixed_tokens),
        keep_last=budgets.keep_last_messages,
        model=model,
        memory=model_context.system[0],  # the builder's first system block is the memory file
        model_id=worker_dependencies.model_id(Stage.COMPRESS),
        task_id=task_state["task_id"],
    )


def _with_transcript(
    update: dict[str, Any], *, before: list[AnyMessage], after: list[AnyMessage]
) -> dict[str, Any]:
    """``update`` with the compacted transcript ``after`` written back when it differs.

    Clearing the transcript and adding ``after`` replaces it in state, so the next turn starts
    from the compacted one and a summary is paid for once, not on every later turn. Stubs keep
    their message ids and ``add_messages`` gives the new summary one.
    """
    if after == before:  # element identity first, then content
        return update
    added = update.get("messages", [])
    return {**update, "messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *after, *added]}


def _build_model_request(
    task_state: TaskState,
    model_context: BuiltContext,
    messages: list[AnyMessage],
    tool_definitions: list[ToolSpec],
    worker_dependencies: WorkerDependencies,
) -> ModelRequest:
    """The execute call: the task request state as the first user turn, then the (compacted)
    loop."""
    return ModelRequest(
        stage=Stage.EXECUTE,
        model=worker_dependencies.model_id(Stage.EXECUTE),
        system=model_context.system,
        messages=[HumanMessage(model_context.user_text), *messages],
        tools=tool_definitions,
        output_schema=TaskResult,
        task_id=task_state["task_id"],
    )


def _execute_inputs(
    task_state: TaskState,
    task_context: TaskContext,
    tool_definitions: list[ToolSpec],
    worker_dependencies: WorkerDependencies,
) -> ContextInputs:
    """Section 8.2 for an execute call: the request state, the task goal, its sources and its
    tools."""
    request_state = task_context.request_state
    return ContextInputs(
        memory=request_state["memory"]["text"],
        stage_prompt=load_prompt(prompt_version(Stage.EXECUTE)),
        chat_id=task_state["chat_id"],
        request=load_request(
            worker_dependencies.repositories.messages,
            request_state["chat_id"],
            request_state["request_message_id"],
        ),
        preferences=request_state["memory"]["preferences"],
        constraints=[*task_state["constraints"], f"Task goal: {task_context.task.goal}"],
        facts=request_state["facts"],
        open_questions=request_state["open_questions"],
        next_action=request_state["next_action"],
        dependency_summaries=task_state["dependency_summaries"],
        evidence_slices=[],
        tools=tool_definitions,
        sources=task_context.sources,
        prior_messages=[],
        loop_messages=task_state["messages"],
    )


def _handle_response(
    response: ModelResponse,
    task_state: TaskState,
    task_context: TaskContext,
    budgets: Budgets,
    stop: str | None,
) -> dict[str, Any]:
    """The state update for one response: its tool calls, its parsed result, a reminder after a
    prose reply, or an error.

    ``stop`` is why this call could only return the result (``_stop_reason``). Its result
    carries that reason as an uncertainty, and a response without one ends the attempt with it
    as the error: nothing usable even then.
    """
    if stop is not None and response.structured is None:
        logger.warning("no result from the call that ends the attempt (%s)", stop)
        return {"error": stop}
    if response.tool_calls and stop is None:
        return {"messages": [_ai_message(response)]}
    if response.structured is None:
        # Live runs 1 and 2: Nova answered in prose and the whole attempt was lost. The reply
        # and a reminder go into the transcript; the turn runs no tool call, so repeated prose
        # ends the run through the no-progress limit.
        logger.info("model replied in text, not a result; reminded to call return_result")
        reply = AIMessage(content=response.text or "(empty reply)")
        return {"messages": [reply, HumanMessage(TEXT_IS_NOT_A_RESULT)]}
    citable = [*task_state.get("evidence_artifact_ids", []), *task_context.dependency_evidence]
    try:
        result = _parse_result(response, task_context.task, citable)
    except ValueError as exc:  # ValidationError is a ValueError
        reason = one_line(exc) if isinstance(exc, ValidationError) else str(exc)
        logger.warning("invalid result: %s", reason)
        return {"error": f"invalid result: {reason}"}
    if not result.facts and task_state.get("evidence_artifact_ids") and not _reminded(task_state):
        # Live run 3: the US task read pages, summarised their figures and cited none. One
        # reminder; a second result without facts is accepted as it is.
        logger.info("result has no facts though tools saved evidence; reminded once")
        reply = AIMessage(content=response.text or "(result without facts)")
        return {"messages": [reply, HumanMessage(NO_FACTS)]}
    if stop is not None:
        note = _stop_note(stop, task_state, budgets)
        result = result.model_copy(update={"uncertainties": [*result.uncertainties, note]})
    return {"result": result.model_dump(mode="json")}


def _stop_reason(task_state: TaskState, budgets: Budgets) -> str | None:
    """Why the attempt's next call may only return its result: every tool call is spent, or
    ``budgets.no_progress_turns`` turns in a row ran none. None while it may still call tools.

    The handoff's "seven responses per attempt" is the cap of tool calls plus this last one.
    """
    if task_state["tool_calls_made"] >= budgets.tool_calls_per_task:
        return TOOL_BUDGET_EXHAUSTED
    if task_state["no_progress_turns"] >= budgets.no_progress_turns:
        return NO_PROGRESS
    return None


def _stop_note(stop: str, task_state: TaskState, budgets: Budgets) -> str:
    """The uncertainty a result returned at the end of its attempt carries: why it stopped and
    how much it read. A page is any distinct tool result that came back: a search's hits, a
    fetched page, a query's rows."""
    pages = len(task_state["seen_calls"])
    read = f"{pages} page{'' if pages == 1 else 's'} read"
    if stop == "request web-tool budget exhausted":
        return f"stopped at the shared request web-tool budget; {read}"
    if stop == TOOL_BUDGET_EXHAUSTED:
        return f"stopped at the tool-call budget; {read}"
    return f"stopped after {budgets.no_progress_turns} turns without progress; {read}"


def _reminded(task_state: TaskState) -> bool:
    """Whether this attempt already got the no-facts reminder."""
    return any(
        isinstance(m, HumanMessage) and m.content == NO_FACTS for m in task_state["messages"]
    )


def _ai_message(response: ModelResponse) -> AIMessage:
    calls = [tool_call(name=c.name, args=c.arguments, id=c.id) for c in response.tool_calls]
    return AIMessage(content=response.text or "", tool_calls=calls)


def _parse_result(response: ModelResponse, task: Task, produced: list[str]) -> TaskResult:
    """The response's TaskResult, keeping only facts grounded in the task's own inputs or in
    ``produced``: what its tools saved, and the evidence its dependencies cite.

    Raises ``ValueError`` (``ValidationError`` included) when it breaks the result schema; the
    schema already caps the facts at six.
    """
    result = TaskResult.model_validate(response.structured)
    validate_structured_data(task.kind, result.structured_data)
    citable = [*task.input_artifact_ids, *produced]
    grounded = [i for i, fact in enumerate(result.facts) if _is_grounded(fact, citable)]
    if len(grounded) < len(result.facts):
        dropped = len(result.facts) - len(grounded)
        logger.info("dropped %d facts that cite no task input", dropped)
    return _keeping_facts(result, grounded)


def _keeping_facts(result: TaskResult, kept: list[int]) -> TaskResult:
    """Remap retained facts; withhold conclusions that lose any declared prerequisite."""
    position = {old: new for new, old in enumerate(kept)}
    conclusions = []
    gaps = list(result.uncertainties)
    for conclusion in result.conclusions:
        supporting = [position[i] for i in conclusion.supporting_facts if i in position]
        if len(supporting) != len(conclusion.supporting_facts):
            reason = (
                "required supporting findings did not all pass"
                if supporting
                else "no finding it rests on held"
            )
            gaps.append(f"unsupported conclusion, {reason}: {conclusion.text[:80]}")
            continue
        conclusions.append(conclusion.model_copy(update={"supporting_facts": supporting}))
    facts = [result.facts[i] for i in kept]
    return result.model_copy(
        update={"facts": facts, "conclusions": conclusions, "uncertainties": gaps}
    )


def _is_grounded(fact: ResultFact, input_artifact_ids: list[str]) -> bool:
    """True when the fact cites evidence and all of it lies in the task's inputs."""
    refs = fact.evidence_refs
    return bool(refs) and all(ref.artifact_id in input_artifact_ids for ref in refs)


def _requested_calls(task_state: TaskState) -> list[ToolCall]:
    """The tool calls of the turn's AIMessage, back in the model boundary's form."""
    last = task_state["messages"][-1] if task_state["messages"] else None
    if not isinstance(last, AIMessage):
        return []
    return [
        ToolCall.model_validate({"id": c["id"], "name": c["name"], "arguments": c["args"]})
        for c in last.tool_calls
    ]


def _execute_tool_call(
    call: ToolCall,
    turn: _Turn,
    task_state: TaskState,
    task: Task,
    worker_dependencies: WorkerDependencies,
) -> ToolMessage:
    """Answer one call: from an earlier identical ok call, as not run past the cap, or by
    running it. Only a run spends a slot. A failed run is never replayed, so the model can
    retry it."""
    key = _call_key(call.name, call.arguments)
    earlier = turn.seen.get(key)
    if earlier is not None:
        logger.info("tool %s repeated; answered from the earlier result", call.name)
        return _replay(call, key, earlier, [*task_state["messages"], *turn.messages])
    if turn.made >= _task_budgets(task.kind, worker_dependencies).tool_calls_per_task:
        return _result_message(call, _NOT_EXECUTED, task_state, worker_dependencies)
    turn.made += 1
    worker_dependencies.activity.emit(
        task_state["request_message_id"], task_state["task_id"], f"Calling {call.name}"
    )
    result = _invoke_tool(call, task_state, task.kind, worker_dependencies)
    worker_dependencies.activity.emit(
        task_state["request_message_id"], task_state["task_id"], f"{call.name}: {result.status}"
    )
    if result.status == "ok":
        turn.artifact_ids.extend(result.artifact_ids)
    logger.info("tool %s returned %s", call.name, result.status)
    message = _result_message(call, result, task_state, worker_dependencies)
    if result.status == "ok":
        turn.seen[key] = str(message.content)
    return message


def _call_key(name: str, arguments: dict[str, Any]) -> str:
    """The tool name with its canonical JSON arguments: equal keys mean the same call."""
    return f"{name}:{json.dumps(arguments, sort_keys=True, separators=(',', ':'))}"


def _replay(
    call: ToolCall, key: str, earlier_content: str, transcript: Sequence[BaseMessage]
) -> ToolMessage:
    """A repeated call answered from its earlier ok result. The rows and artifact are that
    result's while the transcript still holds it; compaction may have summarized it away."""
    earlier = next((m.additional_kwargs for m in reversed(transcript) if _answers(m, key)), {})
    return _tool_message(
        call,
        ALREADY_DONE + earlier_content,
        status="ok",
        row_count=earlier.get("row_count", 0),
        artifact_id=earlier.get("artifact_id"),
    )


def _answers(message: BaseMessage, key: str) -> bool:
    """True when ``message`` is an ok result of the call ``key``."""
    if not isinstance(message, ToolMessage):
        return False
    meta = message.additional_kwargs
    called = _call_key(meta.get("tool_name", ""), meta.get("arguments", {}))
    return meta.get("status") == "ok" and called == key


def _invoke_tool(
    call: ToolCall, task_state: TaskState, kind: TaskKind, worker_dependencies: WorkerDependencies
) -> ToolResult:
    """The tool's result; an unknown tool and a tool that raises become error results."""
    tools = _allowed_tools(kind, worker_dependencies)
    tool = tools.get(call.name)
    if tool is None:
        error = f"unknown tool {call.name}; this task may call: {', '.join(tools) or 'none'}"
        return ToolResult(status="error", error=error, error_class="code")
    context = ToolContext(
        chat_id=task_state["chat_id"],
        user_id=task_state["user_id"],
        request_message_id=task_state["request_message_id"],
        task_id=task_state["task_id"],
        tool_call_id=call.id,
    )
    try:
        if call.name in {"web_search", "fetch_url"}:
            count = worker_dependencies.repositories.messages.reserve_web_tool_call(
                task_state["chat_id"],
                task_state["request_message_id"],
                worker_dependencies.budgets.web_tool_call_budget,
            )
            if count is None:
                return ToolResult(
                    status="not_executed",
                    error="Request web-tool budget exhausted; return the evidence gathered so far.",
                )
        return tool.run(call.arguments, context)
    except Exception as exc:  # a failing tool is a result the model sees, not a crashed task
        logger.warning("tool %s raised: %s", call.name, exc)
        return ToolResult(status="error", error=str(exc), error_class="system", retryable=True)


def _web_budget_spent(task_state: TaskState, worker_dependencies: WorkerDependencies) -> bool:
    request = load_request(
        worker_dependencies.repositories.messages,
        task_state["chat_id"],
        task_state["request_message_id"],
    )
    return request.web_tool_calls >= worker_dependencies.budgets.web_tool_call_budget


def _task_budgets(kind: TaskKind, worker_dependencies: WorkerDependencies) -> Budgets:
    if kind == TaskKind.WEB_SEARCH and worker_dependencies.settings is not None:
        return replace(
            worker_dependencies.budgets,
            tool_calls_per_task=worker_dependencies.settings.web_tool_calls_per_task,
        )
    return worker_dependencies.budgets


def _allowed_tools(kind: TaskKind, worker_dependencies: WorkerDependencies) -> dict[str, Tool]:
    return {
        name: worker_dependencies.tools[name]
        for name in TOOL_NAMES_BY_KIND.get(kind, ())
        if name in worker_dependencies.tools
    }


def _result_message(
    call: ToolCall,
    result: ToolResult,
    task_state: TaskState,
    worker_dependencies: WorkerDependencies,
) -> ToolMessage:
    """Rows as the shaping stub plus the kept rows as JSON; a failure as its JSON envelope."""
    if result.status != "ok":
        return _tool_message(call, _error_content(result), status=result.status)
    if result.rows is None:
        return _tool_message(call, result.text or "", status="ok", artifact_id=_saved(result))
    shaped = _shape_rows(call, result.rows, task_state, worker_dependencies)
    content = f"{shaped.text}\n{json.dumps(shaped.rows, sort_keys=True)}"
    return _tool_message(
        call,
        content,
        status="ok",
        row_count=shaped.total_rows,
        artifact_id=shaped.artifact_id or _saved(result),
    )


def _saved(result: ToolResult) -> str | None:
    """The Artifact a tool saved its own result in (the web tools' snapshots), so compaction
    keeps that handle; None for a tool that saves nothing."""
    return result.artifact_ids[0] if result.artifact_ids else None


def _shape_rows(
    call: ToolCall,
    rows: list[dict[str, Any]],
    task_state: TaskState,
    worker_dependencies: WorkerDependencies,
) -> ProcessedRows:
    """Cap and shape ``rows``; all of them go to a raw_result Artifact when any are cut."""
    budgets = worker_dependencies.budgets
    return process_rows(
        rows,
        cap=budgets.tool_result_row_cap,
        artifacts=worker_dependencies.repositories.artifacts,
        blobs=worker_dependencies.blobs,
        chat_id=task_state["chat_id"],
        user_id=task_state["user_id"],
        name=f"{call.name} result {call.id}",
        artifact_id=raw_result_id(task_state["task_id"], call.id),
        column_cap=budgets.column_cap,
        cell_char_cap=budgets.cell_char_cap,
    )


def _error_content(result: ToolResult) -> str:
    return json.dumps(
        {
            "status": result.status,
            "error": result.error,
            "error_class": result.error_class,
            "retryable": result.retryable,
        }
    )


def _tool_message(
    call: ToolCall,
    content: str,
    *,
    status: str,
    row_count: int = 0,
    artifact_id: str | None = None,
) -> ToolMessage:
    """A ToolMessage with the envelope compaction stubs read: tool, arguments, rows, artifact,
    status. ``row_count`` counts the rows behind the content: 0 for a failure."""
    return ToolMessage(
        content=content,
        tool_call_id=call.id,
        additional_kwargs={
            "tool_name": call.name,
            "arguments": call.arguments,
            "row_count": row_count,
            "artifact_id": artifact_id,
            "status": status,
        },
    )


def _nudges(
    made_before: int, made_after: int, no_progress: int, budgets: Budgets
) -> list[HumanMessage]:
    """The runtime's reminder after a turn: after one that spent slots, one slot left or none
    left; after one that reached the no-progress limit with slots left, return the result now.
    """
    cap = budgets.tool_calls_per_task
    if made_after == made_before:
        if no_progress == budgets.no_progress_turns and made_after < cap:
            return [HumanMessage(NO_PROGRESS_LEFT.format(turns=no_progress))]
        return []
    if made_after >= cap:
        return [HumanMessage(NO_CALLS_REMAIN)]
    if made_after == cap - 1:
        return [HumanMessage(ONE_CALL_REMAINS)]
    return []

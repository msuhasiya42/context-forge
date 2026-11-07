"""Execute stage (spec 9.2): one Task run to its outcome; an sql task through its own subgraph.

The subgraph is the model/tool loop of one task over ``TaskState``. It is compiled without a
checkpointer, so its transcript is dropped when the task ends (spec 8.6 level 2): what outlives
it is the Task record and the outcome the workflow folds into the brief.
"""

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import Any, TypedDict

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
from contextforge.core.models.scripted import ScriptExhausted
from contextforge.core.repositories.base import StaleStatusError
from contextforge.core.state import Fact, RequestState, TaskState
from contextforge.core.tools.base import Tool, ToolContext, ToolResult
from contextforge.worker.deps import WorkerDeps
from contextforge.worker.stages.errors import one_line
from contextforge.worker.stages.guards import (
    CallBudgetExceeded,
    check_call_budget,
    check_not_cancelled,
)
from contextforge.worker.stages.sources import available_artifacts, source_line
from contextforge.worker.stages.stubs import reconcile_stub, skipped_reason
from contextforge.worker.tools import TOOL_NAMES_BY_KIND

logger = logging.getLogger(__name__)

ONE_CALL_REMAINS = "[runtime] one tool call remains; return your structured result after it"
NO_CALLS_REMAIN = "[runtime] no tool calls remain; return your structured result now"
TOOL_BUDGET_EXHAUSTED = "tool call budget exhausted"
NO_PROGRESS = "no progress"
DEPENDENCY_FAILED = "dependency failed"
ALREADY_DONE = "[already done] "

_FINISHED = frozenset({TaskStatus.COMPLETED, TaskStatus.SKIPPED, TaskStatus.FAILED})
# The answer to a call made in a turn that ran past the cap.
_NOT_EXECUTED = ToolResult(status="not_executed", error=TOOL_BUDGET_EXHAUSTED)


class TaskOutcome(TypedDict):
    """What ``execute_stage`` returns: plain JSON values, so the workflow can checkpoint it."""

    task_id: str
    status: str  # a TaskStatus value
    summary: str | None
    facts: list[Fact]
    uncertainties: list[str]
    # Why the task was SKIPPED or FAILED; the report lists it under uncertainties.
    failure_reason: str | None


@dataclass(frozen=True)
class TaskRun:
    """What one subgraph run reads and never changes (its runtime context)."""

    task: Task
    brief: RequestState
    sources: list[SourceLine]


@dataclass
class _Turn:
    """What one ``run_tools`` turn advances call by call; written back to state at its end."""

    made: int
    seen: dict[str, str]
    messages: list[ToolMessage] = field(default_factory=list)


def execute_stage(brief: RequestState, task_id: str, deps: WorkerDeps) -> TaskOutcome:
    """Run one PLANNED Task to COMPLETED, SKIPPED or FAILED, holding a ``deps.semaphore`` slot.

    A re-run that finds the Task already finished (a crash between its write and the checkpoint)
    returns the stored outcome. One that finds it RUNNING runs it again as its next attempt: the
    runner's conditional claim lets one run hold a request, so that attempt died in a crash.
    ``Cancelled`` propagates and leaves the Task RUNNING.
    """
    request_message_id = brief["request_message_id"]
    with request_context(request_message_id, task_id), deps.semaphore:
        try:
            task = deps.repos.tasks.update_status(
                request_message_id, task_id, TaskStatus.PLANNED, TaskStatus.RUNNING
            )
        except StaleStatusError as exc:
            if exc.actual in _FINISHED:
                logger.info("task already %s; returning its stored outcome", exc.actual)
                return _outcome(_get_task(deps, request_message_id, task_id))
            if exc.actual != TaskStatus.RUNNING:
                raise
            task = _next_attempt(_get_task(deps, request_message_id, task_id), deps)
        logger.info("%s task started, attempt %d", task.kind, task.attempt)
        return _outcome(_run_to_end(task, brief, deps))


def build_task_subgraph(
    deps: WorkerDeps,
) -> CompiledStateGraph[TaskState, TaskRun, TaskState, TaskState]:
    """One task's loop: ``call_model``, then ``run_tools`` for as long as the model calls tools."""
    graph = StateGraph(TaskState, context_schema=TaskRun)
    graph.add_node("call_model", partial(call_model, deps=deps))
    graph.add_node("run_tools", partial(run_tools, deps=deps))
    graph.add_edge(START, "call_model")
    graph.add_conditional_edges("call_model", _after_model, ["run_tools", END])
    graph.add_conditional_edges("run_tools", _after_tools, ["call_model", END])
    # False, not None: None would inherit the workflow's checkpointer and save the transcript.
    return graph.compile(checkpointer=False)


def call_model(state: TaskState, runtime: Runtime[TaskRun], *, deps: WorkerDeps) -> dict[str, Any]:
    """One execute call: appends the model's tool calls, or sets ``result``, or sets ``error``.

    The loop messages are compacted first when they overrun their budget, and a compacted
    transcript replaces the one in state. ``Cancelled`` propagates; an exhausted call budget or
    a failed call (the compress call included) ends the run as ``error``.
    """
    check_not_cancelled(deps, state["chat_id"], state["request_message_id"])
    run = runtime.context
    try:
        check_call_budget(deps, state["request_message_id"])
        model = deps.model_for(state["request_message_id"])
        specs = [tool.spec for tool in _allowed_tools(run.task.kind, deps).values()]
        built = build_context(_execute_inputs(state, run, specs), deps.budgets)
        messages = _compacted_messages(state["messages"], built, model, deps)
        response = model.invoke(_model_request(state, built, messages, specs, deps))
    except (CallBudgetExceeded, ScriptExhausted) as exc:
        logger.warning("execute call failed: %s", exc)
        return {"error": str(exc)}
    update = _handle_response(response, state, run.task, deps.budgets)
    return _with_transcript(update, before=state["messages"], after=messages)


def run_tools(state: TaskState, runtime: Runtime[TaskRun], *, deps: WorkerDeps) -> dict[str, Any]:
    """Answer every tool call of the last turn with a ToolMessage, failures included.

    A repeated call is answered from its earlier result and spends no slot; a call past the cap
    is not run. A nudge follows the results as the cap nears. A turn that ran nothing new is a
    no-progress turn, and ``budgets.no_progress_turns`` of them in a row end the run.
    """
    budgets = deps.budgets
    turn = _Turn(made=state["tool_calls_made"], seen=dict(state["seen_calls"]))
    for call in _requested_calls(state):
        turn.messages.append(_run_one_call(call, turn, state, runtime.context.task, deps))
    progressed = turn.made > state["tool_calls_made"]
    no_progress = 0 if progressed else state["no_progress_turns"] + 1
    update: dict[str, Any] = {
        "messages": [
            *turn.messages,
            *_nudges(state["tool_calls_made"], turn.made, budgets.tool_calls_per_task),
        ],
        "tool_calls_made": turn.made,
        "seen_calls": turn.seen,
        "no_progress_turns": no_progress,
    }
    if no_progress >= budgets.no_progress_turns:
        logger.warning("%d turns in a row ran no new tool call", no_progress)
        update["error"] = NO_PROGRESS
    return update


def _run_to_end(task: Task, brief: RequestState, deps: WorkerDeps) -> Task:
    """The Task as stored after its final transition: FAILED when a dependency did not
    complete, whatever the kind; otherwise what that is depends on its kind."""
    results = _dependency_results(task, deps)
    if results is None:
        return _settle(deps, task, TaskStatus.FAILED, failure_reason=DEPENDENCY_FAILED)
    if task.kind == TaskKind.SQL_ANALYSIS:
        return _run_sql_analysis(task, brief, deps, [result.summary for result in results])
    if task.kind == TaskKind.RECONCILE:
        return _settle(deps, task, TaskStatus.COMPLETED, result=reconcile_stub(task, results))
    return _settle(deps, task, TaskStatus.SKIPPED, failure_reason=skipped_reason(task.kind))


def _run_sql_analysis(
    task: Task, brief: RequestState, deps: WorkerDeps, dependency_summaries: list[str]
) -> Task:
    """Run the subgraph, again from a fresh state after an error; COMPLETED or FAILED."""
    subgraph = build_task_subgraph(deps)
    run = TaskRun(task=task, brief=brief, sources=_task_sources(task, brief["chat_id"], deps))
    config: RunnableConfig = {"recursion_limit": _recursion_limit(deps.budgets)}
    error = ""
    for attempt in range(1, deps.budgets.task_retries + 2):
        state = _fresh_state(task, brief, dependency_summaries)
        final = subgraph.invoke(state, config, context=run)
        if final["result"] is not None:
            result = TaskResult.model_validate(final["result"])
            return _settle(deps, task, TaskStatus.COMPLETED, result=result)
        error = final["error"] or "the task ended without a result"
        logger.warning("attempt %d ended: %s", attempt, error)
    return _settle(deps, task, TaskStatus.FAILED, failure_reason=error)


def _settle(
    deps: WorkerDeps,
    task: Task,
    status: TaskStatus,
    *,
    result: TaskResult | None = None,
    failure_reason: str | None = None,
) -> Task:
    """RUNNING -> ``status``: the Task's final transition."""
    logger.info("task %s %s", status.value, failure_reason or "")
    return deps.repos.tasks.update_status(
        task.request_message_id,
        task.task_id,
        TaskStatus.RUNNING,
        status,
        result=result,
        failure_reason=failure_reason,
    )


def _next_attempt(task: Task, deps: WorkerDeps) -> Task:
    """The RUNNING ``task`` saved with ``attempt`` counted up, so the retry is on record."""
    logger.warning("task found RUNNING after attempt %d died; running it again", task.attempt)
    retried = task.model_copy(update={"attempt": task.attempt + 1, "updated_at": now_iso()})
    deps.repos.tasks.save(retried)
    return retried


def _get_task(deps: WorkerDeps, request_message_id: str, task_id: str) -> Task:
    task = deps.repos.tasks.get(request_message_id, task_id)
    if task is None:
        raise LookupError(f"no task {task_id} in request {request_message_id}")
    return task


def _dependency_results(task: Task, deps: WorkerDeps) -> list[TaskResult] | None:
    """Each dependency's result in ``depends_on`` order; None when any did not complete
    (FAILED, SKIPPED or CANCELLED), since the task would otherwise run without its input."""
    dependencies = [_get_task(deps, task.request_message_id, d) for d in task.depends_on_task_ids]
    results = [
        d.result for d in dependencies if d.status == TaskStatus.COMPLETED and d.result is not None
    ]
    return results if len(results) == len(dependencies) else None


def _task_sources(task: Task, chat_id: str, deps: WorkerDeps) -> list[SourceLine]:
    """The ``<sources>`` lines of the task's input Artifacts, with each dataset's columns."""
    return [
        source_line(artifact, deps.blobs)
        for artifact in available_artifacts(deps.repos.artifacts, chat_id)
        if artifact.artifact_id in task.input_artifact_ids
    ]


def _fresh_state(task: Task, brief: RequestState, dependency_summaries: list[str]) -> TaskState:
    return TaskState(
        task_id=task.task_id,
        request_message_id=task.request_message_id,
        chat_id=brief["chat_id"],
        user_id=brief["user_id"],
        constraints=list(brief["constraints"]),
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

    Every turn runs a new call (at most the cap of them, plus the one that ends the run) or is
    one of fewer than ``no_progress_turns`` duplicate-only turns in a row.
    """
    turns = (budgets.tool_calls_per_task + 1) * budgets.no_progress_turns
    return 2 * turns + 1


def _outcome(task: Task) -> TaskOutcome:
    """The outcome of a finished Task, read from its stored record."""
    result = task.result
    return TaskOutcome(
        task_id=task.task_id,
        status=task.status.value,
        summary=result.summary if result else None,
        facts=[_brief_fact(fact) for fact in result.facts] if result else [],
        uncertainties=list(result.uncertainties) if result else [],
        failure_reason=task.failure_reason,
    )


def _brief_fact(fact: ResultFact) -> Fact:
    refs = [ref.model_dump(mode="json", exclude_none=True) for ref in fact.evidence_refs]
    return Fact(text=fact.text, evidence_refs=refs)


def _after_model(state: TaskState) -> str:
    """To ``run_tools`` when the model asked for tools; END once a result or an error is set."""
    finished = state["result"] is not None or state["error"] is not None
    return END if finished else "run_tools"


def _after_tools(state: TaskState) -> str:
    return END if state["error"] is not None else "call_model"


def _compacted_messages(
    messages: list[AnyMessage], built: BuiltContext, model: ModelClient, deps: WorkerDeps
) -> list[AnyMessage]:
    """Section 8 fitted into the window that sections 1 to 7 leave (spec 8.6).

    Only the loop messages are passed, so brief fields cannot be lost to compaction. The
    summary call goes through the request's recording client: it is saved as a ``compress``
    ModelCall and counts against the call budget.
    """
    budgets = deps.budgets
    return compact(
        messages,
        budget_tokens=budgets.messages_budget(built.fixed_tokens),
        keep_last=budgets.keep_last_messages,
        model=model,
        memory=built.system[0],  # the builder's first system block is the memory file
        # Execute and compress run on the same cheap model (implementation plan, model split).
        model_id=deps.executor_model,
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


def _model_request(
    state: TaskState,
    built: BuiltContext,
    messages: list[AnyMessage],
    specs: list[ToolSpec],
    deps: WorkerDeps,
) -> ModelRequest:
    """The execute call: the task brief as the first user turn, then the (compacted) loop."""
    return ModelRequest(
        stage=Stage.EXECUTE,
        model=deps.executor_model,
        system=built.system,
        messages=[HumanMessage(built.user_text), *messages],
        tools=specs,
        output_schema=TaskResult,
        task_id=state["task_id"],
    )


def _execute_inputs(state: TaskState, run: TaskRun, specs: list[ToolSpec]) -> ContextInputs:
    """Section 8.2 for an execute call: the brief, the task goal, its sources and its tools."""
    brief = run.brief
    return ContextInputs(
        memory=load_prompt("agent-memory"),
        stage_prompt=load_prompt(prompt_version(Stage.EXECUTE)),
        chat_id=state["chat_id"],
        request=None,
        preferences={},
        constraints=[*state["constraints"], f"Task goal: {run.task.goal}"],
        facts=brief["facts"],
        open_questions=brief["open_questions"],
        next_action=brief["next_action"],
        dependency_summaries=state["dependency_summaries"],
        evidence_slices=[],
        tools=specs,
        sources=run.sources,
        prior_messages=[],
        loop_messages=state["messages"],
    )


def _handle_response(
    response: ModelResponse, state: TaskState, task: Task, budgets: Budgets
) -> dict[str, Any]:
    """The state update for one response: its tool calls, its parsed result, or an error."""
    if response.tool_calls:
        if state["tool_calls_made"] >= budgets.tool_calls_per_task:
            logger.warning("model called a tool after the last slot was spent")
            return {"error": TOOL_BUDGET_EXHAUSTED}
        return {"messages": [_ai_message(response)]}
    try:
        return {"result": _parse_result(response, task).model_dump(mode="json")}
    except ValueError as exc:  # ValidationError is a ValueError
        reason = one_line(exc) if isinstance(exc, ValidationError) else str(exc)
        logger.warning("invalid result: %s", reason)
        return {"error": f"invalid result: {reason}"}


def _ai_message(response: ModelResponse) -> AIMessage:
    calls = [tool_call(name=c.name, args=c.arguments, id=c.id) for c in response.tool_calls]
    return AIMessage(content=response.text or "", tool_calls=calls)


def _parse_result(response: ModelResponse, task: Task) -> TaskResult:
    """The response's TaskResult, keeping only facts grounded in the task's own inputs.

    Raises ``ValueError`` (``ValidationError`` included) when it breaks the result schema; the
    schema already caps the facts at six.
    """
    result = TaskResult.model_validate(response.structured)
    validate_structured_data(task.kind, result.structured_data)
    grounded = [f for f in result.facts if _is_grounded(f, task.input_artifact_ids)]
    if len(grounded) < len(result.facts):
        dropped = len(result.facts) - len(grounded)
        logger.info("dropped %d facts that cite no task input", dropped)
    return result.model_copy(update={"facts": grounded})


def _is_grounded(fact: ResultFact, input_artifact_ids: list[str]) -> bool:
    """True when the fact cites evidence and all of it lies in the task's inputs."""
    refs = fact.evidence_refs
    return bool(refs) and all(ref.artifact_id in input_artifact_ids for ref in refs)


def _requested_calls(state: TaskState) -> list[ToolCall]:
    """The tool calls of the turn's AIMessage, back in the model boundary's form."""
    last = state["messages"][-1] if state["messages"] else None
    if not isinstance(last, AIMessage):
        return []
    return [
        ToolCall.model_validate({"id": c["id"], "name": c["name"], "arguments": c["args"]})
        for c in last.tool_calls
    ]


def _run_one_call(
    call: ToolCall, turn: _Turn, state: TaskState, task: Task, deps: WorkerDeps
) -> ToolMessage:
    """Answer one call: from an earlier identical ok call, as not run past the cap, or by
    running it. Only a run spends a slot. A failed run is never replayed, so the model can
    retry it."""
    key = _call_key(call.name, call.arguments)
    earlier = turn.seen.get(key)
    if earlier is not None:
        logger.info("tool %s repeated; answered from the earlier result", call.name)
        return _replay(call, key, earlier, [*state["messages"], *turn.messages])
    if turn.made >= deps.budgets.tool_calls_per_task:
        return _result_message(call, _NOT_EXECUTED, state, deps)
    turn.made += 1
    result = _invoke_tool(call, state, task.kind, deps)
    logger.info("tool %s returned %s", call.name, result.status)
    message = _result_message(call, result, state, deps)
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


def _invoke_tool(call: ToolCall, state: TaskState, kind: TaskKind, deps: WorkerDeps) -> ToolResult:
    """The tool's result; an unknown tool and a tool that raises become error results."""
    tools = _allowed_tools(kind, deps)
    tool = tools.get(call.name)
    if tool is None:
        error = f"unknown tool {call.name}; this task may call: {', '.join(tools) or 'none'}"
        return ToolResult(status="error", error=error, error_class="code")
    context = ToolContext(
        chat_id=state["chat_id"],
        user_id=state["user_id"],
        request_message_id=state["request_message_id"],
        task_id=state["task_id"],
        tool_call_id=call.id,
    )
    try:
        return tool.run(call.arguments, context)
    except Exception as exc:  # a failing tool is a result the model sees, not a crashed task
        logger.warning("tool %s raised: %s", call.name, exc)
        return ToolResult(status="error", error=str(exc), error_class="system", retryable=True)


def _allowed_tools(kind: TaskKind, deps: WorkerDeps) -> dict[str, Tool]:
    return {name: deps.tools[name] for name in TOOL_NAMES_BY_KIND.get(kind, ())}


def _result_message(
    call: ToolCall, result: ToolResult, state: TaskState, deps: WorkerDeps
) -> ToolMessage:
    """Rows as the shaping stub plus the kept rows as JSON; a failure as its JSON envelope."""
    if result.status != "ok":
        return _tool_message(call, _error_content(result), status=result.status)
    if result.rows is None:
        return _tool_message(call, result.text or "", status="ok")
    shaped = _shape_rows(call, result.rows, state, deps)
    content = f"{shaped.text}\n{json.dumps(shaped.rows, sort_keys=True)}"
    return _tool_message(
        call, content, status="ok", row_count=shaped.total_rows, artifact_id=shaped.artifact_id
    )


def _shape_rows(
    call: ToolCall, rows: list[dict[str, Any]], state: TaskState, deps: WorkerDeps
) -> ProcessedRows:
    """Cap and shape ``rows``; all of them go to a raw_result Artifact when any are cut."""
    budgets = deps.budgets
    return process_rows(
        rows,
        cap=budgets.tool_result_row_cap,
        artifacts=deps.repos.artifacts,
        blobs=deps.blobs,
        chat_id=state["chat_id"],
        user_id=state["user_id"],
        name=f"{call.name} result {call.id}",
        artifact_id=raw_result_id(state["task_id"], call.id),
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


def _nudges(made_before: int, made_after: int, cap: int) -> list[HumanMessage]:
    """The runtime's reminder after a turn that spent slots: one slot left, or none left."""
    if made_after == made_before:
        return []
    if made_after >= cap:
        return [HumanMessage(NO_CALLS_REMAIN)]
    if made_after == cap - 1:
        return [HumanMessage(ONE_CALL_REMAINS)]
    return []

"""The Deep Dive trace of one request, built from stored records only: its planner calls and
clarifications, its tasks with their model calls and the tool calls derived from transcripts,
its report, and every step as one ordered list of events. Pure: the caller does the reads."""

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from contextforge.core.entities import wire
from contextforge.core.entities.message import (
    ClarificationMessage,
    ClarificationResponseMessage,
    Message,
    ReportMessage,
    RequestMessage,
)
from contextforge.core.entities.model_call import ModelCall, Stage
from contextforge.core.entities.task import Task, TaskStatus
from contextforge.core.models.transcript import Transcript, content_text, tool_results, tool_uses

# Enough of a tool result to recognise it; the whole result is in the next call's transcript.
RESULT_SUMMARY_CHARS = 300
# A tool call no later call of its task answered: the task ended, or is still running.
NO_RESULT = "no_result"
_ENDED = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.SKIPPED, TaskStatus.CANCELLED}


@dataclass(frozen=True)
class TraceRecords:
    """What the trace is built from. ``calls`` are ordered by ``started_at`` then id;
    ``entries`` are the request's Messages; ``transcripts`` are by model call id, and the
    calls without one (not recorded, or no longer stored) are simply absent."""

    request: RequestMessage
    usage: dict[str, Any]
    tasks: list[Task]
    calls: list[ModelCall]
    entries: list[Message]
    transcripts: Mapping[str, Transcript]


def build_trace(records: TraceRecords) -> dict[str, Any]:
    """The trace payload of ``GET /chats/{chatId}/requests/{messageId}/trace``."""
    by_task: dict[str | None, list[ModelCall]] = defaultdict(list)
    for call in records.calls:
        by_task[call.task_id].append(call)
    tool_calls = {
        model_call_id: views
        for task_calls in by_task.values()
        for model_call_id, views in derived_tool_calls(task_calls, records.transcripts).items()
    }

    def summaries(calls: list[ModelCall]) -> list[dict[str, Any]]:
        return [call_summary(c, tool_calls.get(c.model_call_id, [])) for c in calls]

    # Outside a task the model is called by the planner and, once, for the report's summary.
    report_calls = [c for c in by_task[None] if c.stage != Stage.PLAN]
    by_task[None] = [c for c in by_task[None] if c.stage == Stage.PLAN]

    request = records.request
    report = next((m for m in records.entries if isinstance(m, ReportMessage)), None)
    return {
        "request": {
            "messageId": request.message_id,
            "content": request.content,
            "status": request.status.value,
            "failureReason": request.failure_reason,
            "createdAt": request.created_at,
            "usage": records.usage,
        },
        "plan": {
            "calls": summaries(by_task[None]),
            "tasksCreated": [t.task_id for t in records.tasks],
            "clarifications": [
                wire(m) for pair in _clarifications(records.entries) for m in pair if m is not None
            ],
        },
        "tasks": [
            {**wire(task), "calls": summaries(by_task[task.task_id])} for task in records.tasks
        ],
        "report": _report_summary(report, summaries(report_calls)),
        "events": trace_events(records, by_task, tool_calls, report),
    }


def call_summary(call: ModelCall, tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    """A ModelCall as the trace shows it. The transcript's storage ref stays on the server;
    the client asks the transcript route when ``hasTranscript``."""
    summary = wire(call)
    has_transcript = summary.pop("transcriptRef") is not None
    return {
        **summary,
        "durationMs": _duration_ms(call),
        "hasTranscript": has_transcript,
        "toolCalls": tool_calls,
    }


def derived_tool_calls(
    task_calls: list[ModelCall], transcripts: Mapping[str, Transcript]
) -> dict[str, list[dict[str, Any]]]:
    """The tool calls of one task's model calls, by model call id, each joined to its result.

    A call's tool calls are the tool-use blocks of its response. Each result is the ToolMessage
    of the same id in the first later request of the task that carries it; the first, since a
    later compaction may have replaced its content with a stub.
    """
    results: dict[str, tuple[str, dict[str, Any]]] = {}
    for call in task_calls:
        for message in tool_results(transcripts.get(call.model_call_id, {})):
            results.setdefault(message["toolCallId"], (call.model_call_id, message))
    return {
        call.model_call_id: [
            _tool_call_view(use, results.get(use["id"]))
            for use in tool_uses(transcripts[call.model_call_id])
        ]
        for call in task_calls
        if call.model_call_id in transcripts
    }


def _tool_call_view(
    use: dict[str, Any], result: tuple[str, dict[str, Any]] | None
) -> dict[str, Any]:
    view = {"id": use["id"], "name": use["name"], "arguments": use["arguments"]}
    if result is None:
        return {
            **view,
            "status": NO_RESULT,
            "resultSummary": None,
            "rowCount": None,
            "artifactId": None,
            "resultInModelCallId": None,
        }
    carrier, message = result
    return {
        **view,
        "status": message.get("status") or "ok",
        "resultSummary": content_text(message.get("content", ""))[:RESULT_SUMMARY_CHARS],
        "rowCount": message.get("rowCount"),
        "artifactId": message.get("artifactId"),
        "resultInModelCallId": carrier,
    }


def trace_events(
    records: TraceRecords,
    by_task: Mapping[str | None, list[ModelCall]],
    tool_calls: Mapping[str, list[dict[str, Any]]],
    report: ReportMessage | None,
) -> list[dict[str, Any]]:
    """Every step of the request in the order it happened.

    Built in causal order (plan, tasks created, each task's run, report), then sorted by the
    second each step happened. The sort is stable and timestamps are compared to the second
    only, so steps within one second keep their causal order whatever their precision.
    """
    events = _plan_events(by_task.get(None, []), records.entries)
    events += [_event(t.created_at, "task_created", taskId=t.task_id) for t in records.tasks]
    for task in records.tasks:
        events += _task_events(task, by_task.get(task.task_id, []), tool_calls)
    if report is not None:
        events.append(_event(report.created_at, "report", messageId=report.message_id))
    return sorted(events, key=lambda event: event["at"][:19])


def _plan_events(plan_calls: list[ModelCall], entries: list[Message]) -> list[dict[str, Any]]:
    """Planner calls with each question and its answer between the call that asked it and the
    call that resumed after the answer. Both ids are ULIDs minted when the record was made, so
    comparing them orders an answer and a call to the millisecond."""
    pairs = _clarifications(entries)
    events: list[dict[str, Any]] = []
    for call in plan_calls:
        while pairs and _answered_before(pairs[0][1], call):
            events += _question_events(*pairs.pop(0))
        events.append(_event(call.started_at, "planner_call", modelCallId=call.model_call_id))
    for pair in pairs:
        events += _question_events(*pair)
    return events


def _answered_before(answer: Message | None, call: ModelCall) -> bool:
    return answer is not None and answer.message_id < call.model_call_id


def _question_events(
    question: ClarificationMessage, answer: ClarificationResponseMessage | None
) -> list[dict[str, Any]]:
    events = [_event(question.created_at, "clarification", messageId=question.message_id)]
    if answer is not None:
        events.append(
            _event(answer.created_at, "clarification_answer", messageId=answer.message_id)
        )
    return events


def _task_events(
    task: Task, calls: list[ModelCall], tool_calls: Mapping[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    """A task's run: started (its first model call), each call followed by its tool calls
    (made once the call answered), and its end."""
    events = []
    if calls:
        events.append(_event(calls[0].started_at, "task_started", taskId=task.task_id))
    for call in calls:
        refs = {"modelCallId": call.model_call_id, "taskId": task.task_id}
        events.append(_event(call.started_at, "model_call", **refs))
        answered_at = call.ended_at or call.started_at
        events += [
            _event(answered_at, "tool_call", **refs, toolCallId=view["id"])
            for view in tool_calls.get(call.model_call_id, [])
        ]
    if task.status in _ENDED:
        kind = "task_completed" if task.status == TaskStatus.COMPLETED else "task_failed"
        events.append(_event(task.updated_at, kind, taskId=task.task_id))
    return events


def _event(at: str, kind: str, **refs: str) -> dict[str, Any]:
    return {"at": at, "kind": kind, "refs": refs}


Clarification = tuple[ClarificationMessage, ClarificationResponseMessage | None]


def _clarifications(entries: list[Message]) -> list[Clarification]:
    """The request's questions in the order they were asked, each with its answer if any.
    Question ids are derived (hash bits), so they order by time, not id."""
    answers = {
        m.in_reply_to_message_id: m for m in entries if isinstance(m, ClarificationResponseMessage)
    }
    questions = sorted(
        (m for m in entries if isinstance(m, ClarificationMessage)),
        key=lambda m: (m.created_at, m.message_id),
    )
    return [(q, answers.get(q.message_id)) for q in questions]


def _report_summary(
    report: ReportMessage | None, calls: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """The report's counts and the model calls that wrote it (its summary, F10 ruling 2)."""
    if report is None:
        return None
    return {
        "messageId": report.message_id,
        "createdAt": report.created_at,
        "claims": len(report.report.claims),
        "conclusions": len(report.report.conclusions),
        "uncertainties": len(report.report.uncertainties),
        "calls": calls,
    }


def _duration_ms(call: ModelCall) -> int | None:
    if call.ended_at is None:
        return None
    started = datetime.fromisoformat(call.started_at)
    return round((datetime.fromisoformat(call.ended_at) - started).total_seconds() * 1000)

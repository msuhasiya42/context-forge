"""LangGraph state (implementation plan section 5): the request working brief and per-task state.

These are TypedDicts rather than Entities because LangGraph checkpoints state as plain dicts, so
enum values are held as their plain strings.
"""

from typing import Annotated, Any, NotRequired, TypedDict

# AnyMessage comes from langchain_core, its defining module: langgraph.graph.message uses it but
# does not export it, so importing it from there fails mypy's no-implicit-reexport check.
from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from contextforge.core.services.memory import MemorySnapshot


class PlanEntry(TypedDict):
    """In-state copy of one Task; the Task record is the source of truth."""

    task_id: str
    kind: str  # a TaskKind value
    status: str  # a TaskStatus value
    # Task ids this one waits for, so the workflow can order waves from state alone.
    depends_on: list[str]


class Fact(TypedDict):
    """A supported statement merged across tasks; the cite stage turns these into claims."""

    text: str
    evidence_refs: list[dict[str, Any]]


class OpenQuestion(TypedDict):
    text: str
    # The task id or clarification message id that settles it, once known.
    resolved_by: str | None


class PendingInterrupt(TypedDict):
    """The payload passed to ``interrupt()``; the runner shows it as what the request waits on."""

    kind: str
    message_id: str
    question: str
    next_action: str


class RequestState(TypedDict):
    """The working brief passed between workflow tasks and checkpointed with their results.

    It carries no transcript. ``chat_id`` and ``user_id`` are identity, carried so stages can
    write Messages and Artifacts without a lookup. ``memory`` is the user's memory snapshot
    taken when the run starts; every stage reads it, so a run never sees a later edit.
    """

    request_message_id: str
    chat_id: str
    user_id: str
    goal: str
    constraints: list[str]
    plan: list[PlanEntry]
    facts: list[Fact]
    open_questions: list[OpenQuestion]
    next_action: str
    period: NotRequired[dict[str, str] | None]
    # Questions the planner wanted but may not ask, each with the reading it planned on.
    assumptions: NotRequired[list[str]]
    memory: MemorySnapshot


class TaskState(TypedDict):
    """State of one task's execute subgraph; ``messages`` is the only field compaction trims."""

    task_id: str
    request_message_id: str
    chat_id: str
    user_id: str
    constraints: list[str]
    dependency_summaries: list[str]
    messages: Annotated[list[AnyMessage], add_messages]
    result: dict[str, Any] | None
    error: str | None
    # Loop-control counters live outside ``messages`` so they survive transcript compaction.
    tool_calls_made: int
    # Canonical tool-call key (name plus canonical JSON arguments) to its ToolMessage content.
    evidence_artifact_ids: NotRequired[list[str]]
    seen_calls: dict[str, str]
    no_progress_turns: int

"""LangSmith trace tree, one root per request segment (a start or a resume):

    request                      requestId, chatId, status, attempt; thread_id groups segments
    ├─ plan
    │  └─ model:plan             usage (thinking as reasoning), input/output/total cost, model,
    │                            requestedModel (differs when a fallback answered), stopReason
    ├─ round 1                   round
    │  └─ task:<kind>:<taskId>
    │     ├─ model:execute / model:compress
    │     └─ tool:<name>         status, rowCount, artifactIds
    ├─ lead_review 1             action, reason, tasksStarted
    │  └─ model:lead
    ├─ round 2 …                 (rounds and reviews alternate while the lead starts researchers)
    ├─ cite
    │  └─ model:cite
    └─ report                    narrative (written | fallback), summary (written | fallback)
       └─ model:summary / model:report

Parents are explicit: each run is created as a child of a RunTree looked up by request id and
task id, not through LangSmith's implicit context or LangChain callbacks, so the tree holds
across LangGraph's task threads. A call that carries no task id (compress) nests under the
innermost span open in its context, which LangGraph copies into its task threads.

Metadata only by default: the client hides inputs and outputs, and no prompt, page, result or
error text is put on a run unless ``include_content`` is on (CONTEXTFORGE_TRACE_CONTENT, for a
developer's own machine). Every tracing failure is logged and skipped, so telemetry never
blocks or repeats a provider or tool call.
"""

import logging
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Literal

from langchain_core.messages import convert_to_openai_messages
from langgraph.errors import GraphBubbleUp
from langsmith import Client
from langsmith.run_trees import RunTree
from langsmith.schemas import ExtractedUsageMetadata

from contextforge.core.llm.base import ModelClient, ModelRequest, ModelResponse
from contextforge.core.llm.errors import ModelError
from contextforge.core.llm.recording import PriceTable, cost_usd, price_row
from contextforge.core.state import RequestState
from contextforge.core.tools.base import Tool, ToolContext, ToolPermission, ToolResult
from contextforge.core.values import Usage

logger = logging.getLogger(__name__)

RunType = Literal["chain", "llm", "tool"]

# The innermost span (request, stage or task) open in the current context.
_innermost: ContextVar[RunTree | None] = ContextVar("contextforge_innermost_run", default=None)


def langsmith_client(api_key: str, *, include_content: bool) -> Client:
    """A LangSmith client that hides every run's inputs and outputs unless content is traced."""
    hide = not include_content
    return Client(api_key=api_key, hide_inputs=hide, hide_outputs=hide)


class RequestTracer:
    """Builds the trace tree; implements ``core.tracing.Tracer``. Without a client it traces
    nothing, so its hooks cost nothing."""

    def __init__(
        self,
        client: Any = None,
        project: str = "contextforge",
        *,
        include_content: bool = False,
        prices: PriceTable | None = None,
    ) -> None:
        self._client = client
        self._project = project
        self.include_content = include_content
        self._prices: PriceTable = prices or {}
        # Open runs: the root and the stack of open stages by request id, tasks by (request id,
        # task id). Stages open and close on the request's workflow thread only; tasks read the
        # top from their own threads. Single list and dict operations need no lock.
        self._roots: dict[str, RunTree] = {}
        self._stages: dict[str, list[RunTree]] = {}
        self._tasks: dict[tuple[str, str], RunTree] = {}

    def model(self, inner: ModelClient) -> ModelClient:
        """``inner`` with each provider call traced as a ``model:<stage>`` run."""
        return inner if self._client is None else _TracedModel(inner, self)

    def tools(self, tools: Mapping[str, Tool]) -> dict[str, Tool]:
        """``tools`` with each call traced as a ``tool:<name>`` run."""
        if self._client is None:
            return dict(tools)
        return {name: _TracedTool(tool, self) for name, tool in tools.items()}

    @contextmanager
    def request(
        self,
        request_message_id: str,
        chat_id: str,
        summary: Callable[[], Mapping[str, Any]],
    ) -> Iterator[None]:
        """The root run of one segment; ``summary()`` (status, attempt) is added as it ends."""
        metadata = {
            "requestId": request_message_id,
            "chatId": chat_id,
            "thread_id": request_message_id,
        }
        with self._span(self._root(metadata)) as root:
            if root is None:
                yield
                return
            self._roots[request_message_id] = root
            try:
                yield
            finally:
                del self._roots[request_message_id]
                self._safely(lambda: root.add_metadata(dict(summary())))

    @contextmanager
    def stage(
        self, request_message_id: str, name: str, metadata: Mapping[str, Any] | None = None
    ) -> Iterator[None]:
        """A stage run under the innermost open stage of the request, else its root."""
        parent = self._open_stage(request_message_id) or self._roots.get(request_message_id)
        with self._span(
            self.start(parent, name, "chain", {"stage": name, **(metadata or {})})
        ) as run:
            stack = self._stages.setdefault(request_message_id, [])
            if run is not None:
                stack.append(run)
            try:
                yield
            finally:
                if run is not None and stack and stack[-1] is run:
                    stack.pop()
                if not stack:
                    self._stages.pop(request_message_id, None)

    def annotate(self, request_message_id: str, metadata: Mapping[str, Any]) -> None:
        """Add ``metadata`` to the innermost open stage of the request; nothing when untraced."""
        run = self._open_stage(request_message_id)
        if run is not None:
            self._safely(lambda: run.add_metadata(dict(metadata)))

    def _open_stage(self, request_message_id: str) -> RunTree | None:
        stack = self._stages.get(request_message_id)
        return stack[-1] if stack else None

    @contextmanager
    def task(self, request_state: RequestState, task_id: str) -> Iterator[None]:
        """The run of one task, ``task:<kind>:<taskId>``, under the request's root."""
        request_id = request_state["request_message_id"]
        kind = next(
            (e["kind"] for e in request_state["plan"] if e["task_id"] == task_id), "unknown"
        )
        metadata = {"taskId": task_id, "taskKind": kind}
        parent = self._open_stage(request_id) or self._roots.get(request_id)
        with self._span(self.start(parent, f"task:{kind}:{task_id}", "chain", metadata)) as run:
            if run is not None:
                self._tasks[(request_id, task_id)] = run
            try:
                yield
            finally:
                self._tasks.pop((request_id, task_id), None)

    def parent_of(self, request_message_id: str | None, task_id: str | None) -> RunTree | None:
        """Where a model or tool call nests: its task, else the open stage, else the innermost
        span of its request in this context, else the root; None when the request is untraced."""
        if request_message_id is None or (root := self._roots.get(request_message_id)) is None:
            return None
        if task_id is not None and (task := self._tasks.get((request_message_id, task_id))):
            return task
        if stage := self._open_stage(request_message_id):
            return stage
        innermost = _innermost.get()
        return innermost if innermost is not None and innermost.trace_id == root.trace_id else root

    def start(
        self,
        parent: RunTree | None,
        name: str,
        run_type: RunType,
        metadata: Mapping[str, Any],
        inputs: Mapping[str, Any] | None = None,
    ) -> RunTree | None:
        """A posted child run of ``parent``; None when there is no parent or tracing failed."""
        if parent is None:
            return None
        extra = {"metadata": dict(metadata)}
        return self._post(
            lambda: parent.create_child(name, run_type, inputs=dict(inputs or {}), extra=extra)
        )

    def end(
        self,
        run: RunTree | None,
        *,
        error: BaseException | None = None,
        metadata: Mapping[str, Any] | None = None,
        outputs: Mapping[str, Any] | None = None,
        usage: ExtractedUsageMetadata | None = None,
    ) -> None:
        """Finish ``run``. Without content, an error is recorded by its type only: the message
        of a validation error can quote model or page text."""
        if run is None:
            return

        def finish() -> None:
            if usage is not None:
                run.set(usage_metadata=usage)
            run.end(
                outputs=dict(outputs) if outputs is not None and self.include_content else None,
                error=None if error is None else self._error_text(error),
                metadata=dict(metadata or {}),
            )
            run.patch()

        self._safely(finish)

    def cost_usd(self, model: str, usage: Usage | None) -> float | None:
        """The estimated cost of ``usage``, priced as the ModelCall record prices it."""
        return cost_usd(self._prices, model, usage)

    def price_row(self, model: str) -> Mapping[str, float] | None:
        return price_row(self._prices, model)

    @contextmanager
    def _span(self, run: RunTree | None) -> Iterator[RunTree | None]:
        """``run`` kept open for the block as the innermost span, then ended. An interrupt
        pauses the run rather than failing it."""
        if run is None:
            yield None
            return
        token = _innermost.set(run)
        try:
            yield run
        except GraphBubbleUp:
            self.end(run, metadata={"paused": True})
            raise
        except BaseException as exc:
            self.end(run, error=exc)
            raise
        else:
            self.end(run)
        finally:
            _innermost.reset(token)

    def _root(self, metadata: Mapping[str, Any]) -> RunTree | None:
        if self._client is None:
            return None
        return self._post(
            lambda: RunTree(
                name="request",
                run_type="chain",
                extra={"metadata": dict(metadata)},
                project_name=self._project,
                ls_client=self._client,
            )
        )

    def _post(self, build: Callable[[], RunTree]) -> RunTree | None:
        try:
            run = build()
            run.post()
        except Exception as exc:
            logger.warning("Trace run unavailable (%s); continuing without it.", type(exc).__name__)
            return None
        return run

    def _error_text(self, error: BaseException) -> str:
        return f"{type(error).__name__}: {error}" if self.include_content else type(error).__name__

    @staticmethod
    def _safely(action: Callable[[], object]) -> None:
        try:
            action()
        except Exception as exc:
            logger.warning("Could not record trace data (%s); continuing.", type(exc).__name__)


class _TracedModel:
    """Each provider call as a ``model:<stage>`` run under its task or stage."""

    def __init__(self, inner: ModelClient, tracer: RequestTracer) -> None:
        self._inner = inner
        self._tracer = tracer

    def invoke(self, request: ModelRequest) -> ModelResponse:
        tracer = self._tracer
        run = tracer.start(
            tracer.parent_of(request.request_message_id, request.task_id),
            f"model:{request.stage.value}",
            "llm",
            {
                "stage": request.stage.value,
                "model": request.model,
                # The stage's model; differs from ``model`` when a fallback answers for it.
                "requestedModel": request.requested_model or request.model,
                "taskId": request.task_id,
            },
            _model_inputs(request) if tracer.include_content else None,
        )
        try:
            response = self._inner.invoke(request)
        except Exception as exc:
            tracer.end(run, error=exc)
            if run is not None and isinstance(exc, ModelError):
                exc.langsmith_run_id = str(run.id)
            raise
        cost = tracer.cost_usd(request.model, response.usage)
        tracer.end(
            run,
            metadata={"stopReason": response.stop_reason, "estimatedCostUsd": cost},
            outputs=_model_outputs(response),
            usage=_usage_metadata(response.usage, cost, tracer.price_row(request.model)),
        )
        if run is None:
            return response
        return response.model_copy(update={"langsmith_run_id": str(run.id)})


class _TracedTool:
    """Each tool call as a ``tool:<name>`` run under its task."""

    def __init__(self, inner: Tool, tracer: RequestTracer) -> None:
        self._inner = inner
        self._tracer = tracer
        self.spec = inner.spec
        self.permission: ToolPermission = inner.permission

    def run(self, arguments: dict[str, Any], tool_context: ToolContext) -> ToolResult:
        tracer = self._tracer
        run = tracer.start(
            tracer.parent_of(tool_context.request_message_id, tool_context.task_id),
            f"tool:{self.spec.name}",
            "tool",
            {
                "tool": self.spec.name,
                "taskId": tool_context.task_id,
                "toolCallId": tool_context.tool_call_id,
            },
            arguments if tracer.include_content else None,
        )
        try:
            result = self._inner.run(arguments, tool_context)
        except Exception as exc:
            tracer.end(run, error=exc)
            raise
        metadata = {
            "status": result.status,
            "rowCount": None if result.rows is None else len(result.rows),
            "artifactIds": result.artifact_ids,
            "errorClass": result.error_class,
        }
        tracer.end(
            run,
            error=RuntimeError(result.error or f"Tool returned {result.status}")
            if result.status != "ok"
            else None,
            metadata=metadata,
            outputs=result.model_dump(mode="json", by_alias=True),
        )
        return result


def _model_inputs(request: ModelRequest) -> dict[str, Any]:
    return {
        "system": request.system,
        "messages": convert_to_openai_messages(request.messages),
        "tools": [tool.name for tool in request.tools],
    }


def _model_outputs(response: ModelResponse) -> dict[str, Any]:
    return {
        "text": response.text,
        "structured": response.structured,
        "toolCalls": [call.model_dump(by_alias=True) for call in response.tool_calls],
    }


def _usage_metadata(
    usage: Usage | None, cost: float | None, prices: Mapping[str, float] | None = None
) -> ExtractedUsageMetadata | None:
    """Token counts and cost where LangSmith shows them; None when usage is unknown.

    Thinking is billed as output, so it is counted in ``output_tokens`` and named in
    ``output_token_details.reasoning``. With the model's prices, the input and output costs are
    sent too, so LangSmith's cost breakdown adds up to the total."""
    if usage is None:
        return None
    thinking = usage.thinking_tokens or 0
    output = usage.output_tokens + thinking
    counts = ExtractedUsageMetadata(
        input_tokens=usage.input_tokens,
        output_tokens=output,
        total_tokens=usage.input_tokens + output,
    )
    if usage.thinking_tokens is not None:
        counts["output_token_details"] = {"reasoning": thinking}
    if cost is not None:
        counts["total_cost"] = cost
    if prices is not None:
        counts["output_cost"] = output * prices["outputPerMTok"] / 1_000_000
        counts["input_cost"] = cost - counts["output_cost"] if cost is not None else 0.0
    return counts

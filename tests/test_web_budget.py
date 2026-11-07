"""Request-wide admission reaches the real tool boundary, without network traffic."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Lock

from contextforge.core.models.base import ToolCall, ToolSpec
from contextforge.core.models.scripted import ScriptedModel
from contextforge.core.tools.base import ToolContext, ToolResult
from contextforge.worker.stages.execute import _invoke_tool
from tests.helpers import make_deps, seed_chat_with_dataset


class CountingWebTool:
    spec = ToolSpec(name="web_search", description="Test search", input_schema={})
    permission = "READ"

    def __init__(self) -> None:
        self.calls = 0
        self.lock = Lock()

    def run(self, arguments: dict, tool_context: ToolContext) -> ToolResult:
        with self.lock:
            self.calls += 1
        return ToolResult(status="error", error="provider unavailable", retryable=True)


def test_parallel_tasks_and_restarted_workers_share_the_web_budget() -> None:
    worker_dependencies = make_deps(ScriptedModel({}))
    request = seed_chat_with_dataset(
        worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2"
    )
    # Reconstructing worker dependencies must not reset the counter stored on the request.
    other = replace(worker_dependencies)
    tool = CountingWebTool()
    worker_dependencies.tools = other.tools = {"web_search": tool, "fetch_url": tool}
    state = {
        "chat_id": "c1",
        "user_id": "u1",
        "request_message_id": request.message_id,
        "task_id": "t1",
    }

    def call(n: int) -> ToolResult:
        worker = worker_dependencies if n % 2 else other
        return _invoke_tool(
            ToolCall(id=str(n), name="web_search" if n % 2 else "fetch_url", arguments={}),
            state,
            "web_search",
            worker,
        )

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(call, range(75)))
    assert tool.calls == 50
    assert sum(r.status == "error" for r in results) == 50  # Failures still spend a slot.
    assert sum(r.status == "not_executed" for r in results) == 25
    saved = worker_dependencies.repositories.messages.get("c1", request.message_id)
    assert saved.web_tool_calls == 50
    assert call(76).status == "not_executed"
    assert tool.calls == 50


def test_web_budget_is_scoped_to_a_request_and_persists_status_changes() -> None:
    worker_dependencies = make_deps(ScriptedModel({}))
    first = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    second = seed_chat_with_dataset(
        worker_dependencies, chat_id="c2", user_id="u1", dataset_id="a3"
    )
    messages = worker_dependencies.repositories.messages
    assert messages.reserve_web_tool_call("c1", first.message_id, 1) == 1
    messages.update_request_status("c1", first.message_id, "ACCEPTED", "RUNNING")
    messages.update_request_status("c1", first.message_id, "RUNNING", "WAITING_USER")
    messages.update_request_status("c1", first.message_id, "WAITING_USER", "RUNNING")
    assert messages.reserve_web_tool_call("c1", first.message_id, 1) is None
    assert messages.reserve_web_tool_call("c2", second.message_id, 1) == 1

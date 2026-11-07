import json
import sqlite3
from collections import defaultdict

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from contextforge.core.entities.model_call import Stage
from contextforge.core.models.base import ModelRequest, ModelResponse, ToolCall
from contextforge.core.models.config import ModelRoute, load_models_config
from contextforge.core.models.errors import ModelTimeout, ModelUnavailable
from contextforge.core.models.scripted import ScriptedModel
from contextforge.core.settings import Settings
from contextforge.core.values import Usage
from contextforge.infra import tracing
from contextforge.infra.runtime import build_worker
from contextforge.infra.tracing import RequestTracer
from contextforge.worker.deps import WorkerDeps
from contextforge.worker.runner import RequestRunner
from tests.helpers import ByModel, make_deps, seed_chat_with_dataset, t1_result

SQL = "SELECT count(*) AS n FROM t"


class FakeLangSmith:
    """Records the runs a RunTree posts and patches; ``fail`` makes every post raise."""

    def __init__(self, *, fail: bool = False) -> None:
        self.created: list[dict] = []
        self.updated: list[dict] = []
        self.fail = fail

    def create_run(self, **run) -> None:
        if self.fail:
            raise RuntimeError("LangSmith unavailable")
        self.created.append(run)

    def update_run(self, **run) -> None:
        self.updated.append(run)

    def children(self) -> dict[str | None, list[str]]:
        """Child run names by parent run name; the root's parent is None."""
        names = {run["id"]: run["name"] for run in self.created}
        tree: dict[str | None, list[str]] = defaultdict(list)
        for run in self.created:
            tree[names.get(run.get("parent_run_id"))].append(run["name"])
        return {parent: sorted(kids) for parent, kids in tree.items()}

    def ended(self, name: str) -> dict:
        return next(run for run in self.updated if run["name"] == name)


def _traced(deps: WorkerDeps, client: FakeLangSmith, *, content: bool = False) -> WorkerDeps:
    """``deps`` wired the way the runtime wires a tracing tracer."""
    prices = load_models_config().prices
    deps.tracer = RequestTracer(client, "test", include_content=content, prices=prices)
    deps.model = deps.tracer.model(deps.model)
    deps.tools = deps.tracer.tools(deps.tools)
    return deps


def _two_task_script() -> dict[str, list[ModelResponse]]:
    planned = [
        {
            "ref": ref,
            "kind": "sql_analysis",
            "goal": goal,
            "inputArtifactIds": ["a2"],
            "dependsOn": [],
        }
        for ref, goal in (("t1", "Count the rows."), ("t2", "Count the rows again."))
    ]
    plan = {
        "kind": "plan",
        "goal": "Count the transactions.",
        "constraints": [],
        "openQuestions": [],
        "tasks": planned,
        "nextAction": "Run t1 and t2.",
    }
    usage = Usage(input_tokens=1_000, output_tokens=100)
    query = ToolCall(id="q1", name="sql_query", arguments={"dataset_id": "a2", "sql": SQL})
    per_task = [
        ModelResponse(tool_calls=[query], stop_reason="tool_use", usage=usage),
        ModelResponse(structured=t1_result(), usage=usage),
    ]
    return {"plan": [ModelResponse(structured=plan)], "execute": per_task * 2}


@pytest.mark.parametrize("content", [False, True])
def test_trace_tree_of_a_two_task_run_is_metadata_only_unless_content_is_on(content) -> None:
    client = FakeLangSmith()
    deps = _traced(
        make_deps(ScriptedModel(_two_task_script()), tasks_in_flight=1), client, content=content
    )
    req = seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    saver = SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False))
    assert RequestRunner(deps, saver).start("c1", req.message_id).status == "COMPLETED"

    tree = client.children()
    tasks = [name for name in tree["request"] if name.startswith("task:sql_analysis:")]
    assert tree[None] == ["request"] and len(tasks) == 2
    assert tree["request"] == sorted(["plan", "cite", "report", *tasks])
    assert tree["plan"] == ["model:plan"]
    for task in tasks:
        assert tree[task] == ["model:execute", "model:execute", "tool:sql_query"]
    root = client.ended("request")["extra"]["metadata"]
    assert root["requestId"] == req.message_id and root["chatId"] == "c1"
    assert root["status"] == "COMPLETED" and root["attempt"] == 1
    call = client.ended("model:execute")["extra"]["metadata"]
    # The executor, Qwen3-235B, at its Sydney price (F9)
    total = (1_000 * 0.2266 + 100 * 0.9064) / 1e6
    assert call["usage_metadata"]["total_cost"] == pytest.approx(total)
    assert call["model"] == deps.model_id(Stage.EXECUTE) and call["stopReason"] == "tool_use"
    tool = client.ended("tool:sql_query")["extra"]["metadata"]
    assert tool["status"] == "ok" and tool["rowCount"] == 1
    recorded = deps.repos.model_calls.list_by_request(req.message_id)
    traced = {str(run["id"]) for run in client.created}
    assert all(c.langsmith_run_id in traced for c in recorded)

    sent = json.dumps([client.created, client.updated], default=str)
    assert (req.content.splitlines()[0] in sent) is content and (SQL in sent) is content


def test_runtime_traces_only_with_its_own_switch_and_hides_content_by_default(
    monkeypatch,
) -> None:
    made: list[dict] = []

    class Client:
        def __init__(self, **kwargs) -> None:
            made.append(kwargs)

    monkeypatch.setattr(tracing, "Client", Client)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")  # the SDK's switch is not ours
    common = {"_env_file": None, "aws_profile": None, "model_backend": "scripted"}
    untraced = build_worker(Settings(**common, LANGSMITH_API_KEY="test-key"))
    assert isinstance(untraced.model, ScriptedModel) and made == []
    for content in (False, True):
        settings = Settings(
            **common, model_tracing=True, trace_content=content, LANGSMITH_API_KEY="test-key"
        )
        deps = build_worker(settings)
        assert deps.tracer.include_content is content
        assert not isinstance(deps.model, ScriptedModel)
    assert [(m["hide_inputs"], m["hide_outputs"]) for m in made] == [(True, True), (False, False)]


def test_failed_call_keeps_its_run_id_and_a_broken_tracer_never_blocks_a_call() -> None:
    class TimesOut:
        def invoke(self, request: ModelRequest) -> ModelResponse:
            raise ModelTimeout("slow")

    client = FakeLangSmith()
    deps = _traced(make_deps(TimesOut()), client)
    request = ModelRequest(stage="plan", model="m", system=[], messages=[])
    with deps.segment("c1", "r1"), pytest.raises(ModelTimeout):
        deps.model_for("r1").invoke(request)
    failed = client.ended("model:plan")
    assert deps.repos.model_calls.list_by_request("r1")[0].langsmith_run_id == str(failed["run_id"])
    assert failed["error"] == "ModelTimeout"  # the type only: messages can quote content

    calls: list[ModelRequest] = []

    class Model:
        def invoke(self, request: ModelRequest) -> ModelResponse:
            calls.append(request)
            return ModelResponse(text="answer")

    broken = _traced(make_deps(Model()), FakeLangSmith(fail=True))
    with broken.segment("c1", "r2"):
        assert broken.model_for("r2").invoke(request).text == "answer"
    assert len(calls) == 1


def test_a_fallback_call_is_traced_with_the_stage_model_and_the_model_that_answered() -> None:
    client = FakeLangSmith()
    down = ModelUnavailable("ServiceUnavailableException")
    deps = _traced(make_deps(ByModel({"p": [down], "f": [ModelResponse(text="ok")]})), client)
    deps.routes = {Stage.PLAN: ModelRoute("p", ("f",))}
    request = ModelRequest(stage="plan", model="p", system=[], messages=[])
    with deps.segment("c1", "r1"):
        deps.model_for("r1").invoke(request)
    runs = [r["extra"]["metadata"] for r in client.created if r["name"] == "model:plan"]
    assert [(r["model"], r["requestedModel"]) for r in runs] == [("p", "p"), ("f", "p")]

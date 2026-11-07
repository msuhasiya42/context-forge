# tests/test_workflow.py
import dataclasses
import threading

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from contextforge.core.models.base import ModelRequest, ModelResponse, ToolCall
from contextforge.core.models.scripted import ScriptedModel
from contextforge.core.tools.base import ToolContext
from contextforge.worker.tools.sql_query import SqlQueryTool
from contextforge.worker.workflow import build_workflow
from tests.helpers import make_deps, seed_chat_with_dataset, six_task_plan, t1_result


def _cfg(req) -> dict:
    return {"configurable": {"thread_id": req.message_id}}


def _start(deps, req) -> None:
    deps.repos.messages.update_request_status("c1", req.message_id, "ACCEPTED", "RUNNING")


def test_csv_sql_query_finds_planted_transfer_pairs() -> None:
    deps = make_deps(ScriptedModel({}))
    req = seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    ctx = ToolContext(
        chat_id="c1",
        user_id="u1",
        request_message_id=req.message_id,
        task_id="t1",
        tool_call_id="x1",
    )
    rows = (
        SqlQueryTool(deps.repos.artifacts, deps.blobs)
        .run(
            {
                "dataset_id": "a2",
                "sql": "SELECT d.row_id AS debit_row, c.row_id AS credit_row, d.amount "
                "FROM t d JOIN t c "
                "ON d.amount = c.amount AND d.direction='DEBIT' AND c.direction='CREDIT' "
                "AND d.account_id <> c.account_id "
                "AND abs(date_diff('day', d.txn_date, c.txn_date)) <= 2",
            },
            ctx,
        )
        .rows
    )
    assert rows is not None
    assert {(r["debit_row"], r["credit_row"]) for r in rows} >= {(12, 13), (40, 41), (50, 51)}
    # external access is off once the dataset is loaded: a SELECT cannot read host files
    leaked = SqlQueryTool(deps.repos.artifacts, deps.blobs).run(
        {"dataset_id": "a2", "sql": "SELECT count(*) FROM read_text('/etc/hosts')"}, ctx
    )
    assert leaked.status == "error" and leaked.error_class == "code" and leaked.rows is None


def test_fixture_request_produces_six_tasks_and_a_report() -> None:
    script = {
        "plan": [ModelResponse(structured=six_task_plan())],
        "execute": [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="x1",
                        name="sql_query",
                        arguments={"dataset_id": "a2", "sql": "SELECT * FROM t LIMIT 5"},
                    )
                ],
                stop_reason="tool_use",
            ),
            ModelResponse(structured=t1_result()),
            ModelResponse(structured=t1_result()),
            ModelResponse(structured=t1_result()),
        ],
    }
    deps = make_deps(
        ScriptedModel(script), tasks_in_flight=1
    )  # serial, so the script order is deterministic
    req = seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    _start(deps, req)
    out = build_workflow(deps, checkpointer=None).invoke(
        {"chat_id": "c1", "message_id": req.message_id}, _cfg(req)
    )
    assert out["status"] == "COMPLETED"
    tasks = deps.repos.tasks.list_by_request(req.message_id)
    assert len(tasks) == 6
    assert sorted(t.status for t in tasks) == sorted(["COMPLETED"] * 4 + ["SKIPPED"] * 2)
    msgs = deps.repos.messages.list_by_chat("c1")
    report = [m for m in msgs if m.kind == "report"][0]
    assert report.report.claims and all(c.evidence_refs for c in report.report.claims)
    assert deps.repos.messages.get("c1", req.message_id).status == "COMPLETED"
    assert len(deps.repos.model_calls.list_by_request(req.message_id)) == 5


def test_request_completes_under_a_small_context_cap() -> None:
    # Five distinct queries grow the first task's transcript past a 3,000-token window: level 1
    # stubs the oldest results, and level 3 summarizes once the stubs alone no longer fit.
    queries = [
        ModelResponse(
            tool_calls=[
                ToolCall(
                    id=f"q{limit}",
                    name="sql_query",
                    arguments={"dataset_id": "a2", "sql": f"SELECT * FROM t LIMIT {limit}"},
                )
            ],
            stop_reason="tool_use",
        )
        for limit in (5, 10, 15, 20, 25)
    ]
    model = ScriptedModel(
        {
            "plan": [ModelResponse(structured=six_task_plan())],
            "execute": [*queries, *[ModelResponse(structured=t1_result())] * 3],
            "compress": [ModelResponse(text="summary of earlier queries")] * 3,
        }
    )
    requests: list[ModelRequest] = []
    model.on_request = requests.append
    deps = make_deps(model, tasks_in_flight=1)  # serial, so one task's turns are consecutive
    deps.budgets = dataclasses.replace(
        deps.budgets, cap_input_tokens=3000, response_allowance_tokens=500
    )
    req = seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    _start(deps, req)
    out = build_workflow(deps, checkpointer=None).invoke(
        {"chat_id": "c1", "message_id": req.message_id}, _cfg(req)
    )
    assert out["status"] == "COMPLETED"
    tasks = deps.repos.tasks.list_by_request(req.message_id)
    assert sorted(t.status for t in tasks) == sorted(["COMPLETED"] * 4 + ["SKIPPED"] * 2)
    report = [m for m in deps.repos.messages.list_by_chat("c1") if m.kind == "report"][0]
    assert report.report.claims and all(c.evidence_refs for c in report.report.claims)
    calls = deps.repos.model_calls.list_by_request(req.message_id)
    assert any(c.stage == "compress" for c in calls)
    first = next(i for i, r in enumerate(requests) if r.stage == "compress")
    # a summarized tool result is summarized together with the call that made it
    summarized: set[str] = set()
    for message in requests[first].messages:
        if isinstance(message, AIMessage):
            summarized.update(c["id"] for c in message.tool_calls)
        elif isinstance(message, ToolMessage):
            assert message.tool_call_id in summarized
    # the summary is written back: the next turn sees it and the tail, not the summarized calls
    next_turn = requests[first + 2].messages  # first + 1 is this turn's execute call
    assert any(
        isinstance(m, SystemMessage) and m.content.startswith("Summary of earlier work")
        for m in next_turn
    )
    sent = {c["id"] for m in next_turn if isinstance(m, AIMessage) for c in m.tool_calls}
    assert summarized and not sent & summarized


def test_independent_tasks_run_in_parallel() -> None:
    gate = threading.Barrier(3, timeout=5)  # all three sql tasks must be inside the model at once

    class Pausing(ScriptedModel):
        def invoke(self, request):
            if request.stage == "execute":
                gate.wait()
            return super().invoke(request)

    plan = six_task_plan()
    plan["tasks"][1]["dependsOn"] = []  # t1, t2, t3 all independent
    deps = make_deps(
        Pausing(
            {
                "plan": [ModelResponse(structured=plan)],
                "execute": [ModelResponse(structured=t1_result())] * 3,
            }
        ),
        tasks_in_flight=3,
    )
    req = seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    _start(deps, req)
    out = build_workflow(deps, checkpointer=None).invoke(
        {"chat_id": "c1", "message_id": req.message_id}, _cfg(req)
    )
    assert out["status"] == "COMPLETED" and gate.broken is False


def test_cancel_between_waves_stops_the_run() -> None:
    script = {
        "plan": [ModelResponse(structured=six_task_plan())],
        "execute": [ModelResponse(structured=t1_result())] * 3,
    }
    deps = make_deps(ScriptedModel(script), tasks_in_flight=1)
    req = seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    # the API sets CANCELLED; here the hook fires right after the first task completes
    deps.repos.tasks.on_save = lambda t: (
        deps.repos.messages.update_request_status(  # type: ignore[attr-defined]
            "c1", req.message_id, "RUNNING", "CANCELLED"
        )
        if t.status == "COMPLETED" and t.kind == "sql_analysis"
        else None
    )
    _start(deps, req)
    out = build_workflow(deps, checkpointer=None).invoke(
        {"chat_id": "c1", "message_id": req.message_id}, _cfg(req)
    )
    assert out["status"] == "CANCELLED"
    assert deps.repos.messages.get("c1", req.message_id).status == "CANCELLED"
    assert not [m for m in deps.repos.messages.list_by_chat("c1") if m.kind == "report"]


def test_tool_call_cap_fails_the_task_not_the_request() -> None:
    calls = [
        ModelResponse(
            tool_calls=[
                ToolCall(
                    id="x", name="sql_query", arguments={"dataset_id": "a2", "sql": f"SELECT {i}"}
                )
            ],
            stop_reason="tool_use",
        )
        for i in range(14)
    ]
    deps = make_deps(
        ScriptedModel(
            {
                "plan": [ModelResponse(structured=six_task_plan())],
                "execute": calls + [ModelResponse(structured=t1_result())] * 2,
            }
        ),
        tasks_in_flight=1,
    )
    req = seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    _start(deps, req)
    out = build_workflow(deps, checkpointer=None).invoke(
        {"chat_id": "c1", "message_id": req.message_id}, _cfg(req)
    )
    tasks = deps.repos.tasks.list_by_request(req.message_id)
    failed = [t for t in tasks if t.status == "FAILED"]
    assert (
        out["status"] == "COMPLETED"
        and len(failed) >= 1
        and "tool call budget" in failed[0].failure_reason
    )


def test_tool_error_is_a_result_the_model_sees() -> None:
    class Boom:
        spec = SqlQueryTool.spec
        permission = "READ"

        def __init__(self) -> None:
            self.runs = 0

        def run(self, arguments, ctx):
            self.runs += 1
            raise RuntimeError("duckdb exploded")

    boom = ModelResponse(
        tool_calls=[
            ToolCall(id="x", name="sql_query", arguments={"dataset_id": "a2", "sql": "SELECT 1"})
        ],
        stop_reason="tool_use",
    )
    deps = make_deps(
        ScriptedModel(
            {
                "plan": [ModelResponse(structured=six_task_plan())],
                "execute": [boom, ModelResponse(structured=t1_result())] * 3,
            }
        ),
        tasks_in_flight=1,
    )
    deps.tools["sql_query"] = Boom()
    req = seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    _start(deps, req)
    seen = []
    deps.model.on_request = lambda r: seen.extend(m.content for m in r.messages if m.type == "tool")  # type: ignore[attr-defined]
    out = build_workflow(deps, checkpointer=None).invoke(
        {"chat_id": "c1", "message_id": req.message_id}, _cfg(req)
    )
    assert out["status"] == "COMPLETED" and any('"error_class": "system"' in c for c in seen)
    # a failed call is never answered "[already done]": repeating it runs the tool again
    retried = Boom()
    deps = make_deps(
        ScriptedModel(
            {
                "plan": [ModelResponse(structured=six_task_plan())],
                "execute": [boom, boom] + [ModelResponse(structured=t1_result())] * 3,
            }
        ),
        tasks_in_flight=1,
    )
    deps.tools["sql_query"] = retried
    req = seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    _start(deps, req)
    out = build_workflow(deps, checkpointer=None).invoke(
        {"chat_id": "c1", "message_id": req.message_id}, _cfg(req)
    )
    t1 = deps.repos.tasks.list_by_request(req.message_id)[0]
    assert retried.runs == 2 and t1.status == "COMPLETED" and out["status"] == "COMPLETED"


def test_duplicate_tool_call_spends_no_slot() -> None:
    call = ModelResponse(
        tool_calls=[
            ToolCall(id="x", name="sql_query", arguments={"dataset_id": "a2", "sql": "SELECT 1"})
        ],
        stop_reason="tool_use",
    )
    deps = make_deps(
        ScriptedModel(
            {
                "plan": [ModelResponse(structured=six_task_plan())],
                "execute": [call] * 8 + [ModelResponse(structured=t1_result())] * 3,
            }
        ),
        tasks_in_flight=1,
    )
    req = seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    _start(deps, req)
    build_workflow(deps, checkpointer=None).invoke(
        {"chat_id": "c1", "message_id": req.message_id}, _cfg(req)
    )
    t1 = deps.repos.tasks.list_by_request(req.message_id)[0]
    assert (
        t1.status == "FAILED" and t1.failure_reason == "no progress"
    )  # the duplicate never counted; no-progress ended it


def test_scripted_exhaustion_fails_request_with_reason() -> None:
    deps = make_deps(ScriptedModel({"plan": [ModelResponse(structured=six_task_plan())]}))
    req = seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    _start(deps, req)
    out = build_workflow(deps, checkpointer=None).invoke(
        {"chat_id": "c1", "message_id": req.message_id}, _cfg(req)
    )
    m = deps.repos.messages.get("c1", req.message_id)
    assert out["status"] == "FAILED" and m.status == "FAILED" and m.failure_reason
    assert any(c.outcome == "ERROR" for c in deps.repos.model_calls.list_by_request(req.message_id))

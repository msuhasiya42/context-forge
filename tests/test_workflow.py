# tests/test_workflow.py
import dataclasses
import threading

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from contextforge.core.models.base import ModelRequest, ModelResponse, ToolCall
from contextforge.core.models.scripted import ScriptedModel
from contextforge.core.tools.base import ToolContext
from contextforge.infra.bedrock import converse_payload
from contextforge.worker.stages.execute import NO_CALLS_REMAIN
from contextforge.worker.tools.sql_query import SqlQueryTool
from contextforge.worker.workflow import build_workflow
from tests.helpers import (
    clarification,
    make_deps,
    run_answering_the_question,
    seed_chat_with_dataset,
    six_task_plan,
    t1_result,
)


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
    requests: list[ModelRequest] = []
    deps.model.on_request = requests.append  # type: ignore[attr-defined]
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
    # the resolved window reaches the report's coverage, its constraints and every execute call
    period = report.report.coverage["period"]
    stated = f"Period: {period['start']} to {period['end']}"
    assert stated in [c.text for c in report.report.constraints]
    executes = [r for r in requests if r.stage == "execute"]
    assert executes and all(stated in r.messages[0].content for r in executes)


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
    next_turn = next(r for r in requests[first + 2 :] if r.stage == "execute").messages
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


def test_a_task_out_of_tool_calls_returns_the_facts_it_has() -> None:
    """F7 ruling 1 (live run, 7 Nov): the weather task kept searching until its tool-call budget
    ran out and failed, losing all it had read. The call after the last slot now offers no tool,
    so return_result is forced, and the task completes with its facts and says why it stopped."""

    class KeepsQuerying(ScriptedModel):
        """Runs a new query whenever a tool is offered; returns its script's result otherwise."""

        def invoke(self, request):
            if request.stage != "execute" or not request.tools:
                return super().invoke(request)
            turn = len(request.messages)  # the transcript grows, so every query is new
            sql = f"SELECT {turn}"
            call = ToolCall(
                id=f"q{turn}", name="sql_query", arguments={"dataset_id": "a2", "sql": sql}
            )
            return ModelResponse(tool_calls=[call], stop_reason="tool_use")

    model = KeepsQuerying(
        {
            "plan": [ModelResponse(structured=six_task_plan())],
            "execute": [ModelResponse(structured=t1_result())] * 3,
        }
    )
    requests: list[ModelRequest] = []
    model.on_request = requests.append
    deps = make_deps(model, tasks_in_flight=1)
    req = seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    _start(deps, req)
    out = build_workflow(deps, checkpointer=None).invoke(
        {"chat_id": "c1", "message_id": req.message_id}, _cfg(req)
    )
    assert out["status"] == "COMPLETED"
    sql_tasks = [
        t for t in deps.repos.tasks.list_by_request(req.message_id) if t.kind == "sql_analysis"
    ]
    stopped = "stopped at the tool-call budget; 6 pages read"
    for task in sql_tasks:
        assert (task.status, task.attempt, len(task.result.facts)) == ("COMPLETED", 1, 2)
        assert task.result.uncertainties[-1] == stopped
    last_calls = [r for r in requests if r.stage == "execute" and not r.tools]
    assert len(last_calls) == len(sql_tasks)  # one per task, after its sixth query
    assert last_calls[0].messages[-1].content == NO_CALLS_REMAIN
    forced = converse_payload(last_calls[0])["toolConfig"]
    assert forced["toolChoice"] == {"tool": {"name": "return_result"}}
    report = next(m for m in deps.repos.messages.list_by_chat("c1") if m.kind == "report")
    assert report.report.claims and stopped in report.report.uncertainties


def test_no_result_even_at_the_tool_call_cap_fails_the_task_not_the_request() -> None:
    # A model that still calls a tool when only return_result is offered has nothing usable.
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
    # Three repeats in a row run nothing new, so the next call may only return the result.
    call = ModelResponse(
        tool_calls=[
            ToolCall(id="x", name="sql_query", arguments={"dataset_id": "a2", "sql": "SELECT 1"})
        ],
        stop_reason="tool_use",
    )
    model = ScriptedModel(
        {
            "plan": [ModelResponse(structured=six_task_plan())],
            "execute": [call] * 4 + [ModelResponse(structured=t1_result())] * 3,
        }
    )
    requests: list[ModelRequest] = []
    model.on_request = requests.append
    deps = make_deps(model, tasks_in_flight=1)
    req = seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    _start(deps, req)
    build_workflow(deps, checkpointer=None).invoke(
        {"chat_id": "c1", "message_id": req.message_id}, _cfg(req)
    )
    t1 = deps.repos.tasks.list_by_request(req.message_id)[0]
    assert (t1.status, t1.attempt) == ("COMPLETED", 1)
    # the duplicates never counted: one query read, then the no-progress limit ended the loop
    assert t1.result.uncertainties[-1] == "stopped after 3 turns without progress; 1 page read"
    turns = [r for r in requests if r.task_id == t1.task_id]
    assert [bool(r.tools) for r in turns] == [True] * 4 + [False]


def test_failed_dependency_fails_its_dependents() -> None:
    # t1 returns an invalid result on both of its attempts; t3 needs no t1 and completes
    invalid = ModelResponse(structured={})
    deps = make_deps(
        ScriptedModel(
            {
                "plan": [ModelResponse(structured=six_task_plan())],
                "execute": [invalid, invalid, ModelResponse(structured=t1_result())],
            }
        ),
        tasks_in_flight=1,
    )
    req = seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    _start(deps, req)
    out = build_workflow(deps, checkpointer=None).invoke(
        {"chat_id": "c1", "message_id": req.message_id}, _cfg(req)
    )
    t1, t2, t3, _, _, t6 = deps.repos.tasks.list_by_request(req.message_id)
    assert t1.status == "FAILED" and "invalid result" in t1.failure_reason
    assert t2.status == "FAILED" and t2.failure_reason == "dependency failed"
    assert t6.status == "FAILED" and t6.failure_reason == "dependency failed"
    assert t3.status == "COMPLETED" and out["status"] == "COMPLETED"
    assert deps.repos.messages.get("c1", req.message_id).status == "COMPLETED"


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


def test_report_constraints_keep_the_users_facts_with_provenance_and_drop_plan_rules() -> None:
    # Live finding L-2: the planner restated its own output rules as constraints, and the
    # report carried them into the next request as if the user had stated them.
    plan = six_task_plan()
    plan["constraints"] = [
        "The plan must include 2 to 6 tasks.",
        "Each task must have a clear goal, kind, and inputArtifactIds.",
        "Exclude transfers between the user's own accounts from spending.",
    ]
    deps = make_deps(
        ScriptedModel(
            {
                "plan": [
                    ModelResponse(structured=clarification("Which months?")),
                    ModelResponse(structured=plan),
                ],
                "execute": [ModelResponse(structured=t1_result())] * 3,
            }
        ),
        tasks_in_flight=1,
    )
    req = seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    status = run_answering_the_question(
        deps, req, "September and October", answered_at="2026-11-06T09:30:00Z"
    )
    assert status == "COMPLETED"
    report = next(m for m in deps.repos.messages.list_by_chat("c1") if m.kind == "report")
    kept = {c.text: (c.source, c.date) for c in report.report.constraints}
    assert kept["User answered 'Which months?': September and October"] == (
        "clarification",
        "2026-11-06",
    )
    assert kept["Exclude transfers between the user's own accounts from spending."][0] == "request"
    assert not [text for text in kept if "plan" in text.lower() or "task" in text.lower()]

# tests/test_workflow.py
import dataclasses
import threading

import pytest
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from contextforge.core.entities.message import ReportMessage
from contextforge.core.entities.model_call import Stage
from contextforge.core.models.base import ModelRequest, ModelResponse, ToolCall
from contextforge.core.models.errors import ModelError
from contextforge.core.models.scripted import ScriptedModel
from contextforge.core.tools.base import ToolContext
from contextforge.infra.bedrock import converse_payload
from contextforge.worker.dependencies import WorkerDependencies
from contextforge.worker.stages.execute import NO_CALLS_REMAIN
from contextforge.worker.stages.summary import unsupported_terms
from contextforge.worker.tools.sql_query import SqlQueryTool
from contextforge.worker.workflow import build_research_workflow
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


def _start(worker_dependencies, req) -> None:
    worker_dependencies.repositories.messages.update_request_status(
        "c1", req.message_id, "ACCEPTED", "RUNNING"
    )


def test_csv_sql_query_finds_planted_transfer_pairs() -> None:
    worker_dependencies = make_deps(ScriptedModel({}))
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    tool_context = ToolContext(
        chat_id="c1",
        user_id="u1",
        request_message_id=req.message_id,
        task_id="t1",
        tool_call_id="x1",
    )
    rows = (
        SqlQueryTool(worker_dependencies.repositories.artifacts, worker_dependencies.blobs)
        .run(
            {
                "dataset_id": "a2",
                "sql": "SELECT d.row_id AS debit_row, c.row_id AS credit_row, d.amount "
                "FROM t d JOIN t c "
                "ON d.amount = c.amount AND d.direction='DEBIT' AND c.direction='CREDIT' "
                "AND d.account_id <> c.account_id "
                "AND abs(date_diff('day', d.txn_date, c.txn_date)) <= 2",
            },
            tool_context,
        )
        .rows
    )
    assert rows is not None
    assert {(r["debit_row"], r["credit_row"]) for r in rows} >= {(12, 13), (40, 41), (50, 51)}
    # external access is off once the dataset is loaded: a SELECT cannot read host files
    leaked = SqlQueryTool(
        worker_dependencies.repositories.artifacts, worker_dependencies.blobs
    ).run({"dataset_id": "a2", "sql": "SELECT count(*) FROM read_text('/etc/hosts')"}, tool_context)
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
    worker_dependencies = make_deps(
        ScriptedModel(script), tasks_in_flight=1
    )  # serial, so the script order is deterministic
    requests: list[ModelRequest] = []
    worker_dependencies.model.on_request = requests.append  # type: ignore[attr-defined]
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    _start(worker_dependencies, req)
    out = build_research_workflow(worker_dependencies, checkpointer=None).invoke(
        {"chat_id": "c1", "message_id": req.message_id}, _cfg(req)
    )
    assert out["status"] == "COMPLETED"
    tasks = worker_dependencies.repositories.tasks.list_by_request(req.message_id)
    assert len(tasks) == 6
    assert sorted(t.status for t in tasks) == sorted(["COMPLETED"] * 4 + ["SKIPPED"] * 2)
    msgs = worker_dependencies.repositories.messages.list_by_chat("c1")
    report = [m for m in msgs if m.kind == "report"][0]
    assert report.report.claims and all(c.evidence_refs for c in report.report.claims)
    assert worker_dependencies.repositories.messages.get("c1", req.message_id).status == "COMPLETED"
    # Plan, four task calls, and the report's summary (unscripted, so it fails and falls back).
    stages = [
        c.stage
        for c in worker_dependencies.repositories.model_calls.list_by_request(req.message_id)
    ]
    assert len(stages) == 6 and stages[-1] == "summary"
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
    worker_dependencies = make_deps(
        model, tasks_in_flight=1
    )  # serial, so one task's turns are consecutive
    worker_dependencies.budgets = dataclasses.replace(
        worker_dependencies.budgets, cap_input_tokens=3000, response_allowance_tokens=500
    )
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    _start(worker_dependencies, req)
    out = build_research_workflow(worker_dependencies, checkpointer=None).invoke(
        {"chat_id": "c1", "message_id": req.message_id}, _cfg(req)
    )
    assert out["status"] == "COMPLETED"
    tasks = worker_dependencies.repositories.tasks.list_by_request(req.message_id)
    assert sorted(t.status for t in tasks) == sorted(["COMPLETED"] * 4 + ["SKIPPED"] * 2)
    report = [
        m
        for m in worker_dependencies.repositories.messages.list_by_chat("c1")
        if m.kind == "report"
    ][0]
    assert report.report.claims and all(c.evidence_refs for c in report.report.claims)
    calls = worker_dependencies.repositories.model_calls.list_by_request(req.message_id)
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
    worker_dependencies = make_deps(
        Pausing(
            {
                "plan": [ModelResponse(structured=plan)],
                "execute": [ModelResponse(structured=t1_result())] * 3,
            }
        ),
        tasks_in_flight=3,
    )
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    _start(worker_dependencies, req)
    out = build_research_workflow(worker_dependencies, checkpointer=None).invoke(
        {"chat_id": "c1", "message_id": req.message_id}, _cfg(req)
    )
    assert out["status"] == "COMPLETED" and gate.broken is False


def test_cancel_between_waves_stops_the_run() -> None:
    script = {
        "plan": [ModelResponse(structured=six_task_plan())],
        "execute": [ModelResponse(structured=t1_result())] * 3,
    }
    worker_dependencies = make_deps(ScriptedModel(script), tasks_in_flight=1)
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    # the API sets CANCELLED; here the hook fires right after the first task completes
    worker_dependencies.repositories.tasks.on_save = lambda t: (
        worker_dependencies.repositories.messages.update_request_status(  # type: ignore[attr-defined]
            "c1", req.message_id, "RUNNING", "CANCELLED"
        )
        if t.status == "COMPLETED" and t.kind == "sql_analysis"
        else None
    )
    _start(worker_dependencies, req)
    out = build_research_workflow(worker_dependencies, checkpointer=None).invoke(
        {"chat_id": "c1", "message_id": req.message_id}, _cfg(req)
    )
    assert out["status"] == "CANCELLED"
    assert worker_dependencies.repositories.messages.get("c1", req.message_id).status == "CANCELLED"
    assert not [
        m
        for m in worker_dependencies.repositories.messages.list_by_chat("c1")
        if m.kind == "report"
    ]


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
    worker_dependencies = make_deps(model, tasks_in_flight=1)
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    _start(worker_dependencies, req)
    out = build_research_workflow(worker_dependencies, checkpointer=None).invoke(
        {"chat_id": "c1", "message_id": req.message_id}, _cfg(req)
    )
    assert out["status"] == "COMPLETED"
    sql_tasks = [
        t
        for t in worker_dependencies.repositories.tasks.list_by_request(req.message_id)
        if t.kind == "sql_analysis"
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
    report = next(
        m
        for m in worker_dependencies.repositories.messages.list_by_chat("c1")
        if m.kind == "report"
    )
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
    worker_dependencies = make_deps(
        ScriptedModel(
            {
                "plan": [ModelResponse(structured=six_task_plan())],
                "execute": calls + [ModelResponse(structured=t1_result())] * 2,
            }
        ),
        tasks_in_flight=1,
    )
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    _start(worker_dependencies, req)
    out = build_research_workflow(worker_dependencies, checkpointer=None).invoke(
        {"chat_id": "c1", "message_id": req.message_id}, _cfg(req)
    )
    tasks = worker_dependencies.repositories.tasks.list_by_request(req.message_id)
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

        def run(self, arguments, tool_context):
            self.runs += 1
            raise RuntimeError("duckdb exploded")

    boom = ModelResponse(
        tool_calls=[
            ToolCall(id="x", name="sql_query", arguments={"dataset_id": "a2", "sql": "SELECT 1"})
        ],
        stop_reason="tool_use",
    )
    worker_dependencies = make_deps(
        ScriptedModel(
            {
                "plan": [ModelResponse(structured=six_task_plan())],
                "execute": [boom, ModelResponse(structured=t1_result())] * 3,
            }
        ),
        tasks_in_flight=1,
    )
    worker_dependencies.tools["sql_query"] = Boom()
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    _start(worker_dependencies, req)
    seen = []
    worker_dependencies.model.on_request = lambda r: seen.extend(
        m.content for m in r.messages if m.type == "tool"
    )  # type: ignore[attr-defined]
    out = build_research_workflow(worker_dependencies, checkpointer=None).invoke(
        {"chat_id": "c1", "message_id": req.message_id}, _cfg(req)
    )
    assert out["status"] == "COMPLETED" and any('"error_class": "system"' in c for c in seen)
    # a failed call is never answered "[already done]": repeating it runs the tool again
    retried = Boom()
    worker_dependencies = make_deps(
        ScriptedModel(
            {
                "plan": [ModelResponse(structured=six_task_plan())],
                "execute": [boom, boom] + [ModelResponse(structured=t1_result())] * 3,
            }
        ),
        tasks_in_flight=1,
    )
    worker_dependencies.tools["sql_query"] = retried
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    _start(worker_dependencies, req)
    out = build_research_workflow(worker_dependencies, checkpointer=None).invoke(
        {"chat_id": "c1", "message_id": req.message_id}, _cfg(req)
    )
    t1 = worker_dependencies.repositories.tasks.list_by_request(req.message_id)[0]
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
    worker_dependencies = make_deps(model, tasks_in_flight=1)
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    _start(worker_dependencies, req)
    build_research_workflow(worker_dependencies, checkpointer=None).invoke(
        {"chat_id": "c1", "message_id": req.message_id}, _cfg(req)
    )
    t1 = worker_dependencies.repositories.tasks.list_by_request(req.message_id)[0]
    assert (t1.status, t1.attempt) == ("COMPLETED", 1)
    # the duplicates never counted: one query read, then the no-progress limit ended the loop
    assert t1.result.uncertainties[-1] == "stopped after 3 turns without progress; 1 page read"
    turns = [r for r in requests if r.task_id == t1.task_id]
    assert [bool(r.tools) for r in turns] == [True] * 4 + [False]


def test_a_dependent_runs_on_what_completed_and_one_with_nothing_fails() -> None:
    # t1 returns an invalid result on both of its attempts; t3 needs no t1 and completes. t2
    # needs only t1, so it never runs (I-2); t6 needs t1, t2 and t3, so it runs (F7 ruling 2).
    invalid = ModelResponse(structured={})
    worker_dependencies = make_deps(
        ScriptedModel(
            {
                "plan": [ModelResponse(structured=six_task_plan())],
                "execute": [invalid, invalid, ModelResponse(structured=t1_result())],
            }
        ),
        tasks_in_flight=1,
    )
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    _start(worker_dependencies, req)
    out = build_research_workflow(worker_dependencies, checkpointer=None).invoke(
        {"chat_id": "c1", "message_id": req.message_id}, _cfg(req)
    )
    t1, t2, t3, _, _, t6 = worker_dependencies.repositories.tasks.list_by_request(req.message_id)
    assert t1.status == "FAILED" and "invalid result" in t1.failure_reason
    assert t2.status == "FAILED" and t2.failure_reason == "dependency failed"
    assert t3.status == "COMPLETED" and out["status"] == "COMPLETED"
    assert t6.status == "COMPLETED" and t6.result.uncertainties[1:] == [
        f"A dependency did not complete ({t1.failure_reason}); not covered: {t1.goal}",
        f"A dependency did not complete (dependency failed); not covered: {t2.goal}",
    ]
    assert worker_dependencies.repositories.messages.get("c1", req.message_id).status == "COMPLETED"


WEATHER = "Daily weather in Ajmer and Palanpur over the last two weeks."
WHEN_TO_VISIT = "Whether this is a good time to visit, from that weather."
DISTANCE = "Road distance between Ajmer and Palanpur."
TRAINS = "Trains between Ajmer and Palanpur."
ANSWER = "Combine the weather, distance and trains into one answer."


def _f7_plan() -> dict:
    """The plan of the live F7 run (7 Nov, Nova Pro) on the fixture chat."""
    return _sql_plan(
        "Weather, a good time to visit, distance and trains for Ajmer and Palanpur.",
        [
            ("t1", WEATHER, []),
            ("t2", WHEN_TO_VISIT, ["t1"]),
            ("t3", DISTANCE, []),
            ("t4", TRAINS, []),
            ("t5", ANSWER, ["t1", "t2", "t3", "t4"]),
        ],
    )


def _sql_plan(goal: str, tasks: list[tuple[str, str, list[str]]]) -> dict:
    """A plan of sql_analysis tasks over the fixture dataset, as (ref, goal, dependsOn)."""
    return {
        "kind": "plan",
        "goal": goal,
        "constraints": [],
        "openQuestions": [],
        "tasks": [
            {
                "ref": ref,
                "kind": "sql_analysis",
                "goal": goal,
                "inputArtifactIds": ["a2"],
                "dependsOn": depends_on,
            }
            for ref, goal, depends_on in tasks
        ],
        "nextAction": "Run the tasks in dependency order.",
    }


def _f7_result(fact: str, row: str, uncertainties: list[str] | None = None) -> ModelResponse:
    ref = {"artifactId": "a2", "locator": {"kind": "row", "value": row}}
    facts = [{"text": fact, "evidenceRefs": [ref]}]
    return ModelResponse(
        structured={"summary": fact, "facts": facts, "uncertainties": uncertainties or []}
    )


class _F7Agent:
    """The live F7 run's executor, by task goal: the weather task never stops calling tools,
    even when it may only return its result; distance, trains and the answer return facts."""

    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    def invoke(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if request.stage == "plan":
            return ModelResponse(structured=_f7_plan())
        if request.stage == "summary":
            raise ModelError("no summary scripted; the report builds its own")
        if self.goal_of(request) == WEATHER:
            turn = len(request.messages)
            sql = {"dataset_id": "a2", "sql": f"SELECT {turn}"}
            return ModelResponse(
                tool_calls=[ToolCall(id=f"w{turn}", name="sql_query", arguments=sql)]
            )
        return {
            DISTANCE: _f7_result("Ajmer to Palanpur is approximately 350+ kms.", "1"),
            TRAINS: _f7_result("Two trains a day run from Ajmer to Palanpur.", "2"),
            ANSWER: _f7_result(
                "Ajmer to Palanpur is approximately 350+ km.",
                "3",
                ["No weather data: the weather task did not complete."],
            ),
        }[self.goal_of(request)]

    @staticmethod
    def goal_of(request: ModelRequest) -> str:
        return str(request.messages[0].content).split("Task goal: ", 1)[1].split("\n", 1)[0]


def _run_f7() -> tuple[_F7Agent, WorkerDependencies, str]:
    """The live F7 run on the fixture chat: its agent, its worker_dependencies and the request
    id."""
    agent = _F7Agent()
    worker_dependencies = make_deps(agent)
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    _start(worker_dependencies, req)
    out = build_research_workflow(worker_dependencies, checkpointer=None).invoke(
        {"chat_id": "c1", "message_id": req.message_id}, _cfg(req)
    )
    assert out["status"] == "COMPLETED"
    return agent, worker_dependencies, req.message_id


def test_a_failed_task_leaves_a_stated_gap_not_an_unanswered_request() -> None:
    """F7 ruling 2 on the live run: weather failed at its tool budget, the visit task (needing
    only weather) never ran, and the combining task ran on distance and trains, told what is
    missing, instead of failing with "dependency failed"."""
    agent, worker_dependencies, request_id = _run_f7()
    tasks = {t.goal: t for t in worker_dependencies.repositories.tasks.list_by_request(request_id)}
    statuses = {goal: (t.status, t.failure_reason) for goal, t in tasks.items()}
    assert statuses == {
        WEATHER: ("FAILED", "tool call budget exhausted"),
        WHEN_TO_VISIT: ("FAILED", "dependency failed"),
        DISTANCE: ("COMPLETED", None),
        TRAINS: ("COMPLETED", None),
        ANSWER: ("COMPLETED", None),
    }
    executes = [r for r in agent.requests if r.stage == "execute"]
    (request_state,) = {str(r.messages[0].content) for r in executes if agent.goal_of(r) == ANSWER}
    dependencies = request_state.split("<dependencies>")[1].split("</dependencies>")[0]
    assert dependencies.splitlines()[1:] == [
        "- Ajmer to Palanpur is approximately 350+ kms.",
        "- Two trains a day run from Ajmer to Palanpur.",
        f"- A dependency did not complete (tool call budget exhausted); not covered: {WEATHER}",
        f"- A dependency did not complete (dependency failed); not covered: {WHEN_TO_VISIT}",
    ]
    report = next(
        m
        for m in worker_dependencies.repositories.messages.list_by_chat("c1")
        if m.kind == "report"
    )
    assert "No weather data: the weather task did not complete." in report.report.uncertainties


def test_the_report_leads_with_answers_then_names_what_was_not_answered() -> None:
    """F7 ruling 4 on the live run: the report gave distance and trains and left the user's
    actual question (the weather, when to visit) unmentioned outside the limitations. Each
    task without a result now names its goal as not answered, after the answers and before
    the limitations."""
    _, worker_dependencies, _ = _run_f7()
    report = next(
        m
        for m in worker_dependencies.repositories.messages.list_by_chat("c1")
        if m.kind == "report"
    )
    summary, counts, *paragraphs = report.content.split("\n\n")
    assert summary == (
        "Ajmer to Palanpur is approximately 350+ kms. Two trains a day run from Ajmer to Palanpur."
    )
    assert counts.startswith("2 findings, ")
    assert paragraphs[:4] == [
        "1. Ajmer to Palanpur is approximately 350+ kms.",
        "2. Two trains a day run from Ajmer to Palanpur.",
        f"Not answered: {WEATHER}",
        f"Not answered: {WHEN_TO_VISIT}",
    ]
    assert paragraphs[4].startswith("Limitations:\n") and len(paragraphs) == 5


AJMER = "Weather in Ajmer over the next two weeks."
PALANPUR = "Weather in Palanpur over the next two weeks."
VISIT = "Whether now is a good time to visit, comparing the two cities."
RAIL = "Trains between Ajmer and Palanpur."
AJMER_FACT = "Ajmer highs range from 63°F to 90°F."
PALANPUR_FACT = "Palanpur highs range from 86°F to 93°F."
VISIT_CONCLUSION = "Both cities suit a visit now; Ajmer is the milder of the two."


def _row(row: str, artifact_id: str = "a2") -> dict:
    return {"artifactId": artifact_id, "locator": {"kind": "row", "value": row}}


def _visit_result() -> ModelResponse:
    """The comparison task of the 7 Nov chat: it restates the two weather facts and puts its
    judgement in conclusions. Its first fact cites nothing it may read, so the fact goes, and
    the conclusion resting only on it goes with it, in the report's limitations."""
    facts = [
        {"text": "Fares start at 230.", "evidenceRefs": [_row("9", "elsewhere")]},
        {"text": AJMER_FACT, "evidenceRefs": [_row("1")]},
        {"text": PALANPUR_FACT, "evidenceRefs": [_row("2")]},
    ]
    conclusions = [
        {"text": VISIT_CONCLUSION, "supportingFacts": [1, 2], "confidence": "medium"},
        {"text": "Book the cheapest fare early.", "supportingFacts": [0]},
    ]
    return ModelResponse(
        structured={"summary": "Both suit a visit.", "facts": facts, "conclusions": conclusions}
    )


class _VisitAgent:
    """The 7 Nov chat on the fixture chat: weather in each city, a task judging the visit from
    both, and a trains task whose results are invalid, so it fails. The summary model writes
    ``summary``."""

    def __init__(self, summary: str) -> None:
        self.summary, self.requests = summary, []

    def invoke(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if request.stage == "summary":
            return ModelResponse(text=self.summary)
        if request.stage == "plan":
            plan = [
                ("t1", AJMER, []),
                ("t2", PALANPUR, []),
                ("t3", VISIT, ["t1", "t2"]),
                ("t4", RAIL, []),
            ]
            return ModelResponse(structured=_sql_plan("Ajmer and Palanpur visit.", plan))
        return {
            AJMER: _f7_result(AJMER_FACT, "1"),
            PALANPUR: _f7_result(PALANPUR_FACT, "2"),
            VISIT: _visit_result(),
            RAIL: ModelResponse(structured={}),
        }[_F7Agent.goal_of(request)]


def _visit_report(agent: _VisitAgent) -> tuple[ReportMessage, WorkerDependencies]:
    """The report of the 7 Nov chat's request, run serially with ``agent``, and its
    worker_dependencies."""
    worker_dependencies = make_deps(agent, tasks_in_flight=1)
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    _start(worker_dependencies, req)
    out = build_research_workflow(worker_dependencies, checkpointer=None).invoke(
        {"chat_id": "c1", "message_id": req.message_id}, _cfg(req)
    )
    assert out["status"] == "COMPLETED"
    report = next(
        m
        for m in worker_dependencies.repositories.messages.list_by_chat("c1")
        if m.kind == "report"
    )
    return report, worker_dependencies


SUMMARY = (
    "Ajmer highs range from 63°F to 90°F, and Palanpur highs from 86°F to 93°F. Both cities "
    "suit a visit now, Ajmer being the milder. Trains were not answered."
)


def test_a_judgement_is_a_conclusion_under_assessment_linked_to_its_claims() -> None:
    """F10 rulings 1 and 3: the comparison task's judgement had no excerpt to cite and was
    dropped. It is now a conclusion resting on the claims it compares, shown under Assessment
    after the answers and before what was not answered, never with a source link."""
    report, _ = _visit_report(_VisitAgent(SUMMARY))
    ajmer, palanpur = report.report.claims
    (conclusion,) = report.report.conclusions
    assert (ajmer.text, palanpur.text) == (AJMER_FACT, PALANPUR_FACT)
    assert conclusion.text == VISIT_CONCLUSION and conclusion.confidence == "medium"
    assert conclusion.based_on == [ajmer.claim_id, palanpur.claim_id]
    paragraphs = report.content.split("\n\n")[2:]  # after the summary and its counts
    assert paragraphs[:4] == [
        f"1. {AJMER_FACT}",
        f"2. {PALANPUR_FACT}",
        f"Assessment:\n- {VISIT_CONCLUSION} (based on findings 1 and 2; medium confidence)",
        f"Not answered: {RAIL}",
    ]
    assert paragraphs[4].startswith("Limitations:\n")
    assert (
        "unsupported conclusion, no finding it rests on held: Book the cheapest fare early."
        in report.report.uncertainties
    )


def test_the_summary_covers_every_subject_and_the_assessment_from_one_cheap_call() -> None:
    """F10 ruling 2: the chat said only "Ajmer has experienced temperatures ranging from 63°F
    to 90°F" for a request about two cities. The report now opens with a summary of every
    answered part and the assessment, written by one summary call on the cheap model that
    reads the report's claims, conclusions and gaps, never page text."""
    agent = _VisitAgent(SUMMARY)
    report, worker_dependencies = _visit_report(agent)
    summary, counts, first = report.content.split("\n\n")[:3]
    assert (summary, counts, first) == (SUMMARY, "2 findings, 2 limitations.", f"1. {AJMER_FACT}")
    (call,) = [r for r in agent.requests if r.stage == "summary"]
    sent = str(call.messages[0].content)
    assert call.model == worker_dependencies.model_id(Stage.SUMMARY) and call.task_id is None
    assert AJMER_FACT in sent and VISIT_CONCLUSION in sent and f"Not answered: {RAIL}" in sent
    recorded = worker_dependencies.repositories.model_calls.list_by_request(
        report.request_message_id
    )
    assert [(c.stage, c.outcome) for c in recorded][-1] == ("summary", "COMPLETED")
    assert recorded[-1].context_version.endswith("|prompt:summary.1")


def test_a_summary_naming_what_the_report_does_not_is_replaced_by_its_claims() -> None:
    """F10 ruling 2: the summary model's text is checked against the report; a figure or a
    name the report does not hold means the summary is built from the first claim of each
    task, in plan order, and the first conclusion instead."""
    invented = "Ajmer highs reach 95°F, hotter than Jaipur. Both cities suit a visit now."
    report, _ = _visit_report(_VisitAgent(invented))
    sources = [*(c.text for c in report.report.claims), VISIT_CONCLUSION]
    assert unsupported_terms(invented, sources) == ["95", "Jaipur"]
    assert unsupported_terms(SUMMARY, [*sources, f"Not answered: {RAIL}"]) == []
    assert report.content.split("\n\n")[0] == f"{AJMER_FACT} {PALANPUR_FACT} {VISIT_CONCLUSION}"


def test_scripted_exhaustion_fails_request_with_reason() -> None:
    worker_dependencies = make_deps(
        ScriptedModel({"plan": [ModelResponse(structured=six_task_plan())]})
    )
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    _start(worker_dependencies, req)
    out = build_research_workflow(worker_dependencies, checkpointer=None).invoke(
        {"chat_id": "c1", "message_id": req.message_id}, _cfg(req)
    )
    m = worker_dependencies.repositories.messages.get("c1", req.message_id)
    assert out["status"] == "FAILED" and m.status == "FAILED" and m.failure_reason
    assert any(
        c.outcome == "ERROR"
        for c in worker_dependencies.repositories.model_calls.list_by_request(req.message_id)
    )


def test_report_constraints_keep_the_users_facts_with_provenance_and_drop_plan_rules() -> None:
    # Live finding L-2: the planner restated its own output rules as constraints, and the
    # report carried them into the next request as if the user had stated them.
    plan = six_task_plan()
    # F9, bake-off: gpt-oss and MiniMax copied the engine's own instruction.
    leaked = ["Do not ask another question", "Never ask for an upload to answer a web question."]
    plan["constraints"] = [
        "The plan must include 2 to 6 tasks.",
        "Each task must have a clear goal, kind, and inputArtifactIds.",
        "Exclude transfers between the user's own accounts from spending.",
        *leaked,
    ]
    worker_dependencies = make_deps(
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
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    status = run_answering_the_question(
        worker_dependencies, req, "September and October", answered_at="2026-11-06T09:30:00Z"
    )
    assert status == "COMPLETED"
    report = next(
        m
        for m in worker_dependencies.repositories.messages.list_by_chat("c1")
        if m.kind == "report"
    )
    kept = {c.text: (c.source, c.date) for c in report.report.constraints}
    assert kept["User answered 'Which months?': September and October"] == (
        "clarification",
        "2026-11-06",
    )
    assert kept["Exclude transfers between the user's own accounts from spending."][0] == "request"
    assert not [text for text in kept if "plan" in text.lower() or "task" in text.lower()]
    assert not set(leaked) & set(kept)


def _briefed_plan() -> dict:
    """A one-researcher plan with a brief and a full researcher spec (F16)."""
    return {
        "kind": "plan",
        "goal": "Find the own-account transfers.",
        "brief": "One part: the transfers between the user's accounts. Simple fact: 1.",
        "tasks": [
            {
                "ref": "t1",
                "kind": "sql_analysis",
                "goal": "Transfers between own accounts.",
                "objective": "List each transfer pair so spending can exclude it.",
                "outputFormat": "Pairs of row ids with amount and date.",
                "toolsAndSources": "sql_query over a2 only.",
                "boundaries": "Transfers only; card bills are out of scope.",
                "inputArtifactIds": ["a2"],
            }
        ],
        "nextAction": "Run t1.",
    }


def test_the_lead_brief_is_kept_and_each_researcher_gets_its_full_spec() -> None:
    # F16 commit 1: effort scaling allows one researcher; the brief is saved on the request
    # record; the researcher's call carries objective, output format, sources and boundaries.
    worker_dependencies = make_deps(
        ScriptedModel(
            {
                "plan": [ModelResponse(structured=_briefed_plan())],
                "execute": [ModelResponse(structured=t1_result())],
            }
        )
    )
    requests: list[ModelRequest] = []
    worker_dependencies.model.on_request = requests.append  # type: ignore[attr-defined]
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    _start(worker_dependencies, req)
    out = build_research_workflow(worker_dependencies, checkpointer=None).invoke(
        {"chat_id": "c1", "message_id": req.message_id}, _cfg(req)
    )
    assert out["status"] == "COMPLETED"
    stored = worker_dependencies.repositories.messages.get("c1", req.message_id)
    assert stored.brief == _briefed_plan()["brief"]
    (task,) = worker_dependencies.repositories.tasks.list_by_request(req.message_id)
    assert task.boundaries == "Transfers only; card bills are out of scope."
    plan_call = next(r for r in requests if r.stage == "plan")
    # the configured round ceiling is stated; the attribution comment is never sent
    assert "up to 5 researchers" in plan_call.system[-1] and "<!--" not in plan_call.system[-1]
    # One research round (the default here) is the flow before the lead loop: no lead call.
    stages = [
        c.stage
        for c in worker_dependencies.repositories.model_calls.list_by_request(req.message_id)
    ]
    assert stages == ["plan", "execute", "summary"]
    assert [a["text"] for _, a in worker_dependencies.activity.since(req.message_id, 0)] == [
        "Brief written"
    ]
    execute_text = next(r for r in requests if r.stage == "execute").messages[0].content
    for line in (
        "Task goal: Transfers between own accounts.",
        "Task objective: List each transfer pair so spending can exclude it.",
        "Output format: Pairs of row ids with amount and date.",
        "Tools and sources: sql_query over a2 only.",
        "Boundaries: Transfers only; card bills are out of scope.",
    ):
        assert line in execute_text


def _one_fact_result(index: int) -> dict:
    """``t1_result`` keeping only its fact ``index``: each round finds a different fact."""
    result = t1_result()
    return {**result, "facts": [result["facts"][index]], "evidenceRefs": []}


def _follow_up(ref: str, objective: str) -> dict:
    return {
        "ref": ref,
        "kind": "sql_analysis",
        "goal": "Card bill settlements.",
        "objective": objective,
        "outputFormat": "Row pairs with amounts.",
        "toolsAndSources": "sql_query over a2.",
        "boundaries": "Settlements only; transfers are done.",
        "inputArtifactIds": ["a2"],
    }


def _lead(action: str, reason: str, tasks: list | None = None) -> ModelResponse:
    return ModelResponse(
        structured={"action": action, "reason": reason, "brief": None, "tasks": tasks or []}
    )


def _run(worker_dependencies: WorkerDependencies, req) -> str:
    _start(worker_dependencies, req)
    return build_research_workflow(worker_dependencies, checkpointer=None).invoke(
        {"chat_id": "c1", "message_id": req.message_id}, _cfg(req)
    )["status"]


def test_the_lead_starts_a_second_round_and_the_report_covers_both() -> None:
    # Lead review after round 1: one follow-up repeats t1's objective (dropped in code), the
    # other runs in round 2; the review after round 2 completes. Cite and report see both.
    duplicate = _follow_up("f1", "LIST each transfer pair, so spending can exclude it!")
    new = _follow_up("f2", "Find the card bill settlement pairs.")
    worker_dependencies = make_deps(
        ScriptedModel(
            {
                "plan": [ModelResponse(structured=_briefed_plan())],
                "execute": [
                    ModelResponse(structured=_one_fact_result(0)),
                    ModelResponse(structured=_one_fact_result(1)),
                ],
                "lead": [
                    _lead("run_subagents", "Settlements are not covered.", [duplicate, new]),
                    _lead("complete_task", "Transfers and settlements are both found."),
                ],
            }
        ),
        research_rounds=3,
    )
    requests: list[ModelRequest] = []
    worker_dependencies.model.on_request = requests.append  # type: ignore[attr-defined]
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    assert _run(worker_dependencies, req) == "COMPLETED"
    tasks = worker_dependencies.repositories.tasks.list_by_request(req.message_id)
    assert [(t.round, t.status) for t in tasks] == [(1, "COMPLETED"), (2, "COMPLETED")]
    assert tasks[1].objective == "Find the card bill settlement pairs."
    report = next(
        m
        for m in worker_dependencies.repositories.messages.list_by_chat("c1")
        if isinstance(m, ReportMessage)
    )
    texts = " ".join(c.text for c in report.report.claims)
    assert "Rows 12/13" in texts and "Rows 50/51" in texts
    # The second review reads both rounds' condensed findings, never pages or excerpts.
    second_review = [r for r in requests if r.stage == "lead"][1].messages[0].content
    assert "Rows 12/13" in second_review and "Rows 50/51" in second_review
    assert "searchesRun" in second_review and "excerpt" not in second_review
    assert [a["text"] for _, a in worker_dependencies.activity.since(req.message_id, 0)] == [
        "Brief written",
        "Round 2: 1 more researcher",
        "Lead: enough evidence — Transfers and settlements are both found.",
    ]


def test_an_invalid_lead_twice_completes_with_the_findings_so_far() -> None:
    no_tasks = _lead("run_subagents", "More is needed.")  # run_subagents needs tasks
    worker_dependencies = make_deps(
        ScriptedModel(
            {
                "plan": [ModelResponse(structured=_briefed_plan())],
                "execute": [ModelResponse(structured=t1_result())],
                "lead": [no_tasks, no_tasks],
            }
        ),
        research_rounds=3,
    )
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    assert _run(worker_dependencies, req) == "COMPLETED"
    assert len(worker_dependencies.repositories.tasks.list_by_request(req.message_id)) == 1
    calls = worker_dependencies.repositories.model_calls.list_by_request(req.message_id)
    assert [c.stage for c in calls].count("lead") == 2
    assert "Research stopped: lead output invalid" in [
        a["text"] for _, a in worker_dependencies.activity.since(req.message_id, 0)
    ]


@pytest.mark.parametrize(
    ("limit", "stopped"),
    [
        ({"tasks_per_request": 1}, "tasks per request reached"),
        # plan and one researcher spent 2 of 10: no room for the lead, a researcher and the
        # report's calls
        ({"call_budget": 10}, "model-call budget reached"),
    ],
)
def test_caps_stop_the_loop_before_the_lead_is_called(limit, stopped) -> None:
    worker_dependencies = make_deps(
        ScriptedModel(
            {
                "plan": [ModelResponse(structured=_briefed_plan())],
                "execute": [ModelResponse(structured=t1_result())],
                "lead": [_lead("run_subagents", "x", [_follow_up("f1", "Settlements.")])],
            }
        ),
        research_rounds=3,
    )
    worker_dependencies.budgets = dataclasses.replace(worker_dependencies.budgets, **limit)
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    assert _run(worker_dependencies, req) == "COMPLETED"
    calls = worker_dependencies.repositories.model_calls.list_by_request(req.message_id)
    assert "lead" not in [c.stage for c in calls]
    assert len(worker_dependencies.repositories.tasks.list_by_request(req.message_id)) == 1
    assert f"Research stopped: {stopped}" in [
        a["text"] for _, a in worker_dependencies.activity.since(req.message_id, 0)
    ]

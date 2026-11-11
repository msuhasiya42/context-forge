# tests/test_plan_stage.py
import json
from datetime import date

import pytest

from contextforge.core.entities.model_call import Stage
from contextforge.core.entities.user import User
from contextforge.core.models.base import ModelResponse
from contextforge.core.models.config import ModelRoute
from contextforge.core.models.scripted import ScriptedModel
from contextforge.core.planner import (
    PlannedTask,
    PlannerOutput,
    read_plan_leniently,
    with_inferred_dependencies,
)
from contextforge.core.services.memory import memory_snapshot
from contextforge.worker.stages.guards import Cancelled
from contextforge.worker.stages.plan import (
    PlanFailed,
    clarification_id,
    create_research_plan,
    validate_and_save_tasks,
)
from tests.helpers import (
    ByModel,
    clarification,
    create_initial_request_state,
    make_deps,
    seed_chat_with_dataset,
)


def _plan_research_tasks(ids: list[str]) -> dict:
    return {
        "kind": "plan",
        "goal": "g",
        "constraints": ["Exclude transfers"],
        "openQuestions": ["q"],
        "tasks": [
            {
                "ref": "t1",
                "kind": "sql_analysis",
                "goal": "g1",
                "inputArtifactIds": ids,
                "dependsOn": [],
            },
            {
                "ref": "t2",
                "kind": "reconcile",
                "goal": "g2",
                "inputArtifactIds": [],
                "dependsOn": ["t1"],
            },
        ],
        "nextAction": "run t1",
    }


def _clarification(q: str) -> dict:
    return {
        "kind": "clarification",
        "goal": "g",
        "constraints": [],
        "openQuestions": [],
        "question": q,
        "nextAction": "wait",
    }


def test_plan_then_gate_creates_tasks_in_order() -> None:
    worker_dependencies = make_deps(
        ScriptedModel({"plan": [ModelResponse(structured=_plan_research_tasks(["a2"]))]})
    )
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    request_state = create_initial_request_state(req)
    planned = create_research_plan(request_state, 0, worker_dependencies, today=date(2026, 11, 7))
    assert (
        planned["kind"] == "plan"
        and planned["constraints"] == ["Exclude transfers"]
        and planned["next_action"] == "run t1"
    )
    # the fixture request names September and October; the resolver's window is carried on
    assert planned["period"] == {"start": "2026-09-01", "end": "2026-10-31"}
    entries = validate_and_save_tasks(request_state, planned, worker_dependencies)
    tasks = worker_dependencies.repositories.tasks.list_by_request(req.message_id)
    assert [t.kind for t in tasks] == ["sql_analysis", "reconcile"]
    assert tasks[1].depends_on_task_ids == [tasks[0].task_id]
    assert [e["task_id"] for e in entries] == [t.task_id for t in tasks]
    assert (
        validate_and_save_tasks(request_state, planned, worker_dependencies) == entries
    )  # re-run creates nothing new
    assert len(worker_dependencies.repositories.tasks.list_by_request(req.message_id)) == 2


def test_gate_rejects_artifact_from_another_chat() -> None:
    worker_dependencies = make_deps(
        ScriptedModel({"plan": [ModelResponse(structured=_plan_research_tasks(["a9"]))]})
    )
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    seed_chat_with_dataset(worker_dependencies, chat_id="c2", user_id="u1", dataset_id="a9")
    planned = create_research_plan(create_initial_request_state(req), 0, worker_dependencies)
    with pytest.raises(PlanFailed) as e:
        validate_and_save_tasks(create_initial_request_state(req), planned, worker_dependencies)
    assert (
        "a9" in e.value.reason
        and worker_dependencies.repositories.tasks.list_by_request(req.message_id) == []
    )


def test_gate_reads_a_task_ref_in_inputs_as_a_dependency_and_rejects_unknown_ids() -> None:
    # live (Gemini): the synthesis task listed t1 as an input artifact instead of in dependsOn
    worker_dependencies = make_deps(ScriptedModel({}))
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    request_state = create_initial_request_state(req)

    def plan(t2_inputs: list[str]) -> dict:
        tasks = _plan_research_tasks(["a2"])["tasks"]
        tasks[1] = {**tasks[1], "inputArtifactIds": t2_inputs, "dependsOn": []}
        return {"tasks": [PlannedTask.model_validate(task).model_dump() for task in tasks]}

    with pytest.raises(PlanFailed, match="zz"):
        validate_and_save_tasks(request_state, plan(["t1", "zz"]), worker_dependencies)
    assert worker_dependencies.repositories.tasks.list_by_request(req.message_id) == []
    validate_and_save_tasks(request_state, plan(["a2", "t1", "t1"]), worker_dependencies)
    first, second = worker_dependencies.repositories.tasks.list_by_request(req.message_id)
    assert second.input_artifact_ids == ["a2"]
    assert second.depends_on_task_ids == [first.task_id]


def test_clarification_writes_one_message_and_pending_payload() -> None:
    worker_dependencies = make_deps(
        ScriptedModel(
            {"plan": [ModelResponse(structured=_clarification("Which accounts are yours?"))] * 3}
        )
    )
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    worker_dependencies.repositories.messages.update_request_status(
        "c1", req.message_id, "ACCEPTED", "RUNNING"
    )
    first = create_research_plan(create_initial_request_state(req), 0, worker_dependencies)
    assert first["kind"] == "clarification" and first["pending"]["kind"] == "clarification"
    assert (
        first["pending"]["question"] == "Which accounts are yours?"
        and first["pending"]["next_action"] == "wait"
    )
    assert (
        worker_dependencies.repositories.messages.get("c1", req.message_id).status == "WAITING_USER"
    )
    worker_dependencies.repositories.messages.update_request_status(
        "c1", req.message_id, "WAITING_USER", "RUNNING"
    )
    second = create_research_plan(
        create_initial_request_state(req), 0, worker_dependencies
    )  # same attempt re-run = same message id
    assert second["pending"]["message_id"] == first["pending"]["message_id"]
    assert (
        len(
            [
                m
                for m in worker_dependencies.repositories.messages.list_by_chat("c1")
                if m.kind == "clarification"
            ]
        )
        == 1
    )
    # A cancel that lands while the planner is answering stops the run before any question is
    # written for that attempt.
    worker_dependencies.repositories.messages.update_request_status(
        "c1", req.message_id, "WAITING_USER", "RUNNING"
    )
    worker_dependencies.model.on_request = lambda _: (
        worker_dependencies.repositories.messages.update_request_status(  # type: ignore[attr-defined]
            "c1", req.message_id, "RUNNING", "CANCELLED"
        )
    )
    with pytest.raises(Cancelled):
        create_research_plan(create_initial_request_state(req), 1, worker_dependencies)
    assert (
        worker_dependencies.repositories.messages.get("c1", clarification_id(req.message_id, 1))
        is None
    )


def test_third_question_fails_the_plan() -> None:
    worker_dependencies = make_deps(
        ScriptedModel({"plan": [ModelResponse(structured=_clarification("Again?"))]})
    )
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    with pytest.raises(PlanFailed):
        create_research_plan(create_initial_request_state(req), 2, worker_dependencies)


def test_malformed_planner_output_fails_plainly() -> None:
    worker_dependencies = make_deps(
        ScriptedModel({"plan": [ModelResponse(structured={"kind": "plan"})]})
    )
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    with pytest.raises(PlanFailed):
        create_research_plan(create_initial_request_state(req), 0, worker_dependencies)


def test_planner_is_retried_once_with_its_errors() -> None:
    worker_dependencies = make_deps(
        ScriptedModel(
            {
                "plan": [
                    ModelResponse(structured={"kind": "plan"}),
                    ModelResponse(structured=_plan_research_tasks(["a2"])),
                ]
            }
        )
    )
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    planned = create_research_plan(create_initial_request_state(req), 0, worker_dependencies)
    assert (
        planned["kind"] == "plan"
        and len(worker_dependencies.repositories.model_calls.list_by_request(req.message_id)) == 2
    )


def test_a_rejected_plan_is_retried_on_the_same_model_not_routed() -> None:
    """F9: a schema failure is the planner's own retry; only availability routes a call."""
    model = ByModel(
        {
            "p": [
                ModelResponse(structured={"kind": "plan"}),
                ModelResponse(structured=_plan_research_tasks(["a2"])),
            ]
        }
    )
    worker_dependencies = make_deps(model)
    worker_dependencies.routes = {Stage.PLAN: ModelRoute("p", ("f",))}
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    assert (
        create_research_plan(create_initial_request_state(req), 0, worker_dependencies)["kind"]
        == "plan"
    )
    assert model.sent == ["p", "p"]


def test_a_plan_with_an_empty_question_array_runs_without_a_retry() -> None:
    """Live GPT-OSS returned question: [] on both otherwise valid plan attempts."""
    answer = {**_plan_research_tasks(["a2"]), "question": []}
    worker_dependencies = make_deps(ScriptedModel({"plan": [ModelResponse(structured=answer)] * 2}))
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    request_state = create_initial_request_state(req)
    planned = create_research_plan(request_state, 0, worker_dependencies)
    assert len(validate_and_save_tasks(request_state, planned, worker_dependencies)) == 2
    [call] = worker_dependencies.repositories.model_calls.list_by_request(req.message_id)
    assert call.metadata["lenient_fixes"] == ["plan question sent as an empty array"]
    assert answer["question"] == []  # Keep the recorded model output intact.

    # The same malformed question must never become a clarification to the user.
    invalid = {**_clarification("Which accounts?"), "question": []}
    worker_dependencies = make_deps(
        ScriptedModel({"plan": [ModelResponse(structured=invalid)] * 2})
    )
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    with pytest.raises(PlanFailed, match="question"):
        create_research_plan(create_initial_request_state(req), 0, worker_dependencies)


def test_the_slips_of_capable_planners_are_read_leniently_and_recorded() -> None:
    """F9, bake-off: DeepSeek v3.2 sent ``tasks`` as a JSON string, Mistral Large 3 and Nemotron
    left out the plan's ``kind``; a task without a kind is inferred from what it reads."""
    tasks = [
        {"ref": "t1", "goal": "Total by month.", "inputArtifactIds": ["a2"], "dependsOn": []},
        {"ref": "t2", "goal": "Rates news.", "inputArtifactIds": [], "dependsOn": ["t1"]},
    ]
    answer = {**_plan_research_tasks(["a2"]), "tasks": json.dumps(tasks)}
    del answer["kind"]
    worker_dependencies = make_deps(ScriptedModel({"plan": [ModelResponse(structured=answer)]}))
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    planned = create_research_plan(create_initial_request_state(req), 0, worker_dependencies)
    assert [t["kind"] for t in planned["tasks"]] == ["sql_analysis", "web_search"]
    [call] = worker_dependencies.repositories.model_calls.list_by_request(req.message_id)
    assert call.metadata["lenient_fixes"] == [
        "tasks sent as a JSON string",
        "kind missing: plan",
        "t1 kind missing: sql_analysis",
        "t2 kind missing: web_search",
    ]
    upload = {"tasks": [{"ref": "t1", "goal": "g", "inputArtifactIds": ["u1"]}]}
    assert read_plan_leniently(upload, {"a2"})[0]["tasks"][0] == upload["tasks"][0]  # rejected


def test_a_task_that_works_on_earlier_results_is_made_to_depend_on_them() -> None:
    """F9, bake-off: Kimi K3.1 sent no dependsOn, and its last task said "using t1–t3", so it
    would have run beside the searches it needs. Named refs, else every earlier task."""
    goals = ["Pune.", "Hyderabad.", "Incentives.", "Cross-check using t1–t3; draft the memo."]
    tasks = [
        {"ref": f"t{n}", "kind": "web_search", "goal": goal, "inputArtifactIds": []}
        for n, goal in enumerate(goals, start=1)
    ]
    answer = {**_plan_research_tasks([]), "tasks": tasks}
    worker_dependencies = make_deps(ScriptedModel({"plan": [ModelResponse(structured=answer)]}))
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    planned = create_research_plan(create_initial_request_state(req), 0, worker_dependencies)
    assert [t["depends_on"] for t in planned["tasks"]] == [[], [], [], ["t1", "t2", "t3"]]
    [call] = worker_dependencies.repositories.model_calls.list_by_request(req.message_id)
    assert call.metadata["lenient_fixes"] == ["t4 depends on t1, t2, t3"]

    unnamed = {**answer, "tasks": [*tasks[:2], {**tasks[3], "goal": "Combine both."}]}
    inferred, _ = with_inferred_dependencies(PlannerOutput.model_validate(unnamed))
    assert inferred.tasks is not None and inferred.tasks[2].depends_on == ["t1", "t2"]
    forward = {**unnamed, "tasks": [{**tasks[0], "dependsOn": ["t2"]}, {**tasks[1], "goal": "t1"}]}
    output = PlannerOutput.model_validate(forward)
    assert with_inferred_dependencies(output) == (output, [])  # it would close a cycle


@pytest.mark.parametrize(
    ("text", "window"),
    [
        ("Compare October spending with September", ("2026-09-01", "2026-10-31")),
        ("What changed last month?", ("2026-10-01", "2026-10-31")),
        ("Totals over the last 3 months", ("2026-08-01", "2026-10-31")),
        # Relative windows resolve from today, Saturday 7 November 2026 (F5: the planner
        # asked the user for the period of "this week").
        ("Compare the US and Indian stock markets this week.", ("2026-11-02", "2026-11-07")),
        ("How did markets do last week?", ("2026-10-26", "2026-11-01")),
        ("What moved the Nifty over the past week?", ("2026-11-01", "2026-11-07")),
        ("News from the past 3 days", ("2026-11-05", "2026-11-07")),
        ("What happened today?", ("2026-11-07", "2026-11-07")),
        ("And yesterday?", ("2026-11-06", "2026-11-06")),
        ("Compare the US and Indian stock markets.", (None, None)),
    ],
)
def test_resolver_turns_the_named_window_into_dates(text, window) -> None:
    from datetime import date

    from contextforge.core.context.resolver import resolve_context

    r = resolve_context(text, [], today=date(2026, 11, 7), timezone="Asia/Kolkata")
    assert (r.period_start, r.period_end) == window and r.today == "2026-11-07"


def test_call_budget_stops_the_planner() -> None:
    worker_dependencies = make_deps(
        ScriptedModel({"plan": [ModelResponse(structured=_plan_research_tasks(["a2"]))]}),
        call_budget=0,
    )
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    with pytest.raises(PlanFailed) as e:
        create_research_plan(create_initial_request_state(req), 0, worker_dependencies)
    assert "budget" in e.value.reason


def test_planner_runs_on_the_briefs_memory_snapshot() -> None:
    worker_dependencies = make_deps(
        ScriptedModel({"plan": [ModelResponse(structured=_plan_research_tasks(["a2"]))]})
    )
    seen = []
    worker_dependencies.model.on_request = seen.append  # type: ignore[attr-defined]
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    user = User(
        user_id="u1",
        email="local@example.invalid",
        instructions="Quote every figure in INR.",
        preferences={"timezone": "Europe/London"},
        status="ACTIVE",
        created_at="t",
        updated_at="t",
    )
    memory = memory_snapshot(user)
    request_state = {**create_initial_request_state(req), "memory": memory}
    create_research_plan(request_state, 0, worker_dependencies)
    assert "Quote every figure in INR." in seen[0].system[0]
    assert (
        "today: " in seen[0].messages[0].content
        and "(Europe/London)" in seen[0].messages[0].content
    )
    [call] = worker_dependencies.repositories.model_calls.list_by_request(req.message_id)
    assert call.context_version.startswith(f"mem:{memory['version']}|")


def test_a_web_question_is_never_asked_about_and_a_follow_up_sees_the_earlier_question():
    """Live: in a chat with no uploads the planner asked for the period of "this week", and the
    follow-up "Try again." was planned without knowing what to try again. A web question is
    planned on its most likely reading; prior context holds the earlier request and report."""
    from contextforge.core.entities.chat import Chat
    from contextforge.core.entities.message import ReportBody, ReportMessage, RequestMessage
    from contextforge.worker.stages.plan import NO_MORE_QUESTIONS, NO_PERIOD, NOT_ASKED

    web_plan = {
        **_plan_research_tasks([]),
        "tasks": [
            {"ref": "t1", "kind": "web_search", "goal": "US", "inputArtifactIds": []},
            {"ref": "t2", "kind": "web_search", "goal": "India", "inputArtifactIds": []},
        ],
    }
    model = ScriptedModel(
        {
            "plan": [
                ModelResponse(structured=clarification("For which period?")),
                ModelResponse(structured=web_plan),
            ]
        }
    )
    seen: list = []
    model.on_request = seen.append
    worker_dependencies = make_deps(model)
    earlier, later = "2026-11-07T10:00:00Z", "2026-11-07T11:00:00Z"
    worker_dependencies.repositories.chats.save(
        Chat(
            chat_id="c1",
            user_id="u1",
            title="t",
            status="ACTIVE",
            created_at=earlier,
            updated_at=earlier,
        )
    )
    fields = {"chat_id": "c1", "user_id": "u1", "updated_at": earlier}
    question = "Compare the US stock market and the Indian stock market this week."
    for message in (
        RequestMessage(
            message_id="r0",
            sender="user",
            kind="request",
            content=question,
            status="COMPLETED",
            created_at=earlier,
            **fields,
        ),
        ReportMessage(
            message_id="p0",
            sender="assistant",
            kind="report",
            content="Sensex rose.",
            request_message_id="r0",
            created_at=earlier,
            report=ReportBody(claims=[], inputs_consulted=[], uncertainties=[], coverage={}),
            **fields,
        ),
        RequestMessage(
            message_id="r1",
            sender="user",
            kind="request",
            content="Try again.",
            status="RUNNING",
            created_at=later,
            **fields,
        ),
    ):
        worker_dependencies.repositories.messages.save(message)
    request_state = create_initial_request_state(
        worker_dependencies.repositories.messages.get("c1", "r1")
    )

    planned = create_research_plan(request_state, 0, worker_dependencies)
    assert planned["kind"] == "plan" and planned["pending"] is None
    assert planned["assumption"] == NOT_ASKED.format(question="For which period?")
    first = seen[0].messages[0].content
    assert f"period: {NO_PERIOD}" in first and NO_MORE_QUESTIONS in first
    assert f"request: {question}" in first.split("<prior_context>")[1]
    assert worker_dependencies.repositories.messages.get("c1", "r1").status == "RUNNING"

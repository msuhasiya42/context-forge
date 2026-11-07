# tests/test_plan_stage.py
import pytest

from contextforge.core.models.base import ModelResponse
from contextforge.core.models.scripted import ScriptedModel
from contextforge.worker.stages.plan import PlanFailed, plan_gate, plan_stage
from tests.helpers import initial_brief, make_deps, seed_chat_with_dataset  # written in this task


def _plan(ids: list[str]) -> dict:
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
    deps = make_deps(ScriptedModel({"plan": [ModelResponse(structured=_plan(["a2"]))]}))
    req = seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    brief = initial_brief(req)
    planned = plan_stage(brief, 0, deps)
    assert (
        planned["kind"] == "plan"
        and planned["constraints"] == ["Exclude transfers"]
        and planned["next_action"] == "run t1"
    )
    entries = plan_gate(brief, planned, deps)
    tasks = deps.repos.tasks.list_by_request(req.message_id)
    assert [t.kind for t in tasks] == ["sql_analysis", "reconcile"]
    assert tasks[1].depends_on_task_ids == [tasks[0].task_id]
    assert [e["task_id"] for e in entries] == [t.task_id for t in tasks]
    assert plan_gate(brief, planned, deps) == entries  # re-run creates nothing new
    assert len(deps.repos.tasks.list_by_request(req.message_id)) == 2


def test_gate_rejects_artifact_from_another_chat() -> None:
    deps = make_deps(ScriptedModel({"plan": [ModelResponse(structured=_plan(["a9"]))]}))
    req = seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    seed_chat_with_dataset(deps, chat_id="c2", user_id="u1", dataset_id="a9")
    planned = plan_stage(initial_brief(req), 0, deps)
    with pytest.raises(PlanFailed) as e:
        plan_gate(initial_brief(req), planned, deps)
    assert "a9" in e.value.reason and deps.repos.tasks.list_by_request(req.message_id) == []


def test_clarification_writes_one_message_and_pending_payload() -> None:
    deps = make_deps(
        ScriptedModel(
            {"plan": [ModelResponse(structured=_clarification("Which accounts are yours?"))] * 2}
        )
    )
    req = seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    deps.repos.messages.update_request_status("c1", req.message_id, "ACCEPTED", "RUNNING")
    first = plan_stage(initial_brief(req), 0, deps)
    assert first["kind"] == "clarification" and first["pending"]["kind"] == "clarification"
    assert (
        first["pending"]["question"] == "Which accounts are yours?"
        and first["pending"]["next_action"] == "wait"
    )
    assert deps.repos.messages.get("c1", req.message_id).status == "WAITING_USER"
    deps.repos.messages.update_request_status("c1", req.message_id, "WAITING_USER", "RUNNING")
    second = plan_stage(initial_brief(req), 0, deps)  # same attempt re-run = same message id
    assert second["pending"]["message_id"] == first["pending"]["message_id"]
    assert (
        len([m for m in deps.repos.messages.list_by_chat("c1") if m.kind == "clarification"]) == 1
    )


def test_third_question_fails_the_plan() -> None:
    deps = make_deps(ScriptedModel({"plan": [ModelResponse(structured=_clarification("Again?"))]}))
    req = seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    with pytest.raises(PlanFailed):
        plan_stage(initial_brief(req), 2, deps)


def test_malformed_planner_output_fails_plainly() -> None:
    deps = make_deps(ScriptedModel({"plan": [ModelResponse(structured={"kind": "plan"})]}))
    req = seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    with pytest.raises(PlanFailed):
        plan_stage(initial_brief(req), 0, deps)


def test_planner_is_retried_once_with_its_errors() -> None:
    deps = make_deps(
        ScriptedModel(
            {
                "plan": [
                    ModelResponse(structured={"kind": "plan"}),
                    ModelResponse(structured=_plan(["a2"])),
                ]
            }
        )
    )
    req = seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    planned = plan_stage(initial_brief(req), 0, deps)
    assert (
        planned["kind"] == "plan"
        and len(deps.repos.model_calls.list_by_request(req.message_id)) == 2
    )


def test_resolver_turns_months_into_a_window() -> None:
    from datetime import date

    from contextforge.core.context.resolver import resolve_context

    r = resolve_context(
        "Compare October spending with September",
        [],
        today=date(2026, 11, 7),
        timezone="Asia/Kolkata",
    )
    assert (r.period_start, r.period_end) == (
        "2026-09-01",
        "2026-10-31",
    ) and r.today == "2026-11-07"


def test_call_budget_stops_the_planner() -> None:
    deps = make_deps(
        ScriptedModel({"plan": [ModelResponse(structured=_plan(["a2"]))]}), call_budget=0
    )
    req = seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    with pytest.raises(PlanFailed) as e:
        plan_stage(initial_brief(req), 0, deps)
    assert "budget" in e.value.reason

# tests/test_runner.py
import sqlite3
from datetime import UTC, datetime, timedelta

from langgraph.checkpoint.sqlite import SqliteSaver

from contextforge.core.entities.message import ClarificationResponseMessage
from contextforge.core.entities.model_call import Stage
from contextforge.core.models.base import ModelResponse
from contextforge.core.models.config import ModelRoute
from contextforge.core.models.errors import ModelUnavailable
from contextforge.core.models.scripted import ScriptedModel
from contextforge.core.settings import Settings
from contextforge.worker.runner import RequestRunner
from contextforge.worker.stages.plan import FORCED_PLAN, NOT_ASKED
from tests.helpers import (
    ByModel,
    clarification,
    make_deps,
    seed_chat_with_dataset,
    six_task_plan,
    t1_result,
)


def _saver(path) -> SqliteSaver:
    return SqliteSaver(sqlite3.connect(str(path), check_same_thread=False))


def _reply(
    req, target: str, content: str = "ACC-SAV-1 and ACC-CUR-2", *, message_id: str = "m3"
) -> ClarificationResponseMessage:
    return ClarificationResponseMessage(
        message_id=message_id,
        chat_id="c1",
        user_id="u1",
        sender="user",
        kind="clarification_response",
        content=content,
        request_message_id=req.message_id,
        in_reply_to_message_id=target,
        created_at="t",
        updated_at="t",
    )


def test_duplicate_start_runs_once() -> None:
    worker_dependencies = make_deps(
        ScriptedModel(
            {
                "plan": [ModelResponse(structured=six_task_plan())],
                "execute": [ModelResponse(structured=t1_result())] * 3,
            }
        )
    )
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    runner = RequestRunner(worker_dependencies, _saver(":memory:"))
    first = runner.start("c1", req.message_id)
    second = runner.start("c1", req.message_id)
    assert first.started and first.status == "COMPLETED" and not second.started
    assert (
        len(
            [
                c
                for c in worker_dependencies.repositories.model_calls.list_by_request(
                    req.message_id
                )
                if c.stage == "plan"
            ]
        )
        == 1
    )


def test_clarification_interrupt_then_resume_completes(tmp_path) -> None:
    db = tmp_path / "ckpt.sqlite"
    script = {
        "plan": [
            ModelResponse(structured=clarification("Which accounts are yours?")),
            ModelResponse(structured=six_task_plan()),
        ],
        "execute": [ModelResponse(structured=t1_result())] * 3,
    }
    worker_dependencies = make_deps(ScriptedModel(script))
    requests = []
    worker_dependencies.model.on_request = requests.append
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    RequestRunner(worker_dependencies, _saver(db)).start("c1", req.message_id)
    assert (
        worker_dependencies.repositories.messages.get("c1", req.message_id).status == "WAITING_USER"
    )
    pending = worker_dependencies.repositories.messages.pending_for_request("c1", req.message_id)
    assert len(pending) == 1 and pending[0].kind == "clarification"
    # new runner instance = process restart; same sqlite file = same checkpoint
    out = RequestRunner(worker_dependencies, _saver(db)).resume(
        "c1", req.message_id, _reply(req, pending[0].message_id)
    )
    assert (
        out.started
        and worker_dependencies.repositories.messages.get("c1", req.message_id).status
        == "COMPLETED"
    )
    assert len(worker_dependencies.repositories.tasks.list_by_request(req.message_id)) == 6
    plans = [
        c
        for c in worker_dependencies.repositories.model_calls.list_by_request(req.message_id)
        if c.stage == "plan"
    ]
    assert len(plans) == 2  # the first planner call was restored, not re-paid


def test_a_repeated_question_is_never_asked_again(tmp_path) -> None:
    """Live run 2: the planner asked for the period, got the answer, and asked it again. One
    question per request: a second one is not put to the user; the planner is told to plan on
    the most likely reading and the report says what was assumed."""
    db = tmp_path / "ckpt.sqlite"
    script = {
        "plan": [
            ModelResponse(structured=clarification("Which months?")),
            ModelResponse(structured=clarification("Which months?")),
            ModelResponse(structured=six_task_plan()),
        ],
        "execute": [ModelResponse(structured=t1_result())] * 3,
    }
    worker_dependencies = make_deps(ScriptedModel(script))
    requests = []
    worker_dependencies.model.on_request = requests.append
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    RequestRunner(worker_dependencies, _saver(db)).start("c1", req.message_id)
    [pending] = worker_dependencies.repositories.messages.pending_for_request("c1", req.message_id)
    out = RequestRunner(worker_dependencies, _saver(db)).resume(
        "c1", req.message_id, _reply(req, pending.message_id, "answer 0", message_id="m3")
    )
    assert out.started and out.status == "COMPLETED"
    planner_calls = [r for r in requests if r.stage == "plan"]
    assert "answer 0" in planner_calls[1].messages[0].content
    assert FORCED_PLAN.format(question="Which months?") in planner_calls[2].messages[0].content
    chat = worker_dependencies.repositories.messages.list_by_chat("c1")
    assert len([m for m in chat if m.kind == "clarification"]) == 1
    [report] = [m for m in chat if m.kind == "report"]
    assert NOT_ASKED.format(question="Which months?") in report.report.uncertainties


def test_resume_with_wrong_target_is_refused(tmp_path) -> None:
    db = tmp_path / "ckpt.sqlite"
    worker_dependencies = make_deps(
        ScriptedModel({"plan": [ModelResponse(structured=clarification("Which?"))]})
    )
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    RequestRunner(worker_dependencies, _saver(db)).start("c1", req.message_id)
    runner = RequestRunner(worker_dependencies, _saver(db))
    out = runner.resume("c1", req.message_id, _reply(req, "not-the-pending-one"))
    assert (
        not out.started
        and worker_dependencies.repositories.messages.get("c1", req.message_id).status
        == "WAITING_USER"
    )
    assert not [
        m
        for m in worker_dependencies.repositories.messages.list_by_chat("c1")
        if m.kind == "clarification_response"
    ]
    # The API's path: the right target before the API's claim, then a wrong one after it.
    (question,) = worker_dependencies.repositories.messages.pending_for_request(
        "c1", req.message_id
    )
    unclaimed = runner.resume_claimed("c1", req.message_id, _reply(req, question.message_id))
    worker_dependencies.repositories.messages.update_request_status(
        "c1", req.message_id, "WAITING_USER", "RUNNING"
    )
    wrong = runner.resume_claimed("c1", req.message_id, _reply(req, "not-the-pending-one"))
    assert not unclaimed.started and not wrong.started


def test_restart_after_crash_does_not_replay_finished_tasks(tmp_path) -> None:
    db = tmp_path / "ckpt.sqlite"
    script = {
        "plan": [ModelResponse(structured=six_task_plan())],
        "execute": [ModelResponse(structured=t1_result())] * 3,
    }
    worker_dependencies = make_deps(ScriptedModel(script), tasks_in_flight=1)
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    completed = []

    def crash_after_second_completed(t):
        if t.kind == "sql_analysis" and t.status == "COMPLETED":
            completed.append(t.task_id)
            if len(completed) == 2:
                raise RuntimeError("crash")

    worker_dependencies.repositories.tasks.on_save = crash_after_second_completed  # type: ignore[attr-defined]
    try:
        RequestRunner(worker_dependencies, _saver(db)).start("c1", req.message_id)
    except RuntimeError:
        pass
    worker_dependencies.repositories.tasks.on_save = None  # type: ignore[attr-defined]
    calls_before = len(worker_dependencies.repositories.model_calls.list_by_request(req.message_id))
    # operator requeue
    worker_dependencies.repositories.messages.update_request_status(
        "c1", req.message_id, "RUNNING", "ACCEPTED"
    )
    out = RequestRunner(worker_dependencies, _saver(db)).start("c1", req.message_id)
    assert (
        out.started
        and worker_dependencies.repositories.messages.get("c1", req.message_id).status
        == "COMPLETED"
    )
    executes_after = [
        c
        for c in worker_dependencies.repositories.model_calls.list_by_request(req.message_id)
        if c.stage == "execute"
    ]
    # The one task left to run, then the report's summary call.
    assert (
        len(executes_after) == 3
        and len(worker_dependencies.repositories.model_calls.list_by_request(req.message_id))
        == calls_before + 2
    )
    assert (
        len(
            [
                m
                for m in worker_dependencies.repositories.messages.list_by_chat("c1")
                if m.kind == "report"
            ]
        )
        == 1
    )


def test_restart_after_crash_mid_task_completes(tmp_path) -> None:
    db = tmp_path / "ckpt.sqlite"
    script = {
        "plan": [ModelResponse(structured=six_task_plan())],
        "execute": [ModelResponse(structured=t1_result())] * 3,
    }
    worker_dependencies = make_deps(ScriptedModel(script), tasks_in_flight=1)
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    executes = []

    def crash_in_second_execute_call(request):
        if request.stage == "execute":
            executes.append(request.task_id)
            if len(executes) == 2:  # the process dies while t2 is RUNNING
                raise RuntimeError("crash")

    worker_dependencies.model.on_request = crash_in_second_execute_call  # type: ignore[attr-defined]
    try:
        RequestRunner(worker_dependencies, _saver(db)).start("c1", req.message_id)
    except RuntimeError:
        pass
    worker_dependencies.model.on_request = None  # type: ignore[attr-defined]
    interrupted = worker_dependencies.repositories.tasks.get(req.message_id, executes[1])
    assert interrupted.status == "RUNNING" and interrupted.attempt == 1
    # operator requeue
    worker_dependencies.repositories.messages.update_request_status(
        "c1", req.message_id, "RUNNING", "ACCEPTED"
    )
    out = RequestRunner(worker_dependencies, _saver(db)).start("c1", req.message_id)
    assert (
        out.started
        and worker_dependencies.repositories.messages.get("c1", req.message_id).status
        == "COMPLETED"
    )
    retried = worker_dependencies.repositories.tasks.get(req.message_id, executes[1])
    assert retried.status == "COMPLETED" and retried.attempt == 2
    assert worker_dependencies.repositories.tasks.get(req.message_id, executes[0]).attempt == 1


def test_restart_mid_round_two_reruns_neither_round_one_nor_its_lead_review(tmp_path) -> None:
    # F16: every lead review is a checkpointed task, so a resumed run restores round 1's
    # researcher and the review that started round 2 instead of calling them again.
    db = tmp_path / "ckpt.sqlite"
    plan = {**six_task_plan(), "tasks": six_task_plan()["tasks"][:1]}  # t1 alone
    follow_up = {**six_task_plan()["tasks"][2], "ref": "f1", "objective": "Recurring merchants."}
    script = {
        "plan": [ModelResponse(structured=plan)],
        "execute": [ModelResponse(structured=t1_result())] * 2,
        "lead": [
            ModelResponse(
                structured={"action": "run_subagents", "reason": "gap", "tasks": [follow_up]}
            ),
            ModelResponse(structured={"action": "complete_task", "reason": "enough"}),
        ],
    }
    worker_dependencies = make_deps(ScriptedModel(script), tasks_in_flight=1, research_rounds=3)
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    executes = []

    def crash_in_round_two(request):
        if request.stage == "execute":
            executes.append(request.task_id)
            if len(executes) == 2:  # the process dies while the round-2 researcher runs
                raise RuntimeError("crash")

    worker_dependencies.model.on_request = crash_in_round_two  # type: ignore[attr-defined]
    try:
        RequestRunner(worker_dependencies, _saver(db)).start("c1", req.message_id)
    except RuntimeError:
        pass
    worker_dependencies.model.on_request = None  # type: ignore[attr-defined]
    worker_dependencies.repositories.messages.update_request_status(
        "c1", req.message_id, "RUNNING", "ACCEPTED"
    )
    out = RequestRunner(worker_dependencies, _saver(db)).start("c1", req.message_id)
    assert out.started and out.status == "COMPLETED"
    calls = worker_dependencies.repositories.model_calls.list_by_request(req.message_id)
    stages = [c.stage for c in calls]
    # plan, round 1, review 1, round 2 (the crashed call is on record, then its retry),
    # review 2, report summary
    assert stages.count("plan") == 1 and stages.count("lead") == 2
    first, second = worker_dependencies.repositories.tasks.list_by_request(req.message_id)
    assert [c.task_id for c in calls if c.stage == "execute"] == [first.task_id] + [
        second.task_id
    ] * 2
    assert (first.round, first.attempt) == (1, 1) and (second.round, second.attempt) == (2, 2)


def test_pending_interrupt_carries_next_action(tmp_path) -> None:
    db = tmp_path / "ckpt.sqlite"
    worker_dependencies = make_deps(
        ScriptedModel({"plan": [ModelResponse(structured=clarification("Which?"))]})
    )
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    RequestRunner(worker_dependencies, _saver(db)).start("c1", req.message_id)
    pending = RequestRunner(worker_dependencies, _saver(db)).pending_interrupt(req.message_id)
    assert pending is not None and pending["kind"] == "clarification" and pending["next_action"]


def test_deadline_counts_per_segment_so_a_late_request_still_gets_model_calls(tmp_path) -> None:
    """A-C1: a request created 20 minutes ago (deadline 15) still plans, asks, and finishes on
    resume, because the deadline counts from the start of each run, not from creation."""
    script = {
        "plan": [
            ModelResponse(structured=clarification("Which accounts are yours?")),
            ModelResponse(structured=six_task_plan()),
        ],
        "execute": [ModelResponse(structured=t1_result())] * 3,
        "summary": [ModelResponse(text="Rows 12/13 are transfers.")],
    }
    worker_dependencies = make_deps(ScriptedModel(script))
    worker_dependencies.settings = Settings(_env_file=None, request_deadline_seconds=900)
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    created = (datetime.now(UTC) - timedelta(minutes=20)).isoformat()
    worker_dependencies.repositories.messages.save(req.model_copy(update={"created_at": created}))
    runner = RequestRunner(worker_dependencies, _saver(tmp_path / "ckpt.sqlite"))
    runner.start("c1", req.message_id)
    pending = worker_dependencies.repositories.messages.pending_for_request("c1", req.message_id)
    out = runner.resume("c1", req.message_id, _reply(req, pending[0].message_id))
    assert out.status == "COMPLETED"
    calls = worker_dependencies.repositories.model_calls.list_by_request(req.message_id)
    assert [c.stage for c in calls].count("plan") == 2
    assert all(c.outcome == "COMPLETED" for c in calls) and not worker_dependencies.segment_started


def test_a_request_completes_on_the_fallbacks_while_every_stage_model_is_down() -> None:
    """F9: the planner and the executor are down; the request plans and runs on their
    fallbacks, and each is tried once, then left to cool down while the fallback serves."""
    down = ModelUnavailable("ServiceUnavailableException")
    model = ByModel(
        {
            "planner": [down],
            "planner-2": [ModelResponse(structured=six_task_plan())],
            "executor": [down],
            "executor-2": [ModelResponse(structured=t1_result())] * 3,
            # The report's summary call has no route here; it fails and the deterministic
            # summary takes over (summary.py), so the request still completes.
            "scripted": [down],
        }
    )
    worker_dependencies = make_deps(model, tasks_in_flight=1)
    worker_dependencies.routes = {
        Stage.PLAN: ModelRoute("planner", ("planner-2",)),
        Stage.EXECUTE: ModelRoute("executor", ("executor-2",)),
    }
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    assert (
        RequestRunner(worker_dependencies, _saver(":memory:")).start("c1", req.message_id).status
        == "COMPLETED"
    )
    assert model.sent == ["planner", "planner-2", "executor", *["executor-2"] * 3, "scripted"]
    calls = worker_dependencies.repositories.model_calls.list_by_request(req.message_id)
    answered = [c for c in calls if c.outcome == "COMPLETED"]
    assert {(c.model, c.metadata["requested_model"]) for c in answered} == {
        ("bedrock/planner-2", "planner"),
        ("bedrock/executor-2", "executor"),
    }

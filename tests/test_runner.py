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
    deps = make_deps(
        ScriptedModel(
            {
                "plan": [ModelResponse(structured=six_task_plan())],
                "execute": [ModelResponse(structured=t1_result())] * 3,
            }
        )
    )
    req = seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    runner = RequestRunner(deps, _saver(":memory:"))
    first = runner.start("c1", req.message_id)
    second = runner.start("c1", req.message_id)
    assert first.started and first.status == "COMPLETED" and not second.started
    assert (
        len(
            [c for c in deps.repos.model_calls.list_by_request(req.message_id) if c.stage == "plan"]
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
    deps = make_deps(ScriptedModel(script))
    requests = []
    deps.model.on_request = requests.append
    req = seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    RequestRunner(deps, _saver(db)).start("c1", req.message_id)
    assert deps.repos.messages.get("c1", req.message_id).status == "WAITING_USER"
    pending = deps.repos.messages.pending_for_request("c1", req.message_id)
    assert len(pending) == 1 and pending[0].kind == "clarification"
    # new runner instance = process restart; same sqlite file = same checkpoint
    out = RequestRunner(deps, _saver(db)).resume(
        "c1", req.message_id, _reply(req, pending[0].message_id)
    )
    assert out.started and deps.repos.messages.get("c1", req.message_id).status == "COMPLETED"
    assert len(deps.repos.tasks.list_by_request(req.message_id)) == 6
    plans = [c for c in deps.repos.model_calls.list_by_request(req.message_id) if c.stage == "plan"]
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
    deps = make_deps(ScriptedModel(script))
    requests = []
    deps.model.on_request = requests.append
    req = seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    RequestRunner(deps, _saver(db)).start("c1", req.message_id)
    [pending] = deps.repos.messages.pending_for_request("c1", req.message_id)
    out = RequestRunner(deps, _saver(db)).resume(
        "c1", req.message_id, _reply(req, pending.message_id, "answer 0", message_id="m3")
    )
    assert out.started and out.status == "COMPLETED"
    planner_calls = [r for r in requests if r.stage == "plan"]
    assert "answer 0" in planner_calls[1].messages[0].content
    assert FORCED_PLAN.format(question="Which months?") in planner_calls[2].messages[0].content
    chat = deps.repos.messages.list_by_chat("c1")
    assert len([m for m in chat if m.kind == "clarification"]) == 1
    [report] = [m for m in chat if m.kind == "report"]
    assert NOT_ASKED.format(question="Which months?") in report.report.uncertainties


def test_resume_with_wrong_target_is_refused(tmp_path) -> None:
    db = tmp_path / "ckpt.sqlite"
    deps = make_deps(ScriptedModel({"plan": [ModelResponse(structured=clarification("Which?"))]}))
    req = seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    RequestRunner(deps, _saver(db)).start("c1", req.message_id)
    runner = RequestRunner(deps, _saver(db))
    out = runner.resume("c1", req.message_id, _reply(req, "not-the-pending-one"))
    assert (
        not out.started and deps.repos.messages.get("c1", req.message_id).status == "WAITING_USER"
    )
    assert not [
        m for m in deps.repos.messages.list_by_chat("c1") if m.kind == "clarification_response"
    ]
    # The API's path: the right target before the API's claim, then a wrong one after it.
    (question,) = deps.repos.messages.pending_for_request("c1", req.message_id)
    unclaimed = runner.resume_claimed("c1", req.message_id, _reply(req, question.message_id))
    deps.repos.messages.update_request_status("c1", req.message_id, "WAITING_USER", "RUNNING")
    wrong = runner.resume_claimed("c1", req.message_id, _reply(req, "not-the-pending-one"))
    assert not unclaimed.started and not wrong.started


def test_restart_after_crash_does_not_replay_finished_tasks(tmp_path) -> None:
    db = tmp_path / "ckpt.sqlite"
    script = {
        "plan": [ModelResponse(structured=six_task_plan())],
        "execute": [ModelResponse(structured=t1_result())] * 3,
    }
    deps = make_deps(ScriptedModel(script), tasks_in_flight=1)
    req = seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    completed = []

    def crash_after_second_completed(t):
        if t.kind == "sql_analysis" and t.status == "COMPLETED":
            completed.append(t.task_id)
            if len(completed) == 2:
                raise RuntimeError("crash")

    deps.repos.tasks.on_save = crash_after_second_completed  # type: ignore[attr-defined]
    try:
        RequestRunner(deps, _saver(db)).start("c1", req.message_id)
    except RuntimeError:
        pass
    deps.repos.tasks.on_save = None  # type: ignore[attr-defined]
    calls_before = len(deps.repos.model_calls.list_by_request(req.message_id))
    # operator requeue
    deps.repos.messages.update_request_status("c1", req.message_id, "RUNNING", "ACCEPTED")
    out = RequestRunner(deps, _saver(db)).start("c1", req.message_id)
    assert out.started and deps.repos.messages.get("c1", req.message_id).status == "COMPLETED"
    executes_after = [
        c for c in deps.repos.model_calls.list_by_request(req.message_id) if c.stage == "execute"
    ]
    # The one task left to run, then the report's summary call.
    assert (
        len(executes_after) == 3
        and len(deps.repos.model_calls.list_by_request(req.message_id)) == calls_before + 2
    )
    assert len([m for m in deps.repos.messages.list_by_chat("c1") if m.kind == "report"]) == 1


def test_restart_after_crash_mid_task_completes(tmp_path) -> None:
    db = tmp_path / "ckpt.sqlite"
    script = {
        "plan": [ModelResponse(structured=six_task_plan())],
        "execute": [ModelResponse(structured=t1_result())] * 3,
    }
    deps = make_deps(ScriptedModel(script), tasks_in_flight=1)
    req = seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    executes = []

    def crash_in_second_execute_call(request):
        if request.stage == "execute":
            executes.append(request.task_id)
            if len(executes) == 2:  # the process dies while t2 is RUNNING
                raise RuntimeError("crash")

    deps.model.on_request = crash_in_second_execute_call  # type: ignore[attr-defined]
    try:
        RequestRunner(deps, _saver(db)).start("c1", req.message_id)
    except RuntimeError:
        pass
    deps.model.on_request = None  # type: ignore[attr-defined]
    interrupted = deps.repos.tasks.get(req.message_id, executes[1])
    assert interrupted.status == "RUNNING" and interrupted.attempt == 1
    # operator requeue
    deps.repos.messages.update_request_status("c1", req.message_id, "RUNNING", "ACCEPTED")
    out = RequestRunner(deps, _saver(db)).start("c1", req.message_id)
    assert out.started and deps.repos.messages.get("c1", req.message_id).status == "COMPLETED"
    retried = deps.repos.tasks.get(req.message_id, executes[1])
    assert retried.status == "COMPLETED" and retried.attempt == 2
    assert deps.repos.tasks.get(req.message_id, executes[0]).attempt == 1


def test_pending_interrupt_carries_next_action(tmp_path) -> None:
    db = tmp_path / "ckpt.sqlite"
    deps = make_deps(ScriptedModel({"plan": [ModelResponse(structured=clarification("Which?"))]}))
    req = seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    RequestRunner(deps, _saver(db)).start("c1", req.message_id)
    pending = RequestRunner(deps, _saver(db)).pending_interrupt(req.message_id)
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
    deps = make_deps(ScriptedModel(script))
    deps.settings = Settings(_env_file=None, request_deadline_seconds=900)
    req = seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    created = (datetime.now(UTC) - timedelta(minutes=20)).isoformat()
    deps.repos.messages.save(req.model_copy(update={"created_at": created}))
    runner = RequestRunner(deps, _saver(tmp_path / "ckpt.sqlite"))
    runner.start("c1", req.message_id)
    pending = deps.repos.messages.pending_for_request("c1", req.message_id)
    out = runner.resume("c1", req.message_id, _reply(req, pending[0].message_id))
    assert out.status == "COMPLETED"
    calls = deps.repos.model_calls.list_by_request(req.message_id)
    assert [c.stage for c in calls].count("plan") == 2
    assert all(c.outcome == "COMPLETED" for c in calls) and not deps.segment_started


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
        }
    )
    deps = make_deps(model, tasks_in_flight=1)
    deps.routes = {
        Stage.PLAN: ModelRoute("planner", ("planner-2",)),
        Stage.EXECUTE: ModelRoute("executor", ("executor-2",)),
    }
    req = seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    assert RequestRunner(deps, _saver(":memory:")).start("c1", req.message_id).status == "COMPLETED"
    assert model.sent == ["planner", "planner-2", "executor", *["executor-2"] * 3]
    calls = deps.repos.model_calls.list_by_request(req.message_id)
    answered = [c for c in calls if c.outcome == "COMPLETED"]
    assert {(c.model, c.metadata["requested_model"]) for c in answered} == {
        ("bedrock/planner-2", "planner"),
        ("bedrock/executor-2", "executor"),
    }

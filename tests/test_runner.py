# tests/test_runner.py
import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver

from contextforge.core.entities.message import ClarificationResponseMessage
from contextforge.core.models.base import ModelResponse
from contextforge.core.models.scripted import ScriptedModel
from contextforge.worker.runner import RequestRunner
from tests.helpers import clarification, make_deps, seed_chat_with_dataset, six_task_plan, t1_result


def _saver(path) -> SqliteSaver:
    return SqliteSaver(sqlite3.connect(str(path), check_same_thread=False))


def _reply(
    req, target: str, content: str = "ACC-SAV-1 and ACC-CUR-2"
) -> ClarificationResponseMessage:
    return ClarificationResponseMessage(
        message_id="m3",
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


def test_two_clarifications_then_plan(tmp_path) -> None:
    db = tmp_path / "ckpt.sqlite"
    script = {
        "plan": [
            ModelResponse(structured=clarification("Which accounts?")),
            ModelResponse(structured=clarification("Which months?")),
            ModelResponse(structured=six_task_plan()),
        ],
        "execute": [ModelResponse(structured=t1_result())] * 3,
    }
    deps = make_deps(ScriptedModel(script))
    req = seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    RequestRunner(deps, _saver(db)).start("c1", req.message_id)
    for i in range(2):
        pending = [m for m in deps.repos.messages.pending_for_request("c1", req.message_id)]
        assert len(pending) == 1
        out = RequestRunner(deps, _saver(db)).resume(
            "c1", req.message_id, _reply(req, pending[0].message_id, f"answer {i}")
        )
        assert out.started
    assert deps.repos.messages.get("c1", req.message_id).status == "COMPLETED"
    assert (
        len([m for m in deps.repos.messages.list_by_chat("c1") if m.kind == "clarification"]) == 2
    )


def test_resume_with_wrong_target_is_refused(tmp_path) -> None:
    db = tmp_path / "ckpt.sqlite"
    deps = make_deps(ScriptedModel({"plan": [ModelResponse(structured=clarification("Which?"))]}))
    req = seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    RequestRunner(deps, _saver(db)).start("c1", req.message_id)
    out = RequestRunner(deps, _saver(db)).resume(
        "c1", req.message_id, _reply(req, "not-the-pending-one")
    )
    assert (
        not out.started and deps.repos.messages.get("c1", req.message_id).status == "WAITING_USER"
    )
    assert not [
        m for m in deps.repos.messages.list_by_chat("c1") if m.kind == "clarification_response"
    ]


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
    assert (
        len(executes_after) == 3
        and len(deps.repos.model_calls.list_by_request(req.message_id)) == calls_before + 1
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

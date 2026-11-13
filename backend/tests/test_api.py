import asyncio
import json
import logging
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.sqlite import SqliteSaver

from contextforge.api.errors import INTERNAL_MESSAGE
from contextforge.api.main import create_app
from contextforge.api.routes import requests as request_routes
from contextforge.api.streaming import event_stream
from contextforge.core.entities.message import ClarificationMessage, ClarificationResponseMessage
from contextforge.core.ids import now_iso
from contextforge.core.llm.base import ModelResponse, ToolCall
from contextforge.core.llm.scripted import ScriptedModel
from contextforge.core.queue import InProcessQueue, WorkMessage
from contextforge.core.services.application import Application, title_from
from contextforge.core.services.memory import memory_snapshot
from contextforge.core.settings import Settings
from contextforge.worker.consumer import QueueConsumer
from contextforge.worker.runner import RequestRunner
from tests.helpers import clarification, make_deps, seed_chat_with_dataset, six_task_plan, t1_result


@pytest.fixture(autouse=True)
def _bounded_streams(monkeypatch):
    # A stream that fails to close ends the test in seconds, not after 15 minutes.
    monkeypatch.setattr(request_routes, "STREAM_MAX_SECONDS", 10.0)


def _settings(tmp_path, **overrides) -> Settings:
    """App settings for a test: no .env, user u1, a 10 ms stream poll."""
    values = {
        "dev_user_id": "u1",
        "checkpoint_path": tmp_path / "checkpoint.sqlite",
        "sse_poll_seconds": 0.01,
        **overrides,
    }
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def _events(body: str) -> list[tuple[str, dict]]:
    """The (event, data) pairs of an SSE body; comment lines such as heartbeats are skipped."""
    events = []
    for block in body.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if line[:1] != ":")
        if "event" in fields:
            events.append((fields["event"], json.loads(fields["data"])))
    return events


def _until(client: TestClient, path: str, *statuses: str) -> str:
    for _ in range(500):
        status = client.get(path).json()["request"]["status"]
        if status in statuses:
            return status
        time.sleep(0.01)
    return status


def _question(req, message_id: str, content: str) -> ClarificationMessage:
    now = now_iso()
    return ClarificationMessage(
        message_id=message_id,
        chat_id=req.chat_id,
        user_id=req.user_id,
        sender="assistant",
        kind="clarification",
        request_message_id=req.message_id,
        content=content,
        created_at=now,
        updated_at=now,
    )


def test_api_request_lifecycle_memory_paging_and_ownership(tmp_path):
    worker_dependencies = make_deps(
        ScriptedModel(
            {
                "plan": [ModelResponse(structured=six_task_plan())],
                "execute": [ModelResponse(structured=t1_result())] * 3,
            }
        ),
        tasks_in_flight=1,
    )
    # An upload-free public flow is separately covered at the engine seam.
    seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    with TestClient(
        create_app(_settings(tmp_path), worker_dependencies=worker_dependencies)
    ) as client:
        assert client.get("/health").json()["status"] == "ok"
        assert client.get("/health").json()["build"] == "web-research-v3"
        user = client.patch(
            "/me", json={"instructions": "Use concise answers.", "preferences": {"currency": "INR"}}
        )
        assert user.status_code == 200 and user.json()["instructions"] == "Use concise answers."
        assert client.get("/chats/other/messages").status_code == 404
        chat = client.post("/chats", json={"title": "Market notes"}).json()
        assert chat["userId"] == "u1" and chat["title"] == "Market notes"
        headers = {"Idempotency-Key": "same"}
        submitted = client.post(
            "/chats/c1/requests",
            json={"content": "Compare September and October spending"},
            headers=headers,
        )
        assert submitted.status_code == 202
        request_id = submitted.json()["messageId"]
        duplicate = client.post(
            "/chats/c1/requests",
            json={"content": "Compare September and October spending"},
            headers=headers,
        )
        assert duplicate.json()["messageId"] == request_id
        path = f"/chats/c1/requests/{request_id}"
        assert _until(client, path, "COMPLETED", "FAILED") == "COMPLETED"
        assert len(client.get(path).json()["tasks"]) == 6
        # A stream opened on a finished request sends that one request event and closes.
        [(kind, record)] = _events(client.get(path + "/events").text)
        assert kind == "request" and record["status"] == "COMPLETED"
        assert client.post(path + "/cancel").status_code == 409
        page = client.get("/chats/c1/messages?limit=1").json()
        assert len(page["items"]) == 1 and page["nextCursor"]
        assert (
            client.get("/chats/c1/messages", params={"cursor": page["nextCursor"]}).status_code
            == 200
        )
        assert client.delete(f"/chats/{chat['chatId']}").status_code == 204
    assert title_from("word " * 30) == ("word " * 12).strip() + "…"
    assert len(title_from("x" * 100)) == 60 and title_from("  short   title ") == "short title"


def test_connected_run_takes_the_production_call_path_with_the_users_memory(tmp_path):
    settings = _settings(tmp_path, call_reserve_usd=0.10, request_max_usd=1.0)
    model = ScriptedModel(
        {
            "plan": [ModelResponse(structured=six_task_plan())],
            "execute": [ModelResponse(structured=t1_result())] * 3,
        }
    )
    planner_system: list[str] = []
    model.on_request = lambda r: planner_system.extend(r.system) if r.stage == "plan" else None
    worker_dependencies = make_deps(model, tasks_in_flight=1, settings=settings)
    seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    with TestClient(create_app(settings, worker_dependencies=worker_dependencies)) as client:
        client.patch("/me", json={"instructions": "Quote every figure in INR."})
        memory = memory_snapshot(worker_dependencies.repositories.users.get("u1"))
        request_id = client.post(
            "/chats/c1/requests",
            json={"content": "Compare September and October spending"},
            headers={"Idempotency-Key": "settings"},
        ).json()["messageId"]
        path = f"/chats/c1/requests/{request_id}"
        assert _until(client, path, "COMPLETED", "FAILED") == "COMPLETED"
        usage = client.get(path).json()["usage"]
    calls = worker_dependencies.repositories.model_calls.list_by_request(request_id)
    # The budgeted client reserved spend for each of the 5 calls (plan, three tasks, the report's
    # summary, which has no script and fails); without settings it is 0.
    assert usage["modelCalls"] == 5 and usage["estimatedCostUsd"] == pytest.approx(0.5)
    assert "Quote every figure in INR." in planner_system[0]
    assert all(c.context_version.startswith(f"mem:{memory['version']}|") for c in calls)


def test_a_long_report_becomes_a_deliverable_that_the_content_route_serves(tmp_path):
    """F5 Part B: a report text over 1,500 characters is saved whole as a markdown deliverable;
    the chat keeps its summary, its counts and a pointer (F10 ruling 2), and only deliverables
    and raw results are served."""

    def long_result(task: int) -> ModelResponse:
        result = t1_result()
        for fact in result["facts"]:
            fact["text"] += f" Task {task}: the statement rows give the detail." * 12
        return ModelResponse(structured=result)

    worker_dependencies = make_deps(
        ScriptedModel(
            {
                "plan": [ModelResponse(structured=six_task_plan())],
                "execute": [long_result(task) for task in range(3)],
            }
        ),
        tasks_in_flight=1,
    )
    seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    with TestClient(
        create_app(_settings(tmp_path), worker_dependencies=worker_dependencies)
    ) as client:
        request_id = client.post(
            "/chats/c1/requests", json={"content": "Compare"}, headers={"Idempotency-Key": "long"}
        ).json()["messageId"]
        assert _until(client, f"/chats/c1/requests/{request_id}", "COMPLETED", "FAILED") == (
            "COMPLETED"
        )
        items = client.get("/chats/c1/messages").json()["items"]
        report = next(m for m in items if m["kind"] == "report")
        (ref,) = report["report"]["deliverables"]
        served = client.get(f"/chats/c1/artifacts/{ref['artifactId']}/content")
        dataset = client.get("/chats/c1/artifacts/a2/content")
        missing = client.get("/chats/c1/artifacts/none/content")
    claims = report["report"]["claims"]
    assert ref["role"] == "output" and len(claims) == 6
    # No summary is scripted, so the summary is built from the first claim, cut to length.
    summary, counts = report["content"].split("\n\n")
    assert summary.startswith(claims[0]["text"][:100]) and summary.endswith("…")
    assert counts == "6 findings, 4 limitations. Full report attached."
    assert served.status_code == 200 and served.headers["content-type"].startswith("text/markdown")
    assert served.headers["x-content-type-options"] == "nosniff"
    body = served.text
    assert body.startswith(
        f"# Explain how October 2026 spending differs from September 2026.\n\n{summary}\n\n1. "
    )
    assert all(claim["text"] in body for claim in claims)
    assert "Limitations:" in body and "## Evidence" in body and "## Coverage" in body
    artifact = worker_dependencies.repositories.artifacts.get("c1", ref["artifactId"])
    assert artifact.kind == "deliverable" and artifact.name == f"report-{request_id}.md"
    assert (dataset.status_code, missing.status_code) == (404, 404)
    assert dataset.json() == {"error": {"code": "not_found", "message": "Artifact not found."}}


def test_one_clarification_closes_the_stream_and_a_second_is_not_asked(tmp_path):
    """The one question closes the stream and its answer resumes the same request; the
    planner's second question is not asked (it plans instead and the report says so)."""
    worker_dependencies = make_deps(
        ScriptedModel(
            {
                "plan": [
                    ModelResponse(structured=clarification("Which months?")),
                    ModelResponse(structured=clarification("Which accounts?")),
                    ModelResponse(structured=six_task_plan()),
                ],
                "execute": [ModelResponse(structured=t1_result())] * 3,
            }
        ),
        tasks_in_flight=1,
    )
    seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    with TestClient(
        create_app(_settings(tmp_path), worker_dependencies=worker_dependencies)
    ) as client:
        request = client.post(
            "/chats/c1/requests",
            json={"content": "Compare spending"},
            headers={"Idempotency-Key": "clarify"},
        ).json()
        path = f"/chats/c1/requests/{request['messageId']}"
        for question, answer in [("Which months?", "September")]:
            events = _events(client.get(path + "/events").text)
            assert events[-1][0] == "request" and events[-1][1]["status"] == "WAITING_USER"
            [asked] = [
                r
                for kind, r in events
                if kind == "message" and r["kind"] == "clarification" and r["content"] == question
            ]
            blank = client.post(
                path + "/replies", json={"inReplyToMessageId": asked["messageId"], "content": "  "}
            )
            assert blank.status_code == 400
            foreign = client.post(
                path + "/replies", json={"inReplyToMessageId": "nope", "content": "x"}
            )
            assert foreign.status_code == 404 and foreign.json()["error"]["code"] == "not_found"
            reply = client.post(
                path + "/replies",
                json={"inReplyToMessageId": asked["messageId"], "content": answer},
            )
            assert reply.status_code == 201
            assert reply.json()["requestMessageId"] == request["messageId"]
            again = client.post(
                path + "/replies",
                json={"inReplyToMessageId": asked["messageId"], "content": "again"},
            )
            assert again.status_code == 409 and again.json()["error"]["code"] == "not_pending"
        events = _events(client.get(path + "/events").text)
        assert events[-1][1]["status"] == "COMPLETED"
        [report] = [r for k, r in events if k == "message" and r["kind"] == "report"]
        assert "Not asked: 'Which accounts?'" in report["content"]
        assert not [r for k, r in events if k == "message" and r["content"] == "Which accounts?"]


def test_waiting_stream_stays_open_until_the_new_question_exists(tmp_path):
    # The worker writes WAITING_USER before the question. On a second question the first one
    # (already answered) is in the chat; the stream must wait for the new one, not close.
    worker_dependencies = make_deps(ScriptedModel({}))
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    messages = worker_dependencies.repositories.messages
    messages.update_request_status("c1", req.message_id, "ACCEPTED", "RUNNING")
    messages.save(_question(req, "q1", "Which months?"))
    now = now_iso()
    messages.save(
        ClarificationResponseMessage(
            message_id="a1",
            chat_id="c1",
            user_id="u1",
            sender="user",
            kind="clarification_response",
            content="September",
            request_message_id=req.message_id,
            in_reply_to_message_id="q1",
            created_at=now,
            updated_at=now,
        )
    )
    messages.update_request_status("c1", req.message_id, "RUNNING", "WAITING_USER")
    later = threading.Timer(0.2, messages.save, [_question(req, "q2", "Which accounts?")])
    with TestClient(
        create_app(_settings(tmp_path, run_worker=False), worker_dependencies=worker_dependencies)
    ) as client:
        later.start()
        events = _events(client.get(f"/chats/c1/requests/{req.message_id}/events").text)
    asked = [r["content"] for k, r in events if k == "message" and r["kind"] == "clarification"]
    assert asked == ["Which months?", "Which accounts?"]
    assert events[-1][0] == "request" and events[-1][1]["status"] == "WAITING_USER"


def test_open_stream_heartbeats_on_wall_time_and_ends_at_its_cap():
    worker_dependencies = make_deps(ScriptedModel({}))
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")

    async def frames() -> list[str]:
        stream = event_stream(
            Application(worker_dependencies, "u1"),
            req,
            0.01,
            heartbeat_seconds=0.05,
            max_seconds=0.3,
        )
        return [frame async for frame in stream]

    started = time.monotonic()
    sent = asyncio.run(frames())
    assert 0.3 <= time.monotonic() - started < 2
    assert [f for f in sent if f.startswith("event:")] == [sent[0]]  # nothing changed after it
    assert 2 <= sent.count(": heartbeat\n\n") <= 7


def test_cancel_succeeds_and_one_that_loses_a_race_gets_the_409_envelope(tmp_path, monkeypatch):
    worker_dependencies = make_deps(ScriptedModel({}))
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    messages = worker_dependencies.repositories.messages
    with TestClient(
        create_app(_settings(tmp_path, run_worker=False), worker_dependencies=worker_dependencies)
    ) as client:
        cancelled = client.post(f"/chats/c1/requests/{req.message_id}/cancel")
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "CANCELLED"
        racing = client.post(
            "/chats/c1/requests", json={"content": "Again"}, headers={"Idempotency-Key": "r"}
        ).json()["messageId"]
        messages.update_request_status("c1", racing, "ACCEPTED", "RUNNING")
        update = messages.update_request_status

        def worker_finishes_first(chat_id, message_id, expected, new, failure_reason=None):
            if new == "CANCELLED":
                update(chat_id, message_id, "RUNNING", "COMPLETED")
            return update(chat_id, message_id, expected, new, failure_reason)

        monkeypatch.setattr(messages, "update_request_status", worker_finishes_first)
        lost = client.post(f"/chats/c1/requests/{racing}/cancel")
    assert lost.status_code == 409
    assert lost.json() == {"error": {"code": "invalid_state", "message": "Request is COMPLETED."}}


def test_errors_share_one_envelope_without_internals(tmp_path, monkeypatch, caplog):
    worker_dependencies = make_deps(ScriptedModel({}))
    seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    app = create_app(
        _settings(tmp_path, run_worker=False, dev_token="s3cret"),
        worker_dependencies=worker_dependencies,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        denied = client.get("/me")
        assert denied.status_code == 401 and denied.json()["error"]["code"] == "unauthenticated"
        # The browser app's origin: every envelope must carry CORS headers it can read.
        auth = {"Authorization": "Bearer s3cret", "Origin": "http://localhost:5173"}
        assert client.get("/me", headers=auth).status_code == 200
        missing = client.get("/no/such/route", headers=auth)
        assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"

        def explode(*args):
            raise RuntimeError("database password leaked in a trace")

        monkeypatch.setattr(app.state.service, "detail", explode)
        crashed = client.get("/chats/c1/requests/r1", headers=auth)
    assert crashed.status_code == 500
    assert crashed.json() == {"error": {"code": "internal", "message": INTERNAL_MESSAGE}}
    for response in (missing, crashed):
        assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "database password leaked" in caplog.text  # logged with its trace, never sent


def test_patch_me_validates_preferences_and_refuses_credentials(tmp_path):
    worker_dependencies = make_deps(ScriptedModel({}))
    with TestClient(
        create_app(_settings(tmp_path, run_worker=False), worker_dependencies=worker_dependencies)
    ) as client:
        saved = client.patch(
            "/me",
            json={
                "instructions": "Be brief.",
                "preferences": {"currency": "INR", "locale": "en-IN"},
            },
        )
        assert saved.status_code == 200
        secret = client.patch("/me", json={"instructions": "My key is AKIAIOSFODNN7EXAMPLE"})
        assert secret.status_code == 400
        assert secret.json()["error"]["code"] == "credential_in_instructions"
        for bad in [{"currency": "inr"}, {"locale": "en_IN"}, {"timezone": "Mars/Base"}]:
            refused = client.patch("/me", json={"preferences": bad})
            assert refused.status_code == 400, bad
        kept = client.patch("/me", json={"preferences": None, "instructions": "Be thorough."})
    assert kept.json()["preferences"] == {"currency": "INR", "locale": "en-IN"}
    assert kept.json()["instructions"] == "Be thorough."


def test_consumer_redelivers_a_start_that_failed_before_its_claim(monkeypatch, caplog):
    class RecordingQueue(InProcessQueue):
        def __init__(self) -> None:
            super().__init__()
            self.acked: list[WorkMessage] = []

        def ack(self, msg: WorkMessage) -> None:
            self.acked.append(msg)

    worker_dependencies = make_deps(ScriptedModel({}))
    worker_dependencies.queue = RecordingQueue()
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    saver = SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False))
    consumer = QueueConsumer(worker_dependencies, RequestRunner(worker_dependencies, saver))

    def throttled(*args, **kwargs):
        raise RuntimeError("throttled")

    monkeypatch.setattr(
        worker_dependencies.repositories.messages, "update_request_status", throttled
    )
    consumer.handle(WorkMessage(kind="start", chat_id="c1", message_id=req.message_id))
    assert (
        worker_dependencies.queue.acked == []
        and worker_dependencies.repositories.messages.get("c1", req.message_id).status == "ACCEPTED"
    )
    monkeypatch.undo()
    # A resume naming something other than a clarification answer is logged and acked.
    resume = WorkMessage(
        kind="resume", chat_id="c1", message_id=req.message_id, reply_message_id=req.message_id
    )
    with caplog.at_level(logging.WARNING):
        consumer.handle(resume)
    assert (
        worker_dependencies.queue.acked == [resume] and "not a clarification answer" in caplog.text
    )
    consumer.start()
    consumer.stop()
    consumer.start()  # a stopped consumer starts again on a new thread
    assert consumer.running
    consumer.stop()


def _two_task_plan() -> dict:
    """A plan of two sql_analysis tasks on a2, the second after the first."""
    task = {"kind": "sql_analysis", "inputArtifactIds": ["a2"]}
    return {
        "kind": "plan",
        "goal": "Compare September and October spending.",
        "constraints": [],
        "openQuestions": [],
        "tasks": [
            {**task, "ref": "t1", "goal": "Look at the first rows.", "dependsOn": []},
            {**task, "ref": "t2", "goal": "Total spend per month.", "dependsOn": ["t1"]},
        ],
        "nextAction": "Run t1, then t2.",
    }


def _traced_run(tmp_path, *, clarify: bool, **settings) -> tuple:
    """Run one request through the API on a scripted model: optionally one question, then two
    tasks, the first making one sql_query call. The client, the request path and the
    worker_dependencies."""
    query = ToolCall(
        id="x1", name="sql_query", arguments={"dataset_id": "a2", "sql": "SELECT * FROM t LIMIT 5"}
    )
    asks = [ModelResponse(structured=clarification("Which months?"))] if clarify else []
    worker_dependencies = make_deps(
        ScriptedModel(
            {
                "plan": [*asks, ModelResponse(structured=_two_task_plan())],
                "execute": [
                    ModelResponse(tool_calls=[query], stop_reason="tool_use"),
                    ModelResponse(structured=t1_result()),
                    ModelResponse(structured=t1_result()),
                ],
            }
        ),
        tasks_in_flight=1,
        settings=_settings(tmp_path, **settings),
    )
    seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    client = TestClient(
        create_app(worker_dependencies.settings, worker_dependencies=worker_dependencies)
    )
    return client, worker_dependencies


def test_the_trace_shows_the_planner_its_question_the_tasks_their_calls_and_tool_calls(tmp_path):
    """F6 Deep Dive: one request with a clarification, two tasks and one tool call; the trace
    tree, its events in order, and each call's transcript with the tool use and its result."""
    client, worker_dependencies = _traced_run(tmp_path, clarify=True)
    with client:
        request_id = client.post(
            "/chats/c1/requests", json={"content": "Compare"}, headers={"Idempotency-Key": "t"}
        ).json()["messageId"]
        path = f"/chats/c1/requests/{request_id}"
        assert _until(client, path, "WAITING_USER") == "WAITING_USER"
        waiting = client.get(path + "/trace").json()
        [question] = waiting["plan"]["clarifications"]
        client.post(
            path + "/replies",
            json={"inReplyToMessageId": question["messageId"], "content": "Sep and Oct"},
        )
        assert _until(client, path, "COMPLETED", "FAILED") == "COMPLETED"
        trace = client.get(path + "/trace").json()
        first_task_calls = trace["tasks"][0]["calls"]
        transcripts = [
            client.get(f"{path}/model-calls/{call['modelCallId']}/transcript").json()
            for call in first_task_calls
        ]
        foreign = client.get(
            f"/chats/c1/requests/other/model-calls/{first_task_calls[0]['modelCallId']}/transcript"
        )
        missing = client.get(f"{path}/model-calls/nope/transcript")
    assert waiting["request"]["status"] == "WAITING_USER" and waiting["tasks"] == []
    assert [e["kind"] for e in waiting["events"]] == ["planner_call", "clarification"]

    plan = trace["plan"]
    assert [c["stage"] for c in plan["calls"]] == ["plan", "plan"]
    # The report's summary call is the report's, not the planner's (F10 ruling 2).
    assert [c["stage"] for c in trace["report"]["calls"]] == ["summary"]
    assert all(c["hasTranscript"] and c["durationMs"] >= 0 for c in plan["calls"])
    assert [m["content"] for m in plan["clarifications"]] == ["Which months?", "Sep and Oct"]
    first, second = trace["tasks"]
    assert plan["tasksCreated"] == [first["taskId"], second["taskId"]]
    assert second["dependsOnTaskIds"] == [first["taskId"]] and first["inputArtifactIds"] == ["a2"]
    assert [len(t["calls"]) for t in trace["tasks"]] == [2, 1]
    [tool_call] = first["calls"][0]["toolCalls"]
    assert (
        tool_call["name"] == "sql_query"
        and tool_call["arguments"]["sql"] == "SELECT * FROM t LIMIT 5"
    )
    assert tool_call["status"] == "ok" and tool_call["rowCount"] == 5
    assert tool_call["resultInModelCallId"] == first["calls"][1]["modelCallId"]
    assert first["calls"][1]["toolCalls"] == [] and "transcriptRef" not in first["calls"][0]
    assert trace["report"]["claims"] > 0
    assert [e["kind"] for e in trace["events"]] == [
        "planner_call",
        "clarification",
        "clarification_answer",
        "planner_call",
        "task_created",
        "task_created",
        "task_started",
        "model_call",
        "tool_call",
        "model_call",
        "task_completed",
        "task_started",
        "model_call",
        "task_completed",
        "report",
    ]

    asked, answered = transcripts
    assert (
        asked["request"]["stage"] == "execute"
        and asked["request"]["tools"][0]["name"] == "sql_query"
    )
    assert asked["response"]["toolCalls"] == [
        {"id": "x1", "name": "sql_query", "arguments": tool_call["arguments"]}
    ]
    *_, used, result = answered["request"]["messages"]
    assert used["role"] == "assistant" and used["toolCalls"][0]["id"] == "x1"
    assert result["role"] == "tool" and result["toolCallId"] == "x1" and result["rowCount"] == 5
    assert answered["response"]["structured"]["summary"] == t1_result()["summary"]
    assert (foreign.status_code, missing.status_code) == (404, 404)
    assert missing.json()["error"]["code"] == "not_found"


def test_with_transcripts_off_no_call_has_one_and_the_route_is_404(tmp_path):
    client, worker_dependencies = _traced_run(tmp_path, clarify=False, record_transcripts=False)
    with client:
        request_id = client.post(
            "/chats/c1/requests", json={"content": "Compare"}, headers={"Idempotency-Key": "off"}
        ).json()["messageId"]
        path = f"/chats/c1/requests/{request_id}"
        assert _until(client, path, "COMPLETED", "FAILED") == "COMPLETED"
        trace = client.get(path + "/trace").json()
        calls = [*trace["plan"]["calls"], *(c for t in trace["tasks"] for c in t["calls"])]
        routes = {
            client.get(f"{path}/model-calls/{c['modelCallId']}/transcript").status_code
            for c in calls
        }
    assert len(calls) == 4 and not any(c["hasTranscript"] or c["toolCalls"] for c in calls)
    assert routes == {404}

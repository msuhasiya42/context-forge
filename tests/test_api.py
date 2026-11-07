import asyncio
import json
import logging
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.sqlite import SqliteSaver

from contextforge.api import main
from contextforge.api.main import create_app, event_stream
from contextforge.core.entities.message import ClarificationMessage, ClarificationResponseMessage
from contextforge.core.ids import now_iso
from contextforge.core.models.base import ModelResponse
from contextforge.core.models.scripted import ScriptedModel
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
    monkeypatch.setattr(main, "STREAM_MAX_SECONDS", 10.0)


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
    deps = make_deps(
        ScriptedModel(
            {
                "plan": [ModelResponse(structured=six_task_plan())],
                "execute": [ModelResponse(structured=t1_result())] * 3,
            }
        ),
        tasks_in_flight=1,
    )
    # An upload-free public flow is separately covered at the engine seam.
    seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    with TestClient(create_app(_settings(tmp_path), deps=deps)) as client:
        assert client.get("/health").json()["status"] == "ok"
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
    deps = make_deps(model, tasks_in_flight=1, settings=settings)
    seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    with TestClient(create_app(settings, deps=deps)) as client:
        client.patch("/me", json={"instructions": "Quote every figure in INR."})
        memory = memory_snapshot(deps.repos.users.get("u1"))
        request_id = client.post(
            "/chats/c1/requests",
            json={"content": "Compare September and October spending"},
            headers={"Idempotency-Key": "settings"},
        ).json()["messageId"]
        path = f"/chats/c1/requests/{request_id}"
        assert _until(client, path, "COMPLETED", "FAILED") == "COMPLETED"
        usage = client.get(path).json()["usage"]
    calls = deps.repos.model_calls.list_by_request(request_id)
    # The budgeted client reserved spend for each of the 4 calls; without settings it is 0.
    assert usage["modelCalls"] == 4 and usage["estimatedCostUsd"] == pytest.approx(0.4)
    assert "Quote every figure in INR." in planner_system[0]
    assert all(c.context_version.startswith(f"mem:{memory['version']}|") for c in calls)


def test_a_long_report_becomes_a_deliverable_that_the_content_route_serves(tmp_path):
    """F5 Part B: a report text over 1,500 characters is saved whole as a markdown deliverable;
    the chat keeps its first paragraph and a pointer, and only deliverables and raw results
    are served."""
    long_result = t1_result()
    for fact in long_result["facts"]:
        fact["text"] += " The statement rows give the detail." * 12
    deps = make_deps(
        ScriptedModel(
            {
                "plan": [ModelResponse(structured=six_task_plan())],
                "execute": [ModelResponse(structured=long_result)] * 3,
            }
        ),
        tasks_in_flight=1,
    )
    seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    with TestClient(create_app(_settings(tmp_path), deps=deps)) as client:
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
    assert len(report["content"]) < 1500 and report["content"].startswith(claims[0]["text"])
    assert "Full report attached: 6 findings" in report["content"]
    assert served.status_code == 200 and served.headers["content-type"].startswith("text/markdown")
    assert served.headers["x-content-type-options"] == "nosniff"
    body = served.text
    assert body.startswith("# Explain how October 2026 spending differs from September 2026.")
    assert all(claim["text"] in body for claim in claims)
    assert "Limitations:" in body and "## Evidence" in body and "## Coverage" in body
    artifact = deps.repos.artifacts.get("c1", ref["artifactId"])
    assert artifact.kind == "deliverable" and artifact.name == f"report-{request_id}.md"
    assert (dataset.status_code, missing.status_code) == (404, 404)
    assert dataset.json() == {"error": {"code": "not_found", "message": "Artifact not found."}}


def test_two_clarifications_each_close_the_stream_and_resume_the_same_request(tmp_path):
    deps = make_deps(
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
    seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    with TestClient(create_app(_settings(tmp_path), deps=deps)) as client:
        request = client.post(
            "/chats/c1/requests",
            json={"content": "Compare spending"},
            headers={"Idempotency-Key": "clarify"},
        ).json()
        path = f"/chats/c1/requests/{request['messageId']}"
        for question, answer in [("Which months?", "September"), ("Which accounts?", "ACC-1")]:
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
        assert any(k == "message" and r["kind"] == "report" for k, r in events)


def test_waiting_stream_stays_open_until_the_new_question_exists(tmp_path):
    # The worker writes WAITING_USER before the question. On a second question the first one
    # (already answered) is in the chat; the stream must wait for the new one, not close.
    deps = make_deps(ScriptedModel({}))
    req = seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    messages = deps.repos.messages
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
    with TestClient(create_app(_settings(tmp_path, run_worker=False), deps=deps)) as client:
        later.start()
        events = _events(client.get(f"/chats/c1/requests/{req.message_id}/events").text)
    asked = [r["content"] for k, r in events if k == "message" and r["kind"] == "clarification"]
    assert asked == ["Which months?", "Which accounts?"]
    assert events[-1][0] == "request" and events[-1][1]["status"] == "WAITING_USER"


def test_open_stream_heartbeats_on_wall_time_and_ends_at_its_cap():
    deps = make_deps(ScriptedModel({}))
    req = seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")

    async def frames() -> list[str]:
        stream = event_stream(
            Application(deps, "u1"), req, 0.01, heartbeat_seconds=0.05, max_seconds=0.3
        )
        return [frame async for frame in stream]

    started = time.monotonic()
    sent = asyncio.run(frames())
    assert 0.3 <= time.monotonic() - started < 2
    assert [f for f in sent if f.startswith("event:")] == [sent[0]]  # nothing changed after it
    assert 2 <= sent.count(": heartbeat\n\n") <= 7


def test_cancel_succeeds_and_one_that_loses_a_race_gets_the_409_envelope(tmp_path, monkeypatch):
    deps = make_deps(ScriptedModel({}))
    req = seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    messages = deps.repos.messages
    with TestClient(create_app(_settings(tmp_path, run_worker=False), deps=deps)) as client:
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
    deps = make_deps(ScriptedModel({}))
    seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    app = create_app(_settings(tmp_path, run_worker=False, dev_token="s3cret"), deps=deps)
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
    assert crashed.json() == {"error": {"code": "internal", "message": main.INTERNAL_MESSAGE}}
    for response in (missing, crashed):
        assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "database password leaked" in caplog.text  # logged with its trace, never sent


def test_patch_me_validates_preferences_and_refuses_credentials(tmp_path):
    deps = make_deps(ScriptedModel({}))
    with TestClient(create_app(_settings(tmp_path, run_worker=False), deps=deps)) as client:
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

    deps = make_deps(ScriptedModel({}))
    deps.queue = RecordingQueue()
    req = seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    saver = SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False))
    consumer = QueueConsumer(deps, RequestRunner(deps, saver))

    def throttled(*args, **kwargs):
        raise RuntimeError("throttled")

    monkeypatch.setattr(deps.repos.messages, "update_request_status", throttled)
    consumer.handle(WorkMessage(kind="start", chat_id="c1", message_id=req.message_id))
    assert (
        deps.queue.acked == []
        and deps.repos.messages.get("c1", req.message_id).status == "ACCEPTED"
    )
    monkeypatch.undo()
    # A resume naming something other than a clarification answer is logged and acked.
    resume = WorkMessage(
        kind="resume", chat_id="c1", message_id=req.message_id, reply_message_id=req.message_id
    )
    with caplog.at_level(logging.WARNING):
        consumer.handle(resume)
    assert deps.queue.acked == [resume] and "not a clarification answer" in caplog.text
    consumer.start()
    consumer.stop()
    consumer.start()  # a stopped consumer starts again on a new thread
    assert consumer.running
    consumer.stop()

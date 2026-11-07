import time

from fastapi.testclient import TestClient

from contextforge.api.main import create_app
from contextforge.core.models.base import ModelResponse
from contextforge.core.models.scripted import ScriptedModel
from contextforge.core.settings import Settings
from tests.helpers import make_deps, seed_chat_with_dataset, six_task_plan, t1_result


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
    settings = Settings(
        dev_user_id="u1", checkpoint_path=tmp_path / "checkpoint.sqlite", sse_poll_seconds=0.01
    )
    with TestClient(create_app(settings, deps=deps)) as client:
        assert client.get("/health").json()["status"] == "ok"
        user = client.patch(
            "/me", json={"instructions": "Use concise answers.", "preferences": {"currency": "INR"}}
        )
        assert user.status_code == 200 and user.json()["instructions"] == "Use concise answers."
        assert client.get("/chats/other/messages").status_code == 404
        chat = client.post("/chats", json={}).json()
        assert chat["userId"] == "u1"
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
        for _ in range(200):
            detail = client.get(f"/chats/c1/requests/{request_id}").json()
            if detail["request"]["status"] in {"COMPLETED", "FAILED"}:
                break
            time.sleep(0.01)
        assert detail["request"]["status"] == "COMPLETED"
        assert len(detail["tasks"]) == 6
        stream = client.get(f"/chats/c1/requests/{request_id}/events")
        assert "event: request" in stream.text and "COMPLETED" in stream.text
        assert client.post(f"/chats/c1/requests/{request_id}/cancel").status_code == 409
        page = client.get("/chats/c1/messages?limit=1").json()
        assert len(page["items"]) == 1 and page["nextCursor"]
        assert (
            client.get("/chats/c1/messages", params={"cursor": page["nextCursor"]}).status_code
            == 200
        )
        assert client.delete(f"/chats/{chat['chatId']}").status_code == 204


def test_api_clarification_reply_resumes_same_request_and_rejects_wrong_target(tmp_path):
    from tests.helpers import clarification

    deps = make_deps(
        ScriptedModel(
            {
                "plan": [
                    ModelResponse(structured=clarification("Which months?")),
                    ModelResponse(structured=six_task_plan()),
                ],
                "execute": [ModelResponse(structured=t1_result())] * 3,
            }
        ),
        tasks_in_flight=1,
    )
    seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    settings = Settings(
        dev_user_id="u1", checkpoint_path=tmp_path / "resume.sqlite", sse_poll_seconds=0.01
    )
    with TestClient(create_app(settings, deps=deps)) as client:
        request = client.post(
            "/chats/c1/requests",
            json={"content": "Compare spending"},
            headers={"Idempotency-Key": "clarify"},
        ).json()
        path = f"/chats/c1/requests/{request['messageId']}"
        for _ in range(200):
            if client.get(path).json()["request"]["status"] == "WAITING_USER":
                break
            time.sleep(0.01)
        messages = client.get("/chats/c1/messages").json()["items"]
        question = next(m for m in messages if m["kind"] == "clarification")
        assert (
            client.post(
                path + "/replies",
                json={"inReplyToMessageId": "wrong", "content": "September and October"},
            ).status_code
            == 409
        )
        reply = client.post(
            path + "/replies",
            json={"inReplyToMessageId": question["messageId"], "content": "September and October"},
        )
        assert reply.status_code == 201
        assert reply.json()["requestMessageId"] == request["messageId"]
        assert (
            client.post(
                path + "/replies",
                json={"inReplyToMessageId": question["messageId"], "content": "again"},
            ).status_code
            == 409
        )
        for _ in range(200):
            status = client.get(path).json()["request"]["status"]
            if status in {"COMPLETED", "FAILED"}:
                break
            time.sleep(0.01)
        assert status == "COMPLETED"

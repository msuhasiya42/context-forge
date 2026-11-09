"""API automation exercises the same saved records the UI reads, without live providers."""

import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from contextforge.api.main import create_app
from contextforge.core.models.scripted import ScriptedModel
from contextforge.core.settings import Settings
from contextforge.evals.api_research import submit_run, watch_run
from contextforge.infra.web import web_tools
from tests.helpers import make_deps
from tests.test_research_flow import SOURCE, ResearchFixtureModel
from tests.test_web_research import _page, _web


def test_api_submission_progress_and_capture_are_visible_in_the_ui_read_routes(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        model_backend="scripted",
        dev_user_id="u1",
        dev_token="local-test-token",
        call_reserve_usd=0.001,
        request_max_usd=0.25,
        checkpoint_path=tmp_path / "api.sqlite",
    )
    worker_dependencies = make_deps(ResearchFixtureModel(), settings=settings)
    worker_dependencies.budgets = replace(worker_dependencies.budgets, web_tool_call_budget=1)
    worker_dependencies.tools.update(
        web_tools(
            _web(lambda _: _page(f"<p>{SOURCE}</p>")),
            worker_dependencies.repositories,
            worker_dependencies.blobs,
        )
    )
    events = []
    out_dir = tmp_path / "run"
    with TestClient(
        create_app(settings, worker_dependencies=worker_dependencies),
        headers={"Authorization": "Bearer local-test-token"},
    ) as client:
        chat_id, request_id = submit_run(client, "Research the benchmark.", out_dir)
        code = watch_run(
            client,
            chat_id,
            request_id,
            out_dir,
            poll_seconds=0.01,
            timeout_seconds=10,
            emit=events.append,
        )
        assert code == 0
        assert events[-1]["status"] == "COMPLETED"
        assert events[-1]["webToolCalls"] == 1
        assert events[-1]["usage"]["modelCalls"] > 0
        chats = client.get("/chats").json()["items"]
        assert chats[0]["chatId"] == chat_id and chats[0]["title"].startswith("API: ")
        messages = client.get(f"/chats/{chat_id}/messages").json()["items"]
        assert {m["kind"] for m in messages} == {"request", "report"}
        metadata = json.loads((out_dir / "run.json").read_text())
        duplicate = client.post(
            f"/chats/{chat_id}/requests",
            json={"content": "Research the benchmark."},
            headers={"Idempotency-Key": metadata["idempotencyKey"]},
        )
        assert duplicate.json()["messageId"] == request_id
    assert SOURCE in (out_dir / "report.md").read_text()
    bundle = json.loads((out_dir / "bundle.json").read_text())
    assert bundle["snapshots"] and bundle["trace"]
    assert json.loads((out_dir / "result.json").read_text())["findings"] == 1


def test_old_build_and_unauthenticated_calls_never_submit_paid_work(tmp_path) -> None:
    writes = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method != "GET":
            writes.append(request)
        return httpx.Response(200, json={"status": "ok", "build": "old"})

    with httpx.Client(base_url="http://test", transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ValueError, match="Restart the API"):
            submit_run(client, "Question", tmp_path / "old")
    assert writes == []
    settings = Settings(
        _env_file=None,
        dev_token="required-test-token",
        run_worker=False,
        checkpoint_path=tmp_path / "auth.sqlite",
    )
    worker_dependencies = make_deps(ScriptedModel({}))
    with TestClient(create_app(settings, worker_dependencies=worker_dependencies)) as client:
        with pytest.raises(httpx.HTTPStatusError) as error:
            submit_run(client, "Question", tmp_path / "unauthenticated")
    assert error.value.response.status_code == 401
    assert worker_dependencies.repositories.chats.list_by_user(settings.dev_user_id) == []
    assert worker_dependencies.queue.receive() is None


def test_watching_a_timeout_keeps_ids_and_never_submits_or_cancels(tmp_path) -> None:
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path.endswith("/trace"):
            return httpx.Response(200, json={"events": []})
        return httpx.Response(
            200, json={"request": {"status": "RUNNING"}, "tasks": [], "usage": {}}
        )

    with httpx.Client(base_url="http://test", transport=httpx.MockTransport(respond)) as client:
        code = watch_run(client, "c1", "r1", tmp_path, timeout_seconds=0.000001)
    assert code == 2
    assert all(method == "GET" for method, _ in requests)
    assert json.loads((tmp_path / "result.json").read_text())["status"] == "WATCH_TIMEOUT"
    assert (tmp_path / "detail.json").exists() and (tmp_path / "trace.json").exists()

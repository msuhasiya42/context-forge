"""The judge on Gemini's OpenAI-compatible API, over httpx.MockTransport: no network."""

import json
import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import SecretStr

from contextforge.api.main import create_app
from contextforge.core.entities.model_call import Stage
from contextforge.core.models.base import ModelRequest
from contextforge.core.models.config import ModelRoute
from contextforge.core.models.errors import (
    ModelAccessDenied,
    ModelOverloaded,
    ModelRejected,
    ModelThrottled,
    ModelUnavailable,
    ModelUnreachable,
)
from contextforge.core.settings import Settings
from contextforge.evals import judge_client
from contextforge.evals.judge import _ClaimJudgements, judge_claims
from contextforge.evals.judge_client import judge_session, usable_route
from contextforge.evals.suite_cli import live_judge
from contextforge.infra.gemini import GeminiModel, RequestPacer, chat_payload
from contextforge.infra.runtime import (
    ByProvider,
    build_worker,
    model_provider,
    research_providers,
)
from contextforge.worker.dependencies import secret_values
from contextforge.worker.runner import RequestRunner
from tests.helpers import ByModel, make_deps, seed_chat_with_dataset, t1_result
from tests.test_judge import MODELS, _evidence

KEY = "test-gemini-key-do-not-leak"
BASE_URL = "https://gemini.test/v1beta/openai"
Handler = Callable[[httpx.Request], httpx.Response]


def verdicts_reply(*verdicts: str) -> httpx.Response:
    items = [{"id": str(n), "verdict": v, "reason": "Quoted."} for n, v in enumerate(verdicts, 1)]
    call = {
        "id": "call-1",
        "type": "function",
        "function": {"name": "return_result", "arguments": json.dumps({"verdicts": items})},
    }
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {"role": "assistant", "content": None, "tool_calls": [call]},
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 900, "completion_tokens": 80, "total_tokens": 980},
        },
    )


class FakeClock:
    """A monotonic clock that only moves when the pacer sleeps."""

    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def gemini(handler: Handler, clock: FakeClock | None = None) -> GeminiModel:
    clock = clock or FakeClock()
    return GeminiModel(
        httpx.Client(transport=httpx.MockTransport(handler)),
        SecretStr(KEY),
        base_url=BASE_URL,
        pacer=RequestPacer(4.5, clock=lambda: clock.now, sleep=clock.sleep),
        efforts={Stage.JUDGE: "low"},
    )


def test_a_structured_judge_call_round_trips_through_gemini_at_no_cost() -> None:
    sent: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return verdicts_reply("supported")

    bedrock = ByModel({})  # never called: every judge model answers on Gemini
    session = judge_session(ByProvider(bedrock, gemini(handle)), MODELS, provider=model_provider)
    [verdict] = judge_claims([_evidence(1)], model=session.client)

    assert verdict.verdict == "supported" and bedrock.sent == []
    [request] = sent
    assert str(request.url) == f"{BASE_URL}/chat/completions"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    body = json.loads(request.content)
    assert body["model"] == "gemini-3.5-flash" and body["max_tokens"] == 2000
    assert body["tool_choice"] == "required" and body["reasoning_effort"] == "low"
    [tool] = body["tools"]
    assert tool["function"]["name"] == "return_result"
    assert "$defs" not in json.dumps(tool["function"]["parameters"])  # refs inlined
    assert [m["role"] for m in body["messages"]] == ["system", "user"]

    [call] = session.calls.list_by_request(session.session_id)
    assert call.model == "gemini/gemini-3.5-flash"
    assert call.usage is not None and call.usage.input_tokens == 900
    assert call.estimated_cost_usd == 0.0 and session.spent_usd() == 0.0


def test_a_throttled_gemini_falls_back_paced_and_no_key_skips_gemini(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    models_sent: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        models_sent.append(model)
        if model == "gemini-3.5-flash":
            return httpx.Response(429, json={"error": {"message": "Resource exhausted."}})
        return verdicts_reply("partial", "supported")

    clock = FakeClock()
    session = judge_session(
        ByProvider(ByModel({}), gemini(handle, clock)), MODELS, retries=0, provider=model_provider
    )
    verdicts = judge_claims([_evidence(1), _evidence(2)], model=session.client)
    # The second batch call starts on the lite model: flash is cooling down after its 429.
    judge_claims([_evidence(3), _evidence(4)], model=session.client)

    assert [v.verdict for v in verdicts] == ["partial", "supported"]
    assert models_sent == ["gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.5-flash-lite"]
    # Three calls with no time passing between them: each after the first waits its slot.
    assert clock.sleeps == [4.5, 4.5]
    calls = session.calls.list_by_request(session.session_id)
    assert [c.model for c in calls] == [
        "gemini/gemini-3.5-flash",
        "gemini/gemini-3.5-flash-lite",
        "gemini/gemini-3.5-flash-lite",
    ]
    assert calls[0].failure_reason is not None and "429" in calls[0].failure_reason

    # Without a key a judge route grades with its Bedrock models, and says so once; the
    # all-Gemini judge (F17) has none, so the eval CLI refuses to start.
    monkeypatch.delenv("CONTEXTFORGE_GEMINI_API_KEY", raising=False)
    judge_client._warn_gemini_skipped.cache_clear()
    caplog.set_level(logging.WARNING, logger="contextforge.evals.judge_client")
    mixed = ModelRoute("gemini-3.5-flash", ("gemini-3.5-flash-lite", "deepseek.v3.2"))
    assert usable_route(mixed, gemini_key_set=True) == mixed
    assert usable_route(mixed, gemini_key_set=False) == ModelRoute("deepseek.v3.2")
    assert caplog.text.count("skips its Gemini models") == 1
    no_key = Settings(_env_file=None, record_transcripts=False)
    with pytest.raises(SystemExit, match="CONTEXTFORGE_GEMINI_API_KEY"):
        live_judge(no_key, tmp_path)


def test_gemini_failures_route_or_raise_and_never_carry_the_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    def time_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(f"no answer to {request.headers['authorization']}", request=request)

    def answer(response: httpx.Response) -> Handler:
        return lambda _: response

    cases: list[tuple[Handler, type[Exception]]] = [
        (answer(httpx.Response(429, json={"error": {"message": "Quota."}})), ModelThrottled),
        (answer(httpx.Response(503, json=[{"error": {"message": "Busy."}}])), ModelOverloaded),
        (answer(httpx.Response(404, text="not json")), ModelUnavailable),
        (answer(httpx.Response(403, json={"error": {"message": "Denied."}})), ModelAccessDenied),
        (answer(httpx.Response(400, json={"error": {"message": "Bad."}})), ModelRejected),
        (time_out, ModelUnreachable),
    ]
    for handler, error in cases:
        session = judge_session(gemini(handler), MODELS, route=ModelRoute("gemini-x"), retries=0)
        with pytest.raises(error) as raised:
            session.client.invoke(JUDGE_REQUEST)
        [call] = session.calls.list_by_request(session.session_id)
        assert KEY not in str(raised.value) and KEY not in (call.failure_reason or "")
    assert "ReadTimeout" in str(raised.value)
    assert KEY not in caplog.text
    # Transcripts and logs redact every configured secret, the Gemini key among them.
    assert KEY in secret_values(Settings(_env_file=None, gemini_api_key=SecretStr(KEY)))


JUDGE_REQUEST = ModelRequest(
    stage=Stage.JUDGE,
    model="gemini-x",
    system=["Judge."],
    messages=[HumanMessage("Claims.")],
    output_schema=_ClaimJudgements,
)


def chat_reply(message: dict[str, Any], finish_reason: str = "stop") -> httpx.Response:
    choice = {"message": {"role": "assistant", **message}, "finish_reason": finish_reason}
    usage = {"prompt_tokens": 500, "completion_tokens": 50}
    return httpx.Response(200, json={"choices": [choice], "usage": usage})


def function_call(call_id: str, name: str, arguments: dict[str, Any], **extra: Any) -> dict:
    function = {"name": name, "arguments": json.dumps(arguments)}
    return {"id": call_id, "type": "function", "function": function, **extra}


ONE_TASK_PLAN = {
    "kind": "plan",
    "goal": "Count the transactions.",
    "constraints": [],
    "openQuestions": [],
    "tasks": [
        {
            "ref": "t1",
            "kind": "sql_analysis",
            "goal": "Count the rows.",
            "inputArtifactIds": ["a2"],
            "dependsOn": [],
        }
    ],
    "nextAction": "Run t1.",
}
SIGNATURE = {"google": {"thought_signature": "sig-from-flash"}}


def test_a_research_run_plans_and_runs_its_tool_loop_on_gemini() -> None:
    """F17: plan's forced structured call falls back from a throttled flash to flash-lite;
    execute's tool loop sends the assistant's tool call back with its id and thought signature,
    then the tool result under that id, and reads the result the model returns."""
    sent: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        tools = {tool["function"]["name"] for tool in body.get("tools", [])}
        if len(sent) == 1:  # plan, on flash: the free tier's per-minute limit
            return httpx.Response(429, json={"error": {"message": "Resource exhausted."}})
        if len(sent) == 2:
            return chat_reply(
                {"tool_calls": [function_call("p1", "return_result", ONE_TASK_PLAN)]},
                "tool_calls",
            )
        if "sql_query" in tools and not any(m["role"] == "tool" for m in body["messages"]):
            query = {"dataset_id": "a2", "sql": "SELECT count(*) AS n FROM t"}
            call = function_call("fc-1", "sql_query", query, extra_content=SIGNATURE)
            return chat_reply({"content": None, "tool_calls": [call]}, "tool_calls")
        if "sql_query" in tools:
            result = function_call("r1", "return_result", t1_result())
            return chat_reply({"tool_calls": [result]}, "tool_calls")
        return httpx.Response(400, json={"error": {"message": "Not scripted."}})

    worker_dependencies = make_deps(gemini(handle), tasks_in_flight=1)
    worker_dependencies.model_provider = model_provider
    request = seed_chat_with_dataset(
        worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2"
    )
    saver = SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False))
    RequestRunner(worker_dependencies, saver).start("c1", request.message_id)

    plan_429, plan, first_turn, second_turn = sent[:4]
    assert [plan_429["model"], plan["model"]] == ["gemini-3.5-flash", "gemini-3.5-flash-lite"]
    assert plan["tool_choice"] == "required"
    assert "tool_choice" not in first_turn  # tools of its own: the model chooses
    assistant = next(m for m in second_turn["messages"] if m.get("tool_calls"))
    [echoed] = assistant["tool_calls"]
    assert echoed["id"] == "fc-1" and echoed["extra_content"] == SIGNATURE
    assert json.loads(echoed["function"]["arguments"])["dataset_id"] == "a2"
    [tool_result] = [m for m in second_turn["messages"] if m["role"] == "tool"]
    assert tool_result["tool_call_id"] == "fc-1" and tool_result["content"]

    calls = worker_dependencies.repositories.model_calls.list_by_request(request.message_id)
    executed = [c for c in calls if c.stage == Stage.EXECUTE]
    assert [c.stop_reason for c in executed][:2] == ["tool_use", "end_turn"]
    assert all(c.model.startswith("gemini/gemini-3.5-flash") for c in calls)
    assert all(c.usage is not None and c.usage.input_tokens == 500 for c in executed)
    [task] = worker_dependencies.repositories.tasks.list_by_request(request.message_id)
    assert task.status.value == "COMPLETED"


def test_a_tool_call_without_a_kept_signature_is_sent_with_googles_stand_in() -> None:
    """A turn written before F17 or by another model has no signature: the first call carries
    the stand-in, so Gemini 3 does not refuse the history."""
    turn = AIMessage(
        content="",
        tool_calls=[
            {"name": "web_search", "args": {"query": "a"}, "id": "x1"},
            {"name": "web_search", "args": {"query": "b"}, "id": "x2"},
        ],
    )
    request = ModelRequest(
        stage=Stage.EXECUTE,
        model="gemini-3.5-flash",
        system=["Research."],
        messages=[HumanMessage("Go."), turn],
    )
    first, second = chat_payload(request)["messages"][-1]["tool_calls"]
    stand_in = first["extra_content"]["google"]["thought_signature"]
    assert stand_in == "skip_thought_signature_validator" and "extra_content" not in second


def test_the_worker_needs_the_gemini_key_at_startup_and_health_names_the_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("CONTEXTFORGE_GEMINI_API_KEY", raising=False)
    common: dict[str, Any] = {
        "_env_file": None,
        "aws_profile": None,
        "model_backend": "bedrock",
        "run_worker": False,
        "checkpoint_path": tmp_path / "checkpoints.sqlite",
    }
    with pytest.raises(ValueError, match="CONTEXTFORGE_GEMINI_API_KEY"):
        build_worker(Settings(**common))

    keyed = Settings(**common, gemini_api_key=SecretStr(KEY))
    worker_dependencies = build_worker(keyed)
    assert isinstance(worker_dependencies.model, ByProvider)
    assert research_providers(worker_dependencies.routes) == {"gemini"}
    health = TestClient(create_app(keyed, worker_dependencies=worker_dependencies))
    assert health.get("/health").json()["model"] == "gemini"

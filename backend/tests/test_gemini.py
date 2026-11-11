"""The judge on Gemini's OpenAI-compatible API, over httpx.MockTransport: no network."""

import json
import logging
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from langchain_core.messages import HumanMessage
from pydantic import SecretStr

from contextforge.core.entities.model_call import Stage
from contextforge.core.models.base import ModelRequest
from contextforge.core.models.config import ModelRoute
from contextforge.core.models.errors import (
    ModelAccessDenied,
    ModelRejected,
    ModelThrottled,
    ModelUnavailable,
    ModelUnreachable,
)
from contextforge.core.settings import Settings
from contextforge.evals import judge_client
from contextforge.evals.judge import _ClaimJudgements, judge_claims
from contextforge.evals.judge_client import judge_route, judge_session, usable_route
from contextforge.evals.suite_cli import live_judge
from contextforge.infra.gemini import GeminiModel, RequestPacer
from contextforge.infra.runtime import ByProvider, model_provider
from contextforge.worker.dependencies import secret_values
from tests.helpers import ByModel
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
        reasoning_effort="low",
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

    # Without a key the judge grades with its Bedrock fallback, and says so once.
    monkeypatch.delenv("CONTEXTFORGE_GEMINI_API_KEY", raising=False)
    judge_client._warn_gemini_skipped.cache_clear()
    caplog.set_level(logging.WARNING, logger="contextforge.evals.judge_client")
    route = judge_route(MODELS)
    assert usable_route(route, gemini_key_set=True) == route
    assert usable_route(route, gemini_key_set=False) == ModelRoute("deepseek.v3.2")
    no_key = Settings(_env_file=None, record_transcripts=False)
    assert live_judge(no_key, tmp_path).route == ModelRoute("deepseek.v3.2")
    assert caplog.text.count("skips its Gemini models") == 1
    with pytest.raises(ValueError, match="CONTEXTFORGE_GEMINI_API_KEY"):
        usable_route(ModelRoute("gemini-3.5-flash"), gemini_key_set=False)


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
        (answer(httpx.Response(503, json=[{"error": {"message": "Busy."}}])), ModelUnavailable),
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

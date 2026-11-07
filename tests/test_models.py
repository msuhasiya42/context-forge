# tests/test_models.py
import json
import threading
import time

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from contextforge.core.context.prompts import load_prompt, prompt_version
from contextforge.core.entities.model_call import Stage
from contextforge.core.models.base import ModelClient, ModelRequest, ModelResponse, ToolSpec
from contextforge.core.models.config import ModelRoute, load_models_config
from contextforge.core.models.errors import (
    ModelAccessDenied,
    ModelError,
    ModelRejected,
    ModelThrottled,
    ModelTimeout,
    ModelUnavailable,
)
from contextforge.core.models.recording import RecordingModelClient, price_row
from contextforge.core.models.scripted import ScriptedModel, ScriptExhausted
from contextforge.core.models.transcript import TranscriptWriter
from contextforge.core.repositories.memory import Repositories
from contextforge.core.settings import Settings
from contextforge.core.storage import MemoryBlobStore
from contextforge.core.values import Usage
from contextforge.infra.runtime import stage_routes
from contextforge.worker import calls
from contextforge.worker.deps import WorkerDeps
from contextforge.worker.routing import ModelCooldown
from tests.helpers import ByModel, make_deps


def _req(stage: str = "plan") -> ModelRequest:
    return ModelRequest(stage=stage, model="scripted", system=["s"], messages=[])


def test_scripted_model_returns_in_order_then_exhausts() -> None:
    m = ScriptedModel({"plan": [ModelResponse(text="one"), ModelResponse(text="two")]})
    assert m.invoke(_req()).text == "one"
    assert m.invoke(_req()).text == "two"
    with pytest.raises(ScriptExhausted):
        m.invoke(_req())


def test_recording_client_writes_one_model_call_per_invocation() -> None:
    repos = Repositories.in_memory()
    inner = ScriptedModel(
        {"plan": [ModelResponse(text="x")], "execute": [ModelResponse(text="y", replayed=True)]}
    )
    client = RecordingModelClient(
        inner,
        repos.model_calls,
        request_message_id="m1",
        memory_version="1",
        price_version="2026-11-04",
    )
    client.invoke(_req("plan"))
    client.invoke(_req("execute"))
    calls = repos.model_calls.list_by_request("m1")
    assert [c.stage for c in calls] == ["plan"]
    assert calls[0].context_version == "mem:1|prompt:plan.4"


def test_recording_client_records_error_and_reraises() -> None:
    repos = Repositories.in_memory()
    client = RecordingModelClient(
        ScriptedModel({}),
        repos.model_calls,
        request_message_id="m1",
        memory_version="1",
        price_version="2026-11-04",
    )
    with pytest.raises(ScriptExhausted):
        client.invoke(_req("plan"))
    assert repos.model_calls.list_by_request("m1")[0].outcome == "ERROR"


def test_recording_client_records_price_stop_reason_and_order_free_tool_set_hash() -> None:
    repos = Repositories.in_memory()
    reply = ModelResponse(stop_reason="tool_use", usage=Usage(input_tokens=10, output_tokens=2))
    client = RecordingModelClient(
        ScriptedModel({"execute": [reply, reply]}),
        repos.model_calls,
        request_message_id="m1",
        memory_version="1",
        price_version="2026-11-04",
    )
    tools = [
        ToolSpec(name="b", description="B", input_schema={"type": "object"}),
        ToolSpec(name="a", description="A", input_schema={"type": "object"}),
    ]
    for tool_order in (tools, tools[::-1]):
        client.invoke(
            ModelRequest(
                stage="execute",
                model="scripted",
                system=[],
                messages=[],
                tools=tool_order,
                task_id="t1",
            )
        )
    calls = repos.model_calls.list_by_request("m1")
    assert len(calls) == 2
    assert all(c.price_version == "2026-11-04" and c.stop_reason == "tool_use" for c in calls)
    assert all(c.task_id == "t1" and c.usage == reply.usage for c in calls)
    assert calls[0].tool_set_hash is not None
    assert calls[0].tool_set_hash == calls[1].tool_set_hash


def test_a_transcript_is_stored_per_call_redacted_and_never_fails_the_call() -> None:
    """F6: the exact request and the answer (or error) beside each ModelCall, known secrets
    redacted; a store that fails leaves the call recorded without a transcript."""
    repos, blobs = Repositories.in_memory(), MemoryBlobStore()
    asked = AIMessage("", tool_calls=[{"id": "x1", "name": "web_search", "args": {"q": "k"}}])
    result = ToolMessage(
        "page text with KEY-123",
        tool_call_id="x1",
        additional_kwargs={"status": "ok", "row_count": 0, "artifact_id": "a9"},
        response_metadata={"headers": {"x-api-key": "KEY-123"}},
    )
    request = ModelRequest(
        stage="execute",
        model="scripted",
        system=["memory"],
        messages=[HumanMessage("brief"), asked, result],
        tools=[ToolSpec(name="web_search", description="Search", input_schema={"type": "object"})],
        task_id="t1",
    )

    class FailingBlobs(MemoryBlobStore):
        def put(self, key: str, data: bytes) -> str:
            raise OSError("bucket unreachable")

    def recorder(model: ModelClient, store: MemoryBlobStore) -> RecordingModelClient:
        writer = TranscriptWriter(store, secrets=["KEY-123", ""])
        return RecordingModelClient(
            model,
            repos.model_calls,
            request_message_id="m1",
            memory_version="1",
            price_version="p",
            transcripts=writer,
        )

    answer = ModelResponse(text="done", usage=Usage(input_tokens=5, output_tokens=1))
    recorder(ScriptedModel({"execute": [answer]}), blobs).invoke(request)
    with pytest.raises(ScriptExhausted):
        recorder(ScriptedModel({}), blobs).invoke(request)
    unstored_answer = recorder(ScriptedModel({"execute": [answer]}), FailingBlobs()).invoke(request)
    assert unstored_answer.text == answer.text
    answered, failed, unstored = repos.model_calls.list_by_request("m1")
    stored = json.loads(blobs.get(answered.transcript_ref))
    assert answered.transcript_ref == f"mem://transcripts/m1/{answered.model_call_id}.json"
    assert stored["request"]["messages"] == [
        {"role": "user", "content": "brief"},
        {
            "role": "assistant",
            "content": "",
            "toolCalls": [{"id": "x1", "name": "web_search", "arguments": {"q": "k"}}],
        },
        {
            "role": "tool",
            "content": "page text with [redacted]",
            "toolCallId": "x1",
            "status": "ok",
            "rowCount": 0,
            "artifactId": "a9",
        },
    ]
    assert stored["request"]["system"] == ["memory"] and stored["request"]["taskId"] == "t1"
    assert stored["response"]["text"] == "done" and stored["error"] is None
    assert "KEY-123" not in blobs.get(answered.transcript_ref).decode()
    errored = json.loads(blobs.get(failed.transcript_ref))
    assert errored["response"] is None and "no scripted response" in errored["error"]
    assert unstored.outcome == "COMPLETED" and unstored.transcript_ref is None


def test_models_config_loads_the_shipped_routes_each_model_priced() -> None:
    """F9: the bake-off's planners, Qwen3-235B executing, Nova 2 Lite compressing; every model
    a stage may call has a price row, so no call is charged the reserve for want of one."""
    config = load_models_config()
    assert config.price_version == "2026-11-07"
    assert config.stages["plan"].route == ModelRoute(
        "openai.gpt-oss-120b-1:0",
        ("qwen.qwen3-235b-a22b-2507-v1:0", "global.amazon.nova-2-lite-v1:0"),
    )
    assert config.stages["execute"].route == ModelRoute(
        "qwen.qwen3-235b-a22b-2507-v1:0", ("openai.gpt-oss-120b-1:0", "apac.amazon.nova-pro-v1:0")
    )
    assert config.stages["compress"].route == ModelRoute(
        "global.amazon.nova-2-lite-v1:0", ("apac.amazon.nova-lite-v1:0",)
    )
    routed = {m for stage in config.stages.values() for m in (stage.model, *stage.fallbacks)}
    assert [m for m in routed if price_row(config.prices, m) is None] == []


def test_a_call_is_priced_from_its_usage_and_unknown_usage_is_charged_the_reserve() -> None:
    config = load_models_config()
    repos = Repositories.in_memory()
    usage = Usage(input_tokens=31_920, output_tokens=2_724)  # the live run's ten calls in one

    class TimesOut:
        def invoke(self, request: ModelRequest) -> ModelResponse:
            raise ModelTimeout("no answer")

    def recorder(inner: ModelClient) -> RecordingModelClient:
        return RecordingModelClient(
            inner,
            repos.model_calls,
            request_message_id="m1",
            memory_version="1",
            price_version=config.price_version,
            prices=config.prices,
            unknown_cost_usd=0.10,
        )

    pro = ModelRequest(stage="plan", model="apac.amazon.nova-pro-v1:0", system=[], messages=[])
    recorder(ScriptedModel({"plan": [ModelResponse(usage=usage)]})).invoke(pro)
    with pytest.raises(ModelTimeout):
        recorder(TimesOut()).invoke(pro)
    priced, timed_out = repos.model_calls.list_by_request("m1")
    assert priced.model == "bedrock/apac.amazon.nova-pro-v1:0"
    assert priced.price_version == "2026-11-07"
    # Sydney list price (F9): $0.84 / $3.36 per million tokens
    assert priced.estimated_cost_usd == pytest.approx((31_920 * 0.84 + 2_724 * 3.36) / 1e6)
    assert timed_out.usage is None and timed_out.estimated_cost_usd == 0.10


def test_spend_cap_admits_more_than_ten_cheap_calls() -> None:
    """Live finding L-1 (on Nova Lite): the flat reserve recorded per call refused the eleventh
    call."""
    reply = ModelResponse(text="ok", usage=Usage(input_tokens=3_192, output_tokens=272))
    deps = make_deps(ScriptedModel({"execute": [reply] * 12}))
    deps.settings = Settings(_env_file=None, request_max_usd=1.0, call_reserve_usd=0.10)
    deps.prices = load_models_config().prices
    request = ModelRequest(
        stage="execute", model="apac.amazon.nova-lite-v1:0", system=[], messages=[]
    )
    model = deps.model_for("m1")
    for _ in range(12):
        model.invoke(request)
    calls = deps.repos.model_calls.list_by_request("m1")
    assert len(calls) == 12 and sum(c.estimated_cost_usd or 0 for c in calls) < 0.01


def _capped_deps(model: ModelClient, max_usd: float, **settings: float) -> WorkerDeps:
    """Deps on the production call path at ``max_usd`` per request, with config prices."""
    deps = make_deps(model)
    deps.settings = Settings(_env_file=None, request_max_usd=max_usd, **settings)
    deps.prices = load_models_config().prices
    return deps


def test_three_concurrent_calls_are_all_admitted_under_the_smoke_cap() -> None:
    """Re-review I-1: a flat $0.10 reserve let only two calls run in flight under the $0.25
    cap. Each call now reserves its own worst case (the executor's at the context cap: ~$0.013
    for Qwen3-235B), so three parallel tasks run. Each call waits at the barrier until all
    three are in flight."""
    in_flight = threading.Barrier(3, timeout=5)
    reply = ModelResponse(text="ok", usage=Usage(input_tokens=3_000, output_tokens=300))

    class WaitsForTheOthers:
        def invoke(self, request: ModelRequest) -> ModelResponse:
            in_flight.wait()
            return reply

    deps = _capped_deps(WaitsForTheOthers(), 0.25, call_reserve_usd=0.10)
    request = ModelRequest(
        stage="execute", model=deps.model_id(Stage.EXECUTE), system=[], messages=[]
    )
    errors: list[Exception] = []

    def call() -> None:
        try:
            deps.model_for("m1").invoke(request)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=call) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == [] and len(deps.repos.model_calls.list_by_request("m1")) == 3
    assert deps.pending_calls["m1"] == []


def test_spend_cap_refuses_a_call_whose_worst_case_would_pass_it() -> None:
    """A Nova Pro planner call reserves its worst case (~$0.047); after $0.01 spent, a $0.05 cap
    refuses it before the provider is called, and nothing is recorded for it."""
    spent = ModelResponse(text="ok", usage=Usage(input_tokens=10_000, output_tokens=625))
    model = ScriptedModel({"plan": [spent, spent]})
    deps = _capped_deps(model, 0.05)
    request = ModelRequest(stage="plan", model="apac.amazon.nova-pro-v1:0", system=[], messages=[])
    budgeted = deps.model_for("m1")
    budgeted.invoke(request)
    with pytest.raises(ModelError, match="^Request spend budget exhausted\\.$"):
        budgeted.invoke(request)
    assert len(deps.repos.model_calls.list_by_request("m1")) == 1
    assert model.invoke(request) is spent  # the refused call never consumed the script


def test_deadline_refuses_a_call_once_the_segment_has_run_past_it() -> None:
    model = ScriptedModel({"execute": [ModelResponse(text="never")]})
    deps = _capped_deps(model, 1.0, request_deadline_seconds=1)
    deps.segment_started["m1"] = time.monotonic() - 2
    request = ModelRequest(
        stage="execute", model=deps.model_id(Stage.EXECUTE), system=[], messages=[]
    )
    with pytest.raises(ModelError, match="^Request deadline exceeded\\.$"):
        deps.model_for("m1").invoke(request)
    assert deps.repos.model_calls.list_by_request("m1") == []


def test_prompt_version_mapping() -> None:
    stages = (Stage.PLAN, Stage.EXECUTE, Stage.COMPRESS, Stage.CITE)
    assert [prompt_version(s) for s in stages] == ["plan.4", "execute.6", "compress.2", "none"]


def test_load_prompt_reads_file() -> None:
    assert load_prompt("plan.1")


def test_a_call_goes_to_the_fallback_while_its_model_is_down_then_back_after_the_cooldown(
    monkeypatch,
) -> None:
    """F9: the primary fails after the bounded retry, the fallback answers and is recorded as
    the model that answered; for 60 s the primary is tried last, then first again. A model the
    account may not invoke routes at once, with no retry."""
    answer = ModelResponse(text="ok", usage=Usage(input_tokens=10, output_tokens=2))
    down = ModelUnavailable("ServiceUnavailableException")
    model = ByModel({"primary": [down, down, answer], "fallback": [answer, answer]})
    deps = _capped_deps(model, 1.0)
    deps.routes = {Stage.PLAN: ModelRoute("primary", ("fallback",))}
    clock = [1000.0]
    deps.cooldown = ModelCooldown(clock=lambda: clock[0])
    monkeypatch.setattr(calls.time, "sleep", lambda seconds: None)
    request = ModelRequest(stage="plan", model=deps.model_id(Stage.PLAN), system=[], messages=[])

    assert deps.model_for("m1").invoke(request).text == "ok"
    deps.model_for("m1").invoke(request)  # in the cooldown: the fallback first
    clock[0] += 61
    deps.model_for("m1").invoke(request)
    assert model.sent == ["primary", "primary", "fallback", "fallback", "primary"]
    records = deps.repos.model_calls.list_by_request("m1")
    assert [(c.model, c.outcome) for c in records] == [
        ("bedrock/primary", "ERROR"),
        ("bedrock/primary", "ERROR"),
        ("bedrock/fallback", "COMPLETED"),
        ("bedrock/fallback", "COMPLETED"),
        ("bedrock/primary", "COMPLETED"),
    ]
    assert records[2].metadata == {"requested_model": "primary"} and records[4].metadata == {}

    denied = ByModel(
        {"kimi": [ModelAccessDenied("not available for this account")], "deepseek": [answer]}
    )
    deps = _capped_deps(denied, 1.0)
    deps.routes = {Stage.PLAN: ModelRoute("kimi", ("deepseek",))}
    request = request.model_copy(update={"model": "kimi"})
    assert deps.model_for("m2").invoke(request).text == "ok"
    assert denied.sent == ["kimi", "deepseek"]


def test_a_failure_another_model_cannot_fix_is_not_routed() -> None:
    """A refused answer, an exhausted budget or the last fallback down is raised as it is."""
    model = ByModel({"primary": [ModelRejected("guardrail_intervened")], "fallback": []})
    deps = make_deps(model)
    deps.routes = {Stage.EXECUTE: ModelRoute("primary", ("fallback",))}
    request = ModelRequest(stage="execute", model="primary", system=[], messages=[])
    with pytest.raises(ModelRejected):
        deps.model_for("m1").invoke(request)
    assert model.sent == ["primary"]

    down = ModelThrottled("ThrottlingException")
    model = ByModel({"primary": [down], "fallback": [down]})
    deps = make_deps(model)
    deps.routes = {Stage.EXECUTE: ModelRoute("primary", ("fallback",))}
    with pytest.raises(ModelThrottled):
        deps.model_for("m1").invoke(request)
    assert model.sent == ["primary", "fallback"]
    assert deps.cooldown.order(["primary", "fallback", "other"]) == ["other", "primary", "fallback"]


def test_settings_override_the_primary_and_the_fallbacks_of_their_stages() -> None:
    config = load_models_config()
    settings = Settings(
        _env_file=None,
        planner_model="global.moonshotai.kimi-k3.1",
        planner_fallbacks=" deepseek.v3.2, apac.amazon.nova-pro-v1:0 ,",
        executor_fallbacks="",
    )
    routes = stage_routes(config, settings)
    expected_plan = ModelRoute(
        "global.moonshotai.kimi-k3.1", ("deepseek.v3.2", "apac.amazon.nova-pro-v1:0")
    )
    assert routes[Stage.PLAN] == routes[Stage.CITE] == expected_plan
    assert routes[Stage.EXECUTE] == config.stages["execute"].route  # empty keeps the config
    assert routes[Stage.COMPRESS] == config.stages["compress"].route

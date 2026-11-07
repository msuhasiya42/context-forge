# tests/test_models.py
import pytest

from contextforge.core.context.prompts import load_prompt, prompt_version
from contextforge.core.entities.model_call import Stage
from contextforge.core.models.base import ModelClient, ModelRequest, ModelResponse, ToolSpec
from contextforge.core.models.config import load_models_config
from contextforge.core.models.errors import ModelTimeout
from contextforge.core.models.recording import RecordingModelClient
from contextforge.core.models.scripted import ScriptedModel, ScriptExhausted
from contextforge.core.repositories.memory import Repositories
from contextforge.core.settings import Settings
from contextforge.core.values import Usage
from tests.helpers import make_deps


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
    assert calls[0].context_version == "mem:1|prompt:plan.3"


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


def test_models_config_loads_price_version_and_stages() -> None:
    config = load_models_config()
    assert config.price_version == "2026-11-07"
    assert config.stages["plan"].model == "apac.amazon.nova-pro-v1:0"
    assert config.stages["execute"].model == "apac.amazon.nova-lite-v1:0"


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
    assert priced.estimated_cost_usd == pytest.approx((31_920 * 0.80 + 2_724 * 3.20) / 1e6)
    assert timed_out.usage is None and timed_out.estimated_cost_usd == 0.10


def test_spend_cap_admits_more_than_ten_cheap_calls() -> None:
    """Live finding L-1: the flat reserve recorded per call refused the eleventh call."""
    reply = ModelResponse(text="ok", usage=Usage(input_tokens=3_192, output_tokens=272))
    deps = make_deps(ScriptedModel({"execute": [reply] * 12}))
    deps.settings = Settings(_env_file=None, request_max_usd=1.0, call_reserve_usd=0.10)
    deps.prices = load_models_config().prices
    request = ModelRequest(stage="execute", model=deps.executor_model, system=[], messages=[])
    model = deps.model_for("m1")
    for _ in range(12):
        model.invoke(request)
    calls = deps.repos.model_calls.list_by_request("m1")
    assert len(calls) == 12 and sum(c.estimated_cost_usd or 0 for c in calls) < 0.01


def test_prompt_version_mapping() -> None:
    stages = (Stage.PLAN, Stage.EXECUTE, Stage.COMPRESS, Stage.CITE)
    assert [prompt_version(s) for s in stages] == ["plan.3", "execute.3", "compress.2", "none"]


def test_load_prompt_reads_file() -> None:
    assert load_prompt("plan.1")

# tests/test_models.py
import pytest

from contextforge.core.context.prompts import load_prompt, prompt_version
from contextforge.core.entities.model_call import Stage
from contextforge.core.models.base import ModelRequest, ModelResponse, ToolSpec
from contextforge.core.models.config import load_models_config
from contextforge.core.models.recording import RecordingModelClient
from contextforge.core.models.scripted import ScriptedModel, ScriptExhausted
from contextforge.core.repositories.memory import Repositories
from contextforge.core.values import Usage


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
    assert calls[0].context_version == "mem:1|prompt:plan.2"


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
    assert config.price_version == "2026-11-04"
    assert config.stages["plan"].model == "scripted"


def test_prompt_version_mapping() -> None:
    assert prompt_version(Stage.PLAN) == "plan.2"
    assert prompt_version(Stage.CITE) == "none"


def test_load_prompt_reads_file() -> None:
    assert load_prompt("plan.1")

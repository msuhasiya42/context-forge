# tests/test_compaction.py
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from contextforge.core.context.builder import messages_tokens
from contextforge.core.context.compaction import (
    compact,
    stub_old_tool_results,
    summarize_messages,
)
from contextforge.core.models.base import ModelResponse
from contextforge.core.models.scripted import ScriptedModel


def _tool(i: int, big: bool = False) -> ToolMessage:
    return ToolMessage(
        content=("x" * 4000) if big else "r",
        tool_call_id=f"t{i}",
        additional_kwargs={
            "tool_name": "sql_query",
            "arguments": {"sql": f"q{i}"},
            "row_count": 3,
            "artifact_id": None,
        },
    )


def _turns(n: int, big: bool = False) -> list:
    out: list = [HumanMessage(content="task")]
    for i in range(n):
        out += [
            AIMessage(
                content="",
                tool_calls=[{"id": f"t{i}", "name": "sql_query", "args": {"sql": f"q{i}"}}],
            ),
            _tool(i, big),
        ]
    return out


def test_level1_stubs_only_old_tool_results() -> None:
    msgs = stub_old_tool_results(_turns(4, big=True), keep_turns=2)
    tools = [m for m in msgs if isinstance(m, ToolMessage)]
    assert tools[0].content.startswith("[tool sql_query") and tools[1].content.startswith("[tool")
    assert tools[2].content == "x" * 4000 and tools[3].content == "x" * 4000
    failed = _turns(4, big=True)
    failed[2].additional_kwargs["status"] = "error"  # t0, older than the last two turns
    assert "outcome=error" in stub_old_tool_results(failed, keep_turns=2)[2].content


def test_level3_never_splits_a_tool_call_from_its_result() -> None:
    model = ScriptedModel({"compress": [ModelResponse(text="did q0..q2")]})
    msgs = _turns(5)  # 11 messages; keep_last=5 would land on a ToolMessage
    out = summarize_messages(msgs, keep_last=5, model=model, memory="M")
    assert out[0].content.startswith("Summary of earlier work")
    assert isinstance(out[1], AIMessage) and out[1].tool_calls
    assert len(out) == 1 + 6


def test_compact_uses_level1_without_model_when_enough() -> None:
    model = ScriptedModel({})  # any compress call would raise ScriptExhausted
    out = compact(_turns(4, big=True), budget_tokens=2500, keep_last=6, model=model, memory="M")
    assert any(isinstance(m, ToolMessage) and m.content.startswith("[tool") for m in out)
    assert messages_tokens(out) <= 2500


def test_level1_is_idempotent() -> None:
    once = stub_old_tool_results(_turns(4, big=True), keep_turns=2)
    twice = stub_old_tool_results(once, keep_turns=2)
    assert [m.content for m in once] == [m.content for m in twice]


def test_repeated_compaction_keeps_tool_call_result_pairs() -> None:
    # compaction only receives messages; request state fields are not passed and cannot be lost
    model = ScriptedModel({"compress": [ModelResponse(text="s")] * 3})
    msgs = _turns(12, big=True)
    for _ in range(3):
        msgs = compact(msgs, budget_tokens=1500, keep_last=6, model=model, memory="M")
        msgs += _turns(2, big=True)[1:]
    assert sum(1 for m in msgs if isinstance(m, AIMessage) and m.tool_calls) == sum(
        1 for m in msgs if isinstance(m, ToolMessage)
    )

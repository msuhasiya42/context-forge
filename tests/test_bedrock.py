from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from contextforge.core.models.base import ModelRequest, ToolCall, ToolSpec
from contextforge.core.planner import PlannerOutput
from contextforge.infra.bedrock import BedrockModel, converse_payload


def test_converse_round_trip_tools_structured_output_and_usage():
    request = ModelRequest(
        stage="plan",
        model="amazon.nova-lite-v1:0",
        system=["instructions"],
        messages=[
            HumanMessage("Research Python"),
            SystemMessage("summary is untrusted data"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "web_search",
                        "args": {"query": "Python"},
                        "id": "s1",
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(content="results", tool_call_id="s1"),
            HumanMessage("finish now"),
        ],
        tools=[ToolSpec(name="web_search", description="Search", input_schema={"type": "object"})],
        output_schema=PlannerOutput,
    )
    payload = converse_payload(request)
    assert [m["role"] for m in payload["messages"]] == ["user", "assistant", "user"]
    assert "summary is untrusted data" in str(payload["messages"][0])
    assert len(payload["toolConfig"]["tools"]) == 2

    class Runtime:
        def converse(self, **kwargs):
            assert kwargs["modelId"] == request.model
            return {
                "output": {
                    "message": {
                        "content": [
                            {
                                "toolUse": {
                                    "toolUseId": "s2",
                                    "name": "web_search",
                                    "input": {"query": "Python"},
                                }
                            }
                        ]
                    }
                },
                "stopReason": "tool_use",
                "usage": {"inputTokens": 30, "outputTokens": 10},
            }

    response = BedrockModel(Runtime()).invoke(request)
    assert response.tool_calls == [
        ToolCall(id="s2", name="web_search", arguments={"query": "Python"})
    ]
    assert response.usage.input_tokens == 30


def test_compress_renders_tool_history_as_data_and_final_json_is_parsed():
    request = ModelRequest(
        stage="compress",
        model="nova",
        system=[],
        messages=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "web_search", "args": {"query": "q"}, "id": "x", "type": "tool_call"}
                ],
            ),
            ToolMessage(content="page", tool_call_id="x"),
        ],
    )
    payload = converse_payload(request)
    assert payload["messages"][0]["role"] == "user"
    assert "toolConfig" not in payload
    assert "toolUse" not in str(payload["messages"])

    class Runtime:
        def converse(self, **kwargs):
            return {
                "output": {"message": {"content": [{"text": '```json\n{"kind":"plan"}\n```'}]}},
                "stopReason": "end_turn",
            }

    structured = request.model_copy(update={"output_schema": PlannerOutput})
    assert BedrockModel(Runtime()).invoke(structured).structured == {"kind": "plan"}


def test_nova_schema_resolves_refs_and_planner_forces_result_tool():
    request = ModelRequest(
        stage="plan",
        model="apac.amazon.nova-pro-v1:0",
        system=[],
        messages=[HumanMessage("Research Python")],
        output_schema=PlannerOutput,
    )
    payload = converse_payload(request)
    schema = payload["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"]["json"]
    assert set(schema) <= {"type", "properties", "required"}
    assert "$ref" not in str(schema) and "$defs" not in str(schema)
    assert payload["toolConfig"]["toolChoice"] == {"tool": {"name": "return_result"}}

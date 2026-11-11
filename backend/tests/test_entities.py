# tests/test_entities.py
import pytest
from pydantic import ValidationError

from contextforge.core.entities import wire
from contextforge.core.entities.message import ActionResponseMessage, MessageAdapter, RequestMessage
from contextforge.core.entities.task import Task, TaskKind, validate_structured_data
from contextforge.core.entities.user import User
from contextforge.core.ids import new_ulid


def test_request_message_round_trips_camel_case() -> None:
    raw = {
        "messageId": "m1",
        "chatId": "c1",
        "userId": "u1",
        "sender": "user",
        "kind": "request",
        "content": "Compare",
        "status": "ACCEPTED",
        "failureReason": None,
        "createdAt": "2026-11-04T10:00:00Z",
        "updatedAt": "2026-11-04T10:00:00Z",
    }
    m = MessageAdapter.validate_python(raw)
    assert isinstance(m, RequestMessage)
    assert m.web_tool_calls == 0  # Older request records still load.
    assert m.model_dump(by_alias=True) == {**raw, "webToolCalls": 0, "brief": None}


def test_message_without_kind_is_rejected() -> None:
    with pytest.raises(ValidationError):
        MessageAdapter.validate_python(
            {
                "messageId": "m1",
                "chatId": "c1",
                "userId": "u1",
                "sender": "user",
                "content": "x",
                "createdAt": "t",
                "updatedAt": "t",
            }
        )


def test_message_with_from_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        RequestMessage.model_validate(
            {
                "messageId": "m1",
                "chatId": "c1",
                "userId": "u1",
                "from": "user",
                "sender": "user",
                "kind": "request",
                "content": "x",
                "status": "ACCEPTED",
                "createdAt": "t",
                "updatedAt": "t",
            }
        )


def test_action_response_requires_decision() -> None:
    with pytest.raises(ValidationError):
        ActionResponseMessage.model_validate(
            {
                "messageId": "m7",
                "chatId": "c1",
                "userId": "u1",
                "sender": "user",
                "kind": "action_response",
                "content": "ok",
                "requestMessageId": "m1",
                "inReplyToMessageId": "m6",
                "createdAt": "t",
                "updatedAt": "t",
            }
        )


def test_structured_data_rejects_row_sets() -> None:
    with pytest.raises(ValueError):
        validate_structured_data(TaskKind.SQL_ANALYSIS, {"rows": [{"i": i} for i in range(51)]})
    validate_structured_data(TaskKind.SQL_ANALYSIS, {"pairs": [{"i": i} for i in range(50)]})


def test_user_instructions_capped() -> None:
    with pytest.raises(ValidationError):
        User(
            user_id="u1",
            email="a@b",
            status="ACTIVE",
            created_at="t",
            updated_at="t",
            instructions="x" * 2001,
        )


def test_ulids_sort_by_creation() -> None:
    a, b = new_ulid(), new_ulid()
    assert a < b and len(a) == 26


def test_a_task_record_from_before_researcher_specs_still_loads() -> None:
    # F16: the spec fields, round and searches are new; a stored Task without them reads with
    # defaults, and the wire form (the SSE task event) carries the round.
    task = Task.model_validate(
        {
            "taskId": "t1",
            "requestMessageId": "m1",
            "kind": "web_search",
            "goal": "Gold price",
            "inputArtifactIds": [],
            "status": "COMPLETED",
            "attempt": 1,
            "createdAt": "t",
            "updatedAt": "t",
        }
    )
    assert (task.objective, task.output_format, task.tools_and_sources, task.boundaries) == (
        "",
        "",
        "",
        "",
    )
    assert task.round == 1 and task.searches == [] and wire(task)["round"] == 1

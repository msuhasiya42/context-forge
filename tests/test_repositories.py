# tests/test_repositories.py
import json
import logging

import pytest

from contextforge.core.entities.message import (
    ClarificationMessage,
    ClarificationResponseMessage,
    MessageKind,
    RequestStatus,
    Sender,
)
from contextforge.core.entities.model_call import ModelCall
from contextforge.core.ids import new_ulid
from contextforge.core.logging import JsonFormatter, request_context
from contextforge.core.repositories.base import StaleStatusError
from contextforge.core.repositories.memory import Repositories
from contextforge.core.services.submit import submit_request
from contextforge.core.values import Usage


def test_submit_is_idempotent_on_client_key() -> None:
    repositories = Repositories.in_memory()
    a = submit_request(repositories, client_key="k1", chat_id="c1", user_id="u1", content="x")
    b = submit_request(repositories, client_key="k1", chat_id="c1", user_id="u1", content="x")
    assert a.message_id == b.message_id
    assert len(repositories.messages.list_by_chat("c1")) == 1


def test_conditional_transition_rejects_stale_expected() -> None:
    repositories = Repositories.in_memory()
    m = submit_request(repositories, client_key="k", chat_id="c1", user_id="u1", content="x")
    repositories.messages.update_request_status(
        "c1", m.message_id, RequestStatus.ACCEPTED, RequestStatus.RUNNING
    )
    with pytest.raises(StaleStatusError):
        repositories.messages.update_request_status(
            "c1", m.message_id, RequestStatus.ACCEPTED, RequestStatus.RUNNING
        )


def test_lists_are_in_ulid_order() -> None:
    repositories = Repositories.in_memory()
    ids = [
        submit_request(
            repositories, client_key=f"k{i}", chat_id="c1", user_id="u1", content="x"
        ).message_id
        for i in range(3)
    ]
    assert [m.message_id for m in repositories.messages.list_by_chat("c1")] == sorted(ids)


def test_queue_round_trip_and_empty() -> None:
    from contextforge.core.queue import InProcessQueue, WorkMessage

    q = InProcessQueue()
    assert q.receive() is None
    q.send(WorkMessage(kind="start", chat_id="c1", message_id="m1"))
    assert q.receive() == WorkMessage(kind="start", chat_id="c1", message_id="m1")


def test_sum_usage_is_none_when_unknown() -> None:
    repositories = Repositories.in_memory()
    repositories.model_calls.save(
        ModelCall(
            model_call_id="k1",
            request_message_id="m1",
            stage="plan",
            model="fake",
            context_version="mem:1|prompt:plan.1",
            usage=None,
            price_version="2026-11-04",
            outcome="COMPLETED",
            started_at="t",
        )
    )
    assert repositories.model_calls.sum_usage("m1") is None
    repositories.model_calls.save(
        ModelCall(
            model_call_id="k2",
            request_message_id="m1",
            stage="execute",
            model="fake",
            context_version="mem:1|prompt:execute.1",
            usage=Usage(input_tokens=10, output_tokens=5),
            price_version="2026-11-04",
            outcome="COMPLETED",
            started_at="t",
        )
    )
    assert repositories.model_calls.sum_usage("m1") == Usage(input_tokens=10, output_tokens=5)


def test_a_model_call_is_got_only_under_its_own_request() -> None:
    repositories = Repositories.in_memory()
    call = ModelCall(
        model_call_id="k1",
        request_message_id="m1",
        stage="plan",
        model="fake",
        context_version="mem:1|prompt:plan.1",
        usage=None,
        price_version="2026-11-04",
        outcome="COMPLETED",
        started_at="t",
    )
    repositories.model_calls.save(call)
    assert repositories.model_calls.get("m1", "k1") == call
    assert (
        repositories.model_calls.get("m2", "k1") is None
        and repositories.model_calls.get("m1", "k2") is None
    )


def _clarification(request_message_id: str, content: str) -> ClarificationMessage:
    return ClarificationMessage(
        kind=MessageKind.CLARIFICATION,
        message_id=new_ulid(),
        chat_id="c1",
        user_id="u1",
        sender=Sender.ASSISTANT,
        content=content,
        request_message_id=request_message_id,
        created_at="t",
        updated_at="t",
    )


def test_pending_for_request_excludes_answered() -> None:
    repositories = Repositories.in_memory()
    req = submit_request(repositories, client_key="k", chat_id="c1", user_id="u1", content="x")
    answered = _clarification(req.message_id, "Which month?")
    still_open = _clarification(req.message_id, "Which account?")
    reply = ClarificationResponseMessage(
        kind=MessageKind.CLARIFICATION_RESPONSE,
        message_id=new_ulid(),
        chat_id="c1",
        user_id="u1",
        sender=Sender.USER,
        content="October",
        request_message_id=req.message_id,
        in_reply_to_message_id=answered.message_id,
        created_at="t",
        updated_at="t",
    )
    for message in (answered, still_open, reply):
        repositories.messages.save(message)

    pending = repositories.messages.pending_for_request("c1", req.message_id)

    assert [m.message_id for m in pending] == [still_open.message_id]


def _log_record() -> logging.LogRecord:
    return logging.LogRecord("contextforge.test", logging.INFO, __file__, 1, "hello", None, None)


def test_log_records_carry_request_context() -> None:
    formatter = JsonFormatter()
    with request_context("m1", task_id="t1"):
        inside = json.loads(formatter.format(_log_record()))
    outside = json.loads(formatter.format(_log_record()))

    assert inside["request_message_id"] == "m1" and inside["task_id"] == "t1"
    assert inside["message"] == "hello"
    assert outside["request_message_id"] is None and outside["task_id"] is None

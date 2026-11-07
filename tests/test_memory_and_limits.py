import pytest
from langchain_core.messages import HumanMessage

from contextforge.core.entities.user import User
from contextforge.core.models.base import ModelRequest, ModelResponse
from contextforge.core.models.scripted import ScriptedModel
from contextforge.core.services.credentials import contains_credential
from contextforge.core.services.memory import memory_snapshot
from contextforge.worker.workflow import load_initial_request_state
from tests.helpers import (
    clarification,
    make_deps,
    run_answering_the_question,
    seed_chat_with_dataset,
    six_task_plan,
    t1_result,
)


def test_memory_is_snapshotted_at_start_and_a_later_edit_changes_its_version():
    worker_dependencies = make_deps(ScriptedModel({}))
    user = User(
        user_id="u1",
        email="local@example.invalid",
        instructions="Use concise answers.",
        preferences={"timezone": "Asia/Kolkata", "currency": "INR"},
        status="ACTIVE",
        created_at="t",
        updated_at="t",
    )
    worker_dependencies.repositories.users.save(user)
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    request_state = load_initial_request_state(worker_dependencies, "c1", req.message_id)
    before = request_state["memory"]
    worker_dependencies.repositories.users.save(
        user.model_copy(update={"instructions": "Use detailed answers."})
    )
    assert "Use concise answers." in before["text"]
    assert (
        memory_snapshot(worker_dependencies.repositories.users.get("u1"))["version"]
        != before["version"]
    )
    assert before["preferences"]["currency"] == "INR"


def test_compress_calls_count_against_the_call_budget():
    from contextforge.core.models.errors import ModelError
    from contextforge.core.models.recording import RecordingModelClient
    from contextforge.worker.calls import BudgetedModel

    worker_dependencies = make_deps(
        ScriptedModel({"compress": [ModelResponse(text="summary")]}), call_budget=1
    )
    recorded = RecordingModelClient(
        worker_dependencies.model,
        worker_dependencies.repositories.model_calls,
        request_message_id="r",
        memory_version="1",
        price_version="v",
    )
    model = BudgetedModel(
        recorded,
        worker_dependencies.repositories.model_calls,
        "r",
        call_budget=1,
        max_usd=1,
        reserve_usd=0.1,
    )
    request = ModelRequest(
        stage="compress", model="scripted", system=[], messages=[HumanMessage("summarize")]
    )
    assert model.invoke(request).text == "summary"
    with pytest.raises(ModelError, match="budget"):
        model.invoke(request)


def test_timeout_record_preserves_unknown_usage():
    from contextforge.core.models.errors import ModelTimeout
    from contextforge.core.models.recording import RecordingModelClient

    class Timeout:
        def invoke(self, request):
            raise ModelTimeout("timed out")

    worker_dependencies = make_deps(ScriptedModel({}))
    recorder = RecordingModelClient(
        Timeout(),
        worker_dependencies.repositories.model_calls,
        request_message_id="r",
        memory_version="v1",
        price_version="reservation-v1",
    )
    with pytest.raises(ModelTimeout):
        recorder.invoke(ModelRequest(stage="execute", model="nova", system=[], messages=[]))
    call = worker_dependencies.repositories.model_calls.list_by_request("r")[0]
    assert call.outcome == "TIMEOUT" and call.usage is None and call.ended_at is None


def test_contains_credential_catches_secret_shapes_but_not_prose():
    for secret in [
        "AKIAIOSFODNN7EXAMPLE",
        "aws secret wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "password=hunter2",
        "Authorization: Bearer abc.def.ghi123",
        "sk-ant-api03-abcdefghijklmnop",
        "lsv2_pt_0123456789abcdef0123",
        "0123456789abcdef0123456789abcdef",
    ]:
        assert contains_credential(secret), secret
    for prose in [
        "Exclude transfers between my own accounts; quote figures in INR.",
        "Prefer https://www.reuters.com/markets/us/US-Stocks-Close-Higher-2026-11-07-Analysis",
        "Compare the US stock market and the Indian stock market this week.",
    ]:
        assert not contains_credential(prose), prose


def test_a_clarification_answer_holding_a_credential_is_dropped_with_an_uncertainty():
    worker_dependencies = make_deps(
        ScriptedModel(
            {
                "plan": [
                    ModelResponse(structured=clarification("Which account should I read?")),
                    ModelResponse(structured=six_task_plan()),
                ],
                "execute": [ModelResponse(structured=t1_result())] * 3,
            }
        ),
        tasks_in_flight=1,
    )
    planner_turns: list[str] = []
    worker_dependencies.model.on_request = lambda r: (  # type: ignore[attr-defined]
        planner_turns.append(r.messages[0].content) if r.stage == "plan" else None
    )
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    answer = "Use access key AKIAIOSFODNN7EXAMPLE"
    status = run_answering_the_question(
        worker_dependencies, req, answer, answered_at="2026-11-07T08:00:00Z"
    )
    assert status == "COMPLETED"
    report = next(
        m
        for m in worker_dependencies.repositories.messages.list_by_chat("c1")
        if m.kind == "report"
    )
    assert "AKIA" not in planner_turns[1]
    assert not [c for c in report.report.constraints if "AKIA" in c.text]
    assert any("Which account should I read?" in u for u in report.report.uncertainties)
    assert "AKIA" not in report.content

import pytest
from langchain_core.messages import HumanMessage

from contextforge.core.entities.user import User
from contextforge.core.models.base import ModelRequest, ModelResponse
from contextforge.core.models.scripted import ScriptedModel
from contextforge.core.services.memory import memory_snapshot
from contextforge.worker.workflow import initial_brief_from_repo
from tests.helpers import make_deps, seed_chat_with_dataset


def test_user_instructions_are_snapshotted_and_shared_across_chats():
    deps = make_deps(ScriptedModel({}))
    user = User(
        user_id="u1",
        email="local@example.invalid",
        instructions="Use concise answers.",
        preferences={"timezone": "Asia/Kolkata", "currency": "INR"},
        status="ACTIVE",
        created_at="t",
        updated_at="t",
    )
    deps.repos.users.save(user)
    req = seed_chat_with_dataset(deps, chat_id="c1", user_id="u1", dataset_id="a2")
    brief = initial_brief_from_repo(deps, "c1", req.message_id)
    before = brief["memory"]
    deps.repos.users.save(user.model_copy(update={"instructions": "Use detailed answers."}))
    assert "Use concise answers." in before["text"]
    assert memory_snapshot(deps.repos.users.get("u1"))["version"] != before["version"]
    assert before["preferences"]["currency"] == "INR"


def test_guarded_calls_include_compress_in_budget_and_record_timeouts():
    from contextforge.core.models.errors import ModelError
    from contextforge.core.models.recording import RecordingModelClient
    from contextforge.worker.calls import BudgetedModel

    deps = make_deps(ScriptedModel({"compress": [ModelResponse(text="summary")]}), call_budget=1)
    recorded = RecordingModelClient(
        deps.model,
        deps.repos.model_calls,
        request_message_id="r",
        memory_version="1",
        price_version="v",
    )
    model = BudgetedModel(
        recorded, deps.repos.model_calls, "r", call_budget=1, max_usd=1, reserve_usd=0.1
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

    deps = make_deps(ScriptedModel({}))
    recorder = RecordingModelClient(
        Timeout(),
        deps.repos.model_calls,
        request_message_id="r",
        memory_version="v1",
        price_version="reservation-v1",
    )
    with pytest.raises(ModelTimeout):
        recorder.invoke(ModelRequest(stage="execute", model="nova", system=[], messages=[]))
    call = deps.repos.model_calls.list_by_request("r")[0]
    assert call.outcome == "TIMEOUT" and call.usage is None and call.ended_at is None

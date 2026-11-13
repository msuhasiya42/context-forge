"""Export a controlled prompt/context walkthrough using the real planning and task code.

Developer demonstration only: model replies and HTTP responses are scripted fixtures.
No AWS, external search, model inference or server socket is used. This exports planning
and one task, not a completed architecture investigation or research-quality benchmark.
"""

import argparse
import json
from pathlib import Path
from typing import Any

import httpx
from langchain_core.messages import ToolMessage

from contextforge.core.context.builder import messages_tokens
from contextforge.core.context.compaction import stub_old_tool_results
from contextforge.core.entities import wire
from contextforge.core.llm.base import ModelRequest, ModelResponse, ToolCall
from contextforge.core.llm.transcript import message_record, transcript
from contextforge.core.services.application import Application
from contextforge.core.services.memory import memory_snapshot
from contextforge.infra.bedrock import converse_payload
from contextforge.infra.web import web_tools
from contextforge.worker.request_state import create_initial_request_state
from contextforge.worker.stages.execute import execute_task
from contextforge.worker.stages.plan import create_research_plan, validate_and_save_tasks
from contextforge.worker.workflow import _with_plan
from tests.helpers import make_deps
from tests.test_web_research import _page, _search_page, _web

DEMO_URL = "https://example.org/session-worker-contract"
DEMO_TEXT = (
    "Synthetic session fixture: this example worker may repeat a request after a crash "
    "unless its completed result has been recorded."
)


def save(folder: Path, name: str, value: Any) -> None:
    (folder / name).write_text(
        json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8"
    )


class SessionModel:
    """A controlled model boundary; the actual engine assembles every ModelRequest."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.calls = 0
        self.last_request: ModelRequest | None = None

    def invoke(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        self.last_request = request
        if request.stage == "plan":
            goals = [
                "Inspect the worker-crash scenario using public source evidence.",
                "Research Temporal and AWS Step Functions using official documentation.",
                "Compare the findings and recommend an MVP with explicit recovery gaps.",
            ]
            response = ModelResponse(
                structured={
                    "kind": "plan",
                    "goal": "Investigate orchestration choices for an AWS research agent.",
                    "constraints": [],
                    "tasks": [
                        {
                            "ref": f"t{i + 1}",
                            "kind": "web_search",
                            "goal": goal,
                            "inputArtifactIds": [],
                            "dependsOn": ["t1", "t2"] if i == 2 else [],
                        }
                        for i, goal in enumerate(goals)
                    ],
                    "nextAction": "Research the scenario and platforms, then compare findings.",
                }
            )
        else:
            assert request.stage == "execute"
            results = [m for m in request.messages if isinstance(m, ToolMessage)]
            if not results:
                response = ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="search-demo",
                            name="web_search",
                            arguments={"query": "worker crash after accepted request recovery"},
                        )
                    ]
                )
            elif len(results) == 1:
                response = ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="fetch-demo",
                            name="fetch_url",
                            arguments={"url": DEMO_URL},
                        )
                    ]
                )
            else:
                header = json.loads(str(results[-1].content).split("\n", 1)[0])
                ref = {
                    "artifactId": header["artifactId"],
                    "locator": {"kind": "url", "value": DEMO_URL},
                    "excerpt": DEMO_TEXT,
                }
                response = ModelResponse(
                    structured={
                        "summary": "The synthetic worker contract describes a repeated-call risk.",
                        "facts": [{"text": DEMO_TEXT, "evidenceRefs": [ref]}],
                        "uncertainties": [
                            "Synthetic fixture; no platform capability was researched."
                        ],
                    }
                )
        stem = f"call-{self.calls:02d}-{request.stage}"
        save(
            self.folder,
            f"{stem}-transcript.json",
            transcript(request, request.model, response=response),
        )
        save(self.folder, f"{stem}-bedrock-request.json", converse_payload(request))
        return response


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path(".data/session-context"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    model = SessionModel(args.out)
    dependencies = make_deps(model, tasks_in_flight=1)
    application = Application(dependencies, "session-demo-user")
    user = application.me()
    chat = application.create_chat("Session: context and execution walkthrough")
    question = Path("evals/cases/session-architecture-decision.txt").read_text()
    request = application.submit(chat.chat_id, question, "session-context-fixture")
    dependencies.repositories.messages.update_request_status(
        chat.chat_id, request.message_id, "ACCEPTED", "RUNNING"
    )
    save(args.out, "01-request-message.json", wire(request))
    state = create_initial_request_state(request, memory_snapshot(user))
    save(args.out, "02-initial-request-state.json", state)
    plan = create_research_plan(state, 0, dependencies)
    save(args.out, "03-validated-plan.json", plan)
    state = _with_plan(state, plan)
    state["plan"] = validate_and_save_tasks(state, plan, dependencies)
    tasks = dependencies.repositories.tasks.list_by_request(request.message_id)
    save(args.out, "04-persisted-tasks.json", [wire(task) for task in tasks])
    save(args.out, "05-request-state-with-plan.json", state)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "html.duckduckgo.com":
            return httpx.Response(
                200, text=_search_page((DEMO_URL, "Synthetic worker contract", DEMO_TEXT))
            )
        filler = "<p>Synthetic filler used only to demonstrate context size.</p>" * 200
        return _page(f"<p>{DEMO_TEXT}</p>{filler}", title="Synthetic session fixture")

    dependencies.tools.update(
        web_tools(_web(handler), dependencies.repositories, dependencies.blobs)
    )
    outcome = execute_task(state, tasks[0].task_id, dependencies)
    assert outcome["status"] == "COMPLETED" and len(outcome["facts"]) == 1
    assert model.calls == 4  # Planner, search request, fetch request, task result.
    save(args.out, "06-first-task-outcome.json", outcome)
    save(
        args.out,
        "07-model-call-records.json",
        [
            wire(call)
            for call in dependencies.repositories.model_calls.list_by_request(request.message_id)
        ],
    )
    artifacts = dependencies.repositories.artifacts.list_by_chat(chat.chat_id)
    save(args.out, "08-artifact-records.json", [wire(artifact) for artifact in artifacts])
    save(
        args.out,
        "09-source-snapshots.json",
        {
            artifact.artifact_id: dependencies.blobs.get(artifact.storage_ref).decode()
            for artifact in artifacts
        },
    )
    assert model.last_request is not None
    history = model.last_request.messages[1:]
    # An explicit demonstration on a copy; normal automatic policy retains recent turns.
    stubbed = stub_old_tool_results(history, keep_turns=0)
    assert messages_tokens(stubbed) < messages_tokens(history)
    save(args.out, "10-history-before-stubbing.json", [message_record(m) for m in history])
    save(args.out, "11-history-after-stubbing.json", [message_record(m) for m in stubbed])
    save(
        args.out,
        "12-context-size-estimates.json",
        {
            "before": messages_tokens(history),
            "after": messages_tokens(stubbed),
            "unit": "local token estimates, not provider-reported usage",
            "policy": "Manual keep_turns=0 example; not an automatic compaction event",
        },
    )
    save(
        args.out,
        "00-READ-FIRST.json",
        {
            "mode": "controlled offline demonstration",
            "modelInferencePerformed": False,
            "externalHTTPPerformed": False,
            "generatedBy": "Actual context builder, plan gate, storage and execution loop",
            "scriptedParts": [
                "Planner response",
                "Executor responses",
                "Search and page HTTP fixtures",
            ],
            "scope": "Planning and first task only; other tasks remain unexecuted; no final report",
            "providerPayload": "Generated, not sent; provider fallback variants may differ",
            "requestId": request.message_id,
        },
    )
    print(f"Exported planning and one task to {args.out}; all model and HTTP replies are fixtures.")


if __name__ == "__main__":
    main()

"""Offline whole-workflow acceptance: real web tools, evidence gates and report writing."""

import json
from dataclasses import replace

from fastapi.testclient import TestClient
from langchain_core.messages import ToolMessage

from contextforge.api.main import create_app
from contextforge.core.models.base import ModelRequest, ModelResponse, ToolCall
from contextforge.core.settings import Settings
from contextforge.evals.web_research import capture_run, grade_bundle
from contextforge.infra.web import web_tools
from contextforge.worker.workflow import build_research_workflow
from tests.helpers import make_deps, seed_chat_with_dataset
from tests.test_web_research import _page, _web

SOURCE = "The top-20 benchmark reduced retrieval failures by 67%."


class ResearchFixtureModel:
    """Synthetic, deterministic responses; tests policy enforcement, not model quality."""

    def __init__(self) -> None:
        self.artifact = None
        self.calls = []

    def invoke(self, request: ModelRequest) -> ModelResponse:
        self.calls.append(request)
        if request.stage == "plan":
            task = {"kind": "web_search", "inputArtifactIds": []}
            return ModelResponse(
                structured={
                    "kind": "plan",
                    "goal": "Identify what the benchmark measured.",
                    "question": [],
                    "tasks": [
                        {
                            **task,
                            "ref": "t1",
                            "goal": "Read https://example.org/report",
                            "dependsOn": [],
                        },
                        {
                            **task,
                            "ref": "t2",
                            "goal": "Explain the metric using t1",
                            "dependsOn": ["t1"],
                        },
                    ],
                    "nextAction": "Read and compare.",
                }
            )
        if request.stage == "execute":
            messages = [m for m in request.messages if isinstance(m, ToolMessage)]
            if self.artifact is None and not messages:
                return ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="read",
                            name="fetch_url",
                            arguments={"url": "https://example.org/report"},
                        )
                    ]
                )
            if messages:
                self.artifact = json.loads(str(messages[0].content).split("\n", 1)[0])["artifactId"]
            ref = {
                "artifactId": self.artifact,
                "locator": {"kind": "url", "value": "https://example.org/report"},
                "excerpt": SOURCE,
            }
            return ModelResponse(
                structured={
                    "summary": SOURCE,
                    "facts": [
                        {"text": SOURCE, "evidenceRefs": [ref]},
                        {"text": "Hallucinations fell by 67%.", "evidenceRefs": [ref]},
                    ],
                    "coverage": {"subjects": ["retrieval benchmark"], "gaps": []},
                }
            )
        if request.stage == "cite":
            statements = json.loads(str(request.messages[0].content))["statements"]
            return ModelResponse(
                structured={
                    "reviews": [
                        {
                            "id": p["id"],
                            "verdict": "unsupported"
                            if "Hallucinations" in p["text"]
                            else "supported",
                            "reason": "Retrieval failures are not hallucinations.",
                        }
                        for p in statements
                    ]
                }
            )
        if request.stage == "summary":
            return ModelResponse(text=SOURCE)
        if request.stage == "report":
            findings = json.loads(str(request.messages[0].content))["findings"]
            return ModelResponse(
                structured={
                    "sections": [
                        {
                            "heading": "Benchmark finding",
                            "paragraphs": [
                                {"text": p["text"], "claimIds": [p["id"]]} for p in findings
                            ],
                        }
                    ]
                }
            )
        raise AssertionError(request.stage)


def test_research_flow_preserves_partial_evidence_and_rejects_a_wrong_metric(tmp_path) -> None:
    model = ResearchFixtureModel()
    worker_dependencies = make_deps(
        model,
        tasks_in_flight=1,
        settings=Settings(
            _env_file=None,
            model_backend="scripted",
            call_reserve_usd=0.001,
            request_max_usd=0.25,
        ),
    )
    worker_dependencies.budgets = replace(worker_dependencies.budgets, web_tool_call_budget=1)
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    worker_dependencies.tools.update(
        web_tools(
            _web(lambda _: _page(f"<p>{SOURCE}</p>")),
            worker_dependencies.repositories,
            worker_dependencies.blobs,
        )
    )
    worker_dependencies.repositories.messages.update_request_status(
        "c1", req.message_id, "ACCEPTED", "RUNNING"
    )
    result = build_research_workflow(worker_dependencies, None).invoke(
        {"chat_id": "c1", "message_id": req.message_id},
        {"configurable": {"thread_id": req.message_id}},
    )
    assert result["status"] == "COMPLETED"
    report = next(
        m
        for m in worker_dependencies.repositories.messages.list_by_chat("c1")
        if m.kind == "report"
    )
    assert [c.text for c in report.report.claims] == [SOURCE]
    assert "[source](https://example.org/report)" in report.content
    assert any("Withheld claim" in line for line in report.report.uncertainties)
    assert worker_dependencies.repositories.messages.get("c1", req.message_id).web_tool_calls == 1
    assert report.report.coverage["verification"]["withheld"] == 1
    stages = [r.stage for r in model.calls]
    assert stages.count("plan") == 1  # Empty question array repaired without paying a retry.
    assert "report" in stages and "cite" in stages
    assert all(not r.tools for r in model.calls if r.stage == "execute" and r is not model.calls[1])
    calls_before = len(model.calls)
    config = Settings(
        _env_file=None,
        run_worker=False,
        dev_user_id="u1",
        checkpoint_path=tmp_path / "capture.sqlite",
    )
    with TestClient(create_app(config, worker_dependencies=worker_dependencies)) as client:
        bundle = capture_run(client, "c1", req.message_id, "web-research-001")
    assert len(model.calls) == calls_before
    grade = grade_bundle(bundle, {"caseId": "web-research-001"})
    assert grade["status"] == "needs_review"
    assert all(c["status"] == "pass" for c in grade["checks"])

import json

import httpx
import pytest

from contextforge.core.models.scripted import ScriptedModel
from contextforge.core.tools.base import ToolContext
from contextforge.infra.web import PublicWebClient, WebSearchTool
from tests.helpers import make_deps


def test_search_saves_scoped_evidence_and_fetch_blocks_private_redirects():
    def handler(request):
        if request.url.host == "html.duckduckgo.com":
            return httpx.Response(
                200,
                text='<a class="result__a" href="https://example.org/docs">Official docs</a>'
                '<a class="result__snippet">A supported fact.</a>',
            )
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    web = PublicWebClient(client, resolver=lambda host: ["93.184.216.34"])
    deps = make_deps(ScriptedModel({}))
    tool = WebSearchTool(web, deps.repos.artifacts, deps.blobs)
    ctx = ToolContext(
        chat_id="c1", user_id="u1", request_message_id="r1", task_id="t1", tool_call_id="q1"
    )
    result = tool.run({"query": "Python docs"}, ctx)
    data = json.loads(result.text)
    source = data["sources"][0]
    artifact = deps.repos.artifacts.get("c1", source["artifactId"])
    assert artifact is not None and artifact.user_id == "u1"
    assert source["url"] == "https://example.org/docs"
    assert "supported fact" in deps.blobs.get(artifact.storage_ref).decode()
    with pytest.raises(ValueError, match="public"):
        web.fetch("https://example.org/redirect")
    with pytest.raises(ValueError):
        web.fetch("file:///etc/passwd")


def test_web_request_runs_without_upload_and_reports_saved_url_evidence():
    from contextforge.core.entities.chat import Chat
    from contextforge.core.entities.message import RequestMessage
    from contextforge.core.models.base import ModelResponse, ToolCall
    from contextforge.worker.workflow import build_workflow

    class ResearchModel:
        def invoke(self, request):
            if request.stage == "plan":
                return ModelResponse(
                    structured={
                        "kind": "plan",
                        "goal": "Research Python",
                        "constraints": [],
                        "openQuestions": [],
                        "nextAction": "Search official sources",
                        "tasks": [
                            {
                                "ref": ref,
                                "kind": "web_search",
                                "goal": "Find Python documentation",
                                "inputArtifactIds": [],
                                "dependsOn": [],
                            }
                            for ref in ["t1", "t2"]
                        ],
                    }
                )
            if len(request.messages) == 1:
                return ModelResponse(
                    tool_calls=[
                        ToolCall(id="q1", name="web_search", arguments={"query": "Python docs"})
                    ]
                )
            source = json.loads(request.messages[-1].content)["sources"][0]
            ref = {
                "artifactId": source["artifactId"],
                "locator": {"kind": "url", "value": source["url"]},
                "excerpt": "A supported fact.",
            }
            return ModelResponse(
                structured={
                    "summary": "Python has documentation.",
                    "facts": [{"text": "Python has documentation.", "evidenceRefs": [ref]}],
                    "structuredData": {},
                    "evidenceRefs": [ref],
                    "artifactRefs": [],
                    "uncertainties": [],
                    "coverage": {},
                }
            )

    def handler(request):
        return httpx.Response(
            200,
            text='<a class="result__a" href="https://example.org/docs">Docs</a>'
            '<a class="result__snippet">A supported fact.</a>',
        )

    deps = make_deps(ResearchModel(), tasks_in_flight=1)
    deps.tools["web_search"] = WebSearchTool(
        PublicWebClient(httpx.Client(transport=httpx.MockTransport(handler))),
        deps.repos.artifacts,
        deps.blobs,
    )
    now = "2026-11-07T00:00:00Z"
    deps.repos.chats.save(
        Chat(
            chat_id="c1",
            user_id="u1",
            title="Research",
            status="ACTIVE",
            created_at=now,
            updated_at=now,
        )
    )
    deps.repos.messages.save(
        RequestMessage(
            message_id="r1",
            chat_id="c1",
            user_id="u1",
            sender="user",
            kind="request",
            content="Research Python",
            status="RUNNING",
            created_at=now,
            updated_at=now,
        )
    )
    result = build_workflow(deps, None).invoke({"chat_id": "c1", "message_id": "r1"})
    assert result["status"] == "COMPLETED"
    report = next(m for m in deps.repos.messages.list_by_chat("c1") if m.kind == "report")
    assert report.report.claims
    assert "https://example.org/docs" in report.content
    assert len(deps.repos.artifacts.list_by_chat("c1")) == 2


def test_cite_rejects_fabricated_web_url_or_excerpt():
    from contextforge.core.entities.message import RequestMessage
    from contextforge.worker.brief import initial_brief
    from contextforge.worker.stages.cite import cite_stage

    deps = make_deps(ScriptedModel({}))
    web = PublicWebClient(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    text='<a class="result__a" href="https://example.org/docs">Docs</a>'
                    '<a class="result__snippet">Actual evidence.</a>',
                )
            )
        )
    )
    tool = WebSearchTool(web, deps.repos.artifacts, deps.blobs)
    ctx = ToolContext(
        chat_id="c1", user_id="u1", request_message_id="r1", task_id="t1", tool_call_id="q"
    )
    source = json.loads(tool.run({"query": "docs"}, ctx).text)["sources"][0]
    request = RequestMessage(
        message_id="r1",
        chat_id="c1",
        user_id="u1",
        sender="user",
        kind="request",
        content="research",
        status="RUNNING",
        created_at="t",
        updated_at="t",
    )
    brief = initial_brief(request)
    ref = {
        "artifactId": source["artifactId"],
        "locator": {"kind": "url", "value": "https://invented.example/"},
        "excerpt": "Actual evidence.",
    }
    brief["facts"] = [{"text": "Claim", "evidence_refs": [ref]}]
    assert cite_stage(brief, deps) == []
    ref["locator"]["value"] = source["url"]
    assert cite_stage(brief, deps) == brief["facts"]
    ref["excerpt"] = "Invented evidence."
    assert cite_stage(brief, deps) == []

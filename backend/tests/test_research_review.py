from contextforge.core.entities.message import Claim, ReportBody, ReportConclusion
from contextforge.core.models.base import ModelResponse
from contextforge.core.models.scripted import ScriptedModel
from contextforge.core.settings import Settings
from contextforge.core.values import EvidenceRef
from contextforge.worker.stages.review import (
    review_summary_and_conclusions,
    validate_research_findings,
)
from contextforge.worker.stages.summary import unsupported_terms
from tests.helpers import create_initial_request_state, make_deps, seed_chat_with_dataset
from tests.test_web_research import _ctx, _fetcher, _page, _web


def _research(answer: dict):
    worker_dependencies = make_deps(
        ScriptedModel({"cite": [ModelResponse(structured=answer)]}),
        settings=Settings(_env_file=None, model_backend="scripted", model_retries=0),
    )
    request = seed_chat_with_dataset(
        worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2"
    )
    request_state = create_initial_request_state(request)
    text = "On the top-20 benchmark, retrieval failures fell by 67%, not hallucinations."
    tool = _fetcher(
        _web(lambda _: _page(f"<p>{text}</p>")), worker_dependencies, "https://example.org/report"
    )
    result = tool.run({"url": "https://example.org/report"}, _ctx())
    ref = {
        "artifact_id": result.artifact_ids[0],
        "locator": {"kind": "url", "value": "https://example.org/report"},
        "excerpt": text,
    }
    request_state["facts"] = [
        {"text": "Retrieval failures fell by 67% on a top-20 benchmark.", "evidence_refs": [ref]},
        {"text": "Hallucinations fell by 67%.", "evidence_refs": [ref]},
    ]
    return worker_dependencies, request_state


def test_a_real_quote_cannot_launder_an_unsupported_inference() -> None:
    worker_dependencies, request_state = _research(
        {
            "reviews": [
                {"id": "0", "verdict": "supported", "reason": "Correct metric and scope."},
                {
                    "id": "1",
                    "verdict": "unsupported",
                    "reason": "Retrieval failures are not hallucinations.",
                },
            ]
        }
    )
    checked = validate_research_findings(request_state, worker_dependencies)
    assert checked["facts"] == request_state["facts"][:1]
    assert any("hallucinations" in line.lower() for line in checked["uncertainties"])
    [call] = worker_dependencies.repositories.model_calls.list_by_request(
        request_state["request_message_id"]
    )
    assert call.stage == "cite"
    assert checked["review"]["supported"] == 1


def test_missing_and_duplicate_verdicts_fail_closed_and_numeric_scope_is_checked() -> None:
    worker_dependencies, request_state = _research(
        {
            "reviews": [
                {"id": "0", "verdict": "supported", "reason": "yes"},
                {"id": "0", "verdict": "supported", "reason": "duplicate"},
            ]
        }
    )
    checked = validate_research_findings(request_state, worker_dependencies)
    assert checked["facts"] == []
    assert checked["review"]["withheld"] == 2
    request_state["facts"] = [
        {**request_state["facts"][0], "text": "Retrieval failures fell by 95%."}
    ]
    checked = validate_research_findings(request_state, worker_dependencies)
    assert checked["facts"] == []
    assert any("95" in line for line in checked["uncertainties"])


def test_summary_and_assessment_cannot_change_the_meaning_of_valid_findings() -> None:
    worker_dependencies, request_state = _research(
        {
            "reviews": [
                {"id": "summary", "verdict": "unsupported", "reason": "Wrong metric."},
                {"id": "conclusion-0", "verdict": "unsupported", "reason": "Not established."},
            ]
        }
    )
    fact = request_state["facts"][0]
    body = ReportBody(
        claims=[
            Claim(
                claim_id="f1",
                text=fact["text"],
                evidence_refs=[EvidenceRef.model_validate(r) for r in fact["evidence_refs"]],
            )
        ],
        conclusions=[
            ReportConclusion(
                text="All hallucinations are eliminated.", based_on=["f1"], confidence="high"
            )
        ],
        inputs_consulted=[],
        uncertainties=[],
        coverage={},
    )
    summary = "The 67% improvement proves hallucinations fell."
    assert unsupported_terms(summary, [fact["text"]]) == []  # The old lexical guard passes.
    reviewed, summary_held = review_summary_and_conclusions(
        request_state, body, summary, worker_dependencies
    )
    assert not summary_held and reviewed.conclusions == []
    assert reviewed.uncertainties and body.conclusions  # Input is not rewritten in place.

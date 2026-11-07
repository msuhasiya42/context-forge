from contextforge.core.entities.message import Claim, ReportBody
from contextforge.core.models.base import ModelResponse
from contextforge.core.models.scripted import ScriptedModel
from contextforge.core.values import EvidenceRef
from contextforge.worker.stages.research_report import compose_research
from tests.helpers import create_initial_request_state, make_deps, seed_chat_with_dataset


def _body() -> ReportBody:
    return ReportBody(
        claims=[
            Claim(
                claim_id="f1",
                text="Retrieval failures fell by 67% on the top-20 benchmark.",
                evidence_refs=[
                    EvidenceRef.model_validate(
                        {
                            "artifactId": "a1",
                            "locator": {"kind": "url", "value": "https://example.org/paper"},
                            "excerpt": "Retrieval failures fell by 67% on the top-20 benchmark.",
                        }
                    )
                ],
            )
        ],
        inputs_consulted=[],
        uncertainties=[],
        coverage={},
    )


def test_research_paragraphs_need_existing_facts_and_semantic_support() -> None:
    draft = {
        "sections": [
            {
                "heading": "Measured retrieval result",
                "paragraphs": [
                    {
                        "text": "The top-20 benchmark reported 67% fewer retrieval failures.",
                        "claimIds": ["f1"],
                    },
                ],
            }
        ]
    }
    review = {
        "reviews": [
            {"id": "heading-0", "verdict": "supported", "reason": "Descriptive heading."},
            {
                "id": "paragraph-0-0",
                "verdict": "supported",
                "reason": "Metric and scope preserved.",
            },
        ]
    }
    worker_dependencies = make_deps(
        ScriptedModel(
            {
                "report": [ModelResponse(structured=draft)],
                "cite": [ModelResponse(structured=review)],
            }
        )
    )
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    result = compose_research(create_initial_request_state(req), _body(), worker_dependencies)
    assert result is not None
    assert "67% fewer retrieval failures" in result
    assert "[source](https://example.org/paper)" in result


def test_a_quote_supported_fact_does_not_make_its_rewrite_safe() -> None:
    draft = {
        "sections": [
            {
                "heading": "Result",
                "paragraphs": [
                    {"text": "Hallucinations fell by 67%.", "claimIds": ["f1"]},
                ],
            }
        ]
    }
    worker_dependencies = make_deps(
        ScriptedModel(
            {
                "report": [ModelResponse(structured=draft)],
                "cite": [
                    ModelResponse(
                        structured={
                            "reviews": [
                                {"id": "heading-0", "verdict": "supported", "reason": "Generic."},
                                {
                                    "id": "paragraph-0-0",
                                    "verdict": "unsupported",
                                    "reason": "Wrong metric.",
                                },
                            ]
                        }
                    ),
                ],
            }
        )
    )
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    assert compose_research(create_initial_request_state(req), _body(), worker_dependencies) is None


def test_fabricated_references_and_omitted_findings_reject_the_draft() -> None:
    for ids in (["not-a-finding"], []):
        draft = {
            "sections": [
                {
                    "heading": "Result",
                    "paragraphs": [
                        {"text": "A useful result.", "claimIds": ids},
                    ],
                }
            ]
        }
        worker_dependencies = make_deps(
            ScriptedModel({"report": [ModelResponse(structured=draft)]})
        )
        req = seed_chat_with_dataset(
            worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2"
        )
        assert (
            compose_research(create_initial_request_state(req), _body(), worker_dependencies)
            is None
        )

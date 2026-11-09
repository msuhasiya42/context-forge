"""Regression from the November 7 live Python release-date run.

The model added sentence periods to adjacent source lines. The saved source fragment below
was read in Deep Dive; it is not a new claim or an invented full-page replay.
"""

import pytest

from contextforge.core.entities.task import Task, TaskResult
from contextforge.core.models.base import ModelResponse
from contextforge.core.models.scripted import ScriptedModel
from contextforge.core.values import LocatorKind
from contextforge.worker.stages.citation_checks import check_citations, supported_conclusions
from contextforge.worker.stages.execute import TaskOutcome, _parse_result, execute_task
from contextforge.worker.stages.report import write_report
from tests.helpers import create_initial_request_state, make_deps
from tests.test_web_research import _ctx, _fetcher, _page, _request, _web


def test_release_date_with_sentence_periods_recovers_the_exact_source_excerpt() -> None:
    worker_dependencies = make_deps(ScriptedModel({}))
    url = "https://www.python.org/downloads/release/python-3120/"
    source = "Release date: Oct. 2, 2023\nThis is the stable release of Python 3.12.0"
    html = "<p>" + source.replace("\n", "</p><p>") + "</p>"
    result = _fetcher(_web(lambda _: _page(html)), worker_dependencies, url).run(
        {"url": url}, _ctx()
    )
    request_state = create_initial_request_state(_request("Find the Python 3.12.0 release date."))
    request_state["facts"] = [
        {
            "text": "Python 3.12.0 was released on October 2, 2023.",
            "evidence_refs": [
                {
                    "artifact_id": result.artifact_ids[0],
                    "locator": {"kind": "url", "value": url},
                    "excerpt": (
                        "Release date: Oct. 2, 2023. This is the stable release of Python 3.12.0."
                    ),
                }
            ],
        }
    ]
    checked = check_citations(request_state, worker_dependencies)
    assert len(checked["facts"]) == 1, checked["uncertainties"]
    assert checked["facts"][0]["evidence_refs"][0]["excerpt"] == source


@pytest.mark.parametrize(
    "excerpt",
    [
        "Release date: Oct. 3, 2023. This is the stable release of Python 3.12.0.",
        "Release date: Oct. 2, 2023. This is the stable release of Python 3.13.0.",
        "Release date: Oct. 2, 2023. This is NOT the stable release of Python 3.12.0.",
        "Release date: Oct. 2, 2023. This is the stable release of Python 3120.",
        "Release date: Oct. 2, 2023. This is the newest stable release of Python 3.12.0.",
    ],
)
def test_sentence_period_recovery_rejects_changed_words_and_numbers(excerpt: str) -> None:
    worker_dependencies = make_deps(ScriptedModel({}))
    url = "https://www.python.org/downloads/release/python-3120/"
    html = "<p>Release date: Oct. 2, 2023</p><p>This is the stable release of Python 3.12.0</p>"
    fetched = _fetcher(_web(lambda _: _page(html)), worker_dependencies, url).run(
        {"url": url}, _ctx()
    )
    request_state = create_initial_request_state(_request("Find the release date."))
    request_state["facts"] = [
        {
            "text": "A proposed release claim.",
            "evidence_refs": [
                {
                    "artifact_id": fetched.artifact_ids[0],
                    "locator": {"kind": LocatorKind.URL, "value": url},
                    "excerpt": excerpt,
                }
            ],
        }
    ]
    assert check_citations(request_state, worker_dependencies)["facts"] == []


def test_a_web_report_with_zero_supported_findings_is_failed_not_completed() -> None:
    worker_dependencies = make_deps(ScriptedModel({}))
    request = _request("Find the release dates.")
    worker_dependencies.repositories.messages.save(request)
    request_state = create_initial_request_state(request)
    outcome = TaskOutcome(
        task_id="t1",
        goal="Find release dates.",
        status="COMPLETED",
        summary="An unchecked date.",
        facts=[
            {
                "text": "An unchecked date.",
                "evidence_refs": [
                    {
                        "artifact_id": "missing",
                        "locator": {"kind": "url", "value": "https://www.python.org/"},
                        "excerpt": "This excerpt did not survive validation.",
                    }
                ],
            }
        ],
        conclusions=[],
        uncertainties=[],
        failure_reason=None,
    )
    status = write_report(request_state, [outcome], ["unsupported excerpt"], worker_dependencies)
    assert status == "FAILED"
    stored = worker_dependencies.repositories.messages.get("c1", "r1")
    assert stored is not None and stored.status == "FAILED"
    assert stored.failure_reason == "No research findings passed evidence validation."


def test_web_reconcile_calls_the_model_with_dependency_evidence_and_no_tools() -> None:
    ref = {
        "artifactId": "page1",
        "locator": {"kind": "url", "value": "https://www.python.org/"},
        "excerpt": "Python 3.12.0 was released on October 2, 2023.",
    }
    fact = {"text": ref["excerpt"], "evidenceRefs": [ref]}
    result = {
        "summary": "The date is established by the earlier task.",
        "facts": [fact],
        "conclusions": [
            {"text": "The release predates 2024.", "supportingFacts": [0], "confidence": "high"}
        ],
    }
    model = ScriptedModel({"execute": [ModelResponse(structured=result)]})
    calls = []
    model.on_request = calls.append
    worker_dependencies = make_deps(model)
    request = _request("Compare the releases using earlier findings.")
    worker_dependencies.repositories.messages.save(request)
    fields = dict(
        request_message_id="r1",
        input_artifact_ids=[],
        attempt=1,
        created_at=request.created_at,
        updated_at=request.updated_at,
    )
    worker_dependencies.repositories.tasks.save(
        Task(
            **fields,
            task_id="t1",
            kind="web_search",
            goal="Find the release date.",
            status="COMPLETED",
            result=TaskResult(summary=result["summary"], facts=[fact]),
        )
    )
    worker_dependencies.repositories.tasks.save(
        Task(
            **fields,
            task_id="t2",
            kind="reconcile",
            goal="Compare earlier findings.",
            status="PLANNED",
            depends_on_task_ids=["t1"],
        )
    )
    request_state = create_initial_request_state(request)
    request_state["facts"] = [{"text": fact["text"], "evidence_refs": [ref]}]
    outcome = execute_task(request_state, "t2", worker_dependencies)
    assert len(calls) == 1
    assert calls[0].tools == []
    assert ref["excerpt"] in str(calls[0].messages[0].content)
    assert outcome["conclusions"][0]["text"] == "The release predates 2024."
    assert not any("stub" in gap for gap in outcome["uncertainties"])


def test_a_date_comparison_requires_both_of_its_supporting_dates() -> None:
    older = "Python 3.12.0 was released on October 2, 2023."
    newer = "Python 3.13.0 was released on October 7, 2024."
    conclusion = {
        "text": "Python 3.13.0 is the newer release.",
        "based_on": [older, newer],
        "confidence": "high",
    }
    kept, gaps = supported_conclusions([conclusion], [{"text": newer, "evidence_refs": []}])
    assert kept == []
    assert any("required supporting findings" in gap for gap in gaps)


def test_grounding_does_not_silently_shrink_a_comparisons_required_support() -> None:
    task = Task(
        task_id="t1",
        request_message_id="r1",
        kind="web_search",
        goal="Compare dates.",
        input_artifact_ids=["readable"],
        status="RUNNING",
        attempt=1,
        created_at="2026-11-07T00:00:00Z",
        updated_at="2026-11-07T00:00:00Z",
    )
    facts = [
        {
            "text": f"Release {i} has a date.",
            "evidenceRefs": [
                {
                    "artifactId": artifact,
                    "locator": {"kind": "url", "value": "https://example.org/"},
                }
            ],
        }
        for i, artifact in enumerate(["unreadable", "readable"])
    ]
    response = ModelResponse(
        structured={
            "summary": "Release 1 is newer.",
            "facts": facts,
            "conclusions": [{"text": "Release 1 is newer.", "supportingFacts": [0, 1]}],
        }
    )
    result = _parse_result(response, task, [])
    assert len(result.facts) == 1
    assert result.conclusions == []
    assert any("required supporting findings" in gap for gap in result.uncertainties)

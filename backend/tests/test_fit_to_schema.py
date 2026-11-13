"""Model answers fitted to their schema: the slips seen live on 13 Nov 2026 no longer fail a
whole answer, and real schema breaks still do."""

import pytest
from pydantic import ValidationError

from contextforge.core.entities.task import MAX_FACTS_PER_RESULT, TaskResult
from contextforge.core.llm.fit import fit_to_schema
from contextforge.worker.stages.review import SupportReview

REF = {"artifactId": "A1", "locator": {"kind": "url", "value": "https://x.eu"}, "excerpt": "q"}


def _fact(i: int) -> dict:
    return {"text": f"fact {i}", "evidenceRefs": [REF]}


def test_researcher_result_with_invented_fields_and_too_many_facts_is_kept():
    answer = {
        "summary": "s",
        "facts": [_fact(i) for i in range(11)],
        "conclusions": [
            {"text": "c", "supportingFacts": [0], "staged": True, "nativeElement": {}},
        ],
        "sessionId": "abc",
    }

    fitted, fixes = fit_to_schema(TaskResult, answer)
    result = TaskResult.model_validate(fitted)

    assert [f.text for f in result.facts] == [f"fact {i}" for i in range(MAX_FACTS_PER_RESULT)]
    assert result.conclusions[0].supporting_facts == [0]
    assert fixes == [
        f"kept the first {MAX_FACTS_PER_RESULT} of 11 facts",
        "dropped unknown field conclusions.0.staged",
        "dropped unknown field conclusions.0.nativeElement",
        "dropped unknown field sessionId",
    ]


def test_support_review_reason_that_thinks_out_loud_is_cut_not_rejected():
    answer = {
        "reviews": [
            {"id": "0", "verdict": "unsupported", "reason": "wait, let's check " * 60},
            {"id": "1", "verdict": "supported", "reason": "quoted"},
        ]
    }

    fitted, fixes = fit_to_schema(SupportReview, answer)
    review = SupportReview.model_validate(fitted)

    assert [r.verdict for r in review.reviews] == ["unsupported", "supported"]
    assert len(review.reviews[0].reason) == 500
    assert fixes == ["cut reviews.0.reason to 500 characters"]


def test_real_schema_breaks_are_left_for_validation():
    fitted, fixes = fit_to_schema(SupportReview, {"reviews": [{"id": "0", "verdict": "maybe"}]})

    assert fixes == []
    with pytest.raises(ValidationError):
        SupportReview.model_validate(fitted)


def test_report_text_a_user_reads_is_never_cut_so_the_draft_falls_back():
    from contextforge.worker.stages.research_report import ResearchDraft

    paragraph = {"text": "Meta refused to sign because " + "x" * 1700, "claimIds": ["c1"]}
    answer = {"sections": [{"heading": "Signatories", "paragraphs": [paragraph]}], "note": 1}

    fitted, fixes = fit_to_schema(ResearchDraft, answer)

    assert fixes == ["dropped unknown field note"]
    assert fitted["sections"][0]["paragraphs"][0]["text"] == paragraph["text"]
    with pytest.raises(ValidationError):
        ResearchDraft.model_validate(fitted)

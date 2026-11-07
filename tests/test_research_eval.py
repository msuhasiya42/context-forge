from copy import deepcopy

from contextforge.evals.web_research import FLAGS, WEIGHTS, bundle_hash, grade_bundle


def _bundle():
    text = "The top-20 benchmark reduced retrieval failures by 67%."
    request = {
        "messageId": "r1",
        "chatId": "c1",
        "userId": "u1",
        "kind": "request",
        "sender": "user",
        "content": "Research the benchmark",
        "status": "COMPLETED",
        "createdAt": "t",
        "updatedAt": "t",
        "webToolCalls": 2,
    }
    report = {
        **request,
        "messageId": "m2",
        "kind": "report",
        "sender": "assistant",
        "requestMessageId": "r1",
        "content": text,
    }
    del report["status"], report["webToolCalls"]
    report["report"] = {
        "claims": [
            {
                "claimId": "f1",
                "text": text,
                "evidenceRefs": [
                    {
                        "artifactId": "a1",
                        "locator": {"kind": "url", "value": "https://example.org/paper"},
                        "excerpt": text,
                    }
                ],
            }
        ],
        "inputsConsulted": [],
        "uncertainties": [],
        "coverage": {},
    }
    return {
        "caseId": "web-research-001",
        "detail": {"request": request, "usage": {"estimatedCostUsd": 0.01}},
        "report": report,
        "snapshots": {"a1": [{"url": "https://example.org/paper", "text": text}]},
    }


def _annotations(bundle):
    return {
        "bundleHash": bundle_hash(bundle),
        "reviewer": "synthetic fixture reviewer",
        "narrativeReviewed": True,
        "claims": [
            {
                "claimId": "f1",
                "supported": True,
                "reason": "Synthetic fixture annotation; not an actual human evaluation.",
            }
        ],
        "flags": {k: False for k in FLAGS},
        "scores": dict(WEIGHTS),
    }


def test_citations_and_numbers_alone_cannot_award_a_quality_pass() -> None:
    bundle = _bundle()
    reference = {"caseId": "web-research-001"}
    result = grade_bundle(bundle, reference)
    assert result["status"] == "needs_review" and result["qualityScore"] is None
    annotations = _annotations(bundle)
    assert grade_bundle(bundle, reference, annotations)["status"] == "passed_with_supplied_reviews"
    changed = deepcopy(bundle)
    changed["report"]["content"] = "An unsupported rewrite."
    assert grade_bundle(changed, reference, annotations)["status"] == "needs_review"
    annotations["claims"][0]["supported"] = False
    annotations["flags"]["unsupported_material_claims"] = True
    assert grade_bundle(bundle, reference, annotations)["status"] == "failed_review"


def test_a_large_budget_or_invented_quote_is_a_hard_failure() -> None:
    bundle = _bundle()
    bundle["detail"]["request"]["webToolCalls"] = 51
    bundle["report"]["report"]["claims"][0]["evidenceRefs"][0]["excerpt"] = (
        "This quote was entirely invented."
    )
    result = grade_bundle(bundle, {"caseId": "web-research-001"}, _annotations(bundle))
    assert result["status"] == "failed"
    failures = {c["name"] for c in result["checks"] if c["status"] == "fail"}
    assert failures == {"web_tool_budget", "evidence_integrity"}
    del bundle["detail"]["request"]["webToolCalls"]
    result = grade_bundle(bundle, {"caseId": "web-research-001"})
    assert (
        next(c for c in result["checks"] if c["name"] == "web_tool_budget")["status"] == "unknown"
    )

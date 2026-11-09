"""Capture an existing run, then grade evidence integrity and supplied human annotations.

Neither command submits a request or calls a model. Structural checks cannot establish
semantic support; an unannotated run never receives a research-quality pass.
"""

import argparse
import json
import math
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from contextforge.core.entities.message import ReportMessage, RequestMessage
from contextforge.core.models.base import ModelClient
from contextforge.core.settings import Settings
from contextforge.evals.bundle import bundle_hash, normalized, snapshot_texts
from contextforge.evals.scoring import FLAGS, WEIGHTS, judged_grade, judged_status


def capture_run(
    client: httpx.Client, chat_id: str, request_id: str, case_id: str
) -> dict[str, Any]:
    """GET the owned run and only the snapshots/deliverables its report references."""
    base = f"/chats/{chat_id}"
    detail = _get(client, f"{base}/requests/{request_id}").json()
    report = None
    cursor = None
    while True:
        page = _get(
            client,
            f"{base}/messages",
            params={"limit": 100, **({"cursor": cursor} if cursor else {})},
        ).json()
        report = next(
            (
                m
                for m in page["items"]
                if m.get("kind") == "report" and m.get("requestMessageId") == request_id
            ),
            None,
        )
        cursor = page.get("nextCursor")
        if report is not None or cursor is None:
            break
    if report is None:
        raise ValueError("No report is available for this request.")
    body = report["report"]
    ids = {r["artifactId"] for c in body["claims"] for r in c["evidenceRefs"]}
    snapshots = {}
    for artifact_id in sorted(ids):
        raw = _get(client, f"{base}/artifacts/{artifact_id}/content").content
        try:
            snapshots[artifact_id] = [json.loads(line) for line in raw.split(b"\n") if line.strip()]
        except ValueError:
            snapshots[artifact_id] = []
    deliverables = [
        _get(client, f"{base}/artifacts/{r['artifactId']}/content").text
        for r in body.get("deliverables", [])
    ]
    return {
        "bundleVersion": 1,
        "caseId": case_id,
        "detail": detail,
        "report": report,
        "snapshots": snapshots,
        "deliverables": deliverables,
        "trace": _get(client, f"{base}/requests/{request_id}/trace").json(),
    }


def auth_headers(settings: Settings) -> dict[str, str]:
    """The local API's bearer header when a dev token is set."""
    if settings.dev_token is None:
        return {}
    return {"Authorization": f"Bearer {settings.dev_token.get_secret_value()}"}


def _get(client: httpx.Client, path: str, **kwargs: Any) -> httpx.Response:
    response = client.get(path, **kwargs)
    response.raise_for_status()
    return response


def grade_bundle(
    bundle: dict[str, Any],
    reference: dict[str, Any],
    annotations: dict[str, Any] | None = None,
    *,
    max_usd: float = 0.25,
    judge: ModelClient | None = None,
) -> dict[str, Any]:
    """Structural checks, then quality from human ``annotations``; with ``judge``, the judge
    produces the annotation and a supplied human annotation wins for each field it sets
    (``evals.scoring``)."""
    checks = []

    def check(name: str, state: bool | None, detail: str) -> None:
        checks.append(
            {
                "name": name,
                "status": "unknown" if state is None else "pass" if state else "fail",
                "detail": detail,
            }
        )

    try:
        raw_request = bundle["detail"]["request"]
        request = RequestMessage.model_validate(raw_request)
        report = ReportMessage.model_validate(bundle["report"])
    except (KeyError, TypeError, ValidationError) as exc:
        return {
            "status": "failed",
            "checks": [{"name": "records", "status": "fail", "detail": type(exc).__name__}],
            "qualityScore": None,
        }
    check("case", bundle.get("caseId") == reference["caseId"], "Case identity matches reference.")
    check(
        "ownership",
        report.chat_id == request.chat_id
        and report.request_message_id == request.message_id
        and report.user_id == request.user_id,
        "Report belongs to this request and user.",
    )
    check("completed", request.status == "COMPLETED", str(request.status))
    check("findings", bool(report.report.claims), f"{len(report.report.claims)} retained findings.")
    count = raw_request.get("webToolCalls")
    check(
        "web_tool_budget",
        None if count is None else type(count) is int and 0 <= count <= 50,
        f"Durable web-tool admissions: {count}; ceiling 50.",
    )
    cost = bundle.get("detail", {}).get("usage", {}).get("estimatedCostUsd")
    valid_cost = type(cost) in (int, float) and math.isfinite(cost) and cost >= 0
    check(
        "estimated_spend",
        None if cost is None else valid_cost and cost <= max_usd,
        f"Recorded estimate {cost}; cap {max_usd}; not verified billing.",
    )
    errors = evidence_errors(report, bundle.get("snapshots", {}))
    check(
        "evidence_integrity",
        not errors,
        "; ".join(f"{claim_id}: {problem}" for claim_id, problem in errors)
        or "URLs and quoted excerpts match saved snapshots.",
    )
    if judge is not None:
        judged = judged_grade(
            bundle, report, reference, annotations, judge=judge, evidence_errors=errors
        )
        return {
            "caseId": reference["caseId"],
            "bundleHash": bundle_hash(bundle),
            "checks": checks,
            "status": judged_status(checks, judged),
            **judged,
        }
    annotation_issues, score = _annotations(bundle, report, annotations)
    result = {
        "caseId": reference["caseId"],
        "bundleHash": bundle_hash(bundle),
        "checks": checks,
        "qualityScore": score,
        "annotationIssues": annotation_issues,
        "semanticSupportAutomaticallyCertified": False,
        "referenceFacts": len(reference.get("facts", [])),
        "note": (
            "Reference facts guide adjudication; phrase/number matches alone "
            "do not earn a quality pass."
        ),
    }
    if any(c["status"] == "fail" for c in checks):
        result["status"] = "failed"
    elif any(c["status"] == "unknown" for c in checks):
        result["status"] = "incomplete_capture"
    elif (
        annotations
        and annotations.get("bundleHash") == bundle_hash(bundle)
        and (
            any(annotations.get("flags", {}).get(flag) is True for flag in FLAGS)
            or any(r.get("supported") is False for r in annotations.get("claims", []))
        )
    ):
        result["status"] = "failed_review"
    elif annotations is None or annotation_issues:
        result["status"] = "needs_review"
    else:
        flags = annotations["flags"]
        result["status"] = (
            "passed_with_supplied_reviews"
            if score is not None and score >= 85 and not any(flags.values())
            else "failed_review"
        )
    return result


def evidence_errors(report: ReportMessage, snapshots: dict[str, Any]) -> list[tuple[str, str]]:
    """``(claim id, problem)`` for each citation that fails the structural checks."""
    errors = []
    for claim in report.report.claims:
        if not claim.evidence_refs:
            errors.append((claim.claim_id, "no evidence"))
        for ref in claim.evidence_refs:
            url = urlsplit(ref.locator.value)
            if ref.locator.kind != "url" or url.scheme not in {"http", "https"} or not url.hostname:
                errors.append((claim.claim_id, "invalid web locator"))
                continue
            texts = snapshot_texts(snapshots, ref.artifact_id, ref.locator.value)
            excerpt = normalized(ref.excerpt or "")
            if len(excerpt) < 20 or not any(excerpt in normalized(text) for text in texts):
                errors.append((claim.claim_id, "excerpt not found in its source snapshot"))
    return errors


def _annotations(
    bundle: dict[str, Any], report: ReportMessage, annotations: dict[str, Any] | None
) -> tuple[list[str], float | None]:
    if annotations is None:
        return ["Human adjudication of material claims and narrative is required."], None
    issues = []
    if annotations.get("bundleHash") != bundle_hash(bundle):
        issues.append("Annotations do not match this exact captured run.")
    if not annotations.get("reviewer") or annotations.get("narrativeReviewed") is not True:
        issues.append("Name the reviewer and review the full narrative/deliverable.")
    reviews = annotations.get("claims", [])
    ids = [r.get("claimId") for r in reviews]
    if len(set(ids)) != len(ids) or set(ids) != {c.claim_id for c in report.report.claims}:
        issues.append("Review each retained claim exactly once.")
    if any(r.get("supported") is not True or not r.get("reason") for r in reviews):
        issues.append("A claim is unsupported or its review lacks a reason.")
    flags = annotations.get("flags", {})
    if set(flags) != set(FLAGS) or any(type(v) is not bool for v in flags.values()):
        issues.append("Record all hard-failure flags explicitly as booleans.")
    scores = annotations.get("scores", {})
    valid = set(scores) == set(WEIGHTS) and all(
        type(v) in (int, float) and math.isfinite(v) and 0 <= v <= WEIGHTS[k]
        for k, v in scores.items()
    )
    if not valid:
        issues.append("Supply bounded scores for all rubric dimensions.")
    return issues, sum(scores.values()) if valid else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    capture = sub.add_parser("capture", help="Read an existing app run; never starts research.")
    capture.add_argument("--base-url", default="http://localhost:8000")
    capture.add_argument("--chat", required=True)
    capture.add_argument("--request", required=True)
    capture.add_argument("--case", default="web-research-001")
    capture.add_argument("--out", type=Path, required=True)
    grade = sub.add_parser("grade", help="Grade a saved bundle; no network or models.")
    grade.add_argument("--bundle", type=Path, required=True)
    grade.add_argument(
        "--reference", type=Path, default=Path("evals/fixtures/web-research-001-reference.json")
    )
    grade.add_argument("--annotations", type=Path)
    grade.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "capture":
        headers = auth_headers(Settings())
        with httpx.Client(base_url=args.base_url, headers=headers, timeout=30) as client:
            output = capture_run(client, args.chat, args.request, args.case)
        code = 0
    else:
        annotations = json.loads(args.annotations.read_text()) if args.annotations else None
        output = grade_bundle(
            json.loads(args.bundle.read_text()), json.loads(args.reference.read_text()), annotations
        )
        code = 0 if output["status"] == "passed_with_supplied_reviews" else 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Saved {args.out}; status={output.get('status', 'captured')}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

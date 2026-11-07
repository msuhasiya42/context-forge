"""Conservative support review after snapshot/quote validation.

Numerical presence is necessary, not sufficient. A separate model assessment checks metric,
scope, attribution and inference. Missing, duplicate or unavailable assessments are withheld.
This is a quality filter, never a certification of truth or source independence.
"""

import json
import logging
import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from langchain_core.messages import HumanMessage
from pydantic import Field, ValidationError

from contextforge.core.context.prompts import load_prompt, prompt_version
from contextforge.core.entities import Entity
from contextforge.core.entities.message import ReportBody
from contextforge.core.entities.model_call import Stage
from contextforge.core.models.base import ModelRequest
from contextforge.core.models.errors import ModelError
from contextforge.core.state import Fact, RequestState
from contextforge.worker.dependencies import WorkerDependencies
from contextforge.worker.stages.citation_checks import (
    CitationCheckResult,
    _owned_records,
    _record_text,
    check_citations,
)

logger = logging.getLogger(__name__)
_NUMBERS = re.compile(r"(?<![\w])[+-]?\d+(?:[.,]\d+)*")
_BATCH_SIZE = 8


class SupportAssessment(Entity):
    id: str
    verdict: Literal["supported", "unsupported", "uncertain"]
    reason: str = Field(max_length=500)


class SupportReview(Entity):
    reviews: list[SupportAssessment] = Field(max_length=_BATCH_SIZE)


def validate_research_findings(
    request_state: RequestState, worker_dependencies: WorkerDependencies
) -> CitationCheckResult:
    cited = check_citations(request_state, worker_dependencies)
    if worker_dependencies.settings is None or not worker_dependencies.settings.verify_web_claims:
        return cited
    packets = [
        _fact_packet(str(i), fact, request_state, worker_dependencies)
        for i, fact in enumerate(cited["facts"])
    ]
    web = [p for p in packets if p["evidence"]]
    verdicts = review_statement_support(request_state, web, worker_dependencies)
    kept: list[Fact] = []
    gaps = list(cited["uncertainties"])
    for packet, fact in zip(packets, cited["facts"], strict=True):
        if not packet["evidence"]:  # Dataset verification belongs to its own boundary.
            kept.append(fact)
            continue
        verdict = verdicts.get(packet["id"])
        if verdict is not None and verdict.verdict == "supported":
            kept.append(fact)
            if all(e.get("sourceKind") == "search" for e in packet["evidence"]):
                gaps.append(f"Snippet-only evidence; underlying page not reviewed: {fact['text']}")
        else:
            reason = verdict.reason if verdict else "support assessment unavailable"
            gaps.append(f"Withheld claim: {fact['text']} ({reason})")
    return CitationCheckResult(
        facts=kept,
        uncertainties=list(dict.fromkeys(gaps)),
        review={
            "mode": "model_support_review",
            "supported": len(kept),
            "withheld": len(cited["facts"]) - len(kept),
            "independentTruthVerified": False,
        },
    )


def review_statement_support(
    request_state: RequestState,
    packets: list[dict[str, Any]],
    worker_dependencies: WorkerDependencies,
) -> dict[str, SupportAssessment]:
    """Review explicit statements against their supplied evidence; omit unchecked IDs."""
    ready: list[dict[str, Any]] = []
    verdicts: dict[str, SupportAssessment] = {}
    for packet in packets:
        evidence = json.dumps(packet["evidence"], ensure_ascii=False)
        missing = sorted(_numbers(packet["text"]) - _numbers(evidence))
        if missing:
            verdicts[packet["id"]] = SupportAssessment(
                id=packet["id"],
                verdict="unsupported",
                reason="Numbers absent from the cited evidence: " + ", ".join(missing),
            )
        else:
            ready.append(packet)
    for start in range(0, len(ready), _BATCH_SIZE):
        batch = ready[start : start + _BATCH_SIZE]
        request = ModelRequest(
            stage=Stage.CITE,
            model=worker_dependencies.model_id(Stage.CITE),
            system=[load_prompt(prompt_version(Stage.CITE))],
            messages=[HumanMessage(json.dumps({"statements": batch}, ensure_ascii=False))],
            output_schema=SupportReview,
            max_tokens=2000,
        )
        try:
            response = worker_dependencies.model_for(
                request_state["request_message_id"],
                memory_version=request_state["memory"]["version"],
            ).invoke(request)
            reviewed = SupportReview.model_validate(response.structured)
        except (ModelError, ValidationError) as exc:
            logger.warning("support review unavailable: %s", type(exc).__name__)
            continue
        expected = {p["id"] for p in batch}
        counts = Counter(r.id for r in reviewed.reviews)
        if set(counts) - expected:
            continue
        verdicts.update({r.id: r for r in reviewed.reviews if counts[r.id] == 1})
    return verdicts


def review_summary_and_conclusions(
    request_state: RequestState,
    body: ReportBody,
    summary: str,
    worker_dependencies: WorkerDependencies,
) -> tuple[ReportBody, bool]:
    """Check the summary and inferred assessments too; return whether the summary held."""
    claims = {c.claim_id: c.text for c in body.claims}
    packets = [
        {
            "id": "summary",
            "text": summary,
            "evidence": [{"text": c.text} for c in body.claims],
        }
    ]
    packets.extend(
        {
            "id": f"conclusion-{i}",
            "text": "Inference: " + c.text,
            "evidence": [{"text": claims[k]} for k in c.based_on if k in claims],
        }
        for i, c in enumerate(body.conclusions)
    )
    verdicts = review_statement_support(request_state, packets, worker_dependencies)
    kept = []
    gaps = list(body.uncertainties)
    for i, conclusion in enumerate(body.conclusions):
        verdict = verdicts.get(f"conclusion-{i}")
        if verdict is not None and verdict.verdict == "supported":
            kept.append(conclusion)
        else:
            reason = verdict.reason if verdict else "support assessment unavailable"
            gaps.append(f"Withheld assessment: {conclusion.text} ({reason})")
    checked = body.model_copy(update={"conclusions": kept, "uncertainties": gaps})
    verdict = verdicts.get("summary")
    return checked, verdict is not None and verdict.verdict == "supported"


def _fact_packet(
    identity: str, fact: Fact, request_state: RequestState, worker_dependencies: WorkerDependencies
) -> dict[str, Any]:
    evidence = []
    for ref in fact["evidence_refs"]:
        if ref.get("locator", {}).get("kind") != "url":
            continue
        artifact = worker_dependencies.repositories.artifacts.get(
            request_state["chat_id"], ref["artifact_id"]
        )
        for record in _owned_records(artifact, request_state, worker_dependencies):
            if record.get("url") != ref["locator"]["value"]:
                continue
            text = " ".join(_record_text(record).split())
            excerpt = " ".join((ref.get("excerpt") or "").split())
            at = max(0, text.casefold().find(excerpt.casefold()))
            evidence.append(
                {
                    "url": record["url"],
                    "quote": excerpt,
                    "context": text[max(0, at - 350) : at + len(excerpt) + 700],
                    "publishedAt": record.get("publishedAt"),
                    "sourceKind": record.get(
                        "sourceKind", "page" if "text" in record else "search"
                    ),
                }
            )
    return {"id": identity, "text": fact["text"], "evidence": evidence}


def _numbers(text: str) -> set[str]:
    values = set()
    for value in _NUMBERS.findall(text):
        try:
            values.add(str(Decimal(value.replace(",", "")).normalize()))
        except InvalidOperation:  # A dotted version such as 3.13.0, not a decimal.
            values.add(value)
    return values

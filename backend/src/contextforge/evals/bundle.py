"""Reading a captured run bundle (``web_research.capture_run``): its report text, the saved page
text each citation points at, and each claim with its evidence for the judge."""

import hashlib
import json
import re
from typing import Any

from contextforge.core.entities.message import ReportMessage
from contextforge.core.values import EvidenceRef
from contextforge.evals.judge import ClaimEvidence

# Characters of page text the judge sees around a cited excerpt.
CONTEXT_CHARS = 1_500
_CONTEXT_BEFORE = 500


def bundle_hash(bundle: dict[str, Any]) -> str:
    """Names one exact capture, so an annotation cannot be applied to another run."""
    return hashlib.sha256(
        json.dumps(bundle, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def snapshot_texts(snapshots: dict[str, Any], artifact_id: str, url: str) -> list[str]:
    """The saved text (a fetched page, or a search hit's snippet) of ``url`` in one artifact."""
    return [
        str(r.get("text", r.get("snippet", "")))
        for r in snapshots.get(artifact_id, [])
        if isinstance(r, dict) and r.get("url") == url
    ]


def normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def report_text(bundle: dict[str, Any]) -> str:
    """What the user read: the long-report deliverables, else the report message."""
    return "\n\n".join(bundle.get("deliverables") or []) or str(bundle["report"]["content"])


def claim_evidence(report: ReportMessage, snapshots: dict[str, Any]) -> list[ClaimEvidence]:
    """Each claim with its first URL citation whose excerpt is in the saved page (else its first
    URL citation), and the page text around that excerpt. A claim with no URL citation is
    judged with empty evidence, so it cannot be supported."""
    evidence = []
    for claim in report.report.claims:
        located = [
            (ref, *_excerpt_at(ref, snapshots))
            for ref in claim.evidence_refs
            if ref.locator.kind == "url"
        ]
        best = next((item for item in located if item[2] >= 0), located[0] if located else None)
        if best is None:
            evidence.append(
                ClaimEvidence(
                    claim_id=claim.claim_id, claim=claim.text, cited_url="", excerpt="", context=""
                )
            )
            continue
        ref, text, at = best
        excerpt = ref.excerpt or ""
        evidence.append(
            ClaimEvidence(
                claim_id=claim.claim_id,
                claim=claim.text,
                cited_url=ref.locator.value,
                excerpt=excerpt,
                context=_window(text, at, len(" ".join(excerpt.split()))),
            )
        )
    return evidence


def _excerpt_at(ref: EvidenceRef, snapshots: dict[str, Any]) -> tuple[str, int]:
    """The saved page text with whitespace collapsed, and where the excerpt starts in it (-1
    when absent); the first matching record wins."""
    texts = [
        " ".join(t.split()) for t in snapshot_texts(snapshots, ref.artifact_id, ref.locator.value)
    ]
    excerpt = " ".join((ref.excerpt or "").split()).lower()
    for text in texts:
        if excerpt and (at := text.lower().find(excerpt)) >= 0:
            return text, at
    return (texts[0] if texts else ""), -1


def _window(text: str, at: int, excerpt_chars: int) -> str:
    """About ``CONTEXT_CHARS`` of ``text`` around the excerpt, always including all of it; the
    head of the text when the excerpt is absent."""
    if at < 0:
        return text[:CONTEXT_CHARS]
    start = max(0, at - _CONTEXT_BEFORE)
    return text[start : max(start + CONTEXT_CHARS, at + excerpt_chars)]

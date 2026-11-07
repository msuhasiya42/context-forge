"""A report too long to read in the chat: saved whole as a markdown deliverable Artifact, with
a short summary left in the report Message (F5 ruling: over 1,500 characters)."""

from typing import Any

from contextforge.core.entities.artifact import Artifact, ArtifactKind, ArtifactStatus, IndexStatus
from contextforge.core.entities.message import Claim, ReportMessage
from contextforge.core.ids import derived_id, now_iso
from contextforge.core.storage import deliverable_key
from contextforge.core.values import ArtifactRef, LocatorKind
from contextforge.worker.deps import WorkerDeps

LONG_REPORT_CHARS = 1500
MARKDOWN = "text/markdown"


def attach_if_long(report: ReportMessage, goal: str, deps: WorkerDeps) -> ReportMessage:
    """``report`` as it is when its text fits the chat. Otherwise the full report is saved as a
    deliverable Artifact first, then returned with a summary for its text and a ref to it.

    The Artifact id derives from the request, so a re-run overwrites it rather than adding one.
    """
    if len(report.content) <= LONG_REPORT_CHARS:
        return report
    data = render_markdown(report, goal).encode()
    artifact_id = derived_id(f"{report.request_message_id}:report-deliverable")
    deps.repos.artifacts.save(
        Artifact(
            artifact_id=artifact_id,
            user_id=report.user_id,
            chat_id=report.chat_id,
            kind=ArtifactKind.DELIVERABLE,
            name=f"report-{report.request_message_id}.md",
            media_type=MARKDOWN,
            size_bytes=len(data),
            storage_ref=deps.blobs.put(deliverable_key(report.chat_id, artifact_id), data),
            status=ArtifactStatus.AVAILABLE,
            index_status=IndexStatus.NOT_INDEXED,
            created_at=now_iso(),
        )
    )
    body = report.report.model_copy(
        update={"deliverables": [ArtifactRef(artifact_id=artifact_id, role="output")]}
    )
    return report.model_copy(update={"content": _summary(report), "report": body})


def _summary(report: ReportMessage) -> str:
    """The report's first paragraph (cut to fit) and a pointer to the full report."""
    first = report.content.split("\n\n", 1)[0]
    if len(first) > LONG_REPORT_CHARS:
        first = first[: LONG_REPORT_CHARS - 1].rstrip() + "…"
    claims, limits = len(report.report.claims), len(report.report.uncertainties)
    return f"{first}\n\nFull report attached: {claims} findings, {limits} limitations."


def render_markdown(report: ReportMessage, goal: str) -> str:
    """The full report: its text (findings with source links, then limitations), each claim's
    evidence with the excerpt it quotes, and what the run covered."""
    sections = [f"# {_line(goal) or 'Research report'}", report.content]
    evidence = [_evidence(index, claim) for index, claim in enumerate(report.report.claims, 1)]
    if evidence:
        sections.append("## Evidence\n\n" + "\n".join(evidence))
    sections.append("## Coverage\n\n" + "\n".join(_coverage_lines(report.report.coverage)))
    return "\n\n".join(sections) + "\n"


def _evidence(index: int, claim: Claim) -> str:
    lines = [f"{index}. {_line(claim.text)}"]
    for ref in claim.evidence_refs:
        where = (
            f"<{ref.locator.value}>"
            if ref.locator.kind == LocatorKind.URL
            else f"artifact {ref.artifact_id}, {ref.locator.kind.value} {ref.locator.value}"
        )
        quote = f': "{_line(ref.excerpt)}"' if ref.excerpt else ""
        lines.append(f"   - {where}{quote}")
    return "\n".join(lines)


def _coverage_lines(coverage: dict[str, Any]) -> list[str]:
    tasks = coverage.get("tasks")
    lines = []
    if isinstance(tasks, dict):
        lines.append(
            f"- Tasks: {tasks.get('completed', 0)} completed, {tasks.get('skipped', 0)} skipped, "
            f"{tasks.get('failed', 0)} failed"
        )
    period = coverage.get("period")
    if isinstance(period, dict):
        lines.append(f"- Period: {period.get('start')} to {period.get('end')}")
    return lines or ["- Not recorded"]


def _line(text: str | None) -> str:
    """``text`` on one line, so page text cannot start a heading or list of its own."""
    return " ".join((text or "").split())

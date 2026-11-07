"""The report Message's text: its summary and counts first, then the full text when it fits the
chat; a full text too long for the chat is saved whole as a markdown deliverable Artifact
instead (F5 ruling: over 1,500 characters; F10 ruling 2)."""

from typing import Any

from contextforge.core.entities.artifact import Artifact, ArtifactKind, ArtifactStatus, IndexStatus
from contextforge.core.entities.message import Claim, ReportMessage
from contextforge.core.ids import derived_id, now_iso
from contextforge.core.storage import deliverable_key
from contextforge.core.values import ArtifactRef, LocatorKind
from contextforge.worker.deps import WorkerDeps

LONG_REPORT_CHARS = 1500
MARKDOWN = "text/markdown"
ATTACHED = "Full report attached."


def with_summary(report: ReportMessage, goal: str, summary: str, deps: WorkerDeps) -> ReportMessage:
    """``report``, whose content is still its full text, led by ``summary`` and the count of its
    findings and limitations. A full text that fits the chat follows them. A longer one is
    saved whole as a deliverable Artifact first, and the content says it is attached and the
    report refs it.

    The Artifact id derives from the request, so a re-run overwrites it rather than adding one.
    """
    counts = _counts(report)
    if len(report.content) <= LONG_REPORT_CHARS:
        return report.model_copy(update={"content": _paragraphs(summary, counts, report.content)})
    data = render_markdown(report, goal, summary).encode()
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
    content = _paragraphs(summary, f"{counts} {ATTACHED}")
    return report.model_copy(update={"content": content, "report": body})


def _counts(report: ReportMessage) -> str:
    """ "6 findings, 1 limitation.": how much the report holds, for the reader of its summary."""
    claims, limits = len(report.report.claims), len(report.report.uncertainties)
    return f"{_count(claims, 'finding')}, {_count(limits, 'limitation')}."


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}{'' if number == 1 else 's'}"


def _paragraphs(*texts: str) -> str:
    return "\n\n".join(text for text in texts if text)


def render_markdown(report: ReportMessage, goal: str, summary: str) -> str:
    """The full report: its summary, its text (numbered findings with source links, the
    assessment drawn from them, what was not answered, the limitations), each claim's evidence
    with the excerpt it quotes, and what the run covered."""
    sections = [f"# {_line(goal) or 'Research report'}", summary, report.content]
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

"""Phase 1 stand-ins for the task kinds that are not built yet."""

from contextforge.core.entities.task import Task, TaskKind, TaskResult
from contextforge.core.values import EvidenceRef


def reconcile_stub(task: Task, dependency_results: list[TaskResult], gaps: list[str]) -> TaskResult:
    """Carry the completed dependencies' summaries and evidence forward, and the ``gaps`` the
    others left as uncertainties; reconcile nothing.

    ``task`` is unused until phase 4 reconciles for real.
    """
    refs = [ref for result in dependency_results for ref in result.evidence_refs]
    return TaskResult(
        summary="; ".join(result.summary for result in dependency_results),
        facts=[],
        structured_data={"reconciled": False},
        evidence_refs=_union(refs),
        artifact_refs=[],
        uncertainties=["reconcile is a stub in phase 1", *gaps],
        coverage={},
    )


def skipped_reason(kind: TaskKind) -> str:
    """Why a task of ``kind`` is SKIPPED in phase 1."""
    return f"{kind} is not implemented in phase 1"


def _union(refs: list[EvidenceRef]) -> list[EvidenceRef]:
    """``refs`` without repeats, first occurrence first (refs compare by value)."""
    unique: list[EvidenceRef] = []
    for ref in refs:
        if ref not in unique:
            unique.append(ref)
    return unique

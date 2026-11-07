"""Report stage: the request's report Message, and the two ways a planned or failed run ends."""

import logging
from collections import Counter

from contextforge.core.entities.message import (
    Claim,
    MessageKind,
    ReportBody,
    ReportConstraint,
    ReportMessage,
    RequestStatus,
    Sender,
)
from contextforge.core.entities.task import TaskStatus
from contextforge.core.ids import derived_id, now_iso
from contextforge.core.logging import request_context
from contextforge.core.state import Fact, RequestState
from contextforge.core.values import ArtifactRef, EvidenceRef
from contextforge.worker.deps import WorkerDeps
from contextforge.worker.stages.execute import TaskOutcome
from contextforge.worker.stages.guards import leave_running
from contextforge.worker.stages.plan import local_today

logger = logging.getLogger(__name__)

EVERY_TASK_FAILED = "every task failed"
NO_CLAIMS = "No finding could be supported with evidence."


def write_report(brief: RequestState, outcomes: list[TaskOutcome], deps: WorkerDeps) -> str:
    """Save the report, then end the request: COMPLETED when any task completed, else FAILED.

    The report id derives from the request, so a re-run overwrites it. Returns the final status;
    raises ``Cancelled`` when the user cancelled meanwhile.
    """
    with request_context(brief["request_message_id"]):
        report = _report_message(brief, outcomes)
        deps.repos.messages.save(report)
        completed = any(o["status"] == TaskStatus.COMPLETED for o in outcomes)
        status = RequestStatus.COMPLETED if completed else RequestStatus.FAILED
        reason = None if completed else EVERY_TASK_FAILED
        leave_running(deps, brief["chat_id"], brief["request_message_id"], status, reason)
        logger.info("report saved with %d claims; request %s", len(report.report.claims), status)
        return status.value


def fail_request(brief: RequestState, reason: str, deps: WorkerDeps) -> None:
    """End a request that could not be planned: RUNNING -> FAILED with ``reason``, no report."""
    with request_context(brief["request_message_id"]):
        logger.warning("request failed: %s", reason)
        leave_running(
            deps, brief["chat_id"], brief["request_message_id"], RequestStatus.FAILED, reason
        )


def _report_message(brief: RequestState, outcomes: list[TaskOutcome]) -> ReportMessage:
    request_message_id = brief["request_message_id"]
    report_id = derived_id(f"{request_message_id}:report")
    claims = _claims(report_id, brief["facts"])
    now = now_iso()
    return ReportMessage(
        message_id=report_id,
        chat_id=brief["chat_id"],
        user_id=brief["user_id"],
        sender=Sender.ASSISTANT,
        kind=MessageKind.REPORT,
        request_message_id=request_message_id,
        content="\n".join(claim.text for claim in claims) or NO_CLAIMS,
        created_at=now,
        updated_at=now,
        report=ReportBody(
            claims=claims,
            inputs_consulted=_inputs_consulted(claims),
            uncertainties=_uncertainties(outcomes),
            constraints=_constraints(brief["constraints"]),
            coverage=_coverage(outcomes),
        ),
    )


def _claims(report_id: str, facts: list[Fact]) -> list[Claim]:
    """One UNKNOWN claim per fact; checking claims against their evidence is phase 3."""
    return [
        Claim(
            claim_id=derived_id(f"{report_id}:{index}"),
            text=fact["text"],
            evidence_refs=[EvidenceRef.model_validate(ref) for ref in fact["evidence_refs"]],
            status="UNKNOWN",
        )
        for index, fact in enumerate(facts)
    ]


def _inputs_consulted(claims: list[Claim]) -> list[ArtifactRef]:
    """Every Artifact a claim cites, once each, in id order."""
    cited = {ref.artifact_id for claim in claims for ref in claim.evidence_refs}
    return [ArtifactRef(artifact_id=artifact_id, role="input") for artifact_id in sorted(cited)]


def _uncertainties(outcomes: list[TaskOutcome]) -> list[str]:
    """The tasks' own uncertainties, then one line per task that did not complete; no repeats."""
    reported = [line for outcome in outcomes for line in outcome["uncertainties"]]
    unfinished = [
        f"Task {o['task_id']} {o['status'].lower()}: {o['failure_reason']}"
        for o in outcomes
        if o["status"] in (TaskStatus.SKIPPED, TaskStatus.FAILED)
    ]
    return list(dict.fromkeys([*reported, *unfinished]))


def _constraints(constraints: list[str]) -> list[ReportConstraint]:
    """The constraints the request ran under, dated so a later request knows when they held."""
    today = local_today().isoformat()
    return [ReportConstraint(text=text, source="request", date=today) for text in constraints]


def _coverage(outcomes: list[TaskOutcome]) -> dict[str, dict[str, int]]:
    counts = Counter(outcome["status"] for outcome in outcomes)
    return {
        "tasks": {
            "completed": counts[TaskStatus.COMPLETED],
            "skipped": counts[TaskStatus.SKIPPED],
            "failed": counts[TaskStatus.FAILED],
        }
    }

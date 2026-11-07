"""Report stage: the request's report Message, and the two ways a planned or failed run ends."""

import logging
from collections import Counter
from dataclasses import dataclass

from contextforge.core.entities.message import (
    Claim,
    ClarificationMessage,
    ClarificationResponseMessage,
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
from contextforge.core.services.memory import user_timezone
from contextforge.core.state import Fact, RequestState
from contextforge.core.values import ArtifactRef, EvidenceRef
from contextforge.worker.deps import WorkerDeps
from contextforge.worker.stages.cite import merge_repeats
from contextforge.worker.stages.execute import TaskOutcome
from contextforge.worker.stages.guards import check_not_cancelled, leave_running
from contextforge.worker.stages.long_report import attach_if_long
from contextforge.worker.stages.plan import answer_constraint, is_plan_mechanics, local_today

logger = logging.getLogger(__name__)

EVERY_TASK_FAILED = "every task failed"
NO_CLAIMS = "No finding could be supported with evidence."
NOT_ANSWERED = "Not answered: {goal}"
WITHHELD_ANSWER = (
    "Your answer to '{question}' was not used because it looked like a password, key or token."
)


@dataclass(frozen=True)
class _Answer:
    """One clarification answer of the request: its question, the constraint it added (None
    when withheld as a credential) and the UTC date it was given."""

    question: str
    constraint: str | None
    date: str


def write_report(
    brief: RequestState,
    outcomes: list[TaskOutcome],
    cite_uncertainties: list[str],
    deps: WorkerDeps,
) -> str:
    """Save the report, then end the request: COMPLETED when any task completed, else FAILED.

    A report too long for the chat is saved whole as a deliverable Artifact and summarised
    (``long_report``).

    ``cite_uncertainties`` are the cite stage's reasons for the facts it dropped. The report id
    derives from the request, so a re-run overwrites it. Returns the final status; raises
    ``Cancelled`` when the user cancelled meanwhile.
    """
    with request_context(brief["request_message_id"]):
        check_not_cancelled(deps, brief["chat_id"], brief["request_message_id"])
        report = _report_message(brief, outcomes, cite_uncertainties, _answers(brief, deps))
        # The deliverable is saved before the Message, so its ref never points at nothing.
        report = attach_if_long(report, brief["goal"], deps)
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


def _report_message(
    brief: RequestState,
    outcomes: list[TaskOutcome],
    cite_uncertainties: list[str],
    answers: list[_Answer],
) -> ReportMessage:
    request_message_id = brief["request_message_id"]
    report_id = derived_id(f"{request_message_id}:report")
    claims = _claims(report_id, brief["facts"])
    withheld = [WITHHELD_ANSWER.format(question=a.question) for a in answers if not a.constraint]
    assumed = brief.get("assumptions", [])
    uncertainties = [*withheld, *assumed, *_uncertainties(outcomes, cite_uncertainties)]
    now = now_iso()
    return ReportMessage(
        message_id=report_id,
        chat_id=brief["chat_id"],
        user_id=brief["user_id"],
        sender=Sender.ASSISTANT,
        kind=MessageKind.REPORT,
        request_message_id=request_message_id,
        content=_answer_text(claims, _not_answered(outcomes), uncertainties),
        created_at=now,
        updated_at=now,
        report=ReportBody(
            claims=claims,
            inputs_consulted=_inputs_consulted(claims),
            uncertainties=uncertainties,
            constraints=_constraints(brief, answers),
            coverage={
                **_coverage(outcomes),
                **({"period": brief["period"]} if brief.get("period") else {}),
            },
        ),
    )


def _answers(brief: RequestState, deps: WorkerDeps) -> list[_Answer]:
    """The request's clarification answers, read back from the chat, oldest first."""
    in_chat = deps.repos.messages.list_by_chat(brief["chat_id"])
    questions = {m.message_id: m.content for m in in_chat if isinstance(m, ClarificationMessage)}
    replies = sorted(
        (
            m
            for m in in_chat
            if isinstance(m, ClarificationResponseMessage)
            and m.request_message_id == brief["request_message_id"]
            and m.in_reply_to_message_id in questions
        ),
        key=lambda m: (m.created_at, m.message_id),
    )
    return [
        _Answer(
            question=questions[m.in_reply_to_message_id],
            constraint=answer_constraint(questions[m.in_reply_to_message_id], m.content),
            date=m.created_at[:10],
        )
        for m in replies
    ]


def _claims(report_id: str, facts: list[Fact]) -> list[Claim]:
    """One UNKNOWN claim per distinct claim, first occurrence first, citing the evidence of all
    its repeats: a comparison task restates its dependencies' facts (live run 3, F7 ruling 3).
    Checking claims against their evidence is phase 3."""
    return [
        Claim(
            claim_id=derived_id(f"{report_id}:{index}"),
            text=fact["text"],
            evidence_refs=[EvidenceRef.model_validate(ref) for ref in fact["evidence_refs"]],
            status="UNKNOWN",
        )
        for index, fact in enumerate(merge_repeats(facts))
    ]


def _inputs_consulted(claims: list[Claim]) -> list[ArtifactRef]:
    """Every Artifact a claim cites, once each, in id order."""
    cited = {ref.artifact_id for claim in claims for ref in claim.evidence_refs}
    return [ArtifactRef(artifact_id=artifact_id, role="input") for artifact_id in sorted(cited)]


def _uncertainties(outcomes: list[TaskOutcome], cite_uncertainties: list[str]) -> list[str]:
    """The tasks' own uncertainties, why cite dropped facts, then one line per task that did not
    complete; no repeats."""
    reported = [line for outcome in outcomes for line in outcome["uncertainties"]]
    unfinished = [
        f"Task {o['task_id']} {o['status'].lower()}: {o['failure_reason']}"
        for o in outcomes
        if o["status"] in (TaskStatus.SKIPPED, TaskStatus.FAILED)
    ]
    return list(dict.fromkeys([*reported, *cite_uncertainties, *unfinished]))


def _constraints(brief: RequestState, answers: list[_Answer]) -> list[ReportConstraint]:
    """The constraints the request ran under, dated so a later request knows when they held.

    A clarification answer carries ``source="clarification"`` and the date it was given; the
    rest are the request's, dated today in the user's timezone. The planner's restatements of
    its own output rules are dropped, so no later request inherits them as the user's.
    """
    answered = {a.constraint: a.date for a in answers if a.constraint}
    today = local_today(user_timezone(brief["memory"])).isoformat()
    return [
        ReportConstraint(text=text, source="clarification", date=answered[text])
        if text in answered
        else ReportConstraint(text=text, source="request", date=today)
        for text in brief["constraints"]
        if text in answered or not is_plan_mechanics(text)
    ]


def _coverage(outcomes: list[TaskOutcome]) -> dict[str, dict[str, int]]:
    counts = Counter(outcome["status"] for outcome in outcomes)
    return {
        "tasks": {
            "completed": counts[TaskStatus.COMPLETED],
            "skipped": counts[TaskStatus.SKIPPED],
            "failed": counts[TaskStatus.FAILED],
        }
    }


def _not_answered(outcomes: list[TaskOutcome]) -> list[str]:
    """One line per task without a result, naming its goal: the planner writes each goal from
    a part of the user's question, so this is what the report leaves unanswered (F7 ruling 4).
    """
    return [
        NOT_ANSWERED.format(goal=" ".join(outcome["goal"].split()))
        for outcome in outcomes
        if outcome["status"] != TaskStatus.COMPLETED
    ]


def _answer_text(claims: list[Claim], not_answered: list[str], uncertainties: list[str]) -> str:
    """The answers first: each claim with its source links, then what was not answered, then
    the limitations. With no claims the rest still follows, so the reader sees what is missing
    and why nothing could be supported."""
    lines = [] if claims else [NO_CLAIMS]
    for claim in claims:
        urls = list(
            dict.fromkeys(
                ref.locator.value for ref in claim.evidence_refs if ref.locator.kind.value == "url"
            )
        )
        links = " ".join(f"[source]({url})" for url in urls)
        lines.append(f"{claim.text} {links}".strip())
    lines.extend(not_answered)
    if uncertainties:
        lines.append("Limitations:\n" + "\n".join(f"- {text}" for text in uncertainties))
    return "\n\n".join(lines)

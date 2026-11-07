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
    ReportConclusion,
    ReportConstraint,
    ReportMessage,
    RequestStatus,
    Sender,
)
from contextforge.core.entities.task import TaskStatus
from contextforge.core.ids import derived_id, now_iso
from contextforge.core.logging import request_context
from contextforge.core.services.memory import user_timezone
from contextforge.core.state import Conclusion, Fact, RequestState
from contextforge.core.values import ArtifactRef, EvidenceRef, LocatorKind
from contextforge.worker.deps import WorkerDeps
from contextforge.worker.stages.cite import merge_repeats, supported_conclusions
from contextforge.worker.stages.execute import TaskOutcome
from contextforge.worker.stages.guards import check_not_cancelled, leave_running
from contextforge.worker.stages.long_report import with_summary
from contextforge.worker.stages.plan import answer_constraint, is_plan_mechanics, local_today
from contextforge.worker.stages.summary import summarize

logger = logging.getLogger(__name__)

EVERY_TASK_FAILED = "every task failed"
# The summary of a report without claims: there is nothing to summarise.
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

    Its text opens with a summary covering every part of the request it answers (``summary``);
    a report too long for the chat is saved whole as a deliverable Artifact (``long_report``).

    ``cite_uncertainties`` are the cite stage's reasons for the facts it dropped. The report id
    derives from the request, so a re-run overwrites it. Returns the final status; raises
    ``Cancelled`` when the user cancelled meanwhile.
    """
    with request_context(brief["request_message_id"]):
        check_not_cancelled(deps, brief["chat_id"], brief["request_message_id"])
        report = _report_message(brief, outcomes, cite_uncertainties, _answers(brief, deps))
        summary = (
            summarize(brief, outcomes, report.report, _not_answered(outcomes), deps)
            if report.report.claims
            else NO_CLAIMS
        )
        # The deliverable is saved before the Message, so its ref never points at nothing.
        report = with_summary(report, brief["goal"], summary, deps)
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
    # One fact per claim: a comparison task restates its dependencies' facts (F7 ruling 3).
    facts = merge_repeats(brief["facts"])
    claims = _claims(report_id, facts)
    conclusions, unsupported = supported_conclusions(_conclusions(outcomes), facts)
    assessment = _assessment(conclusions, claims)
    withheld = [WITHHELD_ANSWER.format(question=a.question) for a in answers if not a.constraint]
    assumed = brief.get("assumptions", [])
    checked = [*cite_uncertainties, *unsupported]
    uncertainties = [*withheld, *assumed, *_uncertainties(outcomes, checked)]
    now = now_iso()
    return ReportMessage(
        message_id=report_id,
        chat_id=brief["chat_id"],
        user_id=brief["user_id"],
        sender=Sender.ASSISTANT,
        kind=MessageKind.REPORT,
        request_message_id=request_message_id,
        content=_answer_text(claims, assessment, _not_answered(outcomes), uncertainties),
        created_at=now,
        updated_at=now,
        report=ReportBody(
            claims=claims,
            conclusions=assessment,
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
    """One UNKNOWN claim per fact, in order; ``facts`` have their repeats merged, so each
    claim cites the evidence of all of them. Checking claims against their evidence is
    phase 3."""
    return [
        Claim(
            claim_id=derived_id(f"{report_id}:{index}"),
            text=fact["text"],
            evidence_refs=[EvidenceRef.model_validate(ref) for ref in fact["evidence_refs"]],
            status="UNKNOWN",
        )
        for index, fact in enumerate(facts)
    ]


def _conclusions(outcomes: list[TaskOutcome]) -> list[Conclusion]:
    return [c for outcome in outcomes for c in outcome.get("conclusions", [])]


def _assessment(conclusions: list[Conclusion], claims: list[Claim]) -> list[ReportConclusion]:
    """Each conclusion linked to the claims it rests on. Cite named them by the texts of the
    facts those claims were made from (``supported_conclusions``)."""
    claim_ids = {claim.text: claim.claim_id for claim in claims}
    return [
        ReportConclusion(
            text=conclusion["text"],
            based_on=[claim_ids[text] for text in conclusion["based_on"]],
            confidence=conclusion["confidence"],
        )
        for conclusion in conclusions
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


def _answer_text(
    claims: list[Claim],
    assessment: list[ReportConclusion],
    not_answered: list[str],
    uncertainties: list[str],
) -> str:
    """The full text, after the summary: each claim, numbered, with its source links, and the
    assessment drawn from them; then what was not answered, then the limitations (F7 ruling 4,
    F10 rulings 1 and 3). With no claims the rest still follows, so the reader sees what is
    missing and why nothing could be supported."""
    numbers = {claim.claim_id: number for number, claim in enumerate(claims, 1)}
    lines = [f"{numbers[claim.claim_id]}. {_with_sources(claim)}" for claim in claims]
    if assessment:
        conclusions = [_assessment_line(conclusion, numbers) for conclusion in assessment]
        lines.append("Assessment:\n" + "\n".join(conclusions))
    lines.extend(not_answered)
    if uncertainties:
        lines.append("Limitations:\n" + "\n".join(f"- {text}" for text in uncertainties))
    return "\n\n".join(lines)


def _with_sources(claim: Claim) -> str:
    """The claim followed by a link to each URL its evidence cites."""
    urls = dict.fromkeys(
        ref.locator.value for ref in claim.evidence_refs if ref.locator.kind == LocatorKind.URL
    )
    return " ".join([claim.text, *(f"[source]({url})" for url in urls)])


def _assessment_line(conclusion: ReportConclusion, numbers: dict[str, int]) -> str:
    """A conclusion with the numbers of the findings it rests on; never a source link, since
    no source states it."""
    based_on = [str(numbers[claim_id]) for claim_id in conclusion.based_on]
    findings = (
        f"finding {based_on[0]}"
        if len(based_on) == 1
        else f"findings {', '.join(based_on[:-1])} and {based_on[-1]}"
    )
    return f"- {conclusion.text} (based on {findings}; {conclusion.confidence} confidence)"

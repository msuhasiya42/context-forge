"""Lead review stage (F16): after a research round the lead researcher reads the condensed
findings and either starts more researchers or completes.

Adapted from Anthropic, "How we built our multi-agent research system" (2025): the lead keeps
its research plan (the brief), reviews what the subagents return and decides whether more
research is needed. The caps are ours and enforced here in code, before and after the model.
A plain function the workflow wraps as a checkpointed task, so a resumed run never asks again.
"""

import json
import logging
import re
import sys
from collections import defaultdict
from collections.abc import Sequence
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage
from pydantic import ValidationError

from contextforge.core.context.prompts import load_prompt, prompt_version
from contextforge.core.entities.artifact import ArtifactKind
from contextforge.core.entities.model_call import ModelCall, Stage
from contextforge.core.entities.task import Task
from contextforge.core.logging import request_context
from contextforge.core.models.base import ModelRequest
from contextforge.core.models.errors import ModelError
from contextforge.core.planner import LeadDecision, PlannedTask, read_lead_leniently
from contextforge.core.state import RequestState
from contextforge.worker.dependencies import WorkerDependencies
from contextforge.worker.request_state import load_request
from contextforge.worker.stages.errors import one_line
from contextforge.worker.stages.execute import TaskOutcome
from contextforge.worker.stages.guards import check_not_cancelled
from contextforge.worker.stages.plan import save_brief
from contextforge.worker.stages.sources import available_artifacts

logger = logging.getLogger(__name__)

LEAD_INVALID = "lead output invalid"
ALL_REPEATED = "every follow-up repeated earlier research"
# What one more round must leave room for, in model calls: the lead's own call, at least a
# tool call and a result per researcher, and the stages after research (cite batches, the
# report and its summary; about eight for a full request).
_LEAD_CALLS = 1
_CALLS_PER_RESEARCHER = 2
_CALLS_KEPT_FOR_REPORT = 8
# A web researcher needs at least a search and a read.
_WEB_CALLS_PER_RESEARCHER = 2


class LeadReview(TypedDict):
    """What ``lead_review`` returns: a ``LeadDecision`` as plain JSON values, so the workflow
    can checkpoint it. ``tasks`` hold ``PlannedTask`` dumps, deduplicated and capped."""

    action: str
    reason: str
    brief: str | None
    tasks: list[dict[str, Any]]


def lead_review(
    request_state: RequestState,
    outcomes: list[TaskOutcome],
    research_round: int,
    worker_dependencies: WorkerDependencies,
) -> LeadReview:
    """The lead's decision after ``research_round``. It completes, without a model call, when
    a cap leaves no room for another researcher; completes when its answer is invalid twice
    or its call fails; and drops follow-ups that repeat an earlier objective. A revised brief
    is saved on the request record. ``Cancelled`` propagates."""
    request_message_id = request_state["request_message_id"]
    with request_context(request_message_id):
        room, limit = _room_for_researchers(request_state, worker_dependencies)
        if room <= 0:
            return _stop(request_state, f"{limit} reached", worker_dependencies)
        decision = _decide(request_state, outcomes, research_round, room, worker_dependencies)
        if isinstance(decision, str):
            return _stop(request_state, decision, worker_dependencies)
        if decision.brief:
            save_brief(request_state, decision.brief, worker_dependencies)
        if decision.action == "complete_task":
            _emit(worker_dependencies, request_state, f"Lead: enough evidence — {decision.reason}")
            return _review("complete_task", decision.reason, decision.brief, [])
        earlier = worker_dependencies.repositories.tasks.list_by_request(request_message_id)
        tasks = new_follow_ups(decision.tasks, earlier, room)
        if not tasks:
            return _stop(request_state, ALL_REPEATED, worker_dependencies, decision.brief)
        researchers = "researcher" if len(tasks) == 1 else "researchers"
        _emit(
            worker_dependencies,
            request_state,
            f"Round {research_round + 1}: {len(tasks)} more {researchers}",
        )
        return _review("run_subagents", decision.reason, decision.brief, tasks)


def new_follow_ups(
    tasks: Sequence[PlannedTask], earlier: Sequence[Task], room: int
) -> list[PlannedTask]:
    """The first ``room`` of ``tasks`` whose objective (or goal, when it has none) no earlier
    task and no task before it in the list already had, with dependencies on dropped tasks
    removed. Objectives are compared by their words, ignoring case and punctuation."""
    seen = {_topic(task.objective or task.goal) for task in earlier}
    kept: list[PlannedTask] = []
    for task in tasks:
        topic = _topic(task.objective or task.goal)
        if topic in seen:
            logger.info("follow-up %s dropped: it repeats an earlier objective", task.ref)
            continue
        seen.add(topic)
        kept.append(task)
    kept = kept[:room]
    refs = {task.ref for task in kept}
    return [
        task.model_copy(update={"depends_on": [d for d in task.depends_on if d in refs]})
        for task in kept
    ]


def _topic(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def _room_for_researchers(
    request_state: RequestState, worker_dependencies: WorkerDependencies
) -> tuple[int, str]:
    """How many researchers one more round may start, and the limit that sets it."""
    budgets = worker_dependencies.budgets
    request_message_id = request_state["request_message_id"]
    calls = worker_dependencies.repositories.model_calls.list_by_request(request_message_id)
    request = load_request(
        worker_dependencies.repositories.messages, request_state["chat_id"], request_message_id
    )
    calls_left = budgets.call_budget - len(calls) - _LEAD_CALLS - _CALLS_KEPT_FOR_REPORT
    web_left = budgets.web_tool_call_budget - request.web_tool_calls
    limits = {
        "tasks per request": budgets.tasks_per_request - len(request_state["plan"]),
        "tasks per round": budgets.tasks_per_round,
        "model-call budget": calls_left // _CALLS_PER_RESEARCHER,
        "web-tool budget": web_left // _WEB_CALLS_PER_RESEARCHER,
        "spend cap": _researchers_affordable(calls, worker_dependencies),
    }
    limit = min(limits, key=limits.__getitem__)
    return limits[limit], limit


def _researchers_affordable(calls: list[ModelCall], worker_dependencies: WorkerDependencies) -> int:
    """How many more researchers the spend cap allows at the average cost of those so far,
    after keeping as much again as the calls outside tasks (plan, lead) have cost, for the
    lead and report calls still to come. Unlimited without settings or known task costs."""
    settings = worker_dependencies.settings
    if settings is None:
        return sys.maxsize
    by_task: dict[str, float] = defaultdict(float)
    spent = 0.0
    for call in calls:
        cost = (
            call.estimated_cost_usd
            if call.estimated_cost_usd is not None
            else settings.call_reserve_usd
        )
        spent += cost
        if call.task_id is not None:
            by_task[call.task_id] += cost
    researched = sum(by_task.values())
    if not by_task or researched <= 0:
        return sys.maxsize
    outside_tasks = spent - researched
    left = settings.request_max_usd - spent - outside_tasks
    return int(left // (researched / len(by_task)))


def _decide(
    request_state: RequestState,
    outcomes: list[TaskOutcome],
    research_round: int,
    room: int,
    worker_dependencies: WorkerDependencies,
) -> LeadDecision | str:
    """The lead's valid decision, asked again once with the errors of an invalid one; the
    reason to stop when it is invalid twice or the call fails."""
    payload = _review_input(request_state, outcomes, research_round, room, worker_dependencies)
    try:
        return _call_lead(request_state, payload, worker_dependencies)
    except ValidationError as rejected:
        errors = one_line(rejected)
        logger.warning("lead output rejected, retrying once: %s", errors)
        _emit(worker_dependencies, request_state, "Lead output was invalid; retry 1 of 1")
        payload = {**payload, "previousAnswerRejected": errors}
    except ModelError as exc:
        return f"lead call failed: {exc}"
    try:
        return _call_lead(request_state, payload, worker_dependencies)
    except ValidationError as rejected_again:
        logger.warning("lead output invalid twice: %s", one_line(rejected_again))
        return LEAD_INVALID
    except ModelError as exc:
        return f"lead call failed: {exc}"


def _call_lead(
    request_state: RequestState, payload: dict[str, Any], worker_dependencies: WorkerDependencies
) -> LeadDecision:
    """One guarded, recorded lead call, its answer read leniently; ``ValidationError`` when it
    still breaks the schema."""
    check_not_cancelled(
        worker_dependencies, request_state["chat_id"], request_state["request_message_id"]
    )
    request = ModelRequest(
        stage=Stage.LEAD,
        model=worker_dependencies.model_id(Stage.LEAD),
        system=[request_state["memory"]["text"], load_prompt(prompt_version(Stage.LEAD))],
        messages=[HumanMessage(json.dumps(payload, ensure_ascii=False))],
        output_schema=LeadDecision,
    )
    response = worker_dependencies.model_for(
        request_state["request_message_id"], memory_version=request_state["memory"]["version"]
    ).invoke(request)
    datasets = {
        artifact.artifact_id
        for artifact in available_artifacts(
            worker_dependencies.repositories.artifacts, request_state["chat_id"]
        )
        if artifact.kind == ArtifactKind.DATASET
    }
    answer, fixes = read_lead_leniently(response.structured, datasets)
    if fixes:
        logger.warning("lead answer read leniently: %s", "; ".join(fixes))
    return LeadDecision.model_validate(answer)


def _review_input(
    request_state: RequestState,
    outcomes: list[TaskOutcome],
    research_round: int,
    room: int,
    worker_dependencies: WorkerDependencies,
) -> dict[str, Any]:
    """The lead's input: the request, the brief, the round, the room left, each researcher's
    condensed findings (summary and fact texts, never pages or excerpts) and every search
    already run."""
    request = load_request(
        worker_dependencies.repositories.messages,
        request_state["chat_id"],
        request_state["request_message_id"],
    )
    searches = list(dict.fromkeys(q for o in outcomes for q in o.get("searches", [])))
    return {
        "request": request.content,
        "brief": request_state.get("brief") or request_state["goal"],
        "round": research_round,
        "roundsAllowed": worker_dependencies.budgets.research_rounds,
        "maxNewResearchers": room,
        "findings": [_condensed(outcome) for outcome in outcomes],
        "searchesRun": searches,
    }


def _condensed(outcome: TaskOutcome) -> dict[str, Any]:
    gaps = outcome.get("coverage", {}).get("gaps")
    return {
        "goal": outcome["goal"],
        "status": outcome["status"],
        "summary": outcome["summary"],
        "facts": [fact["text"] for fact in outcome["facts"]],
        "uncertainties": outcome["uncertainties"],
        "gaps": gaps if isinstance(gaps, list) else [],
        "failureReason": outcome["failure_reason"],
    }


def _stop(
    request_state: RequestState,
    reason: str,
    worker_dependencies: WorkerDependencies,
    brief: str | None = None,
) -> LeadReview:
    """Complete for a reason other than the lead judging the evidence enough."""
    logger.info("research stops: %s", reason)
    _emit(worker_dependencies, request_state, f"Research stopped: {reason}")
    return _review("complete_task", reason, brief, [])


def _review(action: str, reason: str, brief: str | None, tasks: list[PlannedTask]) -> LeadReview:
    return LeadReview(
        action=action,
        reason=reason,
        brief=brief,
        tasks=[task.model_dump(mode="json") for task in tasks],
    )


def _emit(worker_dependencies: WorkerDependencies, request_state: RequestState, text: str) -> None:
    worker_dependencies.activity.emit(request_state["request_message_id"], "", text)

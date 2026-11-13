"""Lead review stage (F16): after a research round the lead researcher reads the condensed
findings and either starts more researchers or completes.

Adapted from Anthropic, "How we built our multi-agent research system" (2025): the lead keeps
its research plan (the brief), reviews what the subagents return and decides whether more
research is needed. The caps are ours and enforced here in code, before and after the model.
Whether a round fits the budgets is ``round_budget``'s. A plain function the workflow wraps as
a checkpointed task, so a resumed run never asks again.
"""

import json
import logging
import re
from collections.abc import Sequence
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage
from pydantic import ValidationError

from contextforge.core.context.prompts import load_prompt, prompt_version
from contextforge.core.entities.artifact import ArtifactKind
from contextforge.core.entities.model_call import Stage
from contextforge.core.entities.task import Task, TaskKind
from contextforge.core.llm.base import ModelRequest
from contextforge.core.llm.errors import ModelError
from contextforge.core.logging import request_context
from contextforge.core.state import RequestState
from contextforge.worker.dependencies import WorkerDependencies
from contextforge.worker.planner import LeadDecision, PlannedTask, read_lead_leniently
from contextforge.worker.request_state import load_request
from contextforge.worker.stages.errors import one_line
from contextforge.worker.stages.execute import TaskOutcome
from contextforge.worker.stages.guards import check_not_cancelled
from contextforge.worker.stages.plan import save_brief
from contextforge.worker.stages.round_budget import observe_round_budget
from contextforge.worker.stages.sources import available_artifacts

logger = logging.getLogger(__name__)

LEAD_INVALID = "lead output invalid"
ALL_REPEATED = "every follow-up repeated earlier research"
TASK_CAP = "tasks per request reached"


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
    """The lead's decision after ``research_round``. It completes without a model call when
    the task caps leave no room, or when no researcher fits the budgets with cite and report
    still covered (``round_budget``); completes when its answer is invalid twice or its call
    fails; drops follow-ups that repeat an earlier objective and trims the rest to what the
    budgets fit. A revised brief is saved on the request record. ``Cancelled`` propagates."""
    request_message_id = request_state["request_message_id"]
    with request_context(request_message_id):
        budgets = worker_dependencies.budgets
        task_room = min(
            budgets.tasks_per_round, budgets.tasks_per_request - len(request_state["plan"])
        )
        if task_room <= 0:
            return _stop(request_state, TASK_CAP, worker_dependencies)
        earlier = worker_dependencies.repositories.tasks.list_by_request(request_message_id)
        budget = observe_round_budget(
            request_state, outcomes, research_round, earlier, worker_dependencies
        )
        room, limit = budget.fit([_likely_kind(earlier)] * task_room)
        if room == 0:
            return _stop(request_state, _over_budget(limit), worker_dependencies)
        decision = _decide(request_state, outcomes, research_round, room, worker_dependencies)
        if isinstance(decision, str):
            return _stop(request_state, decision, worker_dependencies)
        if decision.brief:
            save_brief(request_state, decision.brief, worker_dependencies)
        if decision.action == "complete_task":
            _emit(worker_dependencies, request_state, f"Lead: enough evidence — {decision.reason}")
            return _review("complete_task", decision.reason, decision.brief, [])
        tasks = new_follow_ups(decision.tasks, earlier, room)
        if not tasks:
            return _stop(request_state, ALL_REPEATED, worker_dependencies, decision.brief)
        # The lead may choose kinds dearer than the pre-check assumed: fit them as chosen.
        fitting, limit = budget.fit([task.kind for task in tasks])
        if fitting == 0:
            return _stop(request_state, _over_budget(limit), worker_dependencies, decision.brief)
        return _review("run_subagents", decision.reason, decision.brief, _first(tasks, fitting))


def new_follow_ups(
    tasks: Sequence[PlannedTask], earlier: Sequence[Task], room: int
) -> list[PlannedTask]:
    """The first ``room`` of ``tasks`` whose objective (or goal, when it has none) no earlier
    task and no task before it in the list already had, with dependencies on dropped tasks
    removed. Objectives are compared by their words, ignoring case, whitespace and
    punctuation."""
    seen = {_topic(task.objective or task.goal) for task in earlier}
    kept: list[PlannedTask] = []
    for task in tasks:
        topic = _topic(task.objective or task.goal)
        if topic in seen:
            logger.info("follow-up %s dropped: it repeats an earlier objective", task.ref)
            continue
        seen.add(topic)
        kept.append(task)
    return _first(kept, room)


def _first(tasks: Sequence[PlannedTask], count: int) -> list[PlannedTask]:
    """The first ``count`` tasks, without dependencies on the tasks cut off."""
    kept = list(tasks[:count])
    refs = {task.ref for task in kept}
    return [
        task.model_copy(update={"depends_on": [d for d in task.depends_on if d in refs]})
        for task in kept
    ]


def _likely_kind(earlier: Sequence[Task]) -> TaskKind:
    """The kind a follow-up is assumed to be before the lead names it: web research when
    the request has any, its dearest kind; else the request's first kind."""
    kinds = [task.kind for task in earlier]
    if TaskKind.WEB_SEARCH in kinds or not kinds:
        return TaskKind.WEB_SEARCH
    return kinds[0]


def _over_budget(limit: str) -> str:
    return f"budget: the {limit} would not cover another round and the report"


def _topic(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


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

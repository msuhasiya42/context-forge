"""Whether one more research round fits the request's budgets (F16, review C1 and I1).

A round is worth starting only if, after it, cite and report still have their calls, money
and time: a round that starves cite withholds every web claim and leaves the answer worse
than one round would. So the cost of a new round is estimated from what research so far
actually used, never from a fixed guess, and the reserve for cite and report grows with the
facts found so far plus those the new round may add. The same reserve (``research_reserve``)
is kept from the researchers while they run: their calls may not use it (``BudgetedModel``).
"""

import math
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from contextforge.core.entities.model_call import ModelCall
from contextforge.core.entities.task import MAX_FACTS_PER_RESULT, Task, TaskKind
from contextforge.core.llm.recording import refused_before_generation
from contextforge.core.state import RequestState
from contextforge.worker.calls import calls_used
from contextforge.worker.dependencies import WorkerDependencies
from contextforge.worker.request_state import load_request
from contextforge.worker.stages.execute import TaskOutcome
from contextforge.worker.stages.review import CITE_BATCH_SIZE

# Calls after the cite batches: the report draft and its review, the summary, and the review
# of the summary and conclusions, with one spare batch for a long report's review.
CALLS_AFTER_CITE = 5
LEAD_CALLS = 1
# A web researcher reads at least a search and a page when nothing has been observed yet.
MIN_WEB_CALLS_PER_RESEARCHER = 2


@dataclass(frozen=True)
class RoundBudget:
    """What is left of the request's budgets and what research so far cost.

    ``usd_left`` and ``seconds_left`` are None when no spend cap or deadline applies (no
    settings). Per-researcher costs are the observed maximum over the tasks so far; a web
    researcher is never estimated below its worst case, every tool call of every attempt.
    """

    calls_left: int
    web_left: int
    usd_left: float | None
    seconds_left: float | None
    facts: int
    calls_per_task: int
    web_researcher_calls: int
    web_calls_per_web_task: int
    usd_per_task: float
    usd_per_call_outside_tasks: float
    seconds_per_round: float
    seconds_per_call: float

    def fit(self, kinds: Sequence[TaskKind]) -> tuple[int, str]:
        """How many of ``kinds``, in order, one more round can run with the lead call and the
        cite and report reserve still covered; and the budget that stops the next one (empty
        when all fit)."""
        for count in range(len(kinds)):
            limit = self._exceeded(kinds[: count + 1])
            if limit:
                return count, limit
        return len(kinds), ""

    def _exceeded(self, kinds: Sequence[TaskKind]) -> str:
        """The first budget a round of researchers of ``kinds`` would overrun, or ""."""
        reserve = self.reserved_calls(len(kinds))
        calls = sum(self._researcher_calls(kind) for kind in kinds)
        if calls + reserve > self.calls_left:
            return "model-call budget"
        web = sum(self.web_calls_per_web_task for k in kinds if k == TaskKind.WEB_SEARCH)
        if web > self.web_left:
            return "web-tool budget"
        if self.usd_left is not None:
            usd = len(kinds) * self.usd_per_task + reserve * self.usd_per_call_outside_tasks
            if usd > self.usd_left:
                return "spend cap"
        if self.seconds_left is not None:
            seconds = self.seconds_per_round + reserve * self.seconds_per_call
            if seconds > self.seconds_left:
                return "request deadline"
        return ""

    def reserved_calls(self, researchers: int) -> int:
        return research_reserve(self.facts, researchers)

    def _researcher_calls(self, kind: TaskKind) -> int:
        floor = self.web_researcher_calls if kind == TaskKind.WEB_SEARCH else 1
        return max(self.calls_per_task, floor)


def research_reserve(facts: int, researchers: int) -> int:
    """The calls kept for after research: the lead call, then cite over the ``facts`` found and
    the most ``researchers`` still to finish may add, then the report's own calls."""
    facts += researchers * MAX_FACTS_PER_RESULT
    return LEAD_CALLS + math.ceil(facts / CITE_BATCH_SIZE) + CALLS_AFTER_CITE


def observe_round_budget(
    request_state: RequestState,
    outcomes: Sequence[TaskOutcome],
    research_round: int,
    tasks: Sequence[Task],
    worker_dependencies: WorkerDependencies,
) -> RoundBudget:
    """The request's ``RoundBudget`` after ``research_round``, read from its ModelCalls, its
    web counter, ``tasks`` and the injected clock."""
    deps = worker_dependencies
    budgets, settings = deps.budgets, deps.settings
    request_message_id = request_state["request_message_id"]
    calls = deps.repositories.model_calls.list_by_request(request_message_id)
    request = load_request(deps.repositories.messages, request_state["chat_id"], request_message_id)
    reserve_usd = settings.call_reserve_usd if settings else 0.0
    # Attempts refused before generating spent none of the call budget.
    per_task_calls = Counter(
        c.task_id for c in calls if c.task_id is not None and not refused_before_generation(c)
    )
    web_tasks = sum(1 for t in tasks if t.kind == TaskKind.WEB_SEARCH)
    web_cap = settings.web_tool_calls_per_task if settings else budgets.tool_calls_per_task
    usd_per_task, usd_outside = _observed_costs(calls, reserve_usd)
    elapsed = 0.0
    seconds_left = None
    if settings is not None:
        now = deps.clock()
        elapsed = max(0.0, now - deps.segment_started.get(request_message_id, now))
        seconds_left = settings.request_deadline_seconds - elapsed
    return RoundBudget(
        calls_left=budgets.call_budget - calls_used(calls),
        web_left=budgets.web_tool_call_budget - request.web_tool_calls,
        usd_left=settings.request_max_usd - _spent(calls, reserve_usd) if settings else None,
        seconds_left=seconds_left,
        facts=sum(len(outcome["facts"]) for outcome in outcomes),
        calls_per_task=max(per_task_calls.values(), default=0),
        # Every tool call and the result call, on every attempt the task may make.
        web_researcher_calls=(web_cap + 1) * (budgets.task_retries + 1),
        web_calls_per_web_task=max(
            MIN_WEB_CALLS_PER_RESEARCHER, math.ceil(request.web_tool_calls / max(1, web_tasks))
        ),
        usd_per_task=usd_per_task,
        usd_per_call_outside_tasks=usd_outside,
        # The segment so far spread over the rounds run in it: an upper bound for one round.
        seconds_per_round=elapsed / research_round,
        seconds_per_call=elapsed / max(1, len(calls)),
    )


def _cost(call: ModelCall, reserve_usd: float) -> float:
    """A call's recorded cost; one without a cost is charged the reserve, as BudgetedModel
    charges it."""
    return call.estimated_cost_usd if call.estimated_cost_usd is not None else reserve_usd


def _spent(calls: Sequence[ModelCall], reserve_usd: float) -> float:
    return sum(_cost(call, reserve_usd) for call in calls)


def _observed_costs(calls: Sequence[ModelCall], reserve_usd: float) -> tuple[float, float]:
    """The most one task has cost so far, and the average cost of a call outside tasks (plan,
    lead), the stand-in for each cite and report call still to come."""
    by_task: dict[str, float] = defaultdict(float)
    outside: list[float] = []
    for call in calls:
        if call.task_id is None:
            outside.append(_cost(call, reserve_usd))
        else:
            by_task[call.task_id] += _cost(call, reserve_usd)
    average_outside = sum(outside) / len(outside) if outside else reserve_usd
    return max(by_task.values(), default=0.0), average_outside

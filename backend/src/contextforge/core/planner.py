"""Planner output schema: the structured result the plan stage asks the model for, and the
lenient reading of the slips capable models made with it (planner bake-off, 7 Nov 2026)."""

import json
import re
from collections.abc import Collection
from typing import Any, Literal, Self

from pydantic import ValidationError, model_validator

from contextforge.core.entities import Entity
from contextforge.core.entities.task import TaskKind

# Effort scales with the question (F16): one researcher for a simple fact, more for a
# comparison or a broad question. Six stays the schema ceiling for a sql plan with a reconcile.
MIN_PLANNED_TASKS = 1
MAX_PLANNED_TASKS = 6
# A task goal that says it works on earlier tasks' results.
_USES_EARLIER = re.compile(
    r"using data from|cross-check|combine|reconcile|compare the two", re.IGNORECASE
)
# "t1-t3", "t1–t3", "t1 to t3": every ref in the range.
_REF_RANGE = re.compile(r"\bt(\d+)\s*(?:-|–|—|to|through)\s*t(\d+)\b", re.IGNORECASE)


class PlannedTask(Entity):
    """One task the planner proposes. ``ref`` is local to the plan; the plan gate maps refs to
    Task ids when it creates the Task records.

    ``goal`` is the short title; the four spec fields brief the researcher (Anthropic's
    subagent description: objective, output format, tools and sources, boundaries). They
    default to empty so an answer or a record from before F16 still reads.
    """

    ref: str
    kind: TaskKind
    goal: str
    input_artifact_ids: list[str]
    depends_on: list[str] = []
    objective: str = ""
    output_format: str = ""
    tools_and_sources: str = ""
    boundaries: str = ""


class PlannerOutput(Entity):
    """Either a plan of 1 to 6 tasks or a single clarification question for the user."""

    kind: Literal["plan", "clarification"]
    goal: str
    # The lead's research plan: how the question decomposes, what a complete answer needs and
    # the effort chosen. Empty when a model leaves it out; a clarification has none.
    brief: str = ""
    # Lists a model may leave out when it has nothing for them (live: constraints).
    constraints: list[str] = []
    open_questions: list[str] = []
    question: str | None = None
    tasks: list[PlannedTask] | None = None
    next_action: str

    @model_validator(mode="after")
    def _check_shape_for_kind(self) -> Self:
        if self.kind == "clarification":
            if not self.question:
                raise ValueError("a clarification needs a question")
            if self.tasks:
                raise ValueError("a clarification carries no tasks")
            return self

        tasks = self.tasks or []
        if not MIN_PLANNED_TASKS <= len(tasks) <= MAX_PLANNED_TASKS:
            raise ValueError(
                f"a plan needs {MIN_PLANNED_TASKS} to {MAX_PLANNED_TASKS} tasks, got {len(tasks)}"
            )
        check_task_graph(tasks)
        return self


def check_task_graph(tasks: list[PlannedTask]) -> None:
    """Raise ``ValueError`` unless refs are unique and ``depends_on`` names other tasks of the
    same list without a cycle."""
    refs = [task.ref for task in tasks]
    if len(set(refs)) != len(refs):
        raise ValueError(f"task refs must be unique, got {refs}")
    for task in tasks:
        for dependency in task.depends_on:
            if dependency == task.ref:
                raise ValueError(f"task {task.ref} depends on itself")
            if dependency not in refs:
                raise ValueError(f"task {task.ref} depends on unknown ref {dependency}")
    cycle = _find_dependency_cycle(tasks)
    if cycle:
        raise ValueError(
            f"tasks depend on each other in a cycle: {' -> '.join(cycle)} "
            "(each task depends on the next)"
        )


def _find_dependency_cycle(tasks: list[PlannedTask]) -> list[str] | None:
    """Return one cycle in the ``depends_on`` graph as a ref path that starts and ends on the
    same ref, or None when the graph is acyclic. Expects every dependency to be a known ref."""
    depends_on = {task.ref: task.depends_on for task in tasks}
    acyclic: set[str] = set()
    path: list[str] = []

    def visit(ref: str) -> list[str] | None:
        if ref in path:
            return path[path.index(ref) :] + [ref]
        if ref in acyclic:
            return None
        path.append(ref)
        for dependency in depends_on[ref]:
            cycle = visit(dependency)
            if cycle:
                return cycle
        path.pop()
        acyclic.add(ref)
        return None

    for ref in depends_on:
        cycle = visit(ref)
        if cycle:
            return cycle
    return None


def read_plan_leniently(
    answer: dict[str, Any] | None, dataset_ids: Collection[str]
) -> tuple[dict[str, Any] | None, list[str]]:
    """The planner's answer with the slips seen live fixed before validation, and a note per
    fix: ``tasks`` sent as a JSON string is decoded (DeepSeek v3.2); a missing ``kind`` is
    ``plan`` when tasks and no question are given (Mistral Large 3, Nemotron); a task without a
    ``kind`` is a web_search when it reads no artifact and a sql_analysis when it reads datasets
    only; a plan's empty ``question`` array is omitted (GPT-OSS). Anything else is left for
    validation to reject."""
    if not isinstance(answer, dict):
        return answer, []
    fixed, fixes = dict(answer), []
    tasks = fixed.get("tasks")
    if isinstance(tasks, str) and isinstance(decoded := _json_value(tasks), list):
        fixed["tasks"] = tasks = decoded
        fixes.append("tasks sent as a JSON string")
    if not isinstance(tasks, list):
        return fixed, fixes
    if "kind" not in fixed and tasks and not fixed.get("question"):
        fixed["kind"] = "plan"
        fixes.append("kind missing: plan")
    if fixed.get("kind") == "plan" and fixed.get("question") == []:
        del fixed["question"]
        fixes.append("plan question sent as an empty array")
    fixed["tasks"] = []
    for task in tasks:
        kind = _missing_kind(task, dataset_ids)
        if kind is not None:
            task = {**task, "kind": kind.value}
            fixes.append(f"{task.get('ref')} kind missing: {kind.value}")
        fixed["tasks"].append(task)
    return fixed, fixes


def with_inferred_dependencies(output: PlannerOutput) -> tuple[PlannerOutput, list[str]]:
    """``output`` with each task that depends on nothing, yet names earlier tasks or says it
    works on their results, made to depend on them: the ones it names, else every earlier task
    (Kimi K3.1 sent no dependsOn; its last task said "using t1–t3"). A note per task changed;
    ``output`` as it was when nothing changed or the change would not validate."""
    tasks = output.tasks or []
    updated, fixes = [], []
    for index, task in enumerate(tasks):
        needed = [] if task.depends_on else _earlier_needed(task.goal, tasks[:index])
        if needed:
            task = task.model_copy(update={"depends_on": needed})
            fixes.append(f"{task.ref} depends on {', '.join(needed)}")
        updated.append(task)
    if not fixes:
        return output, []
    try:
        return PlannerOutput.model_validate(
            output.model_copy(update={"tasks": updated}).model_dump()
        ), fixes
    except ValidationError:  # a forward dependency would close a cycle
        return output, []


def _json_value(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _missing_kind(task: Any, dataset_ids: Collection[str]) -> TaskKind | None:
    """The kind of a planned task that names none; None when it names one, or reads an artifact
    that is not a dataset, so validation rejects it."""
    if not isinstance(task, dict) or "kind" in task:
        return None
    reads = task.get("inputArtifactIds") or []
    if not reads:
        return TaskKind.WEB_SEARCH
    return TaskKind.SQL_ANALYSIS if all(r in dataset_ids for r in reads) else None


def _earlier_needed(goal: str, earlier: list[PlannedTask]) -> list[str]:
    """The earlier refs ``goal`` names, in plan order; every earlier ref when it names none but
    says it works on their results."""
    named = {
        t.ref.lower() for t in earlier if re.search(rf"\b{re.escape(t.ref)}\b", goal, re.IGNORECASE)
    }
    for first, last in _REF_RANGE.findall(goal):
        named |= {f"t{n}" for n in range(int(first), int(last) + 1)}
    refs = [t.ref for t in earlier if t.ref.lower() in named]
    if refs or not _USES_EARLIER.search(goal):
        return refs
    return [t.ref for t in earlier]

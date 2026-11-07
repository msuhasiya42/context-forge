"""Planner output schema: the structured result the plan stage asks the model for."""

from typing import Literal, Self

from pydantic import model_validator

from contextforge.core.entities import Entity
from contextforge.core.entities.task import TaskKind

MIN_PLANNED_TASKS = 2
MAX_PLANNED_TASKS = 6


class PlannedTask(Entity):
    """One task the planner proposes. ``ref`` is local to the plan; the plan gate maps refs to
    Task ids when it creates the Task records."""

    ref: str
    kind: TaskKind
    goal: str
    input_artifact_ids: list[str]
    depends_on: list[str] = []


class PlannerOutput(Entity):
    """Either a plan of 2 to 6 tasks or a single clarification question for the user."""

    kind: Literal["plan", "clarification"]
    goal: str
    constraints: list[str]
    open_questions: list[str]
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
        refs = [task.ref for task in tasks]
        if len(set(refs)) != len(refs):
            raise ValueError(f"task refs must be unique, got {refs}")
        for task in tasks:
            for dependency in task.depends_on:
                if dependency == task.ref:
                    raise ValueError(f"task {task.ref} depends on itself")
                if dependency not in refs:
                    raise ValueError(f"task {task.ref} depends on unknown ref {dependency}")
        return self

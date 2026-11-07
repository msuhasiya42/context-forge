# tests/test_planner_state.py
import pytest
from pydantic import ValidationError

from contextforge.core.planner import PlannedTask, PlannerOutput


def _task(ref: str, deps: list[str] | None = None) -> PlannedTask:
    return PlannedTask(
        ref=ref, kind="sql_analysis", goal="g", input_artifact_ids=["a2"], depends_on=deps or []
    )


def test_plan_requires_two_to_six_tasks() -> None:
    with pytest.raises(ValidationError):
        PlannerOutput(
            kind="plan",
            goal="g",
            constraints=[],
            open_questions=[],
            tasks=[_task("t1")],
            next_action="n",
        )
    with pytest.raises(ValidationError):
        PlannerOutput(
            kind="plan",
            goal="g",
            constraints=[],
            open_questions=[],
            tasks=[_task(f"t{i}") for i in range(7)],
            next_action="n",
        )


def test_plan_dependency_must_reference_a_task() -> None:
    with pytest.raises(ValidationError):
        PlannerOutput(
            kind="plan",
            goal="g",
            constraints=[],
            open_questions=[],
            tasks=[_task("t1"), _task("t2", ["t9"])],
            next_action="n",
        )


def test_clarification_requires_question() -> None:
    with pytest.raises(ValidationError):
        PlannerOutput(
            kind="clarification", goal="g", constraints=[], open_questions=[], next_action="n"
        )
    PlannerOutput(
        kind="clarification",
        goal="g",
        constraints=[],
        open_questions=[],
        question="Which?",
        next_action="n",
    )


def test_spec_example_plan_validates() -> None:
    out = PlannerOutput.model_validate(
        {
            "kind": "plan",
            "goal": "g",
            "constraints": ["c"],
            "openQuestions": [],
            "tasks": [
                {
                    "ref": "t1",
                    "kind": "sql_analysis",
                    "goal": "g",
                    "inputArtifactIds": ["a2"],
                    "dependsOn": [],
                },
                {
                    "ref": "t2",
                    "kind": "reconcile",
                    "goal": "g",
                    "inputArtifactIds": [],
                    "dependsOn": ["t1"],
                },
            ],
            "nextAction": "run",
        }
    )
    assert out.tasks and out.tasks[1].depends_on == ["t1"]


def test_plan_rejects_dependency_cycle() -> None:
    with pytest.raises(ValidationError, match=r"t1.*t2"):
        PlannerOutput(
            kind="plan",
            goal="g",
            constraints=[],
            open_questions=[],
            tasks=[_task("t1", ["t2"]), _task("t2", ["t1"])],
            next_action="n",
        )
    PlannerOutput(
        kind="plan",
        goal="g",
        constraints=[],
        open_questions=[],
        tasks=[_task("t1"), _task("t2", ["t1"]), _task("t3", ["t2"])],
        next_action="n",
    )

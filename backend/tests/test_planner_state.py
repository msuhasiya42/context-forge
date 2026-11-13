# tests/test_planner_state.py
import pytest
from pydantic import ValidationError

from contextforge.worker.planner import LeadDecision, PlannedTask, PlannerOutput


def _task(ref: str, worker_dependencies: list[str] | None = None) -> PlannedTask:
    return PlannedTask(
        ref=ref,
        kind="sql_analysis",
        goal="g",
        input_artifact_ids=["a2"],
        depends_on=worker_dependencies or [],
    )


def test_plan_requires_one_to_six_tasks() -> None:
    # F16 effort scaling: a simple fact is one researcher.
    one = PlannerOutput(kind="plan", goal="g", tasks=[_task("t1")], next_action="n")
    assert one.brief == "" and one.tasks and one.tasks[0].objective == ""
    with pytest.raises(ValidationError):
        PlannerOutput(
            kind="plan",
            goal="g",
            constraints=[],
            open_questions=[],
            tasks=[],
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
    # Live: Nova left out an empty constraints list and the planner call was retried for it.
    asked = PlannerOutput.model_validate(
        {"kind": "clarification", "goal": "g", "question": "Which?", "nextAction": "n"}
    )
    assert (asked.constraints, asked.open_questions) == ([], [])


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


def test_the_lead_may_not_start_a_reconcile_follow_up() -> None:
    # Review I2: a follow-up depends only on its own round, so a reconcile would reconcile
    # nothing; cite and report reconcile every round's findings.
    reconcile = PlannedTask(ref="f1", kind="reconcile", goal="g", input_artifact_ids=[])
    with pytest.raises(ValidationError, match="may not be a reconcile"):
        LeadDecision(action="run_subagents", reason="sources disagree", tasks=[reconcile])
    assert LeadDecision(action="run_subagents", reason="gap", tasks=[_task("f1")]).tasks

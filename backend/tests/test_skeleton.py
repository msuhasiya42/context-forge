import dataclasses
from pathlib import Path

import pytest
import yaml

from contextforge.core.context.budgets import load_budgets
from contextforge.core.models.recording import price_row


def test_package_imports() -> None:
    import contextforge  # noqa: F401


def test_prompt_files_exist_and_are_nonempty() -> None:
    for name in ["agent-memory.md", "plan.1.md", "execute.1.md", "compress.1.md"]:
        assert Path("prompts", name).read_text().strip()


def test_budgets_yaml_has_spec_values() -> None:
    b = yaml.safe_load(Path("config/budgets.yaml").read_text())
    assert b == {
        "capInputTokens": 40000,
        "responseAllowanceTokens": 4000,
        "marginFraction": 0.10,
        "keepLastMessages": 6,
        "toolResultRowCap": 50,
        "factsCap": 30,
        "callBudget": 50,
        "webToolCallBudget": 50,
        "tasksInFlight": 5,
        "toolCallsPerTask": 6,
        "taskRetries": 1,
        "noProgressTurns": 3,
        "columnCap": 20,
        "cellCharCap": 200,
        "researchRounds": 3,
        "tasksPerRound": 5,
        "tasksPerRequest": 12,
    }


def test_models_yaml_covers_every_stage() -> None:
    m = yaml.safe_load(Path("config/models.yaml").read_text())
    assert set(m["stages"]) >= {"plan", "execute", "cite", "compress"}
    # every stage model is priced, so each call's cost is computed from its usage
    assert all(price_row(m["prices"], v["model"]) for v in m["stages"].values())


def test_models_yaml_has_price_version() -> None:
    m = yaml.safe_load(Path("config/models.yaml").read_text())
    assert m["priceVersion"] == "2026-11-07"


@pytest.mark.parametrize("key", ["research_rounds", "tasks_per_round", "tasks_per_request"])
def test_round_limits_must_be_at_least_one(key) -> None:
    with pytest.raises(ValueError, match="must be at least 1"):
        dataclasses.replace(load_budgets(), **{key: 0})

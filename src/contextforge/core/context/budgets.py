"""Context window and run limits, loaded from ``config/budgets.yaml``."""

import math
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

import yaml

# Resolved from this file (src/contextforge/core/context/), not the CWD, so the budgets load
# wherever the process is started.
BUDGETS_PATH = Path(__file__).resolve().parents[4] / "config" / "budgets.yaml"


@dataclass(frozen=True)
class Budgets:
    """Every limit the engine enforces; each field is the snake_case form of a YAML key."""

    cap_input_tokens: int
    response_allowance_tokens: int
    margin_fraction: float
    keep_last_messages: int
    tool_result_row_cap: int
    facts_cap: int
    call_budget: int
    tasks_in_flight: int
    tool_calls_per_task: int
    task_retries: int
    no_progress_turns: int
    column_cap: int
    cell_char_cap: int
    web_tool_call_budget: int

    def __post_init__(self) -> None:
        if not 1 <= self.web_tool_call_budget <= 50:
            raise ValueError("webToolCallBudget must be between 1 and 50")

    def messages_budget(self, fixed_tokens: int) -> int:
        """Tokens left for section 8 (the loop messages) after sections 1 to 7.

        Negative when the fixed sections alone overrun the window.
        """
        # Fraction(str(...)) takes the margin as written in YAML: in floating point
        # 40000 * 0.07 is 2800.0000000000005, which ceil would round up a whole token.
        margin = math.ceil(self.cap_input_tokens * Fraction(str(self.margin_fraction)))
        return self.cap_input_tokens - self.response_allowance_tokens - margin - fixed_tokens


def load_budgets(path: Path = BUDGETS_PATH) -> Budgets:
    """Read the camelCase YAML keys into ``Budgets``.

    A missing key raises ``KeyError`` and a value of the wrong type ``ValueError``: a limit never
    falls back to a default written in code.
    """
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return Budgets(
        cap_input_tokens=_int(raw, "capInputTokens"),
        response_allowance_tokens=_int(raw, "responseAllowanceTokens"),
        margin_fraction=_float(raw, "marginFraction"),
        keep_last_messages=_int(raw, "keepLastMessages"),
        tool_result_row_cap=_int(raw, "toolResultRowCap"),
        facts_cap=_int(raw, "factsCap"),
        call_budget=_int(raw, "callBudget"),
        tasks_in_flight=_int(raw, "tasksInFlight"),
        tool_calls_per_task=_int(raw, "toolCallsPerTask"),
        task_retries=_int(raw, "taskRetries"),
        no_progress_turns=_int(raw, "noProgressTurns"),
        column_cap=_int(raw, "columnCap"),
        cell_char_cap=_int(raw, "cellCharCap"),
        web_tool_call_budget=_int(raw, "webToolCallBudget"),
    )


def _int(raw: dict[str, Any], key: str) -> int:
    value = raw[key]
    # An exact type check: bool is an int subclass, and ``capInputTokens: true`` is a typo.
    if type(value) is not int:
        raise ValueError(f"budgets key {key} must be an integer, got {value!r}")
    return value


def _float(raw: dict[str, Any], key: str) -> float:
    value = raw[key]
    if type(value) not in (int, float):
        raise ValueError(f"budgets key {key} must be a number, got {value!r}")
    return float(value)

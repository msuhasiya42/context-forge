"""ModelCall entity (implementation plan section 3.5): one record per provider call attempt."""

from enum import StrEnum
from typing import Literal

from contextforge.core.entities import Entity
from contextforge.core.values import Usage


class Stage(StrEnum):
    UNDERSTAND = "understand"
    CLARIFY = "clarify"
    PLAN = "plan"
    EXECUTE = "execute"
    SUMMARIZE = "summarize"
    EVALUATE = "evaluate"
    CITE = "cite"
    COMPRESS = "compress"
    TOOL_SELECT = "tool_select"


class Outcome(StrEnum):
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"


class ModelCall(Entity):
    model_call_id: str
    request_message_id: str
    task_id: str | None = None
    stage: Stage
    # Actual provider and model identity, e.g. "bedrock/anthropic.claude-opus-5-5".
    model: str
    # Agent memory version plus stage prompt version, e.g. "mem:3|prompt:plan.1".
    context_version: str
    # Required but nullable: None when the provider did not report usage.
    usage: Usage | None
    estimated_cost_usd: float | None = None
    # The config/models.yaml version the cost was computed with, so a price change never
    # rewrites history.
    price_version: str
    stop_reason: Literal["end_turn", "tool_use", "max_tokens"] | None = None
    # Hash of the sorted tool definitions sent, to relate cache hit rate to tool-set variation.
    tool_set_hash: str | None = None
    outcome: Outcome
    failure_reason: str | None = None
    langsmith_run_id: str | None = None
    started_at: str
    ended_at: str | None = None

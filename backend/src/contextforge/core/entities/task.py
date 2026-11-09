"""Task entity (implementation plan section 3.4) and the structured-data guard."""

from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, NonNegativeInt

from contextforge.core.entities import Entity
from contextforge.core.values import ArtifactRef, EvidenceRef

MAX_FACTS_PER_RESULT = 6
MAX_CONCLUSIONS_PER_RESULT = 3
MAX_STRUCTURED_LIST_ENTRIES = 50


class TaskKind(StrEnum):
    SQL_ANALYSIS = "sql_analysis"
    WEB_SEARCH = "web_search"
    PDF_LOOKUP = "pdf_lookup"
    SOURCE_SEARCH = "source_search"
    RECONCILE = "reconcile"


class TaskStatus(StrEnum):
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


Confidence = Literal["high", "medium", "low"]


class Fact(Entity):
    text: str
    evidence_refs: list[EvidenceRef]


class Conclusion(Entity):
    """A judgement, comparison or recommendation drawn from the result's facts (F10 ruling 1).
    No source states it, so it is never a fact and cites no evidence of its own:
    ``supporting_facts`` are the positions, from 0, of the facts of this result it rests on."""

    text: str
    supporting_facts: list[NonNegativeInt] = []
    # Low when the model leaves it out: an unstated confidence is never read as a high one.
    confidence: Confidence = "low"


class TaskResult(Entity):
    """Only ``summary`` is required. A model often leaves out a list or map it has nothing
    for (live run 1: ``artifactRefs``), so those default to empty rather than fail the task."""

    summary: str
    # Findings accumulated in request state; the caller checks refs against inputArtifactIds.
    facts: list[Fact] = Field(default=[], max_length=MAX_FACTS_PER_RESULT)
    conclusions: list[Conclusion] = Field(default=[], max_length=MAX_CONCLUSIONS_PER_RESULT)
    structured_data: dict[str, Any] = {}
    evidence_refs: list[EvidenceRef] = []
    artifact_refs: list[ArtifactRef] = []
    uncertainties: list[str] = []
    coverage: dict[str, Any] = {}


class Task(Entity):
    task_id: str
    request_message_id: str
    kind: TaskKind
    goal: str
    input_artifact_ids: list[str]
    depends_on_task_ids: list[str] = []
    status: TaskStatus
    attempt: int = Field(ge=1)
    result: TaskResult | None = None
    failure_reason: str | None = None
    created_at: str
    updated_at: str


def validate_structured_data(kind: TaskKind, data: dict[str, Any]) -> None:
    """Reject row sets in ``structured_data``; raise ``ValueError`` on the first violation.

    structuredData holds totals, matches and classifications, never row sets: rows belong in a
    raw_result Artifact. Phase 1 applies one rule to every kind, so ``kind`` is accepted for the
    per-kind validators later phases add. Nested lists are checked too, so a row set cannot hide
    one level down.
    """
    for key, value in data.items():
        _reject_long_lists(value, key)


def _reject_long_lists(value: object, path: str) -> None:
    if isinstance(value, list):
        if len(value) > MAX_STRUCTURED_LIST_ENTRIES:
            raise ValueError(
                f"structuredData.{path} has {len(value)} entries; the limit is "
                f"{MAX_STRUCTURED_LIST_ENTRIES} because structuredData never holds row sets"
            )
        for index, item in enumerate(value):
            _reject_long_lists(item, f"{path}[{index}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            _reject_long_lists(item, f"{path}.{key}")

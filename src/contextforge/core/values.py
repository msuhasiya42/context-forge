"""Shared value types (implementation plan section 2): embedded in entities, never stored alone."""

from enum import StrEnum
from typing import Literal

from contextforge.core.entities import Entity


class LocatorKind(StrEnum):
    PAGE = "page"
    ROW = "row"
    CELL = "cell"
    RANGE = "range"
    MESSAGE = "message"
    SECTION = "section"
    URL = "url"


class Locator(Entity):
    """Position of the evidence inside an Artifact."""

    kind: LocatorKind
    value: str


class EvidenceRef(Entity):
    """A pointer to evidence; Artifacts are immutable, so `artifact_id` names the exact version."""

    artifact_id: str
    locator: Locator
    excerpt: str | None = None


class ArtifactRef(Entity):
    artifact_id: str
    role: Literal["input", "output"]


class Usage(Entity):
    """Token usage reported by the provider.

    Holders store ``None`` instead of a Usage when the provider did not report it: unknown is
    never zero.
    """

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    thinking_tokens: int | None = None

"""Shared value types (implementation plan section 2): embedded in entities, never stored alone."""

from enum import StrEnum
from typing import Literal

from pydantic import ConfigDict
from pydantic.config import JsonDict

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


def _ask_for_an_excerpt(schema: JsonDict) -> None:
    """The schema a model is given requires a string excerpt. Gemini fills only the required
    fields of a result tool: with ``excerpt`` optional and nullable it quoted none on all 31
    refs of a live run (F18), and the cite check dropped every web fact."""
    properties, required = schema["properties"], schema["required"]
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise TypeError("EvidenceRef's schema has no properties or required list")
    properties["excerpt"] = {
        "type": "string",
        "description": "Copied word for word from the cited source; at least 20 characters.",
    }
    schema["required"] = [*required, "excerpt"]


class EvidenceRef(Entity):
    """A pointer to evidence; Artifacts are immutable, so `artifact_id` names the exact version.

    Validation still accepts a ref without an excerpt, so stored records load; the cite check
    drops a web ref that quotes nothing its source holds."""

    model_config = ConfigDict(json_schema_extra=_ask_for_an_excerpt)

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
    # Reasoning tokens not included in ``output_tokens`` but billed at the output price (Gemini
    # reports them only inside its total).
    thinking_tokens: int | None = None

"""Domain entities (implementation plan section 3) and their shared Pydantic base."""

from typing import Any

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class Entity(BaseModel):
    """Base for every entity and value type: snake_case in code, camelCase on the wire.

    Unknown fields are rejected so a stray wire attribute (for example ``from``) fails loudly
    instead of being dropped.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


def wire(value: BaseModel) -> dict[str, Any]:
    """An entity or value as the API sends it: JSON types, camelCase."""
    return dict(value.model_dump(mode="json", by_alias=True))

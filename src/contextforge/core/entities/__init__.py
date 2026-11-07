"""Domain entities (implementation plan section 3) and their shared Pydantic base."""

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class Entity(BaseModel):
    """Base for every entity and value type: snake_case in code, camelCase on the wire.

    Unknown fields are rejected so a stray wire attribute (for example ``from``) fails loudly
    instead of being dropped.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

"""Model configuration (config/models.yaml): the models, effort and token cap per stage."""

from dataclasses import dataclass
from pathlib import Path

import yaml

from contextforge.core.entities import Entity


@dataclass(frozen=True)
class ModelRoute:
    """The model a stage calls, then the models tried in order, for one call, while the ones
    before them are down (``worker.routing``)."""

    primary: str
    fallbacks: tuple[str, ...] = ()


class StageConfig(Entity):
    model: str
    # Any Bedrock model id with a price row; tried in order when the model before is down.
    fallbacks: list[str] = []
    effort: str
    max_tokens: int

    @property
    def route(self) -> ModelRoute:
        return ModelRoute(self.model, tuple(self.fallbacks))


class ModelsConfig(Entity):
    # Recorded on every ModelCall so a later price change never rewrites past costs.
    price_version: str
    # USD per million tokens, keyed by model then price kind. Every ModelCall is priced from its
    # usage with these (``core.models.recording.cost_usd``).
    prices: dict[str, dict[str, float]]
    # Keyed by stage value: plan, execute, cite, compress.
    stages: dict[str, StageConfig]


def load_models_config(path: Path = Path("config/models.yaml")) -> ModelsConfig:
    return ModelsConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))

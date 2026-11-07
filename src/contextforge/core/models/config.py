"""Model configuration (config/models.yaml): the model, effort and token cap per stage."""

from pathlib import Path

import yaml

from contextforge.core.entities import Entity


class StageConfig(Entity):
    model: str
    effort: str
    max_tokens: int


class ModelsConfig(Entity):
    # Recorded on every ModelCall so a later price change never rewrites past costs.
    price_version: str
    # USD per million tokens, keyed by model then price kind. Parsed so the file validates
    # whole; nothing reads it in phase 1.
    prices: dict[str, dict[str, float]]
    # Keyed by stage value: plan, execute, cite, compress.
    stages: dict[str, StageConfig]


def load_models_config(path: Path = Path("config/models.yaml")) -> ModelsConfig:
    return ModelsConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))

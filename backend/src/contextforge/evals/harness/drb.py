"""DeepResearch Bench (Du et al. 2025, github.com/Ayanami0730/deep_research_bench, Apache-2.0):
100 PhD-level research tasks, 50 in English, each with a reference report and RACE criteria.

Three files are read from a download pinned to one commit: ``query.jsonl`` (id, topic,
language, prompt), ``reference.jsonl`` (id, article) and ``criteria.jsonl`` (id,
dimension_weight, criterions). The last two columns are stringified Python dicts, read with
``ast.literal_eval``, which accepts literals only.
"""

import ast
import json
import random
from pathlib import Path
from typing import Any

from contextforge.core.entities import Entity

REPO = "Ayanami0730/deep_research_bench"
FILES = {
    "query": "data/prompt_data/query.jsonl",
    "reference": "data/test_data/cleaned_data/reference.jsonl",
    "criteria": "data/criteria_data/criteria.jsonl",
}


class DrbTask(Entity):
    id: str
    topic: str
    language: str
    prompt: str
    reference_report: str
    dimension_weights: dict[str, float]
    criteria: dict[str, list[dict[str, Any]]]


def load_drb(raw_dir: Path) -> list[DrbTask]:
    """Every task with its reference report and criteria, joined by id."""
    references = {str(row["id"]): row["article"] for row in _rows(raw_dir / "reference.jsonl")}
    criteria = {str(row["id"]): row for row in _rows(raw_dir / "criteria.jsonl")}
    tasks = []
    for row in _rows(raw_dir / "query.jsonl"):
        task_id = str(row["id"])
        graded = criteria[task_id]
        tasks.append(
            DrbTask(
                id=task_id,
                topic=str(row["topic"]),
                language=str(row["language"]),
                prompt=str(row["prompt"]).strip(),
                reference_report=references[task_id],
                dimension_weights=_literal(graded["dimension_weight"]),
                criteria=_literal(graded["criterions"]),
            )
        )
    return tasks


def sample_drb(tasks: list[DrbTask], n: int, seed: int, language: str = "en") -> list[DrbTask]:
    """``n`` tasks in ``language``, each from a different topic, the same for the same tasks
    and seed: ``n`` topics are drawn, then one task within each. Returned in id order."""
    by_topic: dict[str, list[DrbTask]] = {}
    for task in sorted(tasks, key=lambda t: int(t.id)):
        if task.language == language:
            by_topic.setdefault(task.topic, []).append(task)
    if n > len(by_topic):
        raise ValueError(f"only {len(by_topic)} topics in {language}, asked for {n}")
    rng = random.Random(seed)
    topics = rng.sample(sorted(by_topic), n)
    return sorted((rng.choice(by_topic[topic]) for topic in topics), key=lambda t: int(t.id))


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _literal(text: Any) -> Any:
    value = ast.literal_eval(text) if isinstance(text, str) else text
    if not isinstance(value, dict):
        raise ValueError("expected a dict literal")
    return value

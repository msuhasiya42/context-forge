"""The harness's fixed datasets: built once into JSONL, uploaded once to LangSmith, never
changed silently.

- ``cf-frames-dev-20``: 20 FRAMES questions with gold answers (seed 42, stratified by type).
- ``cf-drb-en-5``: 5 English DeepResearch Bench tasks from 5 topics (seed 42), with reference
  reports and the benchmark's own RACE criteria.
- ``cf-handmade``: questions written by hand from live runs, with gold answers and the parts a
  complete answer must cover. Edited only in its JSONL file, by a person.

Each dataset's JSONL hash is stored in its LangSmith metadata. Pushing a dataset that exists
with a different hash is refused: a changed dataset gets a new name, so old scores stay
comparable with the data they were measured on.

    uv run python -m contextforge.evals.harness.datasets build   # offline: JSONL from caches
    uv run python -m contextforge.evals.harness.datasets push    # upload missing datasets
    uv run python -m contextforge.evals.harness.datasets show    # 2 examples each, from LangSmith
"""

import argparse
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from contextforge.evals.benchmarks.frames import (
    DATASET as FRAMES_SOURCE,
)
from contextforge.evals.benchmarks.frames import (
    DEFAULT_CACHE as FRAMES_CACHE,
)
from contextforge.evals.benchmarks.frames import (
    cached_revision,
    load_frames,
    sample_frames,
)
from contextforge.evals.harness.drb import FILES as DRB_FILES
from contextforge.evals.harness.drb import REPO as DRB_REPO
from contextforge.evals.harness.drb import load_drb, sample_drb

SEED = 42
DATASETS_DIR = Path("evals/datasets")
DRB_RAW = Path(".data/benchmarks/drb/raw")
# The DRB commit the raw files were downloaded from (13 Nov 2026).
DRB_COMMIT = "f2735b5c3636759c22f9e936c0232de8bf545b67"
VERSION_TAG = "v1"

Example = dict[str, Any]


class DatasetChanged(Exception):
    """A dataset exists in LangSmith with other contents than the local JSONL."""


def frames_examples() -> tuple[list[Example], dict[str, Any]]:
    items = sample_frames(load_frames(FRAMES_CACHE), 20, seed=SEED)
    examples = [
        {
            "inputs": {"question": item.prompt},
            "outputs": {"answer": item.answer, "wiki_links": item.wiki_links},
            "metadata": {
                "case_id": item.case_id,
                "source_row": item.row,
                "reasoning_types": item.reasoning_types,
            },
        }
        for item in items
    ]
    source = {
        "source": f"huggingface:{FRAMES_SOURCE}",
        "revision": cached_revision(FRAMES_CACHE),
        "license": "Apache-2.0",
        "sampling": f"20 of 824, seed {SEED}, stratified by first reasoning type",
    }
    return examples, source


def drb_examples() -> tuple[list[Example], dict[str, Any]]:
    tasks = sample_drb(load_drb(DRB_RAW), 5, seed=SEED)
    examples = [
        {
            "inputs": {"question": task.prompt},
            "outputs": {
                "reference_report": task.reference_report,
                "dimension_weights": task.dimension_weights,
                "criteria": task.criteria,
            },
            "metadata": {"case_id": f"drb-{task.id}", "task_id": task.id, "domain": task.topic},
        }
        for task in tasks
    ]
    source = {
        "source": f"github:{DRB_REPO}@{DRB_COMMIT}",
        "files": sorted(DRB_FILES.values()),
        "license": "Apache-2.0",
        "sampling": f"5 of 50 English tasks, seed {SEED}, one per randomly drawn topic",
    }
    return examples, source


BUILDERS: dict[str, Callable[[], tuple[list[Example], dict[str, Any]]]] = {
    "cf-frames-dev-20": frames_examples,
    "cf-drb-en-5": drb_examples,
}
HANDMADE = "cf-handmade"


def build(out_dir: Path = DATASETS_DIR) -> list[Path]:
    """Write each built dataset's JSONL and its ``.meta.json``. A file already there with
    other contents is refused: a fixed dataset is never rebuilt into a different one."""
    written = []
    for name, builder in BUILDERS.items():
        examples, source = builder()
        text = to_jsonl(examples)
        path = out_dir / f"{name}.jsonl"
        if path.exists() and path.read_text(encoding="utf-8") != text:
            raise DatasetChanged(f"{path} exists with other contents; use a new dataset name")
        out_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        meta = {**source, "examples": len(examples), "sha256": sha256(text)}
        _meta_path(path).write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        written.append(path)
    return written


def load_local(name: str, out_dir: Path = DATASETS_DIR) -> tuple[list[Example], dict[str, Any]]:
    """A dataset's examples and metadata as cached locally; the hash is recomputed, never
    trusted from the meta file."""
    path = out_dir / f"{name}.jsonl"
    text = path.read_text(encoding="utf-8")
    meta_path = _meta_path(path)
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    meta = {**meta, "examples": len(text.splitlines()), "sha256": sha256(text)}
    return [json.loads(line) for line in text.splitlines() if line.strip()], meta


def push(client: Any, name: str, examples: list[Example], meta: dict[str, Any]) -> bool:
    """Create ``name`` in LangSmith with ``examples`` and tag the version ``v1``. True when
    created; False when it exists with the same hash; ``DatasetChanged`` when it differs.
    ``client`` is a ``langsmith.Client``."""
    if client.has_dataset(dataset_name=name):
        stored = client.read_dataset(dataset_name=name).metadata or {}
        if stored.get("sha256") != meta["sha256"]:
            raise DatasetChanged(
                f"{name} in LangSmith has hash {stored.get('sha256')}, local is "
                f"{meta['sha256']}; use a new dataset name"
            )
        return False
    dataset = client.create_dataset(name, description=meta.get("sampling", name), metadata=meta)
    client.create_examples(dataset_id=dataset.id, examples=examples)
    tag_latest(client, dataset.id)
    return True


def tag_latest(client: Any, dataset_id: Any) -> None:
    """Tag the dataset's newest version ``v1``. A tag must name a version that exists (its
    ``as_of``); a tag at any other time is accepted and silently not applied."""
    newest = max(client.list_dataset_versions(dataset_id=dataset_id), key=lambda v: v.as_of)
    client.update_dataset_tag(dataset_id=dataset_id, as_of=newest.as_of, tag=VERSION_TAG)


def to_jsonl(examples: list[Example]) -> str:
    return "".join(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n" for e in examples)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _meta_path(path: Path) -> Path:
    return path.with_suffix(".meta.json")


def _show(client: Any, name: str, count: int) -> None:
    dataset = client.read_dataset(dataset_name=name)
    print(
        f"\n=== {name}  ({dataset.example_count} examples, metadata sha256 "
        f"{(dataset.metadata or {}).get('sha256', '')[:12]}…)"
    )
    for example in client.list_examples(dataset_id=dataset.id, limit=count):
        print(json.dumps(_clipped(example.inputs), ensure_ascii=False))
        print("  outputs:", json.dumps(_clipped(example.outputs or {}), ensure_ascii=False))
        print("  metadata:", json.dumps(_clipped(example.metadata or {}), ensure_ascii=False))


def _clipped(value: Any, limit: int = 300) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"… [{len(value)} chars]"
    if isinstance(value, dict):
        return {k: _clipped(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_clipped(v, limit) for v in value[:5]] + (
            [f"… {len(value)} items"] if len(value) > 5 else []
        )
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["build", "push", "show"])
    args = parser.parse_args()
    if args.command == "build":
        for path in build():
            print("wrote", path)
        return
    from langsmith import Client

    from contextforge.core.settings import Settings

    key = Settings().langsmith_api_key
    if key is None:
        raise SystemExit("LANGSMITH_API_KEY is not set")
    client = Client(api_key=key.get_secret_value())
    for name in [*BUILDERS, HANDMADE]:
        if args.command == "push":
            examples, meta = load_local(name)
            print(name, "created" if push(client, name, examples, meta) else "unchanged")
        else:
            _show(client, name, 2)


if __name__ == "__main__":
    main()

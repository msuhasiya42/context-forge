"""The FRAMES benchmark dataset: download once, cache, sample by reasoning type, make cases.

FRAMES is Krishna et al. 2024, "Fact, Fetch, and Reason: A Unified Evaluation of
Retrieval-Augmented Generation" (arXiv:2409.12941), published by Google as the Hugging Face
dataset ``google/frames-benchmark`` (Apache-2.0): 824 multi-hop questions, each with a short gold
answer, the Wikipedia pages it needs and its reasoning types. Gold answers are as of the
dataset's creation in 2024.
"""

import ast
import json
import random
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx

from contextforge.core.entities import Entity
from contextforge.core.ids import now_iso
from contextforge.evals.suite import EvalCase

DATASET = "google/frames-benchmark"
SPLIT = "test"
ROWS_URL = "https://datasets-server.huggingface.co/rows"
INFO_URL = f"https://huggingface.co/api/datasets/{DATASET}"
DEFAULT_CACHE = Path(".data/benchmarks/frames/test.jsonl")
SAMPLE_FILE = "frames-sample.json"
# The rows API's largest page.
PAGE_ROWS = 100
# Download attempts per page; only connection failures, 429 and 5xx are retried.
_ATTEMPTS = 3
_UNTYPED = "Untyped"


class FramesItem(Entity):
    row: int
    prompt: str
    answer: str
    wiki_links: list[str]
    reasoning_types: list[str]

    @property
    def case_id(self) -> str:
        return f"frames-{self.row}"

    @property
    def primary_type(self) -> str:
        """The first listed reasoning type; FRAMES lists several for most questions."""
        return self.reasoning_types[0] if self.reasoning_types else _UNTYPED


class FramesCase(EvalCase):
    """A FRAMES question as a suite case: ``question`` is the prompt, ``gold`` the answer."""

    gold: str
    reasoning_types: list[str]
    wiki_links: list[str]


def to_case(item: FramesItem) -> FramesCase:
    return FramesCase(
        id=item.case_id,
        question=item.prompt,
        gold=item.answer,
        reasoning_types=item.reasoning_types,
        wiki_links=item.wiki_links,
    )


def load_frames(
    cache: Path = DEFAULT_CACHE, *, client: httpx.Client | None = None
) -> list[FramesItem]:
    """The test split from ``cache``, downloaded first (with the dataset's revision sha in a
    ``.meta.json`` beside it) when the cache does not exist yet."""
    if not cache.exists():
        own_client = client is None
        http = client or httpx.Client(timeout=30, follow_redirects=True)
        try:
            revision, rows = _download(http)
        finally:
            if own_client:
                http.close()
        _write_cache(cache, [parse_row(row) for row in rows], revision)
    lines = cache.read_text(encoding="utf-8").splitlines()
    return [FramesItem.model_validate_json(line) for line in lines if line.strip()]


def cached_revision(cache: Path) -> str | None:
    meta = _meta_path(cache)
    return json.loads(meta.read_text(encoding="utf-8")).get("revision") if meta.exists() else None


def parse_row(raw: dict[str, Any]) -> FramesItem:
    """One rows-API row (``{"row_idx": n, "row": {...}}``) as an item."""
    row = raw["row"]
    return FramesItem(
        row=raw["row_idx"],
        prompt=str(row["Prompt"]).strip(),
        answer=str(row["Answer"]).strip(),
        wiki_links=_wiki_links(row),
        reasoning_types=[
            kind.strip()
            for kind in str(row.get("reasoning_types") or "").split("|")
            if kind.strip()
        ],
    )


def _wiki_links(row: dict[str, Any]) -> list[str]:
    """``wiki_links`` is a stringified Python list. It is read with ``ast.literal_eval``, which
    accepts literals only, so a crafted cell cannot run code; an unreadable cell falls back to
    the ``wikipedia_link_*`` columns."""
    try:
        links = ast.literal_eval(str(row.get("wiki_links") or "[]"))
    except (ValueError, SyntaxError):
        links = None
    if isinstance(links, list) and all(isinstance(link, str) for link in links):
        return links
    return [
        str(value)
        for key, value in row.items()
        if key.startswith("wikipedia_link_") and isinstance(value, str) and value
    ]


def _download(client: httpx.Client) -> tuple[str, list[dict[str, Any]]]:
    revision = str(_get_json(client, INFO_URL, {})["sha"])
    rows: list[dict[str, Any]] = []
    total = None
    while total is None or len(rows) < total:
        page = _get_json(
            client,
            ROWS_URL,
            {
                "dataset": DATASET,
                "config": "default",
                "split": SPLIT,
                "offset": len(rows),
                "length": PAGE_ROWS,
            },
        )
        total = int(page["num_rows_total"])
        if not page["rows"]:
            break
        rows.extend(page["rows"])
    if len(rows) != total:
        raise ValueError(f"{DATASET}: expected {total} rows, received {len(rows)}")
    return revision, rows


def _get_json(client: httpx.Client, url: str, params: dict[str, Any]) -> dict[str, Any]:
    """A GET with bounded retries for transient failures (connection errors, 429, 5xx)."""
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            response = client.get(url, params=params)
            if response.status_code != 429 and response.status_code < 500:
                response.raise_for_status()
                body: dict[str, Any] = response.json()
                return body
            failure: Exception = httpx.HTTPStatusError(
                f"HTTP {response.status_code}", request=response.request, response=response
            )
        except httpx.TransportError as exc:
            failure = exc
        if attempt == _ATTEMPTS:
            raise failure
        time.sleep(2**attempt)
    raise AssertionError("unreachable")


def _write_cache(cache: Path, items: list[FramesItem], revision: str) -> None:
    """Written beside the final path and renamed, so a failed write leaves no half cache."""
    cache.parent.mkdir(parents=True, exist_ok=True)
    partial = cache.with_suffix(".partial")
    partial.write_text(
        "".join(item.model_dump_json(by_alias=True) + "\n" for item in items), encoding="utf-8"
    )
    meta = {
        "dataset": DATASET,
        "split": SPLIT,
        "revision": revision,
        "rows": len(items),
        "source": ROWS_URL,
        "downloadedAt": now_iso(),
    }
    _meta_path(cache).write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    partial.replace(cache)


def _meta_path(cache: Path) -> Path:
    return cache.with_suffix(".meta.json")


def sample_frames(items: list[FramesItem], n: int, seed: int) -> list[FramesItem]:
    """``n`` items, the same for the same items and seed, stratified by primary reasoning type:
    every type gets at least one question when ``n`` allows, the rest go to types in proportion
    to their size. Returned in dataset order."""
    groups: dict[str, list[FramesItem]] = defaultdict(list)
    for item in sorted(items, key=lambda i: i.row):
        groups[item.primary_type].append(item)
    rng = random.Random(seed)
    for kind in sorted(groups):
        rng.shuffle(groups[kind])
    quotas = _quotas({kind: len(groups[kind]) for kind in sorted(groups)}, n, rng)
    chosen = [item for kind, quota in quotas.items() for item in groups[kind][:quota]]
    return sorted(chosen, key=lambda i: i.row)


def _quotas(sizes: dict[str, int], n: int, rng: random.Random) -> dict[str, int]:
    """Questions per type: one each first, then the remainder by largest remainder (ties by
    type name). With fewer questions than types, ``n`` types are drawn at random."""
    n = min(n, sum(sizes.values()))
    if n < len(sizes):
        return dict.fromkeys(rng.sample(sorted(sizes), n), 1)
    quotas = dict.fromkeys(sizes, 1)
    room = {kind: size - 1 for kind, size in sizes.items()}
    rest = n - len(sizes)
    capacity = sum(room.values())
    shares = {kind: rest * room[kind] / capacity if capacity else 0.0 for kind in sizes}
    for kind, share in shares.items():
        quotas[kind] += int(share)
    left = n - sum(quotas.values())
    by_remainder = sorted(sizes, key=lambda kind: (-(shares[kind] % 1), kind))
    while left:
        for kind in by_remainder:
            if left and quotas[kind] < sizes[kind]:
                quotas[kind] += 1
                left -= 1
    return quotas


def save_sample(
    out_dir: Path, items: list[FramesItem], *, seed: int | None, revision: str | None
) -> Path:
    """The chosen ids in the run directory, so a rerun (``--sample-from``) asks the same
    questions even if the dataset or the sampler changes."""
    path = out_dir / SAMPLE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    sample = {
        "dataset": DATASET,
        "revision": revision,
        "seed": seed,
        "ids": [item.case_id for item in items],
    }
    path.write_text(json.dumps(sample, indent=2) + "\n", encoding="utf-8")
    return path


def sample_ids(run_dir: Path) -> list[str]:
    return list(json.loads((run_dir / SAMPLE_FILE).read_text(encoding="utf-8"))["ids"])


def select_ids(items: list[FramesItem], ids: list[str]) -> list[FramesItem]:
    by_id = {item.case_id: item for item in items}
    missing = [case_id for case_id in ids if case_id not in by_id]
    if missing:
        raise ValueError(f"Not in the cached dataset: {', '.join(missing)}")
    return [by_id[case_id] for case_id in ids]

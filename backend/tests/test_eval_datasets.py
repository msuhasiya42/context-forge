"""The harness's fixed datasets: DRB is read and sampled reproducibly, and a dataset is never
changed silently in LangSmith."""

import json
from types import SimpleNamespace

import pytest

from contextforge.evals.harness.datasets import DatasetChanged, push, sha256, to_jsonl
from contextforge.evals.harness.drb import load_drb, sample_drb


def _write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_drb_joins_files_reads_literals_and_samples_one_task_per_topic(tmp_path):
    topics = ["Games", "Games", "Health", "Law", "Art"]
    _write(
        tmp_path / "query.jsonl",
        [{"id": i, "topic": t, "language": "en", "prompt": f"q{i}"} for i, t in enumerate(topics)]
        + [{"id": 9, "topic": "Food", "language": "zh", "prompt": "中文"}],
    )
    _write(tmp_path / "reference.jsonl", [{"id": str(i), "article": f"ref{i}"} for i in range(10)])
    _write(
        tmp_path / "criteria.jsonl",
        [
            {
                "id": i,
                "dimension_weight": "{'insight': 0.6, 'readability': 0.4}",
                "criterions": "{'insight': [{'criterion': 'c', 'weight': 1.0}]}",
            }
            for i in range(10)
        ],
    )

    tasks = load_drb(tmp_path)
    chosen = sample_drb(tasks, 3, seed=42)

    assert tasks[0].reference_report == "ref0"
    assert tasks[0].dimension_weights == {"insight": 0.6, "readability": 0.4}
    assert len({t.topic for t in chosen}) == 3
    assert all(t.language == "en" for t in chosen)
    assert sample_drb(tasks, 3, seed=42) == chosen
    with pytest.raises(ValueError):
        sample_drb(tasks, 5, seed=42)  # only 4 English topics


class FakeLangSmith:
    def __init__(self):
        self.datasets, self.examples, self.tags = {}, [], []

    def has_dataset(self, *, dataset_name):
        return dataset_name in self.datasets

    def read_dataset(self, *, dataset_name):
        return self.datasets[dataset_name]

    def create_dataset(self, name, *, description, metadata):
        self.datasets[name] = SimpleNamespace(id=name, metadata=metadata)
        return self.datasets[name]

    def create_examples(self, *, dataset_id, examples):
        self.examples.extend(examples)

    def list_dataset_versions(self, *, dataset_id):
        return [SimpleNamespace(as_of=1), SimpleNamespace(as_of=2)]

    def update_dataset_tag(self, *, dataset_id, as_of, tag):
        self.tags.append((tag, as_of))


def test_a_dataset_is_created_once_and_a_changed_one_is_refused():
    client = FakeLangSmith()
    examples = [{"inputs": {"question": "q"}, "outputs": {"answer": "a"}, "metadata": {}}]
    meta = {"sha256": sha256(to_jsonl(examples))}

    assert push(client, "cf-x", examples, meta) is True
    assert push(client, "cf-x", examples, meta) is False
    assert client.examples == examples and client.tags == [("v1", 2)]
    with pytest.raises(DatasetChanged):
        push(client, "cf-x", examples, {"sha256": "different"})

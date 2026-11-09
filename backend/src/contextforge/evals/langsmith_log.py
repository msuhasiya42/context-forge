"""Optional (``--langsmith``): the suite's cases as a LangSmith dataset and a suite run as an
experiment with per-run feedback. Logging is an observation: a failure is reported and never
fails the suite."""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

DATASET = "contextforge-core-suite"


def log_suite(client: Any, card: dict[str, Any], records: list[dict[str, Any]]) -> bool:
    """Upsert the dataset (one example per case) and log each run with its feedback: score,
    factual_support, coverage and flags. True when logged; False, with a warning, on failure.
    ``client`` is a ``langsmith.Client``."""
    try:
        dataset_id = _dataset(client, records)
        examples = {
            e.metadata.get("caseId"): e.id
            for e in client.list_examples(dataset_id=dataset_id)
            if e.metadata
        }
        project = f"contextforge-suite-{card.get('suiteRunId')}"
        client.create_project(project, reference_dataset_id=dataset_id, metadata=card["overall"])
        for record in records:
            _log_run(client, project, examples.get(record["caseId"]), record)
    except Exception as exc:  # any client or network failure; logging never fails the suite
        logger.warning("LangSmith logging failed: %s", type(exc).__name__)
        return False
    return True


def _dataset(client: Any, records: list[dict[str, Any]]) -> Any:
    """The dataset's id, created when missing; a case without an example gets one."""
    if client.has_dataset(dataset_name=DATASET):
        dataset = client.read_dataset(dataset_name=DATASET)
    else:
        dataset = client.create_dataset(DATASET, description="ContextForge core eval suite")
    present = {
        e.metadata.get("caseId") for e in client.list_examples(dataset_id=dataset.id) if e.metadata
    }
    cases = {r["caseId"]: r["case"] for r in records}
    for case_id, case in cases.items():
        if case_id not in present:
            client.create_example(
                inputs={"question": case["question"]},
                reference_outputs={"requiredParts": case["requiredParts"]},
                metadata={"caseId": case_id, "source": case["source"]},
                dataset_id=dataset.id,
            )
    return dataset.id


def _log_run(client: Any, project: str, example_id: Any, record: dict[str, Any]) -> None:
    grade = record["grade"]
    run_id = uuid.uuid4()
    now = datetime.now(UTC)
    client.create_run(
        name=record["runId"],
        inputs={"question": record["case"]["question"]},
        run_type="chain",
        project_name=project,
        id=run_id,
        outputs={"status": grade["status"], "requestId": record.get("requestId")},
        reference_example_id=example_id,
        start_time=now,
        end_time=now,
    )
    scores = grade.get("scores") or {}
    feedback = {
        "score": grade.get("qualityScore"),
        "factual_support": scores.get("factual_support"),
        "coverage": scores.get("question_coverage"),
        "flags": sum(bool(v) for v in (grade.get("flags") or {}).values()),
    }
    for key, score in feedback.items():
        if score is not None:
            client.create_feedback(run_id, key, score=score)

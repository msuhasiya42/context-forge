"""Judge calibration: export claims for a person to label, then measure each judge model's
agreement with those labels (percent agreement and Cohen's kappa), before trusting a judge."""

import csv
import json
import random
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any, get_args

from contextforge.core.entities.message import ReportMessage
from contextforge.evals.bundle import claim_evidence
from contextforge.evals.judge import JUDGE_ERROR, ClaimEvidence, ClaimLabel, judge_claims
from contextforge.evals.judge_client import JudgeSession

COLUMNS = ("run_id", "claim_id", "claim", "cited_url", "excerpt", "context", "human_label")
LABELS = frozenset(get_args(ClaimLabel))


def export_calibration(suite_dir: Path, n: int, *, seed: int = 0) -> Path:
    """Write ``calibration.csv`` with ``n`` claims sampled across the suite's runs (a fixed seed,
    so the sample is repeatable) and a blank ``human_label`` column; the judge's verdicts are
    left out so they cannot anchor the labeller."""
    rows = []
    for path in sorted((suite_dir / "runs").glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("bundle") is None:
            continue
        bundle = record["bundle"]
        report = ReportMessage.model_validate(bundle["report"])
        for claim in claim_evidence(report, bundle.get("snapshots", {})):
            rows.append({"run_id": record["runId"], **claim.model_dump(), "human_label": ""})
    picked = sorted(random.Random(seed).sample(range(len(rows)), min(n, len(rows))))
    out = suite_dir / "calibration.csv"
    with out.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows[i] for i in picked)
    return out


def score_calibration(csv_path: Path, judges: dict[str, JudgeSession]) -> list[dict[str, Any]]:
    """Each judge model on the labelled rows: agreement and Cohen's kappa over the rows it
    judged, with its judge errors counted apart."""
    labelled = _labelled_rows(csv_path)
    evidence = [
        ClaimEvidence(
            claim_id=f"{row['run_id']}/{row['claim_id']}",
            claim=row["claim"],
            cited_url=row["cited_url"],
            excerpt=row["excerpt"],
            context=row["context"],
        )
        for row in labelled
    ]
    results = []
    for model_id, session in judges.items():
        verdicts = judge_claims(evidence, model=session.client)
        pairs = [
            (row["human_label"], v.verdict)
            for row, v in zip(labelled, verdicts, strict=True)
            if v.verdict != JUDGE_ERROR
        ]
        humans, judged = [h for h, _ in pairs], [j for _, j in pairs]
        results.append(
            {
                "model": model_id,
                "labelled": len(labelled),
                "judged": len(pairs),
                "judgeErrors": len(labelled) - len(pairs),
                "agreementPercent": round(100 * _agreement(humans, judged), 1) if pairs else None,
                "kappa": round(cohen_kappa(humans, judged), 3) if pairs else None,
                "costUsd": round(session.spent_usd(), 6),
            }
        )
    return results


def _labelled_rows(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open(newline="", encoding="utf-8") as file:
        rows = [
            {**row, "human_label": row["human_label"].strip().lower()}
            for row in csv.DictReader(file)
            if (row.get("human_label") or "").strip()
        ]
    unknown = sorted({row["human_label"] for row in rows} - LABELS)
    if unknown:
        raise ValueError(f"Unknown labels {unknown}; use one of {sorted(LABELS)}.")
    if not rows:
        raise ValueError(f"No labelled rows in {csv_path}; fill the human_label column first.")
    return rows


def _agreement(first: Sequence[str], second: Sequence[str]) -> float:
    return sum(a == b for a, b in zip(first, second, strict=True)) / len(first)


def cohen_kappa(first: Sequence[str], second: Sequence[str]) -> float:
    """Agreement beyond chance between two labelings of the same items: (po - pe) / (1 - pe).
    When chance agreement is total (both used one and the same label), it is 1.0."""
    count = len(first)
    observed = _agreement(first, second)
    first_counts, second_counts = Counter(first), Counter(second)
    chance = sum(first_counts[label] * second_counts[label] for label in first_counts) / count**2
    return 1.0 if chance == 1 else (observed - chance) / (1 - chance)


def calibration_markdown(results: list[dict[str, Any]]) -> str:
    lines = [
        "| Judge model | Labelled | Judged | Judge errors | Agreement % | Kappa | Cost USD |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    lines += [
        f"| {r['model']} | {r['labelled']} | {r['judged']} | {r['judgeErrors']} "
        f"| {r['agreementPercent']} | {r['kappa']} | {r['costUsd']} |"
        for r in results
    ]
    return "\n".join(lines) + "\n"

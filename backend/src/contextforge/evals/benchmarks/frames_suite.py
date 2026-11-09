"""FRAMES through the suite runner: each question submitted to the running API, its report's
answer extracted and graded against the gold answer, and a scorecard of answer accuracy.

Metrics, per run (a question in one repeat):
- accuracy: correct answers over judged runs (runs with a judge error are left out);
- supportedAccuracy: correct answers that at least one claim stating the answer (or the facts it
  was derived from) supports from its cited page snapshot, over judged runs. It separates answers
  found on the web from answers the model already knew;
- noAnswerRate: runs with no answer, or no captured report, over judged runs.
The F13 scorecard fields are kept (``meanScore`` is accuracy x 100, a run ``passed`` when its
answer is correct), so ``suite compare`` gates FRAMES scorecards too.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from contextforge.core.entities.message import ReportMessage
from contextforge.evals.api_research import save_json
from contextforge.evals.benchmarks.frames import FramesCase
from contextforge.evals.benchmarks.frames_judge import extract_final_answer, grade_answer
from contextforge.evals.bundle import claim_evidence, report_text
from contextforge.evals.judge import JUDGE_ERROR, judge_claims
from contextforge.evals.judge_client import JudgeSession
from contextforge.evals.suite import (
    PASSED,
    regrade_meta,
    regrade_runs,
    run_cases,
    run_meta,
    scorecard,
)

STALE_NOTE = (
    "FRAMES gold answers are as of the dataset's creation (2024). A question whose real-world "
    "answer has since changed may be marked incorrect; the judge flags such answers as "
    "possiblyStale."
)
# Assumed judge tokens per FRAMES run (extraction over the report, one grading call, the few
# claims behind a correct answer), for the estimate printed before any live call.
JUDGE_INPUT_TOKENS_PER_RUN = 15_000
JUDGE_OUTPUT_TOKENS_PER_RUN = 800
_STATUS = {"correct": PASSED, "incorrect": "incorrect", "no_answer": "no_answer"}


def grade_frames_run(record: dict[str, Any], judge: JudgeSession) -> None:
    """Extract, grade and (for a correct answer) check the support of one run's answer, into
    ``record["grade"]``, with the judge spend it took in ``record["judgeCostUsd"]``."""
    before = judge.spent_usd()
    record["grade"] = _grade(record, judge)
    record["judgeCostUsd"] = round(judge.spent_usd() - before, 6)


def _grade(record: dict[str, Any], judge: JudgeSession) -> dict[str, Any]:
    case = FramesCase.model_validate(record["case"])
    bundle = record.get("bundle")
    report = _report(bundle)
    if bundle is None or report is None:
        detail = record.get("error") or "No report was captured for this run."
        return _result("no_capture", "no_answer", detail=detail)
    extracted = extract_final_answer(
        case.question, report_text(bundle), model=judge.client, claims=report.report.claims
    )
    if extracted.error:
        return _result(JUDGE_ERROR, JUDGE_ERROR, detail=extracted.error, judge_errors=1)
    graded = grade_answer(case.question, case.gold, extracted.answer, model=judge.client)
    answer = {"extractedAnswer": extracted.answer, "answerClaimIds": extracted.claim_ids}
    if graded.verdict == JUDGE_ERROR:
        return _result(JUDGE_ERROR, JUDGE_ERROR, detail=graded.reason, judge_errors=1) | answer
    result = _result(_STATUS[graded.verdict], graded.verdict, detail=graded.reason) | answer
    result["possiblyStale"] = graded.possibly_stale
    if graded.verdict == "correct":
        result |= _support(report, bundle, extracted.claim_ids, judge)
    return result


def _report(bundle: dict[str, Any] | None) -> ReportMessage | None:
    if bundle is None:
        return None
    try:
        return ReportMessage.model_validate(bundle["report"])
    except (KeyError, TypeError, ValidationError):
        return None


def _support(
    report: ReportMessage, bundle: dict[str, Any], claim_ids: list[str], judge: JudgeSession
) -> dict[str, Any]:
    """Whether a claim behind the answer is supported by its cited snapshot: True when any is;
    None (unknown) when none is and the judge failed on one; else False."""
    evidence = [
        e for e in claim_evidence(report, bundle.get("snapshots", {})) if e.claim_id in claim_ids
    ]
    verdicts = judge_claims(evidence, model=judge.client)
    errors = sum(v.verdict == JUDGE_ERROR for v in verdicts)
    supported: bool | None = any(v.verdict == "supported" for v in verdicts)
    if not supported and errors:
        supported = None
    return {
        "supported": supported,
        "claimVerdicts": [v.model_dump(mode="json", by_alias=True) for v in verdicts],
        "judgeErrors": errors,
    }


def _result(status: str, verdict: str, *, detail: str, judge_errors: int = 0) -> dict[str, Any]:
    """A run's grade in the F13 shape: a correct answer scores 100, any other answer 0, and a
    judge error has no score."""
    score = None if verdict == JUDGE_ERROR else 100 if verdict == "correct" else 0
    return {
        "status": status,
        "verdict": verdict,
        "qualityScore": score,
        "supported": False,
        "possiblyStale": False,
        "judgeErrors": judge_errors,
        "detail": detail,
    }


def run_frames(
    cases: list[FramesCase],
    *,
    repeats: int,
    client: httpx.Client,
    judge: JudgeSession,
    out_dir: Path,
    max_usd: float,
    meta: dict[str, Any],
    poll_seconds: float = 2,
    timeout_seconds: float = 960,
    emit: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Each question ``repeats`` times through the API, graded; writes the runs and the
    scorecard (``meta`` adds the dataset revision and seed); returns the scorecard."""
    records, stopped = run_cases(
        cases,
        repeats=repeats,
        client=client,
        judge=judge,
        grade=lambda record: grade_frames_run(record, judge),
        out_dir=out_dir,
        max_usd=max_usd,
        poll_seconds=poll_seconds,
        timeout_seconds=timeout_seconds,
        emit=emit,
    )
    card = frames_scorecard(
        records,
        cases=cases,
        repeats=repeats,
        meta={**run_meta(out_dir, max_usd, stopped, judge), **meta},
    )
    write_frames_scorecard(out_dir, card)
    return card


def regrade_frames(run_dir: Path, judge: JudgeSession) -> dict[str, Any]:
    """The saved runs graded again with the current judge; nothing is submitted."""
    previous = json.loads((run_dir / "scorecard.json").read_text(encoding="utf-8"))
    records = regrade_runs(run_dir, lambda record: grade_frames_run(record, judge))
    cases = [FramesCase.model_validate(r["case"]) for r in records]
    card = frames_scorecard(
        records,
        cases=list({case.id: case for case in cases}.values()),
        case_order=list(previous["cases"]),
        repeats=previous["repeats"],
        meta={
            **regrade_meta(previous, judge),
            **{k: previous.get(k) for k in ("dataset", "revision", "seed")},
        },
    )
    write_frames_scorecard(run_dir, card)
    return card


def frames_scorecard(
    records: list[dict[str, Any]],
    *,
    cases: list[FramesCase],
    repeats: int,
    meta: dict[str, Any],
    case_order: list[str] | None = None,
) -> dict[str, Any]:
    """The F13 scorecard (cost, tokens and latency per question, pass@1, pass^k, judge cost)
    with FRAMES accuracy overall, per reasoning type (a question counts under each type it
    lists) and per question."""
    by_id = {case.id: case for case in cases}
    order = case_order or list(by_id)
    card = scorecard(records, cases=order, repeats=repeats, meta=meta)
    card["benchmark"] = "frames"
    card["note"] = STALE_NOTE
    card["overall"] |= answer_metrics(records)
    kinds = sorted({kind for case in cases for kind in case.reasoning_types})
    card["byReasoningType"] = {
        kind: answer_metrics([r for r in records if kind in r["case"]["reasoningTypes"]])
        for kind in kinds
    }
    for case_id, summary in card["cases"].items():
        runs = [r for r in records if r["caseId"] == case_id]
        case = by_id.get(case_id)
        summary |= answer_metrics(runs) | {
            "gold": case.gold if case else None,
            "primaryType": case.reasoning_types[0] if case and case.reasoning_types else None,
            "answers": [r["grade"].get("extractedAnswer") for r in runs],
            "verdicts": [r["grade"]["verdict"] for r in runs],
        }
    return card


def answer_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    grades = [r["grade"] for r in records]
    judged = [g for g in grades if g["verdict"] != JUDGE_ERROR]
    correct = [g for g in judged if g["verdict"] == "correct"]
    return {
        "judgedRuns": len(judged),
        "accuracy": _share(len(correct), len(judged)),
        "supportedAccuracy": _share(sum(g["supported"] is True for g in correct), len(judged)),
        "noAnswerRate": _share(sum(g["verdict"] == "no_answer" for g in judged), len(judged)),
        "possiblyStale": sum(bool(g.get("possiblyStale")) for g in judged),
    }


def _share(part: int, whole: int) -> float | None:
    return round(part / whole, 3) if whole else None


def write_frames_scorecard(out_dir: Path, card: dict[str, Any]) -> None:
    save_json(out_dir / "scorecard.json", card)
    (out_dir / "scorecard.md").write_text(frames_markdown(card), encoding="utf-8")


def frames_markdown(card: dict[str, Any]) -> str:
    overall = card["overall"]
    lines = [
        f"# FRAMES scorecard {card.get('suiteRunId')}",
        "",
        f"Dataset {card.get('dataset')} at revision {card.get('revision')}, seed "
        f"{card.get('seed')}; {len(card['cases'])} questions x {card['repeats']} repeats. "
        f"Judge: {', '.join(card.get('judgeRoute') or [])}. Spent ${card['spentUsd']:.4f} of "
        f"${card.get('maxUsd') or 0:.2f} (judge ${card.get('judgeCostUsd') or 0:.4f}); recorded "
        "estimates, not billing.",
        "",
        f"_{card['note']}_",
    ]
    if card.get("stoppedEarly"):
        lines += ["", f"**{card['stoppedEarly']}**"]
    pass_k = f", pass^k {overall['passHatK']}" if card["repeats"] > 1 else ""
    lines += [
        "",
        f"Overall ({overall['judgedRuns']} judged runs of {overall['runs']}): accuracy "
        f"{overall['accuracy']}, supported accuracy {overall['supportedAccuracy']}, no-answer "
        f"rate {overall['noAnswerRate']}, possibly stale {overall['possiblyStale']}{pass_k}, "
        f"judge errors {overall['judgeErrors']}.",
        "",
        "## By reasoning type",
        "",
        "A question counts under every reasoning type it lists.",
        "",
        "| Reasoning type | Judged runs | Accuracy | Supported accuracy | No answer |",
        "|---|---:|---:|---:|---:|",
    ]
    for kind, metrics in card["byReasoningType"].items():
        lines.append(
            f"| {kind} | {metrics['judgedRuns']} | {metrics['accuracy']} "
            f"| {metrics['supportedAccuracy']} | {metrics['noAnswerRate']} |"
        )
    lines += [
        "",
        "## Per question",
        "",
        "| Question | Primary type | Gold | Answers | Verdicts | Accuracy | Supported "
        "| Cost/req | Tokens/req | Latency s | Judge $ |",
        "|---|---|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for case_id, case in card["cases"].items():
        research = case["research"]
        answers = "; ".join(_cell(a) for a in case["answers"])
        lines.append(
            f"| {case_id} | {case['primaryType']} | {_cell(case['gold'])} | {answers} "
            f"| {', '.join(case['verdicts'])} | {case['accuracy']} | {case['supportedAccuracy']} "
            f"| {research['meanCostUsd']} | {research['meanTokens']} "
            f"| {research['meanLatencySeconds']} | {case['judgeCostUsd']} |"
        )
    return "\n".join(lines) + "\n"


def _cell(text: str | None) -> str:
    """Model text made safe for one markdown table cell."""
    if text is None:
        return "-"
    return " ".join(text.split()).replace("|", "\\|")

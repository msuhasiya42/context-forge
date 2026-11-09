"""The eval suite: cases x repeats through the running API, each run judged, one scorecard.

pass@1 is the share of runs that pass; pass^k (tau-bench's reliability metric) is the share of
cases whose k repeats all pass. Each run is saved with its captured bundle and its case, so a
suite run can be re-graded later with no research spend.
"""

import json
import statistics
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import yaml
from pydantic import Field

from contextforge.core.entities import Entity
from contextforge.core.ids import now_iso
from contextforge.evals.api_research import save_json, submit_run, watch_run
from contextforge.evals.judge_client import JudgeSession
from contextforge.evals.scoring import FLAGS
from contextforge.evals.web_research import grade_bundle

PASSED = "passed"


class SuiteCase(Entity):
    id: str
    question: str
    required_parts: list[str] = Field(min_length=2)
    source: str


def load_suite(path: Path) -> list[SuiteCase]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    cases = [SuiteCase.model_validate(case) for case in raw["cases"]]
    ids = [case.id for case in cases]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{path}: case ids repeat")
    return cases


def select_cases(cases: list[SuiteCase], ids: list[str] | None) -> list[SuiteCase]:
    """The named cases in suite order; every name must be a case."""
    if not ids:
        return cases
    unknown = set(ids) - {case.id for case in cases}
    if unknown:
        raise ValueError(f"Unknown case ids: {', '.join(sorted(unknown))}")
    return [case for case in cases if case.id in ids]


def run_suite(
    cases: list[SuiteCase],
    *,
    repeats: int,
    client: httpx.Client,
    judge: JudgeSession,
    out_dir: Path,
    max_usd: float,
    request_cap_usd: float,
    poll_seconds: float = 2,
    timeout_seconds: float = 960,
    emit: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Submit, watch, capture and judge each case ``repeats`` times, one run at a time; no new
    run is submitted once research and judge spend reach ``max_usd``. Writes runs/*.json and
    the scorecard; returns the scorecard."""
    records: list[dict[str, Any]] = []
    stopped = None
    planned = [(case, repeat) for case in cases for repeat in range(1, repeats + 1)]
    for index, (case, repeat) in enumerate(planned):
        spent = _research_usd(records) + judge.spent_usd()
        if spent >= max_usd:
            stopped = (
                f"Stopped at ${spent:.4f} spent of the ${max_usd:.2f} cap; "
                f"{len(planned) - index} of {len(planned)} runs were not submitted."
            )
            emit(stopped)
            break
        emit(f"[{index + 1}/{len(planned)}] {case.id} repeat {repeat}")
        record = _run_once(
            case, repeat, client, out_dir, poll_seconds=poll_seconds, timeout=timeout_seconds
        )
        _grade(record, judge, request_cap_usd)
        save_json(out_dir / "runs" / f"{record['runId']}.json", record)
        records.append(record)
        emit(f"  status={record['grade']['status']} score={record['grade'].get('qualityScore')}")
    card = scorecard(
        records,
        cases=[case.id for case in cases],
        repeats=repeats,
        meta={
            "suiteRunId": out_dir.name,
            "maxUsd": max_usd,
            "stoppedEarly": stopped,
            "judgeRoute": [judge.route.primary, *judge.route.fallbacks],
            "judgeCostUsd": round(judge.spent_usd(), 6),
        },
    )
    write_scorecard(out_dir, card)
    return card


def regrade_suite(suite_dir: Path, judge: JudgeSession, request_cap_usd: float) -> dict[str, Any]:
    """Grade the saved runs again with the current judge and rewrite the scorecard; no run is
    submitted."""
    previous = json.loads((suite_dir / "scorecard.json").read_text(encoding="utf-8"))
    records = []
    for path in sorted((suite_dir / "runs").glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        _grade(record, judge, request_cap_usd)
        save_json(path, record)
        records.append(record)
    card = scorecard(
        records,
        cases=list(previous["cases"]),
        repeats=previous["repeats"],
        meta={
            **{k: previous.get(k) for k in ("suiteRunId", "maxUsd", "stoppedEarly")},
            "regradedAt": now_iso(),
            "judgeRoute": [judge.route.primary, *judge.route.fallbacks],
            "judgeCostUsd": round(judge.spent_usd(), 6),
        },
    )
    write_scorecard(suite_dir, card)
    return card


def _run_once(
    case: SuiteCase,
    repeat: int,
    client: httpx.Client,
    out_dir: Path,
    *,
    poll_seconds: float,
    timeout: float,
) -> dict[str, Any]:
    """One submitted and watched run. A client or capture failure is recorded on the run,
    never raised, so one bad run does not lose the rest of the suite."""
    run_id = f"{case.id}-r{repeat}"
    raw_dir = out_dir / "raw" / run_id
    record: dict[str, Any] = {
        "runId": run_id,
        "caseId": case.id,
        "repeat": repeat,
        "case": case.model_dump(mode="json", by_alias=True),
    }
    started = time.monotonic()
    try:
        chat_id, request_id = submit_run(client, case.question, raw_dir, title=f"Eval {run_id}")
        record |= {"chatId": chat_id, "requestId": request_id}
        record["watchExit"] = watch_run(
            client,
            chat_id,
            request_id,
            raw_dir,
            poll_seconds=poll_seconds,
            timeout_seconds=timeout,
            case_id=case.id,
        )
    except (httpx.HTTPError, ValueError) as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
    record["elapsedSeconds"] = round(time.monotonic() - started, 1)
    record["research"] = _research_usage(raw_dir / "detail.json", record["elapsedSeconds"])
    bundle_path = raw_dir / "bundle.json"
    record["bundle"] = json.loads(bundle_path.read_text()) if bundle_path.exists() else None
    return record


def _grade(record: dict[str, Any], judge: JudgeSession, request_cap_usd: float) -> None:
    """Judge the run's bundle into ``record["grade"]``, with the judge spend it took."""
    if record.get("bundle") is None:
        reason = record.get("error") or "No report was captured for this run."
        record["grade"] = {"status": "no_capture", "qualityScore": None, "detail": reason}
        record["judgeCostUsd"] = 0.0
        return
    case = SuiteCase.model_validate(record["case"])
    reference = {
        "caseId": case.id,
        "question": case.question,
        "requiredParts": case.required_parts,
    }
    before = judge.spent_usd()
    record["grade"] = grade_bundle(
        record["bundle"], reference, max_usd=request_cap_usd, judge=judge.client
    )
    record["judgeCostUsd"] = round(judge.spent_usd() - before, 6)


def _research_usage(detail_path: Path, elapsed: float) -> dict[str, Any]:
    """The request's recorded cost and tokens (an estimate, not billing), and the wall time
    from submission to capture."""
    usage: dict[str, Any] = {}
    if detail_path.exists():
        usage = json.loads(detail_path.read_text()).get("usage", {})
    tokens_in, tokens_out = usage.get("inputTokens"), usage.get("outputTokens")
    tokens = None
    if isinstance(tokens_in, int) and isinstance(tokens_out, int):
        tokens = tokens_in + tokens_out
    return {
        "costUsd": usage.get("estimatedCostUsd"),
        "tokens": tokens,
        "modelCalls": usage.get("modelCalls"),
        "latencySeconds": elapsed,
    }


def _research_usd(records: list[dict[str, Any]]) -> float:
    return sum(r["research"]["costUsd"] or 0.0 for r in records)


def scorecard(
    records: list[dict[str, Any]], *, cases: list[str], repeats: int, meta: dict[str, Any]
) -> dict[str, Any]:
    """Per case and overall: score mean, stdev and min; pass@1; pass^k; flag and judge-error
    counts; research cost, tokens and latency per request. A case short of ``repeats`` runs (the
    spend cap stopped the suite) does not count as passing pass^k."""
    by_case = {
        case_id: _summary([r for r in records if r["caseId"] == case_id], repeats)
        for case_id in cases
    }
    overall = _summary(records, repeats)
    overall["passHatK"] = (
        round(sum(c["passAllK"] for c in by_case.values()) / len(by_case), 3) if by_case else 0.0
    )
    del overall["passAllK"]
    return {
        **meta,
        "createdAt": now_iso(),
        "repeats": repeats,
        "cases": by_case,
        "overall": overall,
        "spentUsd": round(_research_usd(records) + (meta.get("judgeCostUsd") or 0.0), 6),
    }


def _summary(runs: list[dict[str, Any]], repeats: int) -> dict[str, Any]:
    grades = [r["grade"] for r in runs]
    scores = [g["qualityScore"] for g in grades if isinstance(g.get("qualityScore"), int | float)]
    passed = [g["status"] == PASSED for g in grades]
    flags = Counter(
        flag for g in grades for flag, raised in (g.get("flags") or {}).items() if raised
    )
    return {
        "runs": len(runs),
        "scoredRuns": len(scores),
        "meanScore": _round(statistics.fmean(scores)) if scores else None,
        "stdevScore": _round(statistics.stdev(scores)) if len(scores) > 1 else 0.0,
        "minScore": min(scores) if scores else None,
        "passAt1": round(sum(passed) / len(runs), 3) if runs else 0.0,
        "passAllK": len(runs) >= repeats and all(passed) and bool(runs),
        "statuses": dict(Counter(g["status"] for g in grades)),
        "flags": {flag: flags.get(flag, 0) for flag in FLAGS},
        "judgeErrors": sum(g.get("judgeErrors", 0) for g in grades),
        "research": {
            "meanCostUsd": _mean([r["research"]["costUsd"] for r in runs], 6),
            "meanTokens": _mean([r["research"]["tokens"] for r in runs], 0),
            "meanLatencySeconds": _mean([r["research"]["latencySeconds"] for r in runs], 1),
        },
        "judgeCostUsd": round(sum(r.get("judgeCostUsd", 0.0) for r in runs), 6),
    }


def _mean(values: list[Any], digits: int) -> float | None:
    known = [v for v in values if isinstance(v, int | float)]
    return round(statistics.fmean(known), digits) if known else None


def _round(value: float) -> float:
    return round(value, 2)


def write_scorecard(out_dir: Path, card: dict[str, Any]) -> None:
    save_json(out_dir / "scorecard.json", card)
    (out_dir / "scorecard.md").write_text(scorecard_markdown(card), encoding="utf-8")


def scorecard_markdown(card: dict[str, Any]) -> str:
    overall = card["overall"]
    lines = [
        f"# Scorecard {card.get('suiteRunId')}",
        "",
        f"Repeats: {card['repeats']}. Judge: {', '.join(card.get('judgeRoute') or [])}. "
        f"Spent ${card['spentUsd']:.4f} of ${card.get('maxUsd') or 0:.2f} "
        f"(judge ${card.get('judgeCostUsd') or 0:.4f}); recorded estimates, not billing.",
    ]
    if card.get("stoppedEarly"):
        lines += ["", f"**{card['stoppedEarly']}**"]
    lines += [
        "",
        f"Overall: mean {overall['meanScore']}, stdev {overall['stdevScore']}, "
        f"min {overall['minScore']}, pass@1 {overall['passAt1']}, "
        f"pass^k {overall['passHatK']}, judge errors {overall['judgeErrors']}.",
        "",
        "| Case | Runs | Mean | Stdev | Min | pass@1 | pass^k | Flags | Judge errors "
        "| Cost/req | Tokens/req | Latency s |",
        "|---|---:|---:|---:|---:|---:|---|---|---:|---:|---:|---:|",
    ]
    for case_id, case in card["cases"].items():
        research = case["research"]
        lines.append(
            f"| {case_id} | {case['runs']} | {case['meanScore']} | {case['stdevScore']} "
            f"| {case['minScore']} | {case['passAt1']} | {'yes' if case['passAllK'] else 'no'} "
            f"| {_flag_text(case['flags'])} | {case['judgeErrors']} | {research['meanCostUsd']} "
            f"| {research['meanTokens']} | {research['meanLatencySeconds']} |"
        )
    return "\n".join(lines) + "\n"


def _flag_text(flags: dict[str, int]) -> str:
    return ", ".join(f"{flag} {count}" for flag, count in flags.items() if count) or "none"


def compare_scorecards(
    before: dict[str, Any], after: dict[str, Any], *, fail_on_drop: float | None = None
) -> tuple[str, list[str]]:
    """A markdown diff (after minus before) per case and overall, and the regressions the gate
    fails on: a mean score drop over ``fail_on_drop`` points, a lower pass^k, or a flag a case
    did not raise before. No regressions are reported without ``fail_on_drop``."""
    lines = [
        "| Case | Score | pass@1 | pass^k | New flags | Cost/req |",
        "|---|---|---|---|---|---|",
    ]
    regressions: list[str] = []
    for case_id in dict.fromkeys([*before["cases"], *after["cases"]]):
        old, new = before["cases"].get(case_id), after["cases"].get(case_id)
        if old is None or new is None:
            lines.append(f"| {case_id} | only in {'after' if old is None else 'before'} | | | | |")
            continue
        new_flags = [f for f, n in new["flags"].items() if n and not old["flags"].get(f)]
        if new_flags:
            regressions.append(f"{case_id}: new flags {', '.join(new_flags)}")
        lines.append(
            f"| {case_id} | {_delta(old['meanScore'], new['meanScore'])} "
            f"| {_delta(old['passAt1'], new['passAt1'])} "
            f"| {'yes' if old['passAllK'] else 'no'} -> {'yes' if new['passAllK'] else 'no'} "
            f"| {', '.join(new_flags) or 'none'} "
            f"| {_delta(old['research']['meanCostUsd'], new['research']['meanCostUsd'])} |"
        )
    old, new = before["overall"], after["overall"]
    drop = (old["meanScore"] or 0.0) - (new["meanScore"] or 0.0)
    if fail_on_drop is not None and drop > fail_on_drop:
        regressions.append(f"mean score fell from {old['meanScore']} to {new['meanScore']}")
    if new["passHatK"] < old["passHatK"]:
        regressions.append(f"pass^k fell from {old['passHatK']} to {new['passHatK']}")
    lines += [
        f"| **overall** | {_delta(old['meanScore'], new['meanScore'])} "
        f"| {_delta(old['passAt1'], new['passAt1'])} "
        f"| {_delta(old['passHatK'], new['passHatK'])} | | "
        f"{_delta(old['research']['meanCostUsd'], new['research']['meanCostUsd'])} |",
        "",
        f"Judge cost: {_delta(before.get('judgeCostUsd'), after.get('judgeCostUsd'))}.",
    ]
    if fail_on_drop is None:
        return "\n".join(lines) + "\n", []
    verdict = "; ".join(regressions) if regressions else "no regression"
    lines += ["", f"Gate (drop > {fail_on_drop} points): {verdict}."]
    return "\n".join(lines) + "\n", regressions


def _delta(old: float | None, new: float | None) -> str:
    if old is None or new is None:
        return f"{old} -> {new}"
    return f"{old} -> {new} ({new - old:+.3g})"

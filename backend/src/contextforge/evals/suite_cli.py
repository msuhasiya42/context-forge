"""The ``suite`` and ``calibrate`` commands of ``python -m contextforge.evals.web_research``.

``suite run`` prints its cost estimate and makes no network or model call without ``--yes``.
"""

import argparse
import json
import logging
import math
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

from contextforge.core.entities.model_call import Stage
from contextforge.core.models.config import ModelRoute, ModelsConfig, load_models_config
from contextforge.core.models.transcript import TranscriptWriter
from contextforge.core.settings import Settings
from contextforge.evals.calibration import (
    calibration_markdown,
    export_calibration,
    score_calibration,
)
from contextforge.evals.judge_client import (
    DirectoryBlobs,
    JudgeSession,
    estimate_judge_usd,
    judge_route,
    judge_session,
    shared_with_research,
    usable_route,
)
from contextforge.evals.langsmith_log import log_suite
from contextforge.evals.suite import (
    compare_scorecards,
    load_suite,
    regrade_suite,
    run_suite,
    select_cases,
)
from contextforge.evals.web_research import auth_headers
from contextforge.infra.runtime import judge_model_client, model_provider, stage_routes
from contextforge.infra.tracing import langsmith_client
from contextforge.worker.dependencies import secret_values

logger = logging.getLogger(__name__)

SUITE_COMMANDS = ("suite", "calibrate")
# A refused estimate or a regression: the run did not happen, or the gate failed.
EXIT_NOT_RUN = 2


def add_suite_commands(sub: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    suite = sub.add_parser("suite", help="Run, re-grade or compare judged suite runs.")
    suite_sub = suite.add_subparsers(dest="suite_command", required=True)
    run = suite_sub.add_parser("run", help="Submit each case; can incur model/search charges.")
    run.add_argument("--suite", type=Path, default=Path("evals/cases/core-suite.yaml"))
    run.add_argument("--repeats", type=int, default=1)
    run.add_argument("--cases", help="Comma-separated case ids; default: every case.")
    run.add_argument("--max-usd", type=float, default=3.00)
    run.add_argument("--yes", action="store_true", help="Confirm the estimate; required to run.")
    run.add_argument("--base-url", default="http://localhost:8000")
    run.add_argument("--out-dir", type=Path)
    run.add_argument("--poll-seconds", type=float, default=2)
    run.add_argument("--timeout-seconds", type=float, default=960)
    run.add_argument("--langsmith", action="store_true", help="Also log to LangSmith.")
    grade = suite_sub.add_parser("grade", help="Re-grade saved runs with the current judge.")
    grade.add_argument("suite_dir", type=Path)
    compare = suite_sub.add_parser("compare", help="Diff two scorecards; optional gate.")
    compare.add_argument("before", type=Path)
    compare.add_argument("after", type=Path)
    compare.add_argument("--fail-on-drop", type=float, metavar="POINTS")
    calibrate = sub.add_parser("calibrate", help="Export claims to label; score judge models.")
    calibrate_sub = calibrate.add_subparsers(dest="calibrate_command", required=True)
    export = calibrate_sub.add_parser("export", help="Write calibration.csv for labelling.")
    export.add_argument("suite_dir", type=Path)
    export.add_argument("--n", type=int, default=20)
    score = calibrate_sub.add_parser("score", help="Run judge models on the labelled rows.")
    score.add_argument("csv", type=Path)
    score.add_argument(
        "--judge-model",
        nargs="+",
        help="Model ids to compare; default: the judge stage's model and each fallback.",
    )


def run_suite_command(args: argparse.Namespace) -> int:
    if args.command == "calibrate":
        return _calibrate(args)
    if args.suite_command == "compare":
        return _compare(args)
    if args.suite_command == "grade":
        settings = Settings()
        card = regrade_suite(
            args.suite_dir,
            live_judge(settings, args.suite_dir / "judge-transcripts"),
            settings.request_max_usd,
        )
        print((args.suite_dir / "scorecard.md").read_text(encoding="utf-8"))
        print(f"Judge spend ${card['judgeCostUsd']:.4f}.")
        return 0
    return _run(args)


def _run(args: argparse.Namespace) -> int:
    if args.repeats < 1 or args.max_usd <= 0:
        raise SystemExit("--repeats must be at least 1 and --max-usd positive.")
    cases = select_cases(load_suite(args.suite), _ids(args.cases))
    settings = Settings()
    runs = len(cases) * args.repeats
    research = runs * settings.request_max_usd
    models = load_models_config()
    judging = estimate_judge_usd(models, runs, route=configured_judge_route(settings, models))
    print(
        f"{len(cases)} cases x {args.repeats} repeats = {runs} runs. Worst case research "
        f"${research:.2f} ({runs} x ${settings.request_max_usd:.2f} per-request cap) plus judge "
        f"about ${judging:.2f}; the suite stops submitting at ${args.max_usd:.2f}."
    )
    if not args.yes:
        print("Nothing was submitted. Add --yes to run it.")
        return EXIT_NOT_RUN
    out_dir = args.out_dir or Path(".data/evals") / suite_run_id()
    judge = live_judge(settings, out_dir / "judge-transcripts", max_usd=args.max_usd)
    print(f"Suite run files: {out_dir}", flush=True)
    with httpx.Client(base_url=args.base_url, headers=auth_headers(settings), timeout=30) as client:
        card = run_suite(
            cases,
            repeats=args.repeats,
            client=client,
            judge=judge,
            out_dir=out_dir,
            max_usd=args.max_usd,
            request_cap_usd=settings.request_max_usd,
            poll_seconds=args.poll_seconds,
            timeout_seconds=args.timeout_seconds,
            emit=lambda line: print(line, flush=True),
        )
    if args.langsmith:
        _log_langsmith(settings, card, out_dir)
    print((out_dir / "scorecard.md").read_text(encoding="utf-8"))
    return 0


def _compare(args: argparse.Namespace) -> int:
    before = json.loads(args.before.read_text(encoding="utf-8"))
    after = json.loads(args.after.read_text(encoding="utf-8"))
    markdown, regressions = compare_scorecards(before, after, fail_on_drop=args.fail_on_drop)
    print(markdown)
    return 1 if regressions else 0


def _calibrate(args: argparse.Namespace) -> int:
    if args.calibrate_command == "export":
        path = export_calibration(args.suite_dir, args.n)
        print(
            f"Wrote {path}; fill human_label with supported, partial, unsupported or contradicted."
        )
        return 0
    settings = Settings()
    models = load_models_config()
    configured = configured_judge_route(settings, models)
    model_ids = args.judge_model or [configured.primary, *configured.fallbacks]
    transcripts = args.csv.parent / "judge-transcripts"
    # Each model alone, without fallbacks, so its agreement is its own.
    judges = {
        model_id: live_judge(settings, transcripts, route=ModelRoute(model_id))
        for model_id in model_ids
    }
    results = score_calibration(args.csv, judges)
    out = args.csv.with_name("calibration-score.json")
    out.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(calibration_markdown(results))
    print(f"Saved {out}.")
    return 0


def live_judge(
    settings: Settings,
    transcripts_dir: Path,
    *,
    route: ModelRoute | None = None,
    max_usd: float = math.inf,
) -> JudgeSession:
    """The judge on Gemini and Bedrock, with a warning when one of its models serves a research
    stage. Transcripts are stored beside the run with every configured secret redacted."""
    models = load_models_config()
    try:
        route = usable_route(
            route or judge_route(models), gemini_key_set=settings.gemini_api_key is not None
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    shared = shared_with_research(route, stage_routes(models, settings))
    if shared:
        # F17: with every stage on Gemini the judge shares its models; scores then carry that
        # model family's self-preference, so the run says so instead of refusing.
        logger.warning(
            "Judge model also serves a research stage (%s); it grades its own pipeline.",
            ", ".join(sorted(shared)),
        )
    transcripts = (
        TranscriptWriter(DirectoryBlobs(transcripts_dir), secret_values(settings))
        if settings.record_transcripts
        else None
    )
    return judge_session(
        judge_model_client(settings, reasoning_effort=models.stages[Stage.JUDGE.value].effort),
        models,
        route=route,
        max_usd=max_usd,
        retries=settings.model_retries,
        reserve_usd=settings.call_reserve_usd,
        transcripts=transcripts,
        provider=model_provider,
    )


def configured_judge_route(settings: Settings, models: ModelsConfig) -> ModelRoute:
    """The judge stage's route as it will run, for the cost estimate: without its Gemini models
    when no key is set (``live_judge`` refuses a route left with none)."""
    route = judge_route(models)
    try:
        return usable_route(route, gemini_key_set=settings.gemini_api_key is not None)
    except ValueError:
        return route


def _log_langsmith(settings: Settings, card: dict[str, object], out_dir: Path) -> None:
    if settings.langsmith_api_key is None:
        print("--langsmith: LANGSMITH_API_KEY is not set; not logged.")
        return
    client = langsmith_client(settings.langsmith_api_key.get_secret_value(), include_content=True)
    records = [json.loads(p.read_text()) for p in sorted((out_dir / "runs").glob("*.json"))]
    logged = log_suite(client, card, records)
    print(
        "Logged to LangSmith." if logged else "LangSmith logging failed; the suite is unaffected."
    )


def _ids(text: str | None) -> list[str] | None:
    return [part.strip() for part in text.split(",") if part.strip()] if text else None


def suite_run_id() -> str:
    return f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"

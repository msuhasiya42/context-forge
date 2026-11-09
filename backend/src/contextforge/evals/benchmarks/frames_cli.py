"""The ``bench frames`` command of ``python -m contextforge.evals.web_research``.

Without ``--yes`` it prints the cost estimate and makes no network or model call: the dataset is
not downloaded, no question is submitted and no judge is built.
"""

import argparse
from pathlib import Path

import httpx

from contextforge.core.models.config import load_models_config
from contextforge.core.settings import Settings
from contextforge.evals.benchmarks.frames import (
    DATASET,
    DEFAULT_CACHE,
    cached_revision,
    load_frames,
    sample_frames,
    sample_ids,
    save_sample,
    select_ids,
    to_case,
)
from contextforge.evals.benchmarks.frames_suite import (
    JUDGE_INPUT_TOKENS_PER_RUN,
    JUDGE_OUTPUT_TOKENS_PER_RUN,
    regrade_frames,
    run_frames,
)
from contextforge.evals.judge_client import estimate_judge_usd
from contextforge.evals.suite_cli import EXIT_NOT_RUN, live_judge, suite_run_id
from contextforge.evals.web_research import auth_headers

BENCH_COMMANDS = ("bench",)


def add_bench_commands(sub: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    bench = sub.add_parser("bench", help="Run a public benchmark through the running API.")
    bench_sub = bench.add_subparsers(dest="benchmark", required=True)
    frames = bench_sub.add_parser(
        "frames", help=f"FRAMES ({DATASET}); can incur model/search charges."
    )
    frames.add_argument("--n", type=int, default=30, help="Questions to sample.")
    frames.add_argument("--seed", type=int, default=7)
    frames.add_argument("--repeats", type=int, default=1)
    frames.add_argument("--max-usd", type=float, default=5.00)
    frames.add_argument("--yes", action="store_true", help="Confirm the estimate; required to run.")
    frames.add_argument(
        "--regrade", type=Path, metavar="DIR", help="Re-grade a saved FRAMES run; no research."
    )
    frames.add_argument(
        "--sample-from", type=Path, metavar="DIR", help="Ask the questions a saved run asked."
    )
    frames.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    frames.add_argument("--base-url", default="http://localhost:8000")
    frames.add_argument("--out-dir", type=Path)
    frames.add_argument("--poll-seconds", type=float, default=2)
    frames.add_argument("--timeout-seconds", type=float, default=960)


def run_bench_command(args: argparse.Namespace) -> int:
    settings = Settings()
    if args.regrade:
        card = regrade_frames(
            args.regrade, live_judge(settings, args.regrade / "judge-transcripts")
        )
        print((args.regrade / "scorecard.md").read_text(encoding="utf-8"))
        print(f"Judge spend ${card['judgeCostUsd']:.4f}.")
        return 0
    if args.n < 1 or args.repeats < 1 or args.max_usd <= 0:
        raise SystemExit("--n and --repeats must be at least 1 and --max-usd positive.")
    saved_ids = sample_ids(args.sample_from) if args.sample_from else None
    questions = len(saved_ids) if saved_ids else args.n
    runs = questions * args.repeats
    research = runs * settings.request_max_usd
    judging = estimate_judge_usd(
        load_models_config(),
        runs,
        input_tokens_per_run=JUDGE_INPUT_TOKENS_PER_RUN,
        output_tokens_per_run=JUDGE_OUTPUT_TOKENS_PER_RUN,
    )
    print(
        f"FRAMES: {questions} questions x {args.repeats} repeats = {runs} runs. Worst case "
        f"research ${research:.2f} ({runs} x ${settings.request_max_usd:.2f} per-request cap) "
        f"plus judge about ${judging:.2f}; the bench stops submitting at ${args.max_usd:.2f}."
    )
    if not args.yes:
        print("Nothing was downloaded or submitted. Add --yes to run it.")
        return EXIT_NOT_RUN
    items = load_frames(args.cache)
    chosen = select_ids(items, saved_ids) if saved_ids else sample_frames(items, args.n, args.seed)
    out_dir = args.out_dir or Path(".data/evals") / suite_run_id()
    revision = cached_revision(args.cache)
    seed = None if saved_ids else args.seed
    save_sample(out_dir, chosen, seed=seed, revision=revision)
    judge = live_judge(settings, out_dir / "judge-transcripts", max_usd=args.max_usd)
    print(f"FRAMES run files: {out_dir}", flush=True)
    with httpx.Client(base_url=args.base_url, headers=auth_headers(settings), timeout=30) as client:
        run_frames(
            [to_case(item) for item in chosen],
            repeats=args.repeats,
            client=client,
            judge=judge,
            out_dir=out_dir,
            max_usd=args.max_usd,
            meta={"dataset": DATASET, "revision": revision, "seed": seed},
            poll_seconds=args.poll_seconds,
            timeout_seconds=args.timeout_seconds,
            emit=lambda line: print(line, flush=True),
        )
    print((out_dir / "scorecard.md").read_text(encoding="utf-8"))
    return 0

"""Planner bake-off: one query planned by several Bedrock models through the real plan stage,
written as one markdown report for a person to judge which model plans best.

    uv run python scripts/plan_bakeoff.py --probe listed
    uv run python scripts/plan_bakeoff.py --probe listed \\
        --models deepseek.v3.2,apac.amazon.nova-pro-v1:0 \\
        --query-file docs/evals/queries/complex-001.md [--repeat N] [--dry-run]

``--probe`` sends each model id a three-token call and records it as available, denied or not
found (a refused call is free); ``listed`` probes every id this region lists for a text model.
``--models`` runs ``plan_stage`` once per model (``--repeat`` times) exactly as the worker does:
prompt, context builder, result schema, resolver with today's date, the default memory, no
sources. A live run states its expected cost (a dry pass, offline) before its first call and is
held to ``--max-usd`` by the engine's own request spend cap. ``--dry-run`` answers every call
with a canned plan and estimated usage, offline, through the same Bedrock adapter.
"""

import argparse
import json
import math
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from botocore.exceptions import BotoCoreError, ClientError  # type: ignore[import-untyped]
from pydantic import ValidationError

from contextforge.core.context.budgets import load_budgets
from contextforge.core.context.prompts import prompt_version
from contextforge.core.context.tokens import estimate_tokens
from contextforge.core.entities.message import MessageKind, RequestMessage, RequestStatus, Sender
from contextforge.core.entities.model_call import ModelCall, Stage
from contextforge.core.ids import new_ulid, now_iso
from contextforge.core.models.config import ModelRoute, ModelsConfig, load_models_config
from contextforge.core.models.errors import ModelError
from contextforge.core.models.recording import cost_usd
from contextforge.core.planner import PlannerOutput
from contextforge.core.queue import InProcessQueue
from contextforge.core.repositories.memory import Repositories
from contextforge.core.repositories.shared import sum_known_usage
from contextforge.core.services.memory import memory_snapshot, user_timezone
from contextforge.core.settings import Settings
from contextforge.core.storage import MemoryBlobStore
from contextforge.infra.bedrock import RESULT_TOOL, BedrockModel, parse_response
from contextforge.infra.boto_errors import error_code
from contextforge.worker.brief import initial_brief
from contextforge.worker.deps import WorkerDeps
from contextforge.worker.stages.errors import one_line
from contextforge.worker.stages.plan import PlanFailed, PlanResult, local_today, plan_stage

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MAX_USD = 0.50
# Output tokens a dry run reports per call: about one plan. Reasoning models write more, so a
# live run can cost more than the dry pass expects.
DRY_RUN_OUTPUT_TOKENS = 1000
# What every dry-run call answers: a plan of the shape the planner prompt asks of a comparison.
SAMPLE_PLAN: dict[str, Any] = {
    "kind": "plan",
    "goal": "Dry run: a fixed sample plan, not a model's answer.",
    "constraints": [],
    "openQuestions": [],
    "tasks": [
        {
            "ref": "t1",
            "kind": "web_search",
            "goal": "Facts on the first subject, from primary pages.",
            "inputArtifactIds": [],
            "dependsOn": [],
        },
        {
            "ref": "t2",
            "kind": "web_search",
            "goal": "Facts on the second subject, from primary pages.",
            "inputArtifactIds": [],
            "dependsOn": [],
        },
        {
            "ref": "t3",
            "kind": "web_search",
            "goal": "Compare the two; check the figures on a public page.",
            "inputArtifactIds": [],
            "dependsOn": ["t1", "t2"],
        },
    ],
    "nextAction": "Run t1 and t2, then t3.",
}
_PROBE_MESSAGES = [{"role": "user", "content": [{"text": "Reply with OK."}]}]
_PRINCIPAL = re.compile(r"arn:aws:iam::\d+:\S+")


@dataclass(frozen=True)
class ConverseCall:
    """One Converse call: the payload sent, and Bedrock's answer or why it refused."""

    payload: dict[str, Any]
    response: dict[str, Any] | None
    refusal: str | None


class CapturingRuntime:
    """A bedrock-runtime client that keeps every Converse call it passes on, so the report shows
    the request shape each model accepted and its raw answer."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.calls: list[ConverseCall] = []

    def converse(self, **payload: Any) -> dict[str, Any]:
        try:
            response: dict[str, Any] = self.inner.converse(**payload)
        except (ClientError, BotoCoreError) as exc:
            self.calls.append(ConverseCall(payload, None, _failure(exc)))
            raise
        self.calls.append(ConverseCall(payload, response, None))
        return response


class DryRunRuntime:
    """Answers every Converse call offline with ``SAMPLE_PLAN`` through the result tool. Input
    usage is estimated from the payload and output is ``DRY_RUN_OUTPUT_TOKENS``, so a dry run
    prices each model about as a live run would."""

    def converse(self, **payload: Any) -> dict[str, Any]:
        use = {"toolUseId": "dry-run", "name": RESULT_TOOL, "input": SAMPLE_PLAN}
        return {
            "output": {"message": {"role": "assistant", "content": [{"toolUse": use}]}},
            "stopReason": "tool_use",
            "usage": {
                "inputTokens": estimate_tokens(json.dumps(payload)),
                "outputTokens": DRY_RUN_OUTPUT_TOKENS,
            },
        }


@dataclass(frozen=True)
class PlanRun:
    """One plan-stage run of one model: its result or why it failed, the Converse calls and the
    ModelCall records behind it."""

    model: str
    planned: PlanResult | None
    failure: str | None
    seconds: float
    calls: list[ConverseCall]
    records: list[ModelCall]


@dataclass(frozen=True)
class Probe:
    model: str
    # available, denied, not found, profile only or error
    result: str
    detail: str


def run_bakeoff(
    runtime: Any,
    models: Sequence[str],
    query: str,
    settings: Settings,
    config: ModelsConfig,
    *,
    repeat: int = 1,
    max_usd: float = DEFAULT_MAX_USD,
    on_run: Callable[[PlanRun], None] | None = None,
) -> list[PlanRun]:
    """Plan ``query`` ``repeat`` times with each model, in order. Each run may spend only what
    the runs before it left of ``max_usd``: its request spend cap is set to that."""
    runs: list[PlanRun] = []
    for model in models:
        for _ in range(repeat):
            left = max_usd - spent_usd(runs, settings.call_reserve_usd)
            runs.append(run_plan(runtime, model, query, settings, config, max_usd=left))
            if on_run is not None:
                on_run(runs[-1])
    return runs


def run_plan(
    runtime: Any,
    model: str,
    query: str,
    settings: Settings,
    config: ModelsConfig,
    *,
    max_usd: float,
) -> PlanRun:
    """``plan_stage`` for ``query`` as the first request of an empty chat, planned by ``model``
    through ``runtime``, with the worker's call admission, retries and records."""
    capture = CapturingRuntime(runtime)
    deps = WorkerDeps(
        repos=Repositories.in_memory(),
        blobs=MemoryBlobStore(),
        model=BedrockModel(capture),
        budgets=load_budgets(),
        tools={},
        queue=InProcessQueue(),
        price_version=config.price_version,
        prices=config.prices,
        routes={Stage.PLAN: ModelRoute(model)},  # one model per run: no fallbacks
        settings=settings.model_copy(update={"request_max_usd": max_usd}),
    )
    request = _request(query)
    deps.repos.messages.save(request)
    planned, failure = None, None
    started = time.monotonic()
    try:
        planned = plan_stage(initial_brief(request, memory_snapshot(None)), 0, deps)
    except PlanFailed as exc:
        failure = exc.reason
    seconds = time.monotonic() - started
    records = deps.repos.model_calls.list_by_request(request.message_id)
    return PlanRun(model, planned, failure, seconds, capture.calls, records)


def _request(query: str) -> RequestMessage:
    now = now_iso()
    return RequestMessage(
        message_id=new_ulid(),
        chat_id="plan-bakeoff",
        user_id="plan-bakeoff",
        sender=Sender.USER,
        kind=MessageKind.REQUEST,
        content=query,
        status=RequestStatus.RUNNING,
        created_at=now,
        updated_at=now,
    )


def spent_usd(runs: Sequence[PlanRun], reserve_usd: float) -> float:
    """What ``runs`` spent as the request spend cap counts it: a call without a cost is charged
    the reserve."""
    return sum(
        r.estimated_cost_usd if r.estimated_cost_usd is not None else reserve_usd
        for run in runs
        for r in run.records
    )


def run_cost(run: PlanRun, config: ModelsConfig) -> float | None:
    """The run's cost at ``config``'s prices; None when the model has no price or a call's usage
    is unknown."""
    costs = [cost_usd(config.prices, run.model, r.usage) for r in run.records]
    return None if None in costs else sum(c for c in costs if c is not None)


def answered(run: PlanRun) -> list[ConverseCall]:
    return [call for call in run.calls if call.response is not None]


def first_answer_problem(run: PlanRun) -> str | None:
    """None when the model's first answer was a valid plan; otherwise what was wrong with it."""
    calls = answered(run)
    if not calls or calls[0].response is None:
        return "no answer"
    try:
        structured = parse_response(calls[0].response, expects_result=True).structured
        output = PlannerOutput.model_validate(structured)
    except ModelError as exc:
        return str(exc)
    except ValidationError as exc:
        return f"invalid plan: {one_line(exc)}"
    return None if output.kind == "plan" else f"asked a question: {output.question}"


def call_path(call: ConverseCall) -> str:
    """How the call asked for the result and how the model gave it, e.g.
    "forced tool → reasoning, tool call"."""
    tool_config = call.payload.get("toolConfig")
    shape = "schema in prompt"
    if tool_config is not None:
        shape = "forced tool" if "toolChoice" in tool_config else "tool offered"
    if "temperature" not in call.payload["inferenceConfig"]:
        shape += ", no temperature"
    kinds = [_block_kind(block) for block in _content(call.response)]
    answer = [
        label
        for label, kind in (
            ("reasoning", "reasoningContent"),
            ("tool call", "toolUse"),
            ("text", "text"),
        )
        if kind in kinds
    ]
    return f"{shape} → {', '.join(answer) or 'no content'}"


def run_path(run: PlanRun) -> str:
    paths = list(dict.fromkeys(call_path(call) for call in answered(run)))
    return "; ".join(paths) or "–"


def dependency_depth(tasks: Sequence[dict[str, Any]]) -> int:
    """Tasks on the longest ``depends_on`` chain. A validated plan has no cycle."""
    after = {task["ref"]: task["depends_on"] for task in tasks}

    def depth(ref: str) -> int:
        return 1 + max((depth(dependency) for dependency in after[ref]), default=0)

    return max((depth(ref) for ref in after), default=0)


def _content(response: dict[str, Any] | None) -> list[dict[str, Any]]:
    return list((response or {}).get("output", {}).get("message", {}).get("content", []))


def _block_kind(block: dict[str, Any]) -> str:
    """A content block's kind; a botocore that predates a kind names it under
    SDK_UNKNOWN_MEMBER (live: reasoningContent from gpt-oss and MiniMax)."""
    kind = next(iter(block), "empty")
    return str(block[kind].get("name", kind)) if kind == "SDK_UNKNOWN_MEMBER" else kind


def probe(runtime: Any, model: str) -> Probe:
    """A three-token call: whether this account may invoke ``model``. Bedrock refuses a model
    the account may not use before any inference, so a refused probe costs nothing."""
    try:
        raw = runtime.converse(
            modelId=model, messages=_PROBE_MESSAGES, inferenceConfig={"maxTokens": 3}
        )
    except ClientError as exc:
        return Probe(model, _probe_result(exc), _failure(exc))
    except BotoCoreError as exc:
        return Probe(model, "error", _failure(exc))
    usage = raw.get("usage", {})
    detail = f"{usage.get('inputTokens')} in / {usage.get('outputTokens')} out tokens"
    return Probe(model, "available", detail)


def _probe_result(exc: ClientError) -> str:
    """The probe's verdict on a refusal, read from the whole message: live, a legacy model's
    ResourceNotFoundException said "Access denied", and Kimi K3 answered "The model returned the
    following errors" because three output tokens is below its minimum."""
    code = error_code(exc)
    message = str(exc.response.get("Error", {}).get("Message", "")).lower()
    if code == "AccessDeniedException" or re.search(r"access (?:is )?denied", message):
        return "denied"
    if "the model returned" in message:
        return "available"
    if code == "ResourceNotFoundException" or "identifier is invalid" in message:
        return "not found"
    if "on-demand throughput" in message:
        return "profile only"
    return "error"


def listed_model_ids(bedrock: Any) -> list[str]:
    """Every id this region can invoke for a text model: the model id where it runs on demand,
    and each inference profile (au., apac., global. ...) over a text model."""
    models = bedrock.list_foundation_models(byOutputModality="TEXT")["modelSummaries"]
    text_models = {model["modelId"] for model in models}
    ids = {
        model["modelId"]
        for model in models
        if "ON_DEMAND" in model.get("inferenceTypesSupported", [])
    }
    for profile in _inference_profiles(bedrock):
        profile_id = profile["inferenceProfileId"]
        if profile_id.partition(".")[2] in text_models:
            ids.add(profile_id)
    return sorted(ids)


def _inference_profiles(bedrock: Any) -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    params: dict[str, Any] = {"maxResults": 100}
    while True:
        page = bedrock.list_inference_profiles(**params)
        profiles.extend(page.get("inferenceProfileSummaries", []))
        if not page.get("nextToken"):
            return profiles
        params["nextToken"] = page["nextToken"]


def _failure(exc: ClientError | BotoCoreError) -> str:
    if isinstance(exc, ClientError):
        return f"{error_code(exc)}: {_aws_message(exc)}"
    return str(exc)


def _aws_message(exc: ClientError) -> str:
    """The AWS error message cut to its first sentences (at least 40 characters, so "Access
    denied." keeps its reason), with any IAM principal left out."""
    first, *rest = str(exc.response.get("Error", {}).get("Message", "")).split(". ")
    for sentence in rest:
        if len(first) >= 40:
            break
        first += ". " + sentence
    return _PRINCIPAL.sub("<principal>", first).rstrip(".")


@dataclass(frozen=True)
class Bakeoff:
    """Everything one report shows."""

    query: str
    query_file: str
    live: bool
    max_usd: float
    probes: list[Probe]
    runs: list[PlanRun]
    # The dry pass over the same models, priced before any live call; None without --models.
    expected: list[PlanRun] | None


def render(bakeoff: Bakeoff, config: ModelsConfig) -> str:
    """The report: how it was run, the probe table, the summary table, one section per run."""
    parts = [_header(bakeoff, config)]
    if bakeoff.probes:
        parts.append(_probe_table(bakeoff.probes))
    if bakeoff.runs:
        parts.append(_summary_table(bakeoff.runs, config))
        parts.extend(_run_section(run, config) for run in bakeoff.runs)
    return "\n\n".join(parts) + "\n"


def _header(bakeoff: Bakeoff, config: ModelsConfig) -> str:
    today = local_today(user_timezone(memory_snapshot(None)))
    mode = (
        "live Bedrock Converse calls"
        if bakeoff.live
        else "dry run: no calls; every model answers a fixed sample plan, usage is estimated"
    )
    lines = [
        f"# Planner bake-off, {today.isoformat()}",
        "",
        f"- Mode: {mode}.",
        f"- Plan stage as the worker runs it: prompt {prompt_version(Stage.PLAN)}, the context "
        "builder, the PlannerOutput schema through the result tool, today's date from the "
        "resolver, the default memory (no user instructions or preferences), no sources, so no "
        "question may be asked.",
        f"- Prices: config/models.yaml, price version {config.price_version}, USD per million "
        "tokens.",
    ]
    if bakeoff.expected is not None:
        lines.append(
            f"- Expected cost, stated before the run: {_total(bakeoff.expected, config)} (a dry "
            f"pass: estimated input tokens, {DRY_RUN_OUTPUT_TOKENS} output tokens per call). "
            f"Cap: ${bakeoff.max_usd:.2f} through the request spend cap."
        )
    if bakeoff.live and bakeoff.runs:
        lines.append(f"- Spent: {_total(bakeoff.runs, config)}.")
    if bakeoff.runs:
        quoted = "\n".join(f"> {line}" if line else ">" for line in bakeoff.query.splitlines())
        lines += ["", f"Query (`{bakeoff.query_file}`):", "", quoted]
    return "\n".join(lines)


def _total(runs: Sequence[PlanRun], config: ModelsConfig) -> str:
    """The runs' summed cost, naming the models whose cost is unknown."""
    costs = [(run.model, run_cost(run, config)) for run in runs]
    known = sum(cost for _, cost in costs if cost is not None)
    unknown = list(dict.fromkeys(model for model, cost in costs if cost is None))
    return f"${known:.4f}" + (f" plus unknown for {', '.join(unknown)}" if unknown else "")


def _probe_table(probes: Sequence[Probe]) -> str:
    rows = [f"| `{p.model}` | {p.result} | {_cell(p.detail)} |" for p in probes]
    return "\n".join(["## Probe", "", "| Model id | Result | Detail |", "|---|---|---|", *rows])


_SUMMARY_HEAD = (
    "| Model | Outcome | Valid first try | Path | Tasks | Depth | Calls | Latency (s) "
    "| Tokens in / out | Cost (USD) | Stop |"
)


def _summary_table(runs: Sequence[PlanRun], config: ModelsConfig) -> str:
    rows = [_summary_row(run, config) for run in runs]
    return "\n".join(["## Summary", "", _SUMMARY_HEAD, "|---" * 11 + "|", *rows])


def _summary_row(run: PlanRun, config: ModelsConfig) -> str:
    tasks = run.planned["tasks"] if run.planned else []
    usage = sum_known_usage(r.usage for r in run.records)
    cost = run_cost(run, config)
    calls = answered(run)
    cells = [
        f"`{run.model}`",
        _outcome(run),
        "yes" if first_answer_problem(run) is None else "no",
        run_path(run),
        str(len(tasks)) if tasks else "–",
        str(dependency_depth(tasks)) if tasks else "–",
        str(len(run.records)),
        f"{run.seconds:.1f}",
        f"{usage.input_tokens} / {usage.output_tokens}" if usage else "unknown",
        f"{cost:.4f}" if cost is not None else "unknown",
        str(calls[-1].response.get("stopReason")) if calls and calls[-1].response else "–",
    ]
    return "| " + " | ".join(_cell(c) for c in cells) + " |"


def _outcome(run: PlanRun) -> str:
    if run.planned is None:
        return "failed"
    return "plan, after a question" if run.planned["assumption"] else "plan"


def _run_section(run: PlanRun, config: ModelsConfig) -> str:
    lines = [f"## {run.model}", ""]
    if run.planned is None:
        lines.append(f"**Failed:** {run.failure}")
    else:
        lines += _plan_lines(run.planned)
    problem = first_answer_problem(run)
    if problem is not None and run.calls:
        lines += ["", f"**First answer:** {problem}"]
    for number, call in enumerate(run.calls, start=1):
        lines += ["", *_call_lines(number, call)]
    return "\n".join(lines)


def _plan_lines(planned: PlanResult) -> list[str]:
    lines = [
        f"**Goal:** {planned['goal']}",
        "",
        f"**Constraints:** {'; '.join(planned['constraints']) or 'none'}",
        "",
        f"**Open questions:** {'; '.join(planned['open_questions']) or 'none'}",
    ]
    if planned["assumption"]:
        lines += ["", f"**Assumption:** {planned['assumption']}"]
    lines += ["", f"**Next action:** {planned['next_action']}", ""]
    for number, task in enumerate(planned["tasks"], start=1):
        after = f" (after {', '.join(task['depends_on'])})" if task["depends_on"] else ""
        reads = (
            f" Reads {', '.join(task['input_artifact_ids'])}." if task["input_artifact_ids"] else ""
        )
        lines.append(f"{number}. **{task['ref']}** `{task['kind']}`: {task['goal']}{after}{reads}")
    return lines


def _call_lines(number: int, call: ConverseCall) -> list[str]:
    if call.response is None:
        return [f"Call {number} refused: {call.refusal}"]
    stop = call.response.get("stopReason")
    lines = [f"<details><summary>Call {number}: {call_path(call)}, stop {stop}</summary>", ""]
    for block in _content(call.response):
        lines += [*_block_lines(block), ""]
    return [*lines, "</details>"]


def _block_lines(block: dict[str, Any]) -> list[str]:
    if "toolUse" in block:
        body, language = json.dumps(block["toolUse"].get("input"), indent=2), "json"
    elif "text" in block:
        body, language = block["text"], "text"
    else:
        return [f"({_block_kind(block)} block, not shown)"]
    return [f"````{language}", body, "````"]


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def _progress(run: PlanRun, config: ModelsConfig) -> str:
    cost = run_cost(run, config)
    priced = f"${cost:.4f}" if cost is not None else "cost unknown"
    outcome = _outcome(run) if run.planned else f"failed: {run.failure}"
    return f"{run.model}: {outcome}, {run.seconds:.1f} s, {priced}"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Planner bake-off over Bedrock models.")
    parser.add_argument("--probe", help="Comma-separated model ids, or 'listed'.")
    parser.add_argument("--models", help="Comma-separated model ids to plan with.")
    parser.add_argument("--query-file", type=Path, help="The request text to plan.")
    parser.add_argument("--repeat", type=int, default=1, help="Runs per model.")
    parser.add_argument("--dry-run", action="store_true", help="No calls: a canned plan.")
    parser.add_argument("--max-usd", type=float, default=DEFAULT_MAX_USD, help="Run spend cap.")
    parser.add_argument("--out", type=Path, help="Report path (default docs/evals/...).")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.probe and not args.models:
        parser.error("give --probe, --models or both")
    if args.models and args.query_file is None:
        parser.error("--models needs --query-file")
    if args.probe and args.dry_run:
        parser.error("a probe is a Bedrock call; it has no dry run")
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    config = load_models_config(REPO_ROOT / "config" / "models.yaml")
    # A dry run reads no .env: it needs no AWS profile and must not depend on local settings.
    settings = Settings(_env_file=None) if args.dry_run else Settings()
    session = (
        None
        if args.dry_run
        else boto3.Session(profile_name=settings.aws_profile, region_name=settings.aws_region)
    )
    runtime = DryRunRuntime() if session is None else _planner_runtime(session, settings)
    probes = _probes(session, runtime, args.probe) if args.probe else []
    runs: list[PlanRun] = []
    expected: list[PlanRun] | None = None
    query = args.query_file.read_text(encoding="utf-8").strip() if args.query_file else ""
    if args.models:
        models = _ids(args.models)
        expected = run_bakeoff(
            DryRunRuntime(), models, query, settings, config, repeat=args.repeat, max_usd=math.inf
        )
        print(f"Expected cost {_total(expected, config)}; cap ${args.max_usd:.2f}.")
        runs = expected
        if session is not None:
            runs = run_bakeoff(
                runtime,
                models,
                query,
                settings,
                config,
                repeat=args.repeat,
                max_usd=args.max_usd,
                on_run=lambda run: print(_progress(run, config)),
            )
    bakeoff = Bakeoff(
        query=query,
        query_file=str(args.query_file or ""),
        live=session is not None,
        max_usd=args.max_usd,
        probes=probes,
        runs=runs,
        expected=expected,
    )
    out = args.out or REPO_ROOT / "docs" / "evals" / f"plan-bakeoff-{date.today().isoformat()}.md"
    out.write_text(render(bakeoff, config), encoding="utf-8")
    print(f"Wrote {out}")


def _planner_runtime(session: Any, settings: Settings) -> Any:
    """bedrock-runtime as the worker builds it for the planner: the planner's read timeout and
    no SDK retries, since BudgetedModel's bounded retry is the only one."""
    config = Config(
        connect_timeout=5,
        read_timeout=settings.planner_timeout_seconds,
        retries={"total_max_attempts": 1},
    )
    return session.client("bedrock-runtime", config=config)


def _probes(session: Any, runtime: Any, ids: str) -> list[Probe]:
    models = listed_model_ids(session.client("bedrock")) if ids == "listed" else _ids(ids)
    probes = []
    for model in models:
        probes.append(probe(runtime, model))
        print(f"probe {model}: {probes[-1].result}")
    return probes


def _ids(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


if __name__ == "__main__":
    main()

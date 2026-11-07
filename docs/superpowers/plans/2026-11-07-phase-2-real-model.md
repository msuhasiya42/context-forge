# Phase 2: Real model, tracing, first eval — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The phase 1 engine runs on Amazon Nova through one Bedrock Converse adapter with bounded retries, per-call timeouts, per-task-kind deadlines and a $1.00 spend cap; every call is priced and traced in LangSmith; and an eval harness scores golden-001 (phase 2 scope) and two negative companions, live or offline from recorded transcripts, in one line.

**Architecture:** `core` gains a settings module, a model error hierarchy, a price table with per-call cost, and a `ModelTracer` Protocol. A new `infra` package holds the Bedrock Converse adapter (pure request/response mapping plus a thin boto3 client) and the LangSmith tracer. In `worker`, one `GuardedModelClient` is the only way a stage calls a model: it runs the cancel, call-budget and spend-cap guards before every attempt and retries throttling, 5xx and timeouts once with full-jitter backoff, each attempt its own ModelCall. The eval lives in `src/contextforge/evals/` (cases, checks, trace assertions, judge, harness, replay, CLI) with `evals/run.py` as the entry point and case data under `evals/`.

**Tech Stack:** Phase 1 stack plus `boto3`/`botocore` (Converse API; `botocore.stub.Stubber` in tests), `boto3-stubs[bedrock-runtime]` (dev, types only), `langsmith`, `pydantic-settings`.

**Spec:** `docs/implementation-plan.md` (3.5 ModelCall, 9 to 9.3, 12 and 12.1), `docs/system-design.md` (8 last bullet, 10, 11, 14 row "2 Real model"), `evals/cases/golden-001.md`, and `.superpowers/milestone-2/brief.md` (decisions already made). Read them before any task. The phase 1 plan (`2026-11-04-phase-1-engine.md`) says what exists.

## Global Constraints

- Every phase 1 constraint still holds: Python 3.12, `uv` with a committed `uv.lock`, `ruff`, `ruff format`, `mypy --strict` clean and `pytest` green before every commit; naming per implementation-plan 12.1 (snake_case Python, camelCase in YAML, JSON and on the wire, enum values exactly the spec strings, lower snake_case for new enum-like values).
- Provider: AWS Bedrock, region `ap-southeast-2`, Converse API only, one adapter for every model. Defaults (AWS Free Plan, no Anthropic models): plan, cite and the eval judge on `apac.amazon.nova-pro-v1:0`; execute and compress on `apac.amazon.nova-lite-v1:0`. The Claude ids stay as comments in `config/models.yaml` (`au.anthropic.claude-opus-5-5` for plan and cite, `au.anthropic.claude-haiku-5-5` for execute and compress) and switch in when the plan is upgraded.
- Credentials: the boto3 default chain (the user's `aws configure` profile locally, an IAM role when hosted). Never an access key in code or `.env`. Third-party keys (`LANGSMITH_API_KEY`) come from `.env` through pydantic-settings; `.env.example` lists every setting with no secret value.
- Tracing: `LANGSMITH_TRACING`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT=contextforge`. `ModelCall.langsmithRunId` is filled when tracing is on. Budget checks never read LangSmith. One masking hook, in `infra/langsmith.py`.
- Limits come only from `config/budgets.yaml`, enforced in code, never in prompts. Grill values (5 Nov 2026): per-call timeout 120 s on the strong model stages (plan, cite, evaluate) and 60 s on the cheap ones (execute, compress); `$1.00` spend cap per request with unknown usage charged at the stage estimate; deadlines per task kind.
- Error classes (system-design 8): validation, authentication and access errors are never retried; throttling, 5xx and timeouts are retried once with backoff and jitter; each attempt is its own ModelCall. botocore's own retries are off so ours is the only retry.
- `Usage` is null when the provider did not report it; unknown is never zero. Cache read and write columns are recorded whenever Bedrock reports them. A TIMEOUT ModelCall has `usage: null` and no `endedAt` (spec 3.5).
- Prices are versioned in `config/models.yaml`; bump `priceVersion` whenever a price changes. Nova prices and inference profile ids are verified from AWS pages at implementation time (Task 2) and recorded with the date; nothing in this plan is a verified price.
- Paid calls happen only in the opt-in live smoke test and the live eval run, both gated on `CONTEXTFORGE_LIVE=1` read from the process environment (never from `.env`). The default suite makes no network call: adapter tests use a real botocore client under `Stubber` (no moto, no network), tracing tests run with a mock LangSmith client, and an autouse fixture turns tracing off.
- Trace assertions (`mustCall`, `mustNot`, `maxCalls`, `maxUsd`) look at task kinds and tool names only, never call order. golden-001 phase 2 limits: `maxCalls: 30`, `maxUsd: 0.40`, numeric tolerance zero.
- Two kinds of replay, kept apart: a checkpoint-restored response (`ModelResponse.replayed=True`) never creates a ModelCall; an eval replay response stands in for a provider call, so it is returned with `replayed=False` and recorded with its saved usage.
- Testing style: tests alongside each task, few and clubbed, integration-first, one test module per task; DRY; small single-purpose functions (about 40 lines at most).
- Shared files (other milestone-2 phases edit them too): `worker/dependencies.py`, `tests/helpers.py`, `config/*.yaml`, `pyproject.toml`. Edits there stay additive: new keys, new fields with defaults, new lines; no renames or removals.

## Review Focus

1. A compress call whose messages hold tool calls and tool results but offers no tools: Converse rejects toolUse and toolResult blocks without a `toolConfig`, so the adapter must render those turns as text and the summary call must succeed (Task 3).
2. Turn order Converse refuses: several ToolMessages followed by the runtime nudge, the compaction summary right after the task turn, a compress transcript that starts with an assistant turn. The adapter must merge same-role turns and open with a user turn so every real loop is accepted (Task 3).
3. A call that times out: its ModelCall is TIMEOUT with null usage and no `endedAt`, is charged the stage estimate against the spend cap, is retried once as a second ModelCall, and a second timeout fails the stage with a reason instead of a third attempt (Tasks 2 and 5).
4. Replay under parallel waves: each recorded response must return to the task at the same plan position whatever order the threads run in, and a recording made under another prompt version or another model must refuse to replay rather than score stale answers (Task 10).
5. A real planner that fails the gate (an artifact not in the chat, seven tasks twice) or a run that raises: that case scores zero with the reason, the other cases still run, and the score line still prints (Task 9).

---

## File structure

```
pyproject.toml                                   + boto3, botocore, langsmith, pydantic-settings; dev boto3-stubs[bedrock-runtime]
.env.example                                     every .env setting, no secret values
config/budgets.yaml                              + call timeouts per stage, retries, backoff, spend cap, deadlines per task kind
config/models.yaml                               Nova by default, verified Nova prices, structuredOutput per model, evaluate stage
src/contextforge/core/settings.py                Settings (pydantic-settings over .env), load_settings, live_calls_allowed
src/contextforge/core/context/budgets.py         + the phase 2 limit fields
src/contextforge/core/context/prompts.py         + context_version (shared by recorder and replay)
src/contextforge/core/models/errors.py           ModelError, ModelThrottled, ModelUnavailable, ModelTimeout, ModelRejected
src/contextforge/core/models/pricing.py          cost_usd, CallPricing, call_pricing, spent_usd
src/contextforge/core/models/config.py           + Price, StructuredMode, structured_output
src/contextforge/core/models/base.py             + ModelTracer Protocol
src/contextforge/core/models/scripted.py         ScriptExhausted becomes a ModelError
src/contextforge/core/models/recording.py        + cost, outcome per error class, TIMEOUT without endedAt, run id
src/contextforge/infra/__init__.py
src/contextforge/infra/bedrock/__init__.py
src/contextforge/infra/bedrock/mapping.py        ModelRequest -> Converse request; Converse response -> ModelResponse
src/contextforge/infra/bedrock/client.py         BedrockConverseClient, as_model_error, bedrock_model_client
src/contextforge/infra/langsmith.py              LangSmithTracer, trace_metadata/inputs/outputs (masking hook), configure_tracing
src/contextforge/worker/dependencies.py                  + pricing, tracer fields
src/contextforge/worker/stages/guards.py         + LimitExceeded, SpendCapExceeded, DeadlineExceeded, check_spend_cap, check_deadline
src/contextforge/worker/stages/calls.py          backoff_seconds, GuardedModelClient
src/contextforge/worker/stages/plan.py           guarded planner call; ModelError and LimitExceeded replace the ScriptExhausted catch
src/contextforge/worker/stages/execute.py        guarded execute and compress calls; task deadline
src/contextforge/evals/__init__.py
src/contextforge/evals/cases.py                  Check, TraceAssertions, EvalCase, load_case, load_cases
src/contextforge/evals/checks.py                 CheckResult, ToolUse, run_checks, check_trace
src/contextforge/evals/judge.py                  JudgeVerdict, judge_report, failed_verdicts, Labels, label_agreement
src/contextforge/evals/score.py                  CaseResult, failed_result, case_lines, score_line
src/contextforge/evals/harness.py                RecordedTool, TaskIndex, CaseRun, case_deps, seed_case, run_case, evaluate_case
src/contextforge/evals/replay.py                 Transcript, TranscriptRecorder, ReplayModelClient, ReplayMiss
src/contextforge/evals/cli.py                    main
evals/run.py                                     CLI entry: uv run python evals/run.py [--live --record] [--case ID] [--verbose]
evals/judge.md                                   judge prompt
evals/cases/golden-001.yaml                      phase 2 scope of golden-001 on the 60-row fixture
evals/cases/golden-001-flat.yaml                 negative companion: nothing went up
evals/cases/golden-001-two-causes.yaml           negative companion: two causes of the dining increase
evals/recordings/<caseId>.json                   recorded transcripts (Task 11, live)
evals/labels/<caseId>.yaml                       the user's own rubric verdicts (after Task 11)
fixtures/variants/flat/transactions_sep_oct.csv
fixtures/variants/two-causes/transactions_sep_oct.csv
tests/evals_support.py                           eval test builders (kept out of the shared tests/helpers.py)
tests/test_settings.py  test_pricing.py  test_converse_mapping.py  test_bedrock_client.py  test_bedrock_live.py
tests/test_model_guards.py  test_tracing.py  test_eval_checks.py  test_judge.py  test_eval_harness.py
tests/test_eval_replay.py  test_eval_recordings.py
```

---

### Task 1: Settings, dependencies and the phase 2 limits

**Files:**
- Modify: `pyproject.toml`, `uv.lock`, `config/budgets.yaml`, `src/contextforge/core/context/budgets.py`, `tests/test_skeleton.py`
- Create: `src/contextforge/core/settings.py`, `.env.example`
- Test: `tests/test_settings.py`

**Interfaces:**
- Produces:
  - `settings.Settings(BaseSettings)` with `model_config = SettingsConfigDict(env_file_encoding="utf-8", extra="ignore", populate_by_name=True)` and fields read by `validation_alias`: `aws_region: str = "ap-southeast-2"` (`CONTEXTFORGE_AWS_REGION`), `aws_profile: str | None = None` (`CONTEXTFORGE_AWS_PROFILE`), `trace_mask: bool = False` (`CONTEXTFORGE_TRACE_MASK`), `langsmith_tracing: bool = False` (`LANGSMITH_TRACING`), `langsmith_api_key: SecretStr | None = None` (`LANGSMITH_API_KEY`), `langsmith_project: str = "contextforge"` (`LANGSMITH_PROJECT`).
  - `settings.DOTENV_PATH` (repo-root `.env`, resolved from the file like `BUDGETS_PATH`); `settings.load_settings(env_file: Path | None = DOTENV_PATH) -> Settings` (`Settings(_env_file=env_file)`; process environment wins over `.env`).
  - `settings.live_calls_allowed() -> bool`: `os.environ.get("CONTEXTFORGE_LIVE") == "1"`. Read from the process only, never `.env`, so a paid call is opted into per command.
  - `Budgets` gains `model_call_timeout_seconds: Mapping[str, int]` (keyed by stage value), `model_retries: int`, `retry_base_delay_seconds: float`, `retry_max_delay_seconds: float`, `spend_cap_usd: float`, `deadline_seconds: Mapping[str, int]` (keyed by task kind value). `budgets.MODEL_STAGES = ("plan", "execute", "cite", "compress", "evaluate")`. The loader requires a timeout for every name in `MODEL_STAGES` and a deadline for every `TaskKind` value, integers only (a `_seconds(raw, key, names)` helper beside `_int`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_settings.py
from pathlib import Path

import pytest

from contextforge.core.context.budgets import load_budgets
from contextforge.core.settings import live_calls_allowed, load_settings

_VARS = ["CONTEXTFORGE_AWS_REGION", "CONTEXTFORGE_AWS_PROFILE", "CONTEXTFORGE_TRACE_MASK",
         "LANGSMITH_TRACING", "LANGSMITH_API_KEY", "LANGSMITH_PROJECT", "CONTEXTFORGE_LIVE"]


def test_defaults_then_dotenv_then_process_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _VARS:
        monkeypatch.delenv(name, raising=False)
    s = load_settings(tmp_path / "missing.env")
    assert (s.aws_region, s.langsmith_tracing, s.langsmith_project) == ("ap-southeast-2", False, "contextforge")
    assert s.langsmith_api_key is None and s.aws_profile is None
    env = tmp_path / ".env"
    env.write_text("LANGSMITH_API_KEY=ls-test\nLANGSMITH_TRACING=true\nCONTEXTFORGE_LIVE=1\n")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    s = load_settings(env)
    assert s.langsmith_api_key is not None and s.langsmith_api_key.get_secret_value() == "ls-test"
    assert s.langsmith_tracing is False and "ls-test" not in repr(s)
    assert live_calls_allowed() is False          # CONTEXTFORGE_LIVE in .env never enables paid calls
    monkeypatch.setenv("CONTEXTFORGE_LIVE", "1")
    assert live_calls_allowed() is True


def test_env_example_lists_every_setting_without_secrets() -> None:
    lines = [ln for ln in Path(".env.example").read_text().splitlines() if ln and not ln.startswith("#")]
    pairs = dict(ln.split("=", 1) for ln in lines)
    assert set(pairs) == {"CONTEXTFORGE_AWS_REGION", "CONTEXTFORGE_AWS_PROFILE", "CONTEXTFORGE_TRACE_MASK",
                          "LANGSMITH_TRACING", "LANGSMITH_API_KEY", "LANGSMITH_PROJECT"}
    assert pairs["LANGSMITH_API_KEY"] == "" and pairs["CONTEXTFORGE_AWS_PROFILE"] == ""


def test_phase2_limits_load_from_budgets() -> None:
    b = load_budgets()
    assert b.model_call_timeout_seconds == {"plan": 120, "cite": 120, "evaluate": 120,
                                            "execute": 60, "compress": 60}
    assert (b.model_retries, b.retry_base_delay_seconds, b.retry_max_delay_seconds) == (1, 1.0, 8.0)
    assert b.spend_cap_usd == 1.00
    assert b.deadline_seconds == {"sql_analysis": 300, "pdf_lookup": 300, "source_search": 180,
                                  "reconcile": 120}
```

Extend `tests/test_skeleton.py::test_budgets_yaml_has_spec_values` with the six new keys and their values above.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_settings.py tests/test_skeleton.py -v`
Expected: FAIL, `ModuleNotFoundError: contextforge.core.settings`.

- [ ] **Step 3: Add the dependencies, settings, `.env.example` and limits**

`uv add "boto3>=1.40" "botocore>=1.40" "langsmith>=0.3" "pydantic-settings>=2.6"` and `uv add --dev "boto3-stubs[bedrock-runtime]>=1.40"` (the stubs are imported only under `TYPE_CHECKING`). `.env.example` holds the six names, `CONTEXTFORGE_AWS_REGION=ap-southeast-2`, `LANGSMITH_PROJECT=contextforge`, `LANGSMITH_TRACING=false`, `CONTEXTFORGE_TRACE_MASK=false`, the other two empty, with a comment that `CONTEXTFORGE_LIVE=1` is set per command, never here. Append to `config/budgets.yaml` (additive):

```yaml
# Model calls (phase 2). Timeout per call by stage: 120 s on the strong model, 60 s on the cheap one.
modelCallTimeoutSeconds: { plan: 120, cite: 120, evaluate: 120, execute: 60, compress: 60 }
# Throttling, 5xx and timeouts are retried this many times, with full-jitter backoff.
modelRetries: 1
retryBaseDelaySeconds: 1.0
retryMaxDelaySeconds: 8.0
# Per request, USD. A call with unknown usage is charged its stage's worst-case estimate.
spendCapUsd: 1.00
# Wall clock per task kind, its automatic retry included.
deadlineSeconds: { sql_analysis: 300, pdf_lookup: 300, source_search: 180, reconcile: 120 }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -v && uv run ruff check . && uv run ruff format --check . && uv run mypy src`
Expected: all tests pass (3 new); clean.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock .env.example config/budgets.yaml src/contextforge/core tests/test_settings.py tests/test_skeleton.py
git commit -m "feat: settings from .env and the phase 2 limits in budgets"
```

---

### Task 2: Model errors, the price table and a cost on every ModelCall

**Files:**
- Create: `src/contextforge/core/models/errors.py`, `src/contextforge/core/models/pricing.py`
- Modify: `src/contextforge/core/models/scripted.py`, `config.py`, `recording.py`; `src/contextforge/core/context/prompts.py`; `config/models.yaml`; `src/contextforge/worker/dependencies.py`; `tests/helpers.py`; `tests/test_models.py`, `tests/test_skeleton.py` (the two assertions that pinned `scripted` and `2026-11-04`)
- Test: `tests/test_pricing.py`

**Interfaces:**
- Consumes: `Budgets` (Task 1), `ModelCall`, `Usage`, `Outcome`.
- Produces:
  - `errors.ModelError(Exception)` with class attributes `retryable: ClassVar[bool] = False` and `outcome: ClassVar[Outcome] = Outcome.ERROR`; subclasses `ModelThrottled` (retryable), `ModelUnavailable` (retryable: 5xx, connection), `ModelTimeout` (retryable, `outcome = Outcome.TIMEOUT`), `ModelRejected` (not retryable: validation, access, authentication, content filtered).
  - `scripted.ScriptExhausted(ModelError)` (not retryable).
  - `config.Price(Entity)`: `input_per_mtok: float`, `output_per_mtok: float`, `cache_read_per_mtok: float = 0.0`, `cache_write_per_mtok: float = 0.0`. `config.StructuredMode = Literal["tool", "json"]`. `ModelsConfig.prices: dict[str, Price]`; `ModelsConfig.structured_output: dict[str, StructuredMode] = {}` (YAML `structuredOutput`).
  - `pricing.cost_usd(usage: Usage, price: Price) -> float` = (input × in + output × out + cache_read × cache read + cache_write × cache write) / 1,000,000, missing cache columns counted as 0.
  - `pricing.CallPricing` (frozen dataclass): `prices: Mapping[str, Price]`, `unknown_usage_usd: Mapping[str, float]` (by stage value); `charge(self, stage: Stage, model: str, usage: Usage | None) -> float`: `cost_usd` when usage is known and the model is priced, else the stage estimate.
  - `pricing.call_pricing(models: ModelsConfig, budgets: Budgets) -> CallPricing`: each configured stage's estimate is `cost_usd(Usage(input_tokens=budgets.cap_input_tokens, output_tokens=stage.max_tokens), prices[stage.model])` (the worst case one call of that stage can bill); `ValueError` naming the stage when its model has no price.
  - `pricing.spent_usd(calls: Iterable[ModelCall]) -> float`: sum of `estimated_cost_usd`; a record without one (phase 1, unpriced) counts 0.0.
  - `prompts.context_version(memory_version: str, stage: Stage) -> str` = `f"mem:{memory_version}|prompt:{prompt_version(stage)}"`; the recorder uses it instead of its inline f-string.
  - `RecordingModelClient(..., pricing: CallPricing | None = None, run_id: Callable[[], str | None] | None = None)` (new keyword arguments; existing callers unchanged): `estimated_cost_usd = pricing.charge(stage, request.model, usage)` when `pricing` is given; on an exception `outcome = exc.outcome` for a `ModelError`, else `ERROR`; `ended_at` is None when the outcome is TIMEOUT; `langsmith_run_id = run_id()` when given (the call to `run_id` happens inside `invoke`, after the inner call returns or raises).
  - `WorkerDependencies.pricing: CallPricing | None = None` (new field); `model_for` passes it to the recorder. `tests/helpers.make_deps` computes `budgets` once and adds `pricing=call_pricing(models, budgets)`, so every test run is priced.

- [ ] **Step 1: Verify and record the Nova ids and prices**

Inference profile ids: Bedrock console, region ap-southeast-2, "Cross-region inference", or `aws bedrock list-inference-profiles --region ap-southeast-2 --query "inferenceProfileSummaries[?contains(inferenceProfileId,'nova')].inferenceProfileId"`; confirm `apac.amazon.nova-pro-v1:0` and `apac.amazon.nova-lite-v1:0` exist (if the ids differ, use the listed ones everywhere this plan names them). Prices: https://aws.amazon.com/bedrock/pricing/ → Amazon → Nova, the row that applies to the Sydney source region (cross-region profiles bill at the source region's price); record input, output, cache read and cache write per million tokens. Write them into `config/models.yaml`:

```yaml
priceVersion: "<the verification date, e.g. 2026-11-08>"
# USD per million tokens, Bedrock on-demand, source region ap-southeast-2.
# Nova rows verified <date> at https://aws.amazon.com/bedrock/pricing/ (APAC profiles bill at the source region).
# Claude rows from 6 Nov 2026; Anthropic models are not available on the Free Plan.
prices:
  apac.amazon.nova-pro-v1:0:  { inputPerMTok: <page>, outputPerMTok: <page>, cacheReadPerMTok: <page>, cacheWritePerMTok: <page or 0.0> }
  apac.amazon.nova-lite-v1:0: { inputPerMTok: <page>, outputPerMTok: <page>, cacheReadPerMTok: <page>, cacheWritePerMTok: <page or 0.0> }
  anthropic.claude-opus-5-5:  { inputPerMTok: 4.00, outputPerMTok: 20.00, cacheReadPerMTok: 0.20 }
  anthropic.claude-haiku-5-5: { inputPerMTok: 0.10, outputPerMTok: 0.50,  cacheReadPerMTok: 0.01 }
  scripted:                   { inputPerMTok: 0.0,  outputPerMTok: 0.0,   cacheReadPerMTok: 0.0 }
# How each model returns a structured answer: tool (a result tool) or json (text parsed as JSON).
structuredOutput:
  apac.amazon.nova-pro-v1:0: tool
  apac.amazon.nova-lite-v1:0: tool
stages:
  plan:     { model: apac.amazon.nova-pro-v1:0,  effort: high,   maxTokens: 4000 }   # plan upgrade: au.anthropic.claude-opus-5-5
  execute:  { model: apac.amazon.nova-lite-v1:0, effort: medium, maxTokens: 4000 }   # plan upgrade: au.anthropic.claude-haiku-5-5
  cite:     { model: apac.amazon.nova-pro-v1:0,  effort: high,   maxTokens: 4000 }   # plan upgrade: au.anthropic.claude-opus-5-5
  compress: { model: apac.amazon.nova-lite-v1:0, effort: low,    maxTokens: 1000 }   # plan upgrade: au.anthropic.claude-haiku-5-5
  evaluate: { model: apac.amazon.nova-pro-v1:0,  effort: high,   maxTokens: 1000 }   # the eval judge
```

For orientation only (not a verified value): US-region list prices in 2025 were about 0.80/3.20 for Nova Pro and 0.06/0.24 for Nova Lite; record what the page says today.

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_pricing.py
import pytest

from contextforge.core.context.budgets import load_budgets
from contextforge.core.entities.model_call import Stage
from contextforge.core.models.base import ModelRequest, ModelResponse
from contextforge.core.models.config import Price, load_models_config
from contextforge.core.models.errors import (ModelError, ModelRejected, ModelThrottled, ModelTimeout,
                                             ModelUnavailable)
from contextforge.core.models.pricing import call_pricing, cost_usd, spent_usd
from contextforge.core.models.recording import RecordingModelClient
from contextforge.core.models.scripted import ScriptExhausted
from contextforge.core.repositories.memory import Repositories
from contextforge.core.values import Usage


def test_nova_is_the_default_and_every_stage_model_is_priced() -> None:
    models = load_models_config()
    pro, lite = "apac.amazon.nova-pro-v1:0", "apac.amazon.nova-lite-v1:0"
    assert [models.stages[s].model for s in ("plan", "cite", "evaluate")] == [pro] * 3
    assert [models.stages[s].model for s in ("execute", "compress")] == [lite] * 2
    assert models.price_version >= "2026-11-07"
    for stage in models.stages.values():
        price = models.prices[stage.model]
        assert 0 < price.input_per_mtok < price.output_per_mtok
    assert models.structured_output == {pro: "tool", lite: "tool"}


def test_cost_counts_cache_columns_and_unknown_usage_is_the_stage_worst_case() -> None:
    price = Price(input_per_mtok=1.0, output_per_mtok=4.0, cache_read_per_mtok=0.25, cache_write_per_mtok=1.25)
    usage = Usage(input_tokens=1_000_000, output_tokens=500_000, cache_read_tokens=2_000_000,
                  cache_write_tokens=400_000)
    assert cost_usd(usage, price) == pytest.approx(1.0 + 2.0 + 0.5 + 0.5)
    models = load_models_config()
    plan = models.stages["plan"]
    p = models.prices[plan.model]
    worst = (40000 * p.input_per_mtok + 4000 * p.output_per_mtok) / 1_000_000
    pricing = call_pricing(models, load_budgets())
    assert pricing.charge(Stage.PLAN, plan.model, None) == pytest.approx(worst)
    assert pricing.charge(Stage.PLAN, plan.model, Usage(input_tokens=0, output_tokens=0)) == 0.0


def test_recorder_prices_calls_and_records_a_timeout_without_usage_or_end() -> None:
    models, repositories = load_models_config(), Repositories.in_memory()
    pricing = call_pricing(models, load_budgets())
    usage = Usage(input_tokens=1000, output_tokens=100, cache_read_tokens=800)

    class TimesOutOnce:
        calls = 0

        def invoke(self, request: ModelRequest) -> ModelResponse:
            self.calls += 1
            if self.calls == 1:
                raise ModelTimeout("read timed out after 120 s")
            return ModelResponse(structured={}, usage=usage)

    client = RecordingModelClient(TimesOutOnce(), repositories.model_calls, request_message_id="m1",
                                  memory_version="1", price_version=models.price_version,
                                  pricing=pricing, run_id=lambda: "run-1")
    req = ModelRequest(stage="plan", model=models.stages["plan"].model, system=["s"], messages=[])
    with pytest.raises(ModelTimeout):
        client.invoke(req)
    client.invoke(req)
    by_outcome = {c.outcome: c for c in repositories.model_calls.list_by_request("m1")}
    timeout, done = by_outcome["TIMEOUT"], by_outcome["COMPLETED"]
    assert (timeout.usage, timeout.ended_at) == (None, None)
    assert timeout.estimated_cost_usd == pytest.approx(pricing.charge(Stage.PLAN, req.model, None))
    assert done.ended_at is not None and done.langsmith_run_id == "run-1"
    assert done.estimated_cost_usd == pytest.approx(cost_usd(usage, models.prices[req.model]))
    assert spent_usd([timeout, done]) == pytest.approx(timeout.estimated_cost_usd + done.estimated_cost_usd)


def test_error_classes() -> None:
    assert ModelThrottled.retryable and ModelUnavailable.retryable and ModelTimeout.retryable
    assert not ModelRejected.retryable and not ScriptExhausted.retryable
    assert ModelTimeout.outcome == "TIMEOUT" and ModelRejected.outcome == "ERROR"
    assert issubclass(ScriptExhausted, ModelError)
```

In `tests/test_models.py::test_models_config_loads_price_version_and_stages` and `tests/test_skeleton.py`, replace the pinned `2026-11-04` and `scripted` with the Task 2 values (the phase 1 recorder tests that pass `price_version="2026-11-04"` stay as they are).

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_pricing.py -v`
Expected: FAIL, `ModuleNotFoundError: contextforge.core.models.errors`.

- [ ] **Step 4: Implement errors, pricing, the config types, the recorder changes, the deps field and the helper line**

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest -v && uv run ruff check . && uv run ruff format --check . && uv run mypy src`
Expected: all pass (4 new); clean. Phase 1 tests still pass under real Nova pricing: unknown usage is charged per call but no test reaches $1.00.

- [ ] **Step 6: Commit**

```bash
git add config/models.yaml src/contextforge/core src/contextforge/worker/dependencies.py tests
git commit -m "feat: model error classes, versioned Nova prices and a cost on every ModelCall"
```

---

### Task 3: Converse request and response mapping

**Files:**
- Create: `src/contextforge/infra/__init__.py`, `src/contextforge/infra/bedrock/__init__.py`, `src/contextforge/infra/bedrock/mapping.py`
- Test: `tests/test_converse_mapping.py`

**Interfaces:**
- Consumes: `ModelRequest`, `ModelResponse`, `ToolSpec`, `ToolCall`, `Usage`, `StructuredMode`, `ModelRejected`.
- Produces (all pure):
  - `TEMPERATURE = 0` (every call; eval reproducibility), `RESULT_TOOL_PREFIX = "return_"`, `result_tool_name(schema: type[BaseModel]) -> str` = `"return_" + schema.__name__`.
  - `inline_refs(schema: dict[str, Any]) -> dict[str, Any]`: every `{"$ref": "#/$defs/X"}` replaced by its definition, recursively; `$defs` dropped.
  - `converse_request(request: ModelRequest, mode: StructuredMode) -> dict[str, Any]` with keys `modelId`, `system` (`[{"text": block}]` in order), `messages`, `inferenceConfig` (`{"maxTokens": request.max_tokens, "temperature": TEMPERATURE}`) and, only when tools are offered, `toolConfig`. Tools: one `{"toolSpec": {"name", "description", "inputSchema": {"json": ...}}}` per `ToolSpec`, plus, when `output_schema` is set and `mode == "tool"`, the result tool with `inline_refs(output_schema.model_json_schema(by_alias=True))`; sorted by name (the tool set is part of the cache key). `toolChoice`: `{"tool": {"name": <result tool>}}` when the result tool is the only tool; `{"auto": {}}` when real tools are offered (never forced while the model may still call tools, phase 1 rule). With `mode == "json"` there is no result tool; `system` gains a last block `"Return only a JSON object that validates against this JSON Schema:\n" + json.dumps(schema)`.
  - `converse_messages(messages: Sequence[AnyMessage], *, tools_offered: bool) -> list[dict[str, Any]]`: `HumanMessage` and `SystemMessage` become user text blocks; `AIMessage` an assistant turn with its text (when non-empty) then one `toolUse` (`toolUseId`, `name`, `input`) per tool call; `ToolMessage` a user `toolResult` (`toolUseId`, `content: [{"text": content}]`). When `tools_offered` is False, a tool call renders as the text `f"[called {name} {json.dumps(args, sort_keys=True)}]"` and a result as `f"[result {tool_call_id}] {content}"`. Consecutive same-role turns merge into one message; a first turn that is not the user's gets a user turn `"(the conversation so far)"` before it.
  - `parse_json_object(text: str | None) -> dict[str, Any] | None`: the text from its first `{` to its last `}` parsed as JSON; None when absent or invalid (covers fenced code blocks).
  - `model_response(raw: Mapping[str, Any], request: ModelRequest, mode: StructuredMode) -> ModelResponse`: `text` = joined text blocks or None (reasoning blocks ignored); `tool_calls` = every `toolUse` except the result tool; when the result tool was called, `structured` = its input, the other calls are dropped and `stop_reason = "end_turn"`; otherwise, when `output_schema` is set and no tool was called, `structured = parse_json_object(text)`; `stop_reason` = `"tool_use"` when tool calls remain, `"max_tokens"` when Bedrock says so, else `"end_turn"`; Bedrock `guardrail_intervened` or `content_filtered` raises `ModelRejected(f"bedrock stopped: {reason}")`; `usage` from `inputTokens`, `outputTokens`, `cacheReadInputTokens`, `cacheWriteInputTokens` (absent cache keys stay None), or None when the response has no `usage`.

- [ ] **Step 1: Verify the Nova facts this mapping relies on**

From the Bedrock user guide ("Supported models and model features" for the Converse API) and the Amazon Nova user guide (https://docs.aws.amazon.com/nova/latest/userguide/, "Tool use" and "Complete request schema"), confirm for Nova Pro and Nova Lite: Converse with system prompts and tool use; the `toolChoice` values accepted (`auto`, `any`, `tool`); the JSON Schema features accepted in `inputSchema` (nested objects, `anyOf` with `null`; `$ref` is inlined here regardless); the accepted `temperature` range; whether Converse reports `cacheReadInputTokens`/`cacheWriteInputTokens` for Nova. Consequences, decided now: if forced `tool` choice is not accepted, set `structuredOutput: json` for that model in `config/models.yaml` (no code change); if `temperature: 0` is rejected, set `TEMPERATURE` to the smallest accepted value with a comment citing the page. Note the findings in the commit message.

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_converse_mapping.py
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from contextforge.core.entities.task import TaskResult
from contextforge.core.models.base import ModelRequest
from contextforge.core.models.errors import ModelRejected
from contextforge.core.planner import PlannerOutput
from contextforge.core.values import Usage
from contextforge.infra.bedrock.mapping import converse_request, model_response
from contextforge.worker.tools.sql_query import SqlQueryTool

SQL = SqlQueryTool.spec
LITE = "apac.amazon.nova-lite-v1:0"


def _calls() -> AIMessage:
    return AIMessage(content="", tool_calls=[
        {"id": "tu1", "name": "sql_query", "args": {"dataset_id": "a2", "sql": "SELECT 1"}},
        {"id": "tu2", "name": "sql_query", "args": {"dataset_id": "a2", "sql": "SELECT 2"}}])


def _loop() -> list:
    return [HumanMessage("task t1"), SystemMessage("Summary of earlier work: found 2 pairs"), _calls(),
            ToolMessage(content="showing 1 of 1 rows", tool_call_id="tu1"),
            ToolMessage(content="showing 0 of 0 rows", tool_call_id="tu2"),
            HumanMessage("[runtime] one tool call remains; return your structured result after it")]


def _raw(content: list, stop: str = "tool_use", usage: dict | None = None) -> dict:
    raw = {"output": {"message": {"role": "assistant", "content": content}}, "stopReason": stop,
           "metrics": {"latencyMs": 900}}
    return raw | ({"usage": usage} if usage else {})


def test_planner_request_forces_the_result_tool_with_an_inlined_camel_case_schema() -> None:
    req = ModelRequest(stage="plan", model="apac.amazon.nova-pro-v1:0", system=["MEM", "PLAN"],
                       messages=[HumanMessage("<request>r</request>")], output_schema=PlannerOutput)
    body = converse_request(req, "tool")
    assert body["system"] == [{"text": "MEM"}, {"text": "PLAN"}]
    assert body["inferenceConfig"] == {"maxTokens": 4000, "temperature": 0}
    (tool,) = body["toolConfig"]["tools"]
    assert tool["toolSpec"]["name"] == "return_PlannerOutput"
    assert body["toolConfig"]["toolChoice"] == {"tool": {"name": "return_PlannerOutput"}}
    schema = json.dumps(tool["toolSpec"]["inputSchema"]["json"])
    assert "$ref" not in schema and "$defs" not in schema and "inputArtifactIds" in schema
    as_json = converse_request(req, "json")
    assert "toolConfig" not in as_json and as_json["system"][-1]["text"].startswith("Return only a JSON object")


def test_execute_request_offers_tools_unforced_and_alternates_roles() -> None:
    req = ModelRequest(stage="execute", model=LITE, system=["MEM", "EXEC"], messages=_loop(),
                       tools=[SQL], output_schema=TaskResult, task_id="t1")
    body = converse_request(req, "tool")
    assert [t["toolSpec"]["name"] for t in body["toolConfig"]["tools"]] == ["return_TaskResult", "sql_query"]
    assert body["toolConfig"]["toolChoice"] == {"auto": {}}
    first, assistant, last = body["messages"]
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]
    assert [b["text"] for b in first["content"]] == ["task t1", "Summary of earlier work: found 2 pairs"]
    assert [b["toolUse"]["toolUseId"] for b in assistant["content"]] == ["tu1", "tu2"]
    assert [next(iter(b)) for b in last["content"]] == ["toolResult", "toolResult", "text"]


def test_compress_request_renders_tool_turns_as_text_and_opens_with_a_user_turn() -> None:
    loop = _loop()[2:5]                           # assistant tool calls and both results, no user turn first
    req = ModelRequest(stage="compress", model=LITE, system=["MEM", "COMPRESS"], messages=loop, max_tokens=1000)
    body = converse_request(req, "tool")
    flat = json.dumps(body["messages"])
    assert "toolConfig" not in body and "toolUse" not in flat and "toolResult" not in flat
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]
    assert '[called sql_query {\\"dataset_id\\": \\"a2\\", \\"sql\\": \\"SELECT 1\\"}]' in flat


def test_result_tool_wins_and_usage_carries_cache_columns() -> None:
    req = ModelRequest(stage="execute", model=LITE, system=[], messages=[HumanMessage("x")],
                       tools=[SQL], output_schema=TaskResult)
    raw = _raw([{"text": "done"},
                {"toolUse": {"toolUseId": "tu9", "name": "sql_query", "input": {"dataset_id": "a2", "sql": "SELECT 3"}}},
                {"toolUse": {"toolUseId": "tu10", "name": "return_TaskResult", "input": {"summary": "s"}}}],
               usage={"inputTokens": 1200, "outputTokens": 80, "totalTokens": 1280,
                      "cacheReadInputTokens": 1000, "cacheWriteInputTokens": 0})
    resp = model_response(raw, req, "tool")
    assert (resp.structured, resp.tool_calls, resp.stop_reason) == ({"summary": "s"}, [], "end_turn")
    assert resp.usage == Usage(input_tokens=1200, output_tokens=80, cache_read_tokens=1000, cache_write_tokens=0)


def test_tool_use_json_fallback_truncation_and_filtering() -> None:
    req = ModelRequest(stage="execute", model=LITE, system=[], messages=[HumanMessage("x")],
                       tools=[SQL], output_schema=TaskResult)
    use = model_response(_raw([{"toolUse": {"toolUseId": "tu9", "name": "sql_query",
                                            "input": {"dataset_id": "a2", "sql": "SELECT 3"}}}]), req, "tool")
    assert use.stop_reason == "tool_use" and use.tool_calls[0].arguments["sql"] == "SELECT 3" and use.usage is None
    fenced = model_response(_raw([{"text": 'Result:\n```json\n{"summary": "s"}\n```'}], "end_turn"), req, "json")
    assert fenced.structured == {"summary": "s"}
    cut = model_response(_raw([{"text": '{"summ'}], "max_tokens"), req, "tool")
    assert cut.stop_reason == "max_tokens" and cut.structured is None
    with pytest.raises(ModelRejected):
        model_response(_raw([{"text": ""}], "content_filtered"), req, "tool")
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_converse_mapping.py -v`
Expected: FAIL, `ModuleNotFoundError: contextforge.infra`.

- [ ] **Step 4: Implement `mapping.py`**

One small function per rule (tool specs, tool choice, one message, merge, lead, usage, stop reason); `converse_request` and `model_response` only compose them.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_converse_mapping.py -v && uv run mypy src && uv run ruff check .`
Expected: 5 passed, clean.

- [ ] **Step 6: Commit**

```bash
git add src/contextforge/infra tests/test_converse_mapping.py config/models.yaml
git commit -m "feat: Converse mapping with result-tool structured output and strict turn order"
```

---

### Task 4: Bedrock Converse client, error classes and the live smoke test

**Files:**
- Create: `src/contextforge/infra/bedrock/client.py`
- Test: `tests/test_bedrock_client.py`, `tests/test_bedrock_live.py`

**Interfaces:**
- Consumes: Task 3 mapping; Task 2 errors; `Settings`, `Budgets`, `ModelsConfig`.
- Produces:
  - `BedrockConverseClient(runtimes: Mapping[str, BedrockRuntimeClient], structured: Mapping[str, StructuredMode])` implementing `ModelClient`. `runtime_for(stage: str) -> BedrockRuntimeClient`. `invoke(request)`: mode = `structured.get(request.model, "tool")`; `raw = runtime_for(request.stage.value).converse(**converse_request(request, mode))` (cast to `Mapping[str, Any]` at this one boundary); returns `model_response(raw, request, mode)`; a `ClientError` or `BotoCoreError` is re-raised as `as_model_error(exc)` with the original chained. Other exceptions propagate unchanged (they are bugs, not provider failures). `BedrockRuntimeClient` comes from `mypy_boto3_bedrock_runtime` under `TYPE_CHECKING` only.
  - `as_model_error(exc: ClientError | BotoCoreError) -> ModelError`, message `f"{code}: {message}"`, never the request payload. `ThrottlingException` → `ModelThrottled`; `ModelTimeoutException` → `ModelTimeout`; `ServiceUnavailableException`, `InternalServerException`, `ModelNotReadyException` or any HTTP status ≥ 500 → `ModelUnavailable`; any other `ClientError` (`ValidationException`, `AccessDeniedException`, `ResourceNotFoundException`, `ServiceQuotaExceededException`, `ModelErrorException`, …) → `ModelRejected`; `ReadTimeoutError`, `ConnectTimeoutError` → `ModelTimeout`; `EndpointConnectionError` → `ModelUnavailable`; any other `BotoCoreError` (`NoCredentialsError`, …) → `ModelRejected`.
  - `bedrock_model_client(settings: Settings, models: ModelsConfig, budgets: Budgets) -> BedrockConverseClient`: one `boto3.Session(profile_name=settings.aws_profile, region_name=settings.aws_region)`; one `bedrock-runtime` client per distinct value in `budgets.model_call_timeout_seconds`, each with `botocore.config.Config(connect_timeout=t, read_timeout=t, retries={"total_max_attempts": 1})`; each stage mapped to the client of its timeout; `structured=models.structured_output`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_bedrock_client.py
import boto3
import pytest
from botocore.exceptions import EndpointConnectionError, NoCredentialsError, ReadTimeoutError
from botocore.stub import Stubber
from langchain_core.messages import HumanMessage

from contextforge.core.context.budgets import MODEL_STAGES, load_budgets
from contextforge.core.entities.task import TaskResult
from contextforge.core.models.base import ModelRequest
from contextforge.core.models.config import load_models_config
from contextforge.core.models.errors import ModelRejected, ModelThrottled, ModelTimeout, ModelUnavailable
from contextforge.core.models.pricing import call_pricing
from contextforge.core.models.recording import RecordingModelClient
from contextforge.core.repositories.memory import Repositories
from contextforge.core.settings import Settings
from contextforge.infra.bedrock.client import BedrockConverseClient, as_model_error, bedrock_model_client
from contextforge.worker.tools.sql_query import SqlQueryTool

TOOL_USE = {
    "output": {"message": {"role": "assistant", "content": [{"toolUse": {
        "toolUseId": "tu1", "name": "sql_query", "input": {"dataset_id": "a2", "sql": "SELECT 1"}}}]}},
    "stopReason": "tool_use",
    "usage": {"inputTokens": 1200, "outputTokens": 40, "totalTokens": 1240,
              "cacheReadInputTokens": 1000, "cacheWriteInputTokens": 0},
    "metrics": {"latencyMs": 812},
}


def _runtime():  # a real botocore client; Stubber answers before anything is sent
    return boto3.client("bedrock-runtime", region_name="ap-southeast-2",
                        aws_access_key_id="test", aws_secret_access_key="test")


def _request() -> ModelRequest:
    return ModelRequest(stage="execute", model="apac.amazon.nova-lite-v1:0", system=["MEM"],
                        messages=[HumanMessage("task")], tools=[SqlQueryTool.spec],
                        output_schema=TaskResult, task_id="t1")


def test_stubbed_call_is_mapped_and_recorded_with_cache_usage_and_cost() -> None:
    runtime, repositories = _runtime(), Repositories.in_memory()
    client = BedrockConverseClient(dict.fromkeys(MODEL_STAGES, runtime), {})
    recorded = RecordingModelClient(client, repositories.model_calls, request_message_id="m1", memory_version="1",
                                    price_version="v", pricing=call_pricing(load_models_config(), load_budgets()))
    with Stubber(runtime) as stub:
        stub.add_response("converse", TOOL_USE)
        resp = recorded.invoke(_request())
    assert resp.stop_reason == "tool_use" and resp.tool_calls[0].name == "sql_query"
    (call,) = repositories.model_calls.list_by_request("m1")
    assert call.usage is not None and call.usage.cache_read_tokens == 1000 and call.estimated_cost_usd > 0


@pytest.mark.parametrize(("code", "status", "error"), [
    ("ThrottlingException", 429, ModelThrottled), ("ServiceUnavailableException", 503, ModelUnavailable),
    ("InternalServerException", 500, ModelUnavailable), ("ModelTimeoutException", 408, ModelTimeout),
    ("ValidationException", 400, ModelRejected), ("AccessDeniedException", 403, ModelRejected),
    ("ResourceNotFoundException", 404, ModelRejected)])
def test_client_errors_map_to_their_retry_class(code: str, status: int, error: type) -> None:
    runtime = _runtime()
    client = BedrockConverseClient(dict.fromkeys(MODEL_STAGES, runtime), {})
    with Stubber(runtime) as stub:
        stub.add_client_error("converse", service_error_code=code, service_message="m", http_status_code=status)
        with pytest.raises(error) as raised:
            client.invoke(_request())
    assert raised.value.retryable is (error is not ModelRejected)


def test_transport_failures_and_factory_settings() -> None:
    assert isinstance(as_model_error(ReadTimeoutError(endpoint_url="https://bedrock")), ModelTimeout)
    assert isinstance(as_model_error(EndpointConnectionError(endpoint_url="https://bedrock")), ModelUnavailable)
    assert isinstance(as_model_error(NoCredentialsError()), ModelRejected)
    client = bedrock_model_client(Settings(_env_file=None), load_models_config(), load_budgets())
    plan, execute = client.runtime_for("plan"), client.runtime_for("execute")
    assert (plan.meta.config.read_timeout, execute.meta.config.read_timeout) == (120, 60)
    assert plan.meta.config.retries == {"total_max_attempts": 1} and plan.meta.region_name == "ap-southeast-2"
    assert client.runtime_for("cite") is plan
```

```python
# tests/test_bedrock_live.py — paid calls; runs only with CONTEXTFORGE_LIVE=1 in the shell
import pytest
from langchain_core.messages import HumanMessage

from contextforge.core.context.budgets import load_budgets
from contextforge.core.context.prompts import load_prompt
from contextforge.core.entities.task import TaskResult
from contextforge.core.models.base import ModelRequest
from contextforge.core.models.config import load_models_config
from contextforge.core.planner import PlannerOutput
from contextforge.core.settings import live_calls_allowed, load_settings
from contextforge.infra.bedrock.client import bedrock_model_client
from contextforge.worker.tools.sql_query import SqlQueryTool

pytestmark = pytest.mark.skipif(not live_calls_allowed(), reason="paid Bedrock call; set CONTEXTFORGE_LIVE=1")
SOURCES = ("<sources>a2  dataset  transactions_sep_oct.csv  rows: 60, columns: row_id, txn_date, amount, "
           "direction, merchant, account_id, category, description</sources>")


def _invoke(stage: str, user: str, **extra: object):
    models = load_models_config()
    prompt = {"plan": "plan.1", "execute": "execute.1"}[stage]
    request = ModelRequest(stage=stage, model=models.stages[stage].model,
                           system=[load_prompt("agent-memory"), load_prompt(prompt)],
                           messages=[HumanMessage(user)], max_tokens=models.stages[stage].max_tokens, **extra)
    return bedrock_model_client(load_settings(), models, load_budgets()).invoke(request)


def test_nova_pro_plans_through_the_forced_result_tool() -> None:
    resp = _invoke("plan", "<request>Compare October spending with September.</request>\n" + SOURCES,
                   output_schema=PlannerOutput)
    PlannerOutput.model_validate(resp.structured)
    assert resp.usage is not None and resp.usage.input_tokens > 0


def test_nova_lite_calls_sql_query_when_offered() -> None:
    resp = _invoke("execute", "<task>Total DEBIT amount in a2 for October 2026.</task>\n" + SOURCES,
                   tools=[SqlQueryTool.spec], output_schema=TaskResult, task_id="t1")
    assert resp.stop_reason == "tool_use" and resp.tool_calls[0].name == "sql_query"
    assert resp.tool_calls[0].arguments["dataset_id"] == "a2"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_bedrock_client.py tests/test_bedrock_live.py -v`
Expected: FAIL, `ModuleNotFoundError: contextforge.infra.bedrock.client`; the live module is skipped.

- [ ] **Step 3: Implement `client.py`**

If `Stubber` rejects `cacheReadInputTokens` in `TOOL_USE`, the installed botocore predates those fields: raise the floor in `pyproject.toml` rather than dropping the keys.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_bedrock_client.py tests/test_bedrock_live.py -v && uv run mypy src && uv run ruff check .`
Expected: 9 passed, 2 skipped; clean.

- [ ] **Step 5: Run the live smoke once (needs Prerequisites 1 to 3)**

Run: `CONTEXTFORGE_LIVE=1 uv run pytest tests/test_bedrock_live.py -v`
Expected: 2 passed, spending well under one cent. A failure here that names toolChoice, temperature or a schema keyword is the Task 3 Step 1 fallback: fix it in `config/models.yaml` or `TEMPERATURE`, rerun.

- [ ] **Step 6: Commit**

```bash
git add src/contextforge/infra/bedrock/client.py tests/test_bedrock_client.py tests/test_bedrock_live.py
git commit -m "feat: Bedrock Converse client with error classes, per-stage timeouts and a live smoke test"
```

---

### Task 5: Guarded model calls: retries with backoff, the spend cap and task deadlines

**Files:**
- Create: `src/contextforge/worker/stages/calls.py`
- Modify: `src/contextforge/worker/stages/guards.py`, `src/contextforge/worker/stages/plan.py`, `src/contextforge/worker/stages/execute.py`
- Test: `tests/test_model_guards.py`

**Interfaces:**
- Consumes: `ModelError` and its classes, `spent_usd`, `Budgets` phase 2 fields, `WorkerDependencies.model_for`.
- Produces:
  - `guards.LimitExceeded(Exception)`; `CallBudgetExceeded`, `SpendCapExceeded` and `DeadlineExceeded` subclass it. `guards.DEADLINE_EXCEEDED = "deadline exceeded"`.
  - `guards.check_spend_cap(deps: WorkerDependencies, request_message_id: str) -> None`: raises `SpendCapExceeded(f"spend cap of ${cap:.2f} reached: ${spent:.4f} spent")` when `spent_usd(...) >= deps.budgets.spend_cap_usd` (one call may cross the cap; the next is refused).
  - `guards.check_deadline(deadline_at: float, what: str) -> None`: raises `DeadlineExceeded(f"{DEADLINE_EXCEEDED}: {what}")` when `time.monotonic() >= deadline_at`.
  - `calls.backoff_seconds(attempt: int, base: float, cap: float, rng: random.Random) -> float` = `rng.uniform(0, min(cap, base * 2 ** (attempt - 1)))` (full jitter).
  - `calls.GuardedModelClient(deps: WorkerDependencies, chat_id: str, request_message_id: str, *, sleep: Callable[[float], None] = time.sleep, rng: random.Random | None = None)` implementing `ModelClient`: at most `1 + budgets.model_retries` attempts; before each, `check_not_cancelled`, `check_call_budget`, `check_spend_cap`; each attempt is `deps.model_for(request_message_id).invoke(request)`, so each is its own ModelCall; a retryable `ModelError` with attempts left sleeps `backoff_seconds(attempt, budgets.retry_base_delay_seconds, budgets.retry_max_delay_seconds, rng)` and tries again; anything else propagates.
  - `plan._call_planner` invokes through `GuardedModelClient` (its explicit guard calls move into it); `plan._plan_or_fail` maps `LimitExceeded` to `PlanFailed(f"model budget exhausted: {exc}")` and `ModelError` to `PlanFailed(f"planner model call failed: {exc}")`. The `ScriptExhausted` import and catch go.
  - `execute.TaskContext.deadline_at: float = math.inf` (new field). `_run_model_tool_loop` sets it once, `time.monotonic() + budgets.deadline_seconds[task.kind.value]`, so the deadline covers the retry, and does not retry an error starting with `DEADLINE_EXCEEDED`. `call_model` calls `check_deadline(run.deadline_at, run.task.kind.value)` first inside its `try`, uses one `GuardedModelClient` for both the compaction summary and the execute call, and catches `(LimitExceeded, ModelError)` instead of `(CallBudgetExceeded, ScriptExhausted)`. `Cancelled` still propagates. Deadlines are checked before model calls, not inside a running tool.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_model_guards.py
import dataclasses
import random

import pytest

from contextforge.core.models.base import ModelRequest, ModelResponse
from contextforge.core.models.config import load_models_config
from contextforge.core.models.errors import ModelError, ModelRejected, ModelThrottled, ModelTimeout
from contextforge.core.models.pricing import cost_usd
from contextforge.core.models.scripted import ScriptedModel
from contextforge.core.values import Usage
from contextforge.worker.dependencies import WorkerDependencies
from contextforge.worker.stages.calls import backoff_seconds
from contextforge.worker.workflow import build_research_workflow
from tests.helpers import make_deps, seed_chat_with_dataset, six_task_plan, t1_result


class Flaky(ScriptedModel):
    """Raises the queued errors of a stage, one per call, before answering from the script."""

    def __init__(self, script: dict[str, list[ModelResponse]], errors: dict[str, list[ModelError]]) -> None:
        super().__init__(script)
        self.errors = {stage: list(queued) for stage, queued in errors.items()}

    def invoke(self, request: ModelRequest) -> ModelResponse:
        queued = self.errors.get(request.stage.value)
        if queued:
            raise queued.pop(0)
        return super().invoke(request)


def _script(**plan_extra: object) -> dict[str, list[ModelResponse]]:
    return {"plan": [ModelResponse(structured=six_task_plan(), **plan_extra)],
            "execute": [ModelResponse(structured=t1_result())] * 3}


def _run(worker_dependencies: WorkerDependencies) -> tuple[str, str]:
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    worker_dependencies.repositories.messages.update_request_status("c1", req.message_id, "ACCEPTED", "RUNNING")
    worker_dependencies.budgets = dataclasses.replace(worker_dependencies.budgets, retry_base_delay_seconds=0.0)
    out = build_research_workflow(worker_dependencies, checkpointer=None).invoke(
        {"chat_id": "c1", "message_id": req.message_id}, {"configurable": {"thread_id": req.message_id}})
    return out["status"], req.message_id


def test_throttled_planner_is_retried_once_and_each_attempt_is_a_priced_model_call() -> None:
    worker_dependencies = make_deps(Flaky(_script(), {"plan": [ModelThrottled("ThrottlingException: slow down")]}))
    status, rid = _run(worker_dependencies)
    plans = [c for c in worker_dependencies.repositories.model_calls.list_by_request(rid) if c.stage == "plan"]
    assert status == "COMPLETED" and sorted(c.outcome for c in plans) == ["COMPLETED", "ERROR"]
    assert all(c.estimated_cost_usd is not None and c.estimated_cost_usd > 0 for c in plans)


@pytest.mark.parametrize(("errors", "outcome"), [
    ([ModelRejected("AccessDeniedException: no model access")], "ERROR"),
    ([ModelTimeout("read timed out"), ModelTimeout("read timed out again")], "TIMEOUT")])
def test_rejected_or_twice_timed_out_planner_fails_with_a_reason(errors: list[ModelError], outcome: str) -> None:
    worker_dependencies = make_deps(Flaky(_script(), {"plan": errors}))
    status, rid = _run(worker_dependencies)
    calls = worker_dependencies.repositories.model_calls.list_by_request(rid)
    assert status == "FAILED" and len(calls) == len(errors) and {c.outcome for c in calls} == {outcome}
    assert all(c.ended_at is None for c in calls if c.outcome == "TIMEOUT")
    assert "planner model call failed" in (worker_dependencies.repositories.messages.get("c1", rid).failure_reason or "")


def test_spend_cap_counts_recorded_cost_and_refuses_the_next_call() -> None:
    usage = Usage(input_tokens=1000, output_tokens=100)
    worker_dependencies = make_deps(ScriptedModel(_script(usage=usage)))
    cap = cost_usd(usage, load_models_config().prices[worker_dependencies.planner_model])
    worker_dependencies.budgets = dataclasses.replace(worker_dependencies.budgets, spend_cap_usd=cap)
    status, rid = _run(worker_dependencies)
    assert status == "FAILED" and [c.stage for c in worker_dependencies.repositories.model_calls.list_by_request(rid)] == ["plan"]
    t1 = worker_dependencies.repositories.tasks.list_by_request(rid)[0]
    assert t1.status == "FAILED" and "spend cap" in (t1.failure_reason or "")


def test_task_deadline_fails_the_task_before_any_call_and_without_a_retry() -> None:
    worker_dependencies = make_deps(ScriptedModel(_script()))
    worker_dependencies.budgets = dataclasses.replace(
        worker_dependencies.budgets, deadline_seconds={**worker_dependencies.budgets.deadline_seconds, "sql_analysis": 0})
    _, rid = _run(worker_dependencies)
    sql = [t for t in worker_dependencies.repositories.tasks.list_by_request(rid) if t.kind == "sql_analysis"]
    assert all(t.status == "FAILED" and (t.failure_reason or "").startswith("deadline exceeded") for t in sql)
    assert not [c for c in worker_dependencies.repositories.model_calls.list_by_request(rid) if c.stage == "execute"]


def test_backoff_is_full_jitter_under_the_cap() -> None:
    rng = random.Random(7)
    first = [backoff_seconds(1, 1.0, 8.0, rng) for _ in range(50)]
    later = [backoff_seconds(a, 1.0, 8.0, rng) for a in (3, 4, 5, 6) for _ in range(50)]
    assert all(0 <= d <= 1.0 for d in first) and all(0 <= d <= 8.0 for d in later)
    assert len(set(first)) > 1 and max(later) > 1.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_model_guards.py -v`
Expected: FAIL, `ModuleNotFoundError: contextforge.worker.stages.calls`.

- [ ] **Step 3: Implement `calls.py`, the guards and the stage changes**

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -v && uv run mypy src && uv run ruff check .`
Expected: all pass (8 new); phase 1's `test_call_budget_stops_the_planner` and `test_scripted_exhaustion_fails_request_with_reason` still pass through the new catches; clean.

- [ ] **Step 5: Commit**

```bash
git add src/contextforge/worker/stages tests/test_model_guards.py
git commit -m "feat: guarded model calls with one jittered retry, spend cap and task deadlines"
```

---

### Task 6: LangSmith tracing with the run id on every ModelCall

**Files:**
- Create: `src/contextforge/infra/langsmith.py`
- Modify: `src/contextforge/core/models/base.py` (add `ModelTracer`), `src/contextforge/worker/dependencies.py`, `tests/conftest.py` (autouse fixture)
- Test: `tests/test_tracing.py`

**Interfaces:**
- Consumes: `RecordingModelClient(run_id=...)` (Task 2), `Settings`, `prompt_version`.
- Produces:
  - `base.ModelTracer` Protocol: `wrap(self, inner: ModelClient, request_message_id: str) -> ModelClient`; `current_run_id(self) -> str | None`.
  - `WorkerDependencies.tracer: ModelTracer | None = None` (new field). `model_for` builds the recorder with `run_id=self.tracer.current_run_id` when a tracer is set and returns `self.tracer.wrap(recorder, request_message_id)`; otherwise the bare recorder as before. The span is outside the recorder, so the recorder runs inside it and reads its id.
  - `langsmith.LangSmithTracer(client: langsmith.Client | None = None, *, mask: bool = False)` implementing `ModelTracer`. Its wrapped client calls the inner client directly when `langsmith.utils.tracing_is_enabled()` is false; otherwise opens `langsmith.trace(name=f"model.{stage}", run_type="llm", inputs=trace_inputs(request, mask=mask), metadata=trace_metadata(request, request_message_id), client=client)`, ends it with `trace_outputs(response)`, and lets an exception mark the run errored and propagate. `current_run_id()` is `str(langsmith.get_current_run_tree().id)` or None.
  - `trace_metadata(request: ModelRequest, request_message_id: str) -> dict[str, str]`: `requestMessageId`, `thread_id` (= request message id, so LangSmith groups a request's runs as one thread), `stage`, `model`, `promptVersion`, and `taskId` when set.
  - `trace_inputs(request: ModelRequest, *, mask: bool) -> dict[str, Any]` (the single masking hook): `{"system": [...], "messages": [{"role", "content"}], "tools": [names]}`; with `mask`, every system and message text becomes `f"[masked {len(text)} chars]"`.
  - `trace_outputs(response: ModelResponse) -> dict[str, Any]`: `text`, `structured`, `toolCalls` (names), `stopReason`, and `usage_metadata` (`input_tokens`, `output_tokens`, `total_tokens`) when usage is known.
  - `configure_tracing(settings: Settings) -> LangSmithTracer | None`: with `langsmith_tracing` and an API key, exports `LANGSMITH_TRACING=true`, `LANGSMITH_API_KEY` and `LANGSMITH_PROJECT` to `os.environ` (so LangGraph traces the workflow's own runs too) and returns `LangSmithTracer(mask=settings.trace_mask)`; otherwise None, logging a warning when tracing is on but the key is missing.
  - `tests/conftest.py` autouse fixture: `LANGSMITH_TRACING=false` and no `LANGSMITH_API_KEY` for every test, so a developer's shell never makes the default suite send traces.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tracing.py
import json
import os
import uuid
from unittest.mock import MagicMock

import langsmith
import pytest
from langchain_core.messages import HumanMessage

from contextforge.core.models.base import ModelRequest, ModelResponse
from contextforge.core.models.scripted import ScriptedModel
from contextforge.core.settings import Settings
from contextforge.infra.langsmith import LangSmithTracer, configure_tracing, trace_inputs, trace_metadata
from contextforge.worker.tools.sql_query import SqlQueryTool
from tests.helpers import make_deps


def test_metadata_and_the_masking_hook() -> None:
    req = ModelRequest(stage="execute", model="apac.amazon.nova-lite-v1:0", system=["MEM"],
                       messages=[HumanMessage("ACC-SAV-1 sent 25000")], tools=[SqlQueryTool.spec], task_id="t1")
    assert trace_metadata(req, "m1") == {"requestMessageId": "m1", "thread_id": "m1", "taskId": "t1",
                                         "stage": "execute", "model": "apac.amazon.nova-lite-v1:0",
                                         "promptVersion": "execute.1"}
    masked = trace_inputs(req, mask=True)
    assert "25000" not in json.dumps(masked) and masked["tools"] == ["sql_query"]
    assert "25000" in json.dumps(trace_inputs(req, mask=False))


def test_model_call_carries_the_run_id_only_while_tracing() -> None:
    worker_dependencies = make_deps(ScriptedModel({"plan": [ModelResponse(text="a"), ModelResponse(text="b")]}))
    worker_dependencies.tracer = LangSmithTracer(client=MagicMock(spec=langsmith.Client))
    req = ModelRequest(stage="plan", model=worker_dependencies.planner_model, system=["s"], messages=[HumanMessage("x")])
    with langsmith.tracing_context(enabled=False):
        assert worker_dependencies.model_for("m1").invoke(req).text == "a"
    with langsmith.tracing_context(enabled=True):
        assert worker_dependencies.model_for("m1").invoke(req).text == "b"
    run_ids = [c.langsmith_run_id for c in worker_dependencies.repositories.model_calls.list_by_request("m1")]
    assert run_ids.count(None) == 1
    uuid.UUID(next(r for r in run_ids if r is not None))


def test_configure_tracing_needs_a_key_and_exports_the_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert configure_tracing(Settings(_env_file=None, langsmith_tracing=True)) is None
    assert configure_tracing(Settings(_env_file=None, langsmith_tracing=False, langsmith_api_key="ls-test")) is None
    tracer = configure_tracing(Settings(_env_file=None, langsmith_tracing=True, langsmith_api_key="ls-test"))
    assert tracer is not None
    assert (os.environ["LANGSMITH_TRACING"], os.environ["LANGSMITH_PROJECT"]) == ("true", "contextforge")
```

The autouse fixture sets and deletes through `monkeypatch`, so the variables `configure_tracing` writes are restored after each test. If the installed `langsmith` will not post through a `MagicMock(spec=Client)`, build `langsmith.Client(api_key="test", auto_batch_tracing=False)` with its HTTP session mocked instead; the assertions stay.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_tracing.py -v`
Expected: FAIL, `ModuleNotFoundError: contextforge.infra.langsmith`.

- [ ] **Step 3: Implement `ModelTracer`, the deps field, `infra/langsmith.py` and the fixture**

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -v && uv run mypy src && uv run ruff check .`
Expected: all pass (3 new); clean.

- [ ] **Step 5: Commit**

```bash
git add src/contextforge/core/models/base.py src/contextforge/infra/langsmith.py src/contextforge/worker/dependencies.py tests/conftest.py tests/test_tracing.py
git commit -m "feat: LangSmith spans per model call with the run id on its ModelCall"
```

---

### Task 7: Eval cases, fixture variants, deterministic checks and trace assertions

**Files:**
- Create: `src/contextforge/evals/__init__.py`, `cases.py`, `checks.py`; `evals/cases/golden-001.yaml`, `golden-001-flat.yaml`, `golden-001-two-causes.yaml`; `fixtures/variants/flat/transactions_sep_oct.csv`, `fixtures/variants/two-causes/transactions_sep_oct.csv`; `tests/evals_support.py`
- Test: `tests/test_eval_checks.py`

**Interfaces:**
- Consumes: `ReportBody`, `Claim`, `EvidenceRef`, `ModelCall`, `Task`, `spent_usd`.
- Produces:
  - `cases.CheckKind = Literal["claim_numbers", "forbidden_numbers", "cited_rows", "every_claim_has_evidence", "claim_pattern"]`.
  - `cases.Check(Entity)`: `check_id: str`, `kind: CheckKind`, `numbers: list[float] = []`, `rows: list[int] = []`, `pattern: str | None = None`, `unless: str | None = None`, `must_match: bool = True`.
  - `cases.TraceAssertions(Entity)`: `must_call: list[str]`, `must_not: list[str]`, `max_calls: int`, `max_usd: float`.
  - `cases.EvalCase(Entity)`: `case_id`, `request`, `standing_instruction: str | None = None`, `clarification_answer: str`, `dataset_id: str`, `dataset_csv: str` (repo-relative), `checks: list[Check]`, `trace: TraceAssertions`, `rubric: list[str]`; `request_text() -> str` = the request, plus `"\n\nStanding instruction: " + standing_instruction` when set (phase 7 moves it to `User.instructions`); `dataset_path() -> Path` resolved from the repo root.
  - `cases.CASES_DIR`; `load_case(path: Path) -> EvalCase`; `load_cases(only: Sequence[str] = (), directory: Path = CASES_DIR) -> list[EvalCase]` sorted by `case_id`, `ValueError` for an unknown id in `only`.
  - `checks.CheckResult(check_id: str, passed: bool, detail: str)` and `checks.ToolUse(task_id: str, name: str, permission: str)`, frozen dataclasses.
  - `checks.numbers_in(text: str) -> set[float]`: every `\d[\d,]*(?:\.\d+)?` with commas removed, rounded to two decimals (so `INR 1,10,000` and `10,522.00` read as 110000.0 and 10522.0).
  - `checks.cited_rows(report: ReportBody, artifact_id: str) -> set[int]`: rows named by `row` locators on that artifact across all claims; a value is comma-separated ids or `a-b` ranges.
  - `checks.run_checks(report: ReportBody | None, checks: Sequence[Check], dataset_id: str, *, failure: str | None = None) -> list[CheckResult]`, one result per check in order. `claim_numbers`: every number appears in some claim text. `forbidden_numbers`: none does. `cited_rows`: every row is cited on the dataset. `every_claim_has_evidence`: at least one claim, each with an evidence ref. `claim_pattern`: a claim matches when `pattern` matches it and `unless` does not; passes when (some claim matches) equals `must_match`. A None report fails every check with detail `f"no report: {failure}"`.
  - `checks.check_trace(assertions: TraceAssertions, calls: Sequence[ModelCall], tasks: Sequence[Task], tool_uses: Sequence[ToolUse]) -> list[CheckResult]`: `mustCall:<x>` passes when `x` is a used tool name or the kind of a task; `mustNot:<x>` passes when no tool use has that name or permission; `maxCalls` passes when `len(calls) <= max_calls` (every outcome counts); `maxUsd` when `spent_usd(calls) <= max_usd`. Ids exactly `mustCall:<x>`, `mustNot:<x>`, `maxCalls`, `maxUsd`.
  - `tests/evals_support.py`: `REPO_ROOT`, `golden_case() -> EvalCase`, `report_with(*claims: tuple[str, str]) -> ReportBody` (each claim text with one `row` locator on `a2`, or none when the rows string is empty).

The three case files (camelCase keys, snake_case enum values). The request text is golden-001's, verbatim, in all three:

```yaml
# evals/cases/golden-001.yaml — phase 2 scope: t1 to t3 on the 60-row fixture (truths computed from it)
caseId: golden-001
request: >-
  Review my September and October spending. Explain every category that went up and name the
  transactions behind it. Find subscriptions I am paying twice or whose price went up, and confirm
  the current price from the provider's website. Check whether both October card bills were paid.
  Check whether the refund Decathlon promised in September has arrived. Cite evidence for every
  claim. If anyone still owes me a refund, draft an email asking for it.
standingInstruction: >-
  Transfers between my own accounts are never spending. A card purchase and its bill payment count once.
clarificationAnswer: >-
  ACC-SAV-1 and ACC-CUR-2 are my bank accounts; ACC-CARD-HDFC and ACC-CARD-ICICI are my credit cards.
datasetId: a2
datasetCsv: fixtures/transactions_sep_oct.csv
checks:
  - { checkId: dining-totals, kind: claim_numbers, numbers: [4499, 10522] }
  # September with the 25,000 transfer; October with the 15,000 transfer and/or the 4,312 settlement.
  - { checkId: totals-exclude-transfers, kind: forbidden_numbers, numbers: [58853.25, 54147, 43459, 58459] }
  - { checkId: transfer-pairs-cited, kind: cited_rows, rows: [12, 13, 40, 41] }
  - { checkId: netflix-price-rise, kind: claim_numbers, numbers: [649, 799] }
  - { checkId: netflix-rows, kind: cited_rows, rows: [1, 32] }
  - { checkId: spotify-twice, kind: cited_rows, rows: [33, 34] }
  - { checkId: every-claim-cited, kind: every_claim_has_evidence }
trace: { mustCall: [sql_query, sql_analysis], mustNot: [WRITE, DESTRUCTIVE], maxCalls: 30, maxUsd: 0.40 }
rubric:
  - No claim is made without evidence.
  - Increases are attributed to named transactions, not only to categories.
  - Unknowns are stated as unknown.
```

`golden-001-flat.yaml`: same `request`, `standingInstruction`, `clarificationAnswer`, `datasetId` and `trace`; `datasetCsv: fixtures/variants/flat/transactions_sep_oct.csv`; checks `{ checkId: no-invented-increase, kind: claim_pattern, pattern: '(?i)\b(increased|went up|rose|higher)\b', unless: '(?i)\b(no|not|nothing|none|neither|unchanged)\b', mustMatch: false }` and `every-claim-cited`; rubric = golden's three plus "Says plainly that no spending category went up from September to October, and invents no increase."

`golden-001-two-causes.yaml`: same shared fields; `datasetCsv: fixtures/variants/two-causes/transactions_sep_oct.csv`; checks `dining-totals` `[6899, 13642]`, `{ checkId: price-rise-numbers, kind: claim_numbers, numbers: [600, 780] }`, `{ checkId: new-restaurant-rows, kind: cited_rows, rows: [48, 56] }`, `every-claim-cited`; rubric = golden's three plus "Reports both causes of the dining increase, the new restaurant visits and the Toit team-lunch price rise, each with its own rows."

Fixture variants (generate with a throwaway script; commit the CSVs, not the script; same header as the base fixture; both files are named `transactions_sep_oct.csv` so the artifact name never hints at the case):
- `flat`: base rows 1 to 31 verbatim, then rows 32 to 62 = rows 1 to 31 again with `row_id + 31` and `txn_date + 30 days` (2026-09-01 becomes 2026-10-01), other columns unchanged. Every category total is equal in both months (dining 4,499.00 each).
- `two-causes`: base rows 1 to 60 verbatim plus rows 61 to 68, `Toit, ACC-CARD-HDFC, dining, Team lunch`, DEBIT: 61 to 64 dated 2026-09-08, 09-15, 09-22, 09-29 at 600.00; 65 to 68 dated 2026-10-06, 10-13, 10-20, 10-27 at 780.00. Dining is 6,899.00 in September and 13,642.00 in October: new restaurant visits (rows 48 Byg Brewski, 56 Karavalli) and a Toit price rise.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_eval_checks.py
import csv
from collections import defaultdict
from pathlib import Path

import pytest

from contextforge.core.entities.model_call import ModelCall
from contextforge.core.entities.task import Task
from contextforge.evals.cases import load_case, load_cases
from contextforge.evals.checks import ToolUse, check_trace, run_checks
from tests.evals_support import REPO_ROOT, golden_case, report_with

CASES = REPO_ROOT / "evals" / "cases"


def _dining(path: Path) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    for row in csv.DictReader(path.open()):
        if row["category"] == "dining":
            totals[row["txn_date"][:7]] += float(row["amount"])
    return dict(totals)


def test_cases_load_and_variants_hold_their_planted_truths() -> None:
    cases = load_cases()
    assert [c.case_id for c in cases] == ["golden-001", "golden-001-flat", "golden-001-two-causes"]
    assert {c.dataset_path().name for c in cases} == {"transactions_sep_oct.csv"}
    assert [_dining(c.dataset_path()) for c in cases] == [
        {"2026-09": 4499.0, "2026-10": 10522.0}, {"2026-09": 4499.0, "2026-10": 4499.0},
        {"2026-09": 6899.0, "2026-10": 13642.0}]
    assert golden_case().request_text().endswith("A card purchase and its bill payment count once.")
    with pytest.raises(ValueError):
        load_cases(only=["golden-404"])


def test_golden_checks_pass_on_a_correct_report_and_catch_transfer_totals() -> None:
    case = golden_case()
    good = report_with(
        ("Dining rose from INR 4,499 in September to INR 10,522.00 in October.", "43,48,56,58"),
        ("Rows 12/13 and 40/41 are own-account transfers and are excluded.", "12,13,40,41"),
        ("Netflix went from 649 to 799.", "1,32"),
        ("Spotify was charged twice in October, once on each card.", "33-34"))
    assert [r.check_id for r in run_checks(good, case.checks, "a2") if not r.passed] == []
    bad = report_with(("September spending was INR 58,853.25.", "1-31"), ("Dining went up.", ""))
    failed = {r.check_id for r in run_checks(bad, case.checks, "a2") if not r.passed}
    assert failed == {"dining-totals", "totals-exclude-transfers", "transfer-pairs-cited",
                      "netflix-price-rise", "netflix-rows", "spotify-twice", "every-claim-cited"}
    missing = run_checks(None, case.checks, "a2", failure="Planned task t4 names artifact a4")
    assert len(missing) == 7 and all(not r.passed and "a4" in r.detail for r in missing)


def test_flat_pattern_allows_a_negated_increase_only() -> None:
    checks = load_case(CASES / "golden-001-flat.yaml").checks
    assert all(r.passed for r in run_checks(report_with(("No category increased; dining was 4,499 both months.", "7")), checks, "a2"))
    assert not all(r.passed for r in run_checks(report_with(("Dining increased by INR 300.", "7")), checks, "a2"))


def test_trace_assertions_on_kinds_tool_names_call_count_and_usd() -> None:
    call = ModelCall(model_call_id="k1", request_message_id="m1", stage="execute", model="m",
                     context_version="mem:1|prompt:execute.1", usage=None, estimated_cost_usd=0.02,
                     price_version="v", outcome="COMPLETED", started_at="t")
    task = Task(task_id="t1", request_message_id="m1", kind="sql_analysis", goal="g", input_artifact_ids=["a2"],
                depends_on_task_ids=[], status="COMPLETED", attempt=1, created_at="t", updated_at="t")
    read = [ToolUse(task_id="t1", name="sql_query", permission="READ")]
    trace = golden_case().trace
    assert all(r.passed for r in check_trace(trace, [call] * 20, [task], read))
    over = check_trace(trace, [call] * 31, [], read + [ToolUse("t1", "email_draft", "WRITE")])
    assert {r.check_id for r in over if not r.passed} == {"mustCall:sql_analysis", "mustNot:WRITE",
                                                          "maxCalls", "maxUsd"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_eval_checks.py -v`
Expected: FAIL, `ModuleNotFoundError: contextforge.evals`.

- [ ] **Step 3: Write the case files and variants; implement `cases.py`, `checks.py` and `tests/evals_support.py`**

Adjust the `Task` constructor in the test to the entity's actual required fields if they differ; the assertions stay.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_eval_checks.py -v && uv run mypy src && uv run ruff check .`
Expected: 4 passed, clean.

- [ ] **Step 5: Commit**

```bash
git add src/contextforge/evals evals/cases fixtures/variants tests/evals_support.py tests/test_eval_checks.py
git commit -m "feat: eval cases with fixture variants, deterministic checks and trace assertions"
```

---

### Task 8: The judge, its rubric prompt and label agreement

**Files:**
- Create: `evals/judge.md`, `src/contextforge/evals/judge.py`
- Test: `tests/test_judge.py`

**Interfaces:**
- Consumes: `ModelClient`, `StageConfig` (`models.stages["evaluate"]`), `CheckResult`, `ModelError`.
- Produces:
  - `CriterionVerdict(Entity)`: `criterion: int` (1-based), `passed: bool`, `reason: str`. `JudgeVerdict(Entity)`: `verdicts: list[CriterionVerdict]`.
  - `JUDGE_PROMPT_PATH` (repo-root `evals/judge.md`); `render_report(report: ReportBody) -> str`: claims numbered with their evidence as `artifactId locatorKind=value`, then `Uncertainties:` lines; deterministic.
  - `judge_request(report: ReportBody, rubric: Sequence[str], stage: StageConfig) -> ModelRequest`: `stage=evaluate`, `model=stage.model`, `system=[judge prompt]`, one `HumanMessage` of `"<rubric>\n1. …\n</rubric>\n<report>\n…\n</report>"`, `output_schema=JudgeVerdict`, `max_tokens=stage.max_tokens`.
  - `failed_verdicts(rubric: Sequence[str], reason: str) -> list[CheckResult]`: one failed `judge:<n>` result per item.
  - `judge_report(report: ReportBody | None, rubric: Sequence[str], model: ModelClient, stage: StageConfig) -> list[CheckResult]`: one `judge:<n>` result per rubric item, detail = the judge's reason; no report → `failed_verdicts(rubric, "no report")` without a call; a `ModelError` or an output that fails `JudgeVerdict` → `failed_verdicts(rubric, "judge failed: …")`; an item without a verdict fails with detail `"no verdict"`. The judge call is not a request ModelCall and never counts toward `maxCalls` or `maxUsd`.
  - `Labels(Entity)`: `recorded_at: str`, `verdicts: list[bool]` (the user's own pass or fail per rubric item, in order, for the report of one recording); `load_labels(path: Path) -> Labels | None`; `label_agreement(judged: Sequence[CheckResult], labels: Sequence[bool]) -> tuple[int, int]` = (items where judge and label agree, number of labels), `ValueError` when the lengths differ.

`evals/judge.md`, exactly:

```text
You grade one research report against a numbered rubric.

Judge only from the report below: its claims, the evidence each claim cites, and its stated
uncertainties. Do not use outside knowledge, do not recompute numbers, and do not reward
length or a confident tone.

For each rubric item, in order, return its number, whether the report clearly satisfies it,
and one sentence of reason that quotes or names the claim that decides it. When the report
gives too little to decide, the item is not satisfied.
```

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_judge.py
import pytest

from contextforge.core.models.base import ModelRequest, ModelResponse
from contextforge.core.models.config import load_models_config
from contextforge.core.models.scripted import ScriptedModel
from contextforge.evals.checks import CheckResult
from contextforge.evals.judge import JudgeVerdict, judge_report, label_agreement
from tests.evals_support import golden_case, report_with

RUBRIC = golden_case().rubric
STAGE = load_models_config().stages["evaluate"]
REPORT = report_with(("Dining rose from 4,499 to 10,522.", "43,48"))


def test_one_result_per_criterion_from_the_judge_call() -> None:
    model = ScriptedModel({"evaluate": [ModelResponse(structured={"verdicts": [
        {"criterion": 1, "passed": True, "reason": "every claim cites rows"},
        {"criterion": 2, "passed": False, "reason": "the increase is not tied to rows"}]})]})
    seen: list[ModelRequest] = []
    model.on_request = seen.append
    results = judge_report(REPORT, RUBRIC, model, STAGE)
    assert [(r.check_id, r.passed) for r in results] == [("judge:1", True), ("judge:2", False), ("judge:3", False)]
    assert results[2].detail == "no verdict"
    assert seen[0].stage == "evaluate" and seen[0].output_schema is JudgeVerdict
    assert "1. No claim is made without evidence." in seen[0].messages[0].content
    assert "a2 row=43,48" in seen[0].messages[0].content


def test_judge_failures_and_a_missing_report_score_zero_without_raising() -> None:
    for model in (ScriptedModel({}), ScriptedModel({"evaluate": [ModelResponse(structured={"verdicts": "yes"})]})):
        assert [r.passed for r in judge_report(REPORT, RUBRIC, model, STAGE)] == [False] * 3
    assert all(r.detail == "no report" for r in judge_report(None, RUBRIC, ScriptedModel({}), STAGE))


def test_label_agreement() -> None:
    judged = [CheckResult("judge:1", True, ""), CheckResult("judge:2", False, "")]
    assert label_agreement(judged, [True, True]) == (1, 2)
    with pytest.raises(ValueError):
        label_agreement(judged, [True])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_judge.py -v`
Expected: FAIL, `ModuleNotFoundError: contextforge.evals.judge`.

- [ ] **Step 3: Write `evals/judge.md` and implement `judge.py`**

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_judge.py -v && uv run mypy src && uv run ruff check .`
Expected: 3 passed, clean.

- [ ] **Step 5: Commit**

```bash
git add evals/judge.md src/contextforge/evals/judge.py tests/test_judge.py
git commit -m "feat: rubric judge with per-criterion verdicts and label agreement"
```

---

### Task 9: The eval harness and the one-line score

**Files:**
- Create: `src/contextforge/evals/harness.py`, `src/contextforge/evals/score.py`
- Modify: `tests/evals_support.py` (add `golden_script`)
- Test: `tests/test_eval_harness.py`

**Interfaces:**
- Consumes: Tasks 2 to 8; `RequestRunner`, `SqliteSaver`, `SqlQueryTool`, `dataset_key`, `ClarificationResponseMessage`.
- Produces:
  - `harness.RecordedTool(inner: Tool, uses: list[ToolUse])` implementing `Tool` (same `spec` and `permission`); `run` appends `ToolUse(ctx.task_id, spec.name, permission)` and delegates.
  - `harness.TaskIndex`: `bind(task_ids: Callable[[], list[str]]) -> None`; `ordinal(task_id: str | None) -> int | None` = the task's position in plan order (task ids are created in plan order), None for no task or an unknown id.
  - `harness.CaseRun` (frozen dataclass): `request_message_id: str`, `status: str` (a `RequestStatus` value, or `ERROR` when the run raised), `failure_reason: str | None`, `report: ReportBody | None`, `tasks: list[Task]`, `calls: list[ModelCall]`, `tool_uses: list[ToolUse]`.
  - `harness.case_deps(model: ModelClient, uses: list[ToolUse], *, tracer: ModelTracer | None = None) -> WorkerDependencies`: in-memory repositories, `MemoryBlobStore`, `load_budgets()`, stage models and `price_version` from `config/models.yaml`, `pricing=call_pricing(...)`, `tools={"sql_query": RecordedTool(SqlQueryTool(repos.artifacts, blobs), uses)}`, an `InProcessQueue`, `tracer`.
  - `harness.seed_case(deps: WorkerDependencies, case: EvalCase) -> RequestMessage`: Chat `c1` of user `u1`, an AVAILABLE `dataset` Artifact `case.dataset_id` named after the CSV file with its bytes stored at `dataset_key("c1", dataset_id)`, and an ACCEPTED request Message whose content is `case.request_text()`.
  - `harness.run_case(case: EvalCase, model: ModelClient, *, index: TaskIndex | None = None, tracer: ModelTracer | None = None) -> CaseRun`: builds deps, seeds, binds `index` to the request's task ids, runs `RequestRunner(deps, SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False))).start`, and while the request is WAITING_USER (at most two rounds, the clarification limit) answers the pending interrupt with a `ClarificationResponseMessage` whose content is `case.clarification_answer`. Any exception from the run becomes `status="ERROR"`, `failure_reason=str(exc)`; one case never stops the others.
  - `harness.evaluate_case(case: EvalCase, model: ModelClient, *, index: TaskIndex | None = None, tracer: ModelTracer | None = None) -> CaseResult`: `run_case`, then `run_checks(run.report, case.checks, case.dataset_id, failure=run.failure_reason)`, `check_trace(case.trace, run.calls, run.tasks, run.tool_uses)` and `judge_report(run.report, case.rubric, model, models.stages["evaluate"])` (the judge goes straight to `model`, not through the request's recorder); `calls=len(run.calls)`, `usd=spent_usd(run.calls)`.
  - `score.CaseResult` (frozen dataclass): `case_id`, `status`, `checks`, `trace`, `judge: list[CheckResult]`, `calls: int`, `usd: float`, `judge_agreement: tuple[int, int] | None = None`; property `passed` = status `COMPLETED` and every check, trace assertion and judge item passed.
  - `score.failed_result(case: EvalCase, status: str, reason: str) -> CaseResult`: every item failed with `reason`, built from `run_checks(None, …)`, `check_trace(case.trace, [], [], [])` with `passed=False`, and `failed_verdicts`, so denominators match a real run.
  - `score.case_lines(result: CaseResult) -> list[str]`: `f"{case_id} {status} checks {a}/{b} trace {c}/{d} judge {e}/{f} calls {n} usd {usd:.4f}"`, then `f"  FAIL {check_id}: {detail}"` per failed item.
  - `score.score_line(results: Sequence[CaseResult], *, mode: str, price_version: str) -> str`: `f"eval mode={mode} priceVersion={pv} cases={n} passed={p} checks={a}/{b} trace={c}/{d} judge={e}/{f} judgeAgree={g}/{h} calls={calls} usd={usd:.4f}"`, with `judgeAgree=-` when no case has labels.
  - `tests/evals_support.golden_script(*, ask_first: bool = True) -> dict[str, list[ModelResponse]]`: plan = (a clarification first when `ask_first`) then t1 to t3 `sql_analysis` on `a2` (t2 after t1) and t4 `reconcile` after all three; execute = `[call 1, result, call 2, result, call 3, result]` where call n is `sql_query` with `SELECT n` and every result is one `TaskResult` with four facts (dining 4,499 to 10,522 citing rows 43,48,56,58; transfers citing 12,13,40,41; Netflix 649 to 799 citing 1,32; Spotify twice citing 33,34), each response with `Usage(input_tokens=3000, output_tokens=200)`; evaluate = all three criteria passed. Any interleaving of three parallel tasks consumes the list validly: each task stops at its first result.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_eval_harness.py
from contextforge.core.models.base import ModelRequest, ModelResponse
from contextforge.core.models.scripted import ScriptedModel
from contextforge.evals.checks import CheckResult
from contextforge.evals.harness import TaskIndex, evaluate_case, run_case
from contextforge.evals.score import CaseResult, case_lines, score_line
from tests.evals_support import golden_case, golden_script


def test_scripted_golden_run_answers_the_question_and_scores_everything() -> None:
    index = TaskIndex()
    run = run_case(golden_case(), ScriptedModel(golden_script()), index=index)
    assert run.status == "COMPLETED" and len(run.tasks) == 4
    assert [index.ordinal(t.task_id) for t in run.tasks] == [0, 1, 2, 3]
    assert {u.name for u in run.tool_uses} == {"sql_query"}
    result = evaluate_case(golden_case(), ScriptedModel(golden_script()))
    assert result.passed, case_lines(result)
    assert (len(result.checks), len(result.trace), len(result.judge)) == (7, 6, 3)
    assert result.calls == 8 and 0 < result.usd < 0.40


def test_gate_failure_and_a_raising_run_are_scored_not_raised() -> None:
    script = golden_script(ask_first=False)
    script["plan"][0].structured["tasks"][0]["inputArtifactIds"] = ["a9"]    # not in the chat
    gated = evaluate_case(golden_case(), ScriptedModel(script))
    assert gated.status == "FAILED" and not gated.passed
    assert all("a9" in r.detail for r in gated.checks)
    assert {r.check_id for r in gated.trace if not r.passed} == {"mustCall:sql_query"}

    class Broken(ScriptedModel):
        def invoke(self, request: ModelRequest) -> ModelResponse:
            raise RuntimeError("adapter bug")

    broken = evaluate_case(golden_case(), Broken({}))
    assert broken.status == "ERROR" and "adapter bug" in broken.checks[0].detail
    line = score_line([gated, broken], mode="replay", price_version="v")
    assert line.startswith("eval mode=replay priceVersion=v cases=2 passed=0 checks=0/14 ")


def test_score_line_and_case_lines_format() -> None:
    ok, bad = CheckResult("x", True, ""), CheckResult("y", False, "why")
    r1 = CaseResult("golden-001", "COMPLETED", [ok] * 7, [ok] * 4, [ok] * 3, calls=12, usd=0.0123,
                    judge_agreement=(3, 3))
    r2 = CaseResult("golden-001-flat", "FAILED", [bad] * 3, [ok] * 3 + [bad], [bad] * 4, calls=1, usd=0.0011)
    assert score_line([r1, r2], mode="replay", price_version="2026-11-07") == (
        "eval mode=replay priceVersion=2026-11-07 cases=2 passed=1 checks=7/10 trace=7/8 "
        "judge=3/7 judgeAgree=3/3 calls=13 usd=0.0134")
    assert case_lines(r2)[0] == "golden-001-flat FAILED checks 0/3 trace 3/4 judge 0/4 calls 1 usd 0.0011"
    assert "  FAIL y: why" in case_lines(r2)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_eval_harness.py -v`
Expected: FAIL, `ModuleNotFoundError: contextforge.evals.harness`.

- [ ] **Step 3: Implement `harness.py`, `score.py` and `golden_script`**

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_eval_harness.py -v && uv run mypy src && uv run ruff check .`
Expected: 3 passed, clean.

- [ ] **Step 5: Commit**

```bash
git add src/contextforge/evals tests/evals_support.py tests/test_eval_harness.py
git commit -m "feat: eval harness that runs a case end to end and prints a one-line score"
```

---

### Task 10: Recorded replay and the CLI

**Files:**
- Create: `src/contextforge/evals/replay.py`, `src/contextforge/evals/cli.py`, `evals/run.py`
- Test: `tests/test_eval_replay.py`

**Interfaces:**
- Consumes: `TaskIndex`, `run_case`, `evaluate_case`, `failed_result`, `case_lines`, `score_line`, `load_labels`, `label_agreement`, `context_version`, `MEMORY_VERSION`, `bedrock_model_client`, `configure_tracing`, `live_calls_allowed`.
- Produces:
  - `TranscriptEntry(Entity)`: `stage: Stage`, `task_ordinal: int | None`, `turn: int`, `context_version: str`, `response: ModelResponse`. `Transcript(Entity)`: `case_id`, `recorded_at`, `models: dict[str, str]` (stage → model id at recording), `entries: list[TranscriptEntry]`.
  - `ReplayMiss(ModelError)` (not retryable).
  - `TranscriptRecorder(inner: ModelClient, index: TaskIndex)` implementing `ModelClient`: on a successful call appends an entry keyed by `(stage, index.ordinal(request.task_id), turn)`, where `turn` counts successful calls per `(stage, ordinal)` (failed attempts are not recorded, so turns line up on replay); thread-safe. `transcript(case_id: str, models: ModelsConfig) -> Transcript` stamps `recorded_at = now_iso()` and the stage models.
  - `ReplayModelClient(transcript: Transcript, index: TaskIndex, models: ModelsConfig)` implementing `ModelClient`: serves the entry for the same key with `replayed=False`, so the recorder writes a ModelCall with the saved usage. Raises `ReplayMiss` naming the key and ending "re-record with --live --record" when there is no entry, when the entry's `context_version` differs from `context_version(MEMORY_VERSION, stage)` now, or when `transcript.models[stage]` differs from today's stage model.
  - `save_transcript(transcript: Transcript, path: Path) -> None` (camelCase JSON, indent 2, entries sorted by stage, ordinal, turn) and `load_transcript(path: Path) -> Transcript`.
  - `cli.main(argv: Sequence[str]) -> int` with flags `--case ID` (repeatable; default every case), `--live`, `--record`, `--verbose`, `--recordings DIR` (default `evals/recordings`), `--labels DIR` (default `evals/labels`). `--live` without `live_calls_allowed()` prints "live calls are paid; set CONTEXTFORGE_LIVE=1 for this command" and returns 2; `--record` without `--live` returns 2. Live: one `bedrock_model_client`, one `configure_tracing`, per case a fresh `TaskIndex` and `TranscriptRecorder`, `evaluate_case`, and with `--record` the transcript saved to `<recordings>/<caseId>.json`. Replay (default): a missing file gives `failed_result(case, "NO_RECORDING", "no recording at <path>; run with --live --record")`; otherwise `evaluate_case` over a `ReplayModelClient`, and when `<labels>/<caseId>.yaml` exists with `recordedAt` equal to the transcript's, `judge_agreement` is set. Prints `case_lines` for failed cases (every case with `--verbose`), then the score line; returns 0 when every case passed, else 1.
  - `evals/run.py`: `raise SystemExit(main(sys.argv[1:]))`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_eval_replay.py
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage

from contextforge.core.models.base import ModelRequest, ModelResponse
from contextforge.core.models.config import load_models_config
from contextforge.core.models.pricing import spent_usd
from contextforge.core.models.scripted import ScriptedModel
from contextforge.evals.cli import main
from contextforge.evals.harness import TaskIndex, run_case
from contextforge.evals.replay import (ReplayMiss, ReplayModelClient, Transcript, TranscriptEntry,
                                       TranscriptRecorder, load_transcript, save_transcript)
from tests.evals_support import golden_case, golden_script

MODELS = load_models_config()


def _distinct(script: dict[str, list[ModelResponse]]) -> dict[str, list[ModelResponse]]:
    """Each execute result names its position, so a response replayed to the wrong task changes the report."""
    results = [r for r in script["execute"] if r.structured is not None]
    for n, response in enumerate(results):
        response.structured["facts"][0]["text"] += f" (response {n})"
    return script


def test_a_recording_replays_the_same_report_under_parallel_waves(tmp_path: Path) -> None:
    record_index = TaskIndex()
    recorder = TranscriptRecorder(ScriptedModel(_distinct(golden_script())), record_index)
    first = run_case(golden_case(), recorder, index=record_index)      # tasksInFlight 5 from budgets.yaml
    path = tmp_path / "golden-001.json"
    save_transcript(recorder.transcript("golden-001", MODELS), path)
    replay_index = TaskIndex()
    second = run_case(golden_case(), ReplayModelClient(load_transcript(path), replay_index, MODELS), index=replay_index)
    assert first.status == second.status == "COMPLETED"
    assert first.report is not None and second.report is not None
    assert [c.text for c in second.report.claims] == [c.text for c in first.report.claims]
    assert len(second.calls) == len(first.calls) and spent_usd(second.calls) == pytest.approx(spent_usd(first.calls))


def test_stale_or_incomplete_recordings_refuse_to_replay() -> None:
    plan = ModelRequest(stage="plan", model=MODELS.stages["plan"].model, system=["s"], messages=[HumanMessage("x")])
    entry = TranscriptEntry(stage="plan", task_ordinal=None, turn=0, context_version="mem:1|prompt:plan.0",
                            response=ModelResponse(text="old"))
    current = {stage: cfg.model for stage, cfg in MODELS.stages.items()}
    with pytest.raises(ReplayMiss, match="re-record"):
        ReplayModelClient(Transcript(case_id="c", recorded_at="t", models=current, entries=[entry]),
                          TaskIndex(), MODELS).invoke(plan)
    with pytest.raises(ReplayMiss, match="re-record"):
        ReplayModelClient(Transcript(case_id="c", recorded_at="t", models=current | {"plan": "other"},
                                     entries=[entry.model_copy(update={"context_version": "mem:1|prompt:plan.1"})]),
                          TaskIndex(), MODELS).invoke(plan)
    index = TaskIndex()
    empty = ReplayModelClient(Transcript(case_id="c", recorded_at="t", models=current, entries=[]), index, MODELS)
    run = run_case(golden_case(), empty, index=index)
    assert run.status == "FAILED" and "re-record" in (run.failure_reason or "")


def test_cli_refuses_paid_calls_without_the_switch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                   capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("CONTEXTFORGE_LIVE", raising=False)
    assert main(["--live"]) == 2 and main(["--record"]) == 2
    assert main(["--case", "golden-001", "--recordings", str(tmp_path)]) == 1
    out = capsys.readouterr().out.strip().splitlines()
    assert out[-1].startswith("eval mode=replay ") and "cases=1 passed=0" in out[-1]
    assert any("NO_RECORDING" in line for line in out)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_eval_replay.py -v`
Expected: FAIL, `ModuleNotFoundError: contextforge.evals.replay`.

- [ ] **Step 3: Implement `replay.py`, `cli.py` and `evals/run.py`**

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -v && uv run mypy src && uv run ruff check . && uv run ruff format --check .`
Expected: all pass (3 new); clean.

- [ ] **Step 5: Commit**

```bash
git add src/contextforge/evals evals/run.py tests/test_eval_replay.py
git commit -m "feat: record and replay eval transcripts keyed by plan position, and the eval CLI"
```

---

### Task 11: Phase close: the first live eval, recordings and docs

**Files:**
- Create: `evals/recordings/golden-001.json`, `golden-001-flat.json`, `golden-001-two-causes.json`; `tests/test_eval_recordings.py`
- Modify: `docs/progress.md`, `docs/implementation-plan.md` (9.3 measured column), `README.md`

- [ ] **Step 1: Run the live eval and record it (needs every prerequisite)**

Run: `CONTEXTFORGE_LIVE=1 uv run python evals/run.py --live --record --verbose`
Expected: case lines and one score line `eval mode=live …`; three files under `evals/recordings/`; total `usd` well under the three cases' `maxUsd` sum. A failing case is a result to report, not a reason to edit the checks.

- [ ] **Step 2: Check the traces**

In LangSmith, project `contextforge`: each request is one thread; `model.plan` and `model.execute` runs carry the Task 6 metadata; their run ids match `langsmithRunId` on the recorded ModelCalls. Note in the progress log whether model runs nest under the workflow's runs; if they appear as separate roots, they still group by `thread_id`.

- [ ] **Step 3: Replay offline**

Run: `uv run python evals/run.py`
Expected: the same per-case results with `mode=replay`, and no network call.

- [ ] **Step 4: Add the recordings test**

```python
# tests/test_eval_recordings.py
import pytest

from contextforge.evals.cli import main


def test_committed_recordings_replay_offline(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) in (0, 1)               # a later prompt bump without re-recording shows as failed cases
    line = capsys.readouterr().out.strip().splitlines()[-1]
    assert line.startswith("eval mode=replay ") and " cases=3 " in line
```

- [ ] **Step 5: Update the docs**

`docs/progress.md`: phase 2 row `Done <date>`; log entry with the live score line, total cost, the Nova facts verified in Tasks 2 and 3, the trace nesting finding, and the deviations below. `docs/implementation-plan.md` 9.3: a column "measured, Nova, golden-001 phase 2 scope, <date>" beside the estimates for the stages that ran (calls, input, output, USD summed from the recordings' usage; a one-off script, not committed). `README.md`: `.env` setup from `.env.example`; offline replay (`uv run python evals/run.py`); live runs (`CONTEXTFORGE_LIVE=1 … --live --record`) to be made before merging any prompt, judge or model change; labelling a recording in `evals/labels/<caseId>.yaml` (`recordedAt` copied from the recording, `verdicts` in rubric order).

- [ ] **Step 6: Run everything**

Run: `uv run pytest -v && uv run mypy src && uv run ruff check . && uv run ruff format --check .`
Expected: every test passes, the live module skipped; clean.

- [ ] **Step 7: Commit**

```bash
git add evals/recordings tests/test_eval_recordings.py docs/progress.md docs/implementation-plan.md README.md
git commit -m "docs: phase 2 complete; first live golden-001 eval recorded"
```

---

## Prerequisites (the user, outside the code)

1. **Before Task 4 Step 5 and Task 11:** add an IAM policy to the IAM user behind the local `aws configure` profile (replace `<ACCOUNT_ID>`). The `*` region on foundation models is needed because the APAC inference profiles route to other APAC regions.
   ```json
   {"Version": "2012-10-17", "Statement": [
     {"Sid": "InvokeBedrockModels", "Effect": "Allow",
      "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
      "Resource": ["arn:aws:bedrock:*::foundation-model/*", "arn:aws:bedrock:*:<ACCOUNT_ID>:inference-profile/*"]},
     {"Sid": "ReadInferenceProfiles", "Effect": "Allow",
      "Action": ["bedrock:ListInferenceProfiles", "bedrock:GetInferenceProfile"], "Resource": "*"}]}
   ```
2. **Before Task 2 Step 1:** in the Bedrock console (ap-southeast-2) confirm Amazon Nova Pro and Nova Lite are enabled under model access and the APAC inference profiles are listed. Confirm the $5 usage alert and MFA are still in place (system-design 11).
3. **Before Task 4 Step 5:** the local `aws configure` profile works for `ap-southeast-2` (`aws sts get-caller-identity`); set `CONTEXTFORGE_AWS_PROFILE` in `.env` only if it is not the default profile.
4. **Before Task 11:** a LangSmith API key in `.env` (copied from `.env.example`): `LANGSMITH_TRACING=true`, `LANGSMITH_API_KEY=<key>`, `LANGSMITH_PROJECT=contextforge`.
5. **Paid calls:** only `CONTEXTFORGE_LIVE=1 uv run pytest tests/test_bedrock_live.py` (Task 4) and `CONTEXTFORGE_LIVE=1 uv run python evals/run.py --live …` (Task 11) spend money; the default suite never does, and `CONTEXTFORGE_LIVE` in `.env` is ignored. Expected spend for the whole phase is cents (estimated, not measured).
6. **After Task 11:** read each recorded report (`uv run python evals/run.py --verbose`) and write your own verdicts in `evals/labels/<caseId>.yaml`; the next replay reports `judgeAgree`.
7. **When the AWS plan is upgraded:** switch the stage models to the commented `au.anthropic.*` ids, add their prices under those ids, bump `priceVersion`, and re-record live.

## Deviations from the spec, decided in this plan

- The eval entry point is `evals/run.py` as the milestone instruction names it; spec 12 lists `evals/run_evals.py`. The logic lives in `src/contextforge/evals/` so mypy checks it and tests import it.
- Three runnable cases instead of the phase row's ten requests: golden-001 (phase 2 scope) and the `flat` and `two-causes` companions on new fixture variants. `source-down` and `injected` wait for phases 5 and 6; the 400-row golden fixtures arrive with phase 3, so the phase 2 truths are computed from the 60-row fixture (dining 4,499 to 10,522; Netflix rows 1 and 32; Spotify rows 33 and 34; transfers 12/13 and 40/41).
- The standing instruction is appended to the request text until phase 7 moves it to `User.instructions`.
- The judge runs on stage `evaluate` with the plan model; its calls are not request ModelCalls and never count toward `maxCalls` or `maxUsd`. Label agreement applies only to the recording the labels name.
- Per-task-kind deadline values (300, 300, 180, 120 s) are this plan's choice; the spec fixes only the per-call 60/120 s and the 15-minute request deadline, which phase 4's lazy reaper enforces, so no request deadline key is added here. Deadlines are checked before model calls, not inside a running tool.
- Spend admission refuses a call once the request's spend is at or above the cap, so one call may cross it. Unknown usage is charged the stage's worst case (`capInputTokens` input plus the stage's `maxTokens` output) and stored as that call's `estimatedCostUsd`.
- `ModelCall.model` is the configured Bedrock id without a `bedrock/` prefix; every model in this project is on Bedrock and Converse does not echo the model.
- Effort is not sent: `ModelRequest` carries none and Nova has no such parameter; the Claude switch adds it. `temperature` is 0 on every call for eval reproducibility. Prompt cache points are not sent (phase 9); cache usage is recorded when reported.
- `estimate_tokens` stays the phase 1 length heuristic: Nova has no published tokenizer. Recorded usage lets phase 9 calibrate it.
- Model spans carry `requestMessageId`, `taskId`, stage, model and prompt version; `chatId` is not on them and `modelCallId` joins through `ModelCall.langsmithRunId`. Log lines do not gain the run id.
- `evals/harness.seed_case` and `case_deps` repeat what `tests/helpers.seed_chat_with_dataset` and `make_deps` do, to keep the shared helper file additive. When phase 3 lands its ingestion service and tool registry, both the harness and the helpers should switch to them in one follow-up.
- Merge notes for the parallel wave: `src/contextforge/core/settings.py` and `src/contextforge/infra/__init__.py` may also be created by phase 10; merge the fields and keep one file. `tests/test_skeleton.py`'s budgets equality grows with every phase's new keys.

"""The planner bake-off harness (scripts/plan_bakeoff.py), offline: a dry run, a run over a fake
Bedrock runtime, and the probe."""

import copy
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

from contextforge.core.models.config import load_models_config
from contextforge.core.settings import Settings
from scripts import plan_bakeoff
from scripts.plan_bakeoff import SAMPLE_PLAN, Bakeoff, listed_model_ids, probe, render, run_bakeoff

REPO_ROOT = Path(__file__).resolve().parent.parent
QUERY = "Compare Pune and Hyderabad office rent per seat over the last 12 months."
LEGACY = "Access denied. This Model is marked by provider as Legacy. Upgrade."
KIMI_MINIMUM = (
    "The model returned the following errors: Invalid 'max_output_tokens': integer below "
    "minimum value"
)


def _refusal(code: str, message: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "Converse")


def _answer(plan: dict[str, Any]) -> dict[str, Any]:
    use = {"toolUseId": "r", "name": "return_result", "input": plan}
    content = [{"SDK_UNKNOWN_MEMBER": {"name": "reasoningContent"}}, {"toolUse": use}]
    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": "tool_use",
        "usage": {"inputTokens": 2000, "outputTokens": 500},
    }


class FakeRuntime:
    """Answers each model's Converse calls from its own list, in order; an exception is raised.
    A call nobody scripted fails the test."""

    def __init__(self, script: dict[str, list[Any]]) -> None:
        self.script = script

    def converse(self, **payload: Any) -> dict[str, Any]:
        answer = self.script[payload["modelId"]].pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_dry_run_writes_a_report_comparing_two_models(tmp_path: Path) -> None:
    query_file, out = tmp_path / "query.md", tmp_path / "report.md"
    query_file.write_text(QUERY + "\n", encoding="utf-8")
    models = "apac.amazon.nova-pro-v1:0,deepseek.v3.2"
    plan_bakeoff.main(
        ["--models", models, "--query-file", str(query_file), "--dry-run", "--out", str(out)]
    )
    report = out.read_text(encoding="utf-8")
    assert "- Mode: dry run" in report and "prompt plan.6" in report and f"> {QUERY}" in report
    assert "- Expected cost, stated before the run: $0." in report and "Cap: $0.50" in report
    for model in models.split(","):
        assert f"| `{model}` | plan | yes | forced tool → tool call | 3 | 2 | 1 |" in report
        assert f"## {model}" in report
    assert "3. **t3** `web_search`: Compare the two" in report and "(after t1, t2)" in report
    assert report.count("````json") == 2 and "Spent:" not in report


def test_a_run_records_the_refused_field_the_rejected_first_answer_and_the_run_cap() -> None:
    """Live (F8): Kimi K3.1 refuses temperature; a first plan may break the schema and be
    retried once; a model whose worst case no longer fits the run cap is never called."""
    no_task = {**copy.deepcopy(SAMPLE_PLAN), "tasks": []}
    temperature = "This model doesn't support the temperature field. Remove temperature."
    runtime = FakeRuntime(
        {
            "qwen.qwen3-235b-a22b-2507-v1:0": [
                _refusal("ValidationException", temperature),
                _answer(no_task),
                _answer(SAMPLE_PLAN),
            ],
            "global.moonshotai.kimi-k3.1": [],
        }
    )
    config = load_models_config(REPO_ROOT / "config" / "models.yaml")
    # Kimi's worst case (the context cap in, maxTokens out at $4 / $20 per MTok) is over $0.10.
    runs = run_bakeoff(
        runtime,
        ["qwen.qwen3-235b-a22b-2507-v1:0", "global.moonshotai.kimi-k3.1"],
        QUERY,
        Settings(_env_file=None),
        config,
        max_usd=0.10,
    )
    qwen, kimi = runs
    assert qwen.planned is not None and len(qwen.records) == 2 and len(qwen.calls) == 3
    assert kimi.planned is None and "spend budget exhausted" in str(kimi.failure)
    assert kimi.calls == [] and kimi.records == []
    report = render(Bakeoff(QUERY, "q.md", True, 0.10, [], runs, runs), config)
    path = "forced tool, no temperature → reasoning, tool call"
    assert f"| `qwen.qwen3-235b-a22b-2507-v1:0` | plan | no | {path} | 3 | 2 | 2 |" in report
    assert "| 4000 / 1000 | 0.0018 | tool_use |" in report
    assert "Call 1 refused: ValidationException: This model doesn't support the temperature" in (
        report
    )
    assert "**First answer:** invalid plan: Value error, a plan needs 1 to 6 tasks, got 0" in report
    assert "| `global.moonshotai.kimi-k3.1` | failed | no | – |" in report


def test_probe_lists_every_text_model_id_and_classifies_each_answer() -> None:
    class Control:
        def list_foundation_models(self, byOutputModality: str) -> dict[str, Any]:
            assert byOutputModality == "TEXT"
            on_demand = ["ON_DEMAND", "INFERENCE_PROFILE"]
            return {
                "modelSummaries": [
                    {"modelId": "amazon.nova-pro-v1:0", "inferenceTypesSupported": on_demand},
                    {"modelId": "anthropic.claude-opus-5-5", "inferenceTypesSupported": []},
                ]
            }

        def list_inference_profiles(self, **params: Any) -> dict[str, Any]:
            if "nextToken" not in params:
                page = [{"inferenceProfileId": "apac.amazon.nova-pro-v1:0"}]
                return {"inferenceProfileSummaries": page, "nextToken": "2"}
            # An embedding model's profile is not a text model's: not listed.
            ids = ["au.anthropic.claude-opus-5-5", "global.cohere.embed-v4:0"]
            return {"inferenceProfileSummaries": [{"inferenceProfileId": i} for i in ids]}

    listed = listed_model_ids(Control())
    assert listed == [
        "amazon.nova-pro-v1:0",
        "apac.amazon.nova-pro-v1:0",
        "au.anthropic.claude-opus-5-5",
    ]
    ok = {"output": {"message": {"content": []}}, "usage": {"inputTokens": 9, "outputTokens": 3}}
    runtime = FakeRuntime(
        {
            "apac.amazon.nova-pro-v1:0": [ok],
            "au.anthropic.claude-opus-5-5": [
                _refusal(
                    "AccessDeniedException",
                    "anthropic.claude-opus-5-5 is not available for this account. Contact AWS.",
                )
            ],
            "deepseek.v3.2": [
                _refusal(
                    "AccessDeniedException",
                    "User: arn:aws:iam::123456789012:user/dev is not authorized to perform: x",
                )
            ],
            "apac.amazon.nova-premier-v1:0": [
                _refusal("ValidationException", "The provided model identifier is invalid.")
            ],
            "amazon.nova-2-lite-v1:0": [
                _refusal(
                    "ValidationException",
                    "Invocation with on-demand throughput isn’t supported. Retry.",
                )
            ],
            # Live: a legacy model's refusal, and Kimi K3 refusing three output tokens.
            "au.anthropic.claude-sonnet-4-5": [_refusal("ResourceNotFoundException", LEGACY)],
            "global.moonshotai.kimi-k3": [_refusal("ValidationException", KIMI_MINIMUM)],
        }
    )
    probes = [probe(runtime, model) for model in runtime.script]
    assert [(p.result, p.detail) for p in probes] == [
        ("available", "9 in / 3 out tokens"),
        (
            "denied",
            "AccessDeniedException: anthropic.claude-opus-5-5 is not available for this account",
        ),
        ("denied", "AccessDeniedException: User: <principal> is not authorized to perform: x"),
        ("not found", "ValidationException: The provided model identifier is invalid"),
        (
            "profile only",
            "ValidationException: Invocation with on-demand throughput isn’t supported",
        ),
        ("denied", f"ResourceNotFoundException: {LEGACY.removesuffix('. Upgrade.')}"),
        ("available", f"ValidationException: {KIMI_MINIMUM}"),
    ]
    config = load_models_config(REPO_ROOT / "config" / "models.yaml")
    report = render(Bakeoff("", "", True, 0.5, probes, [], None), config)
    assert "| `au.anthropic.claude-opus-5-5` | denied |" in report and "## Summary" not in report

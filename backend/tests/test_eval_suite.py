"""The suite runner end to end through the real API with scripted research and a scripted judge;
the scorecard compare gate; judge calibration; the no-live-call guard. No network, no AWS."""

import csv
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from contextforge.api.main import create_app
from contextforge.core.models.base import ModelRequest, ModelResponse
from contextforge.core.settings import Settings
from contextforge.evals import suite_cli
from contextforge.evals.calibration import cohen_kappa, export_calibration, score_calibration
from contextforge.evals.judge_client import judge_session
from contextforge.evals.langsmith_log import log_suite
from contextforge.evals.suite import SuiteCase, compare_scorecards, regrade_suite, run_suite
from contextforge.evals.web_research import main
from contextforge.infra.web import web_tools
from tests.helpers import make_deps
from tests.test_judge import MODELS, PRICED_JUDGE, claims_answer, judge, report_answer
from tests.test_research_flow import SOURCE, ResearchFixtureModel
from tests.test_web_research import _page, _web

CASE = SuiteCase(
    id="bench",
    question="Research the benchmark.",
    required_parts=["what was measured", "the result"],
    source="test",
)


class _EachRequest(ResearchFixtureModel):
    """The research fixture, reset at each plan so every suite run cites its own page."""

    def invoke(self, request: ModelRequest) -> ModelResponse:
        if request.stage == "plan":
            self.artifact = None
        return super().invoke(request)


def _judged_runs(count: int, *, coverage: str = "answered") -> list[ModelResponse]:
    return [
        answer
        for _ in range(count)
        for answer in (claims_answer("supported"), report_answer("answered", coverage))
    ]


@contextmanager
def research_api(tmp_path: Path, name: str) -> Iterator[TestClient]:
    """The real API in-process: scripted research whose one web read returns ``SOURCE``."""
    settings = Settings(
        _env_file=None,
        model_backend="scripted",
        dev_token="local-test-token",
        call_reserve_usd=0.001,
        request_max_usd=0.25,
        checkpoint_path=tmp_path / f"{name}.sqlite",
    )
    worker_dependencies = make_deps(_EachRequest(), settings=settings)
    worker_dependencies.budgets = replace(worker_dependencies.budgets, web_tool_call_budget=1)
    worker_dependencies.tools.update(
        web_tools(
            _web(lambda _: _page(f"<p>{SOURCE}</p>")),
            worker_dependencies.repositories,
            worker_dependencies.blobs,
        )
    )
    with TestClient(
        create_app(settings, worker_dependencies=worker_dependencies),
        headers={"Authorization": "Bearer local-test-token"},
    ) as client:
        yield client


def _run(
    tmp_path: Path, name: str, *, repeats: int, max_usd: float, judged: list
) -> dict[str, Any]:
    with research_api(tmp_path, name) as client:
        return run_suite(
            [CASE],
            repeats=repeats,
            client=client,
            judge=judge_session(judge(*judged), MODELS, route=PRICED_JUDGE),
            out_dir=tmp_path / name,
            max_usd=max_usd,
            request_cap_usd=0.25,
            poll_seconds=0.01,
            timeout_seconds=10,
            emit=lambda line: None,
        )


def test_suite_run_scores_regrades_and_stops_at_its_spend_cap(tmp_path) -> None:
    card = _run(tmp_path, "full", repeats=2, max_usd=3.0, judged=_judged_runs(2))
    suite_dir = tmp_path / "full"
    case = card["cases"]["bench"]
    assert case["runs"] == 2 and case["statuses"] == {"passed": 2}
    assert case["meanScore"] == 92.5 and case["stdevScore"] == 0.0 and case["minScore"] == 92.5
    assert case["passAt1"] == 1.0 and case["passAllK"] is True
    assert card["overall"]["passHatK"] == 1.0 and card["stoppedEarly"] is None
    assert card["judgeRoute"] == ["deepseek.v3.2"]
    assert card["judgeCostUsd"] > 0 and case["research"]["meanCostUsd"] > 0
    assert case["research"]["meanLatencySeconds"] is not None
    assert "| bench | 2 | 92.5 |" in (suite_dir / "scorecard.md").read_text()
    runs = sorted(p.name for p in (suite_dir / "runs").glob("*.json"))
    assert runs == ["bench-r1.json", "bench-r2.json"]

    # Re-grading spends nothing on research: the saved bundles meet a stricter judge.
    regraded = regrade_suite(
        suite_dir, judge_session(judge(*_judged_runs(2, coverage="missing")), MODELS), 0.25
    )
    assert regraded["cases"]["bench"]["meanScore"] == 82.5
    assert regraded["overall"]["passAt1"] == 1.0
    assert json.loads((suite_dir / "scorecard.json").read_text())["regradedAt"]

    # The first run's spend reaches the cap: no second or third run is submitted.
    stopped = _run(tmp_path, "capped", repeats=3, max_usd=0.0001, judged=_judged_runs(1))
    assert stopped["cases"]["bench"]["runs"] == 1
    assert "2 of 3 runs were not submitted" in stopped["stoppedEarly"]
    assert stopped["cases"]["bench"]["passAllK"] is False and stopped["overall"]["passHatK"] == 0
    assert "2 of 3 runs were not submitted" in (tmp_path / "capped" / "scorecard.md").read_text()

    # Compare: the capped run lost pass^k, so the gate fails; the CLI exits 1.
    markdown, regressions = compare_scorecards(card, stopped, fail_on_drop=5)
    assert "| bench | 92.5 -> 92.5 (+0) | 1.0 -> 1.0 (+0) | yes -> no |" in markdown
    assert regressions == ["pass^k fell from 1.0 to 0.0"]
    assert compare_scorecards(card, stopped)[1] == []
    flagged = json.loads(json.dumps(card))
    flagged["cases"]["bench"]["flags"]["unsupported_material_claims"] = 1
    flagged["overall"]["meanScore"] = 80.0
    assert len(compare_scorecards(card, flagged, fail_on_drop=5)[1]) == 2
    args = ["suite", "compare", str(suite_dir / "scorecard.json")]
    assert main([*args, str(tmp_path / "capped" / "scorecard.json"), "--fail-on-drop", "5"]) == 1
    assert main([*args, str(suite_dir / "scorecard.json"), "--fail-on-drop", "5"]) == 0

    # Calibration: export the claims for labelling, then score two judge models against them.
    exported = export_calibration(suite_dir, n=20)
    with exported.open(newline="") as file:
        rows = list(csv.DictReader(file))
    assert len(rows) == 2 and rows[0]["human_label"] == "" and SOURCE in rows[0]["context"]
    rows[0]["human_label"], rows[1]["human_label"] = "supported", "Unsupported"
    with exported.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    agreeing = judge_session(judge(claims_answer("supported", "unsupported")), MODELS)
    lenient = judge_session(judge(claims_answer("supported", "supported")), MODELS)
    scored = score_calibration(exported, {"a": agreeing, "b": lenient})
    assert [(r["agreementPercent"], r["kappa"]) for r in scored] == [(100.0, 1.0), (50.0, 0.0)]
    assert cohen_kappa(["s", "s", "u", "u"], ["s", "u", "u", "u"]) == 0.5

    # LangSmith logging records the suite, and a failing client never fails it.
    records = [json.loads(p.read_text()) for p in sorted((suite_dir / "runs").glob("*.json"))]
    logged = _FakeLangSmith()
    assert log_suite(logged, card, records) is True
    assert logged.examples == ["bench"] and len(logged.runs) == 2
    assert ("score", 82.5) in logged.feedback and ("flags", 0) in logged.feedback
    assert log_suite(object(), card, records) is False


def test_suite_run_without_yes_prints_the_estimate_and_calls_nothing(
    tmp_path, monkeypatch, capsys
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("no live call may be made without --yes")

    monkeypatch.setattr(suite_cli, "judge_model_client", refuse)
    monkeypatch.setattr(httpx, "Client", refuse)
    monkeypatch.setenv("CONTEXTFORGE_REQUEST_MAX_USD", "0.25")
    suite = tmp_path / "suite.yaml"
    suite.write_text(
        "cases:\n"
        + "".join(
            f"  - {{id: c{n}, source: t, question: Q{n}, requiredParts: [a, b]}}\n" for n in (1, 2)
        )
    )
    code = main(
        ["suite", "run", "--suite", str(suite), "--repeats", "3", "--out-dir", str(tmp_path / "o")]
    )
    out = capsys.readouterr().out
    assert code == 2 and "Nothing was submitted" in out
    assert "2 cases x 3 repeats = 6 runs" in out and "$1.50 (6 x $0.25" in out
    assert not (tmp_path / "o").exists()
    with pytest.raises(ValueError, match="Unknown case ids: c9"):
        main(["suite", "run", "--suite", str(suite), "--cases", "c1,c9"])


class _FakeLangSmith:
    """Records what the suite logs, as a ``langsmith.Client`` would receive it."""

    def __init__(self) -> None:
        self.examples: list[str] = []
        self.runs: list[str] = []
        self.feedback: list[tuple[str, float]] = []

    def has_dataset(self, *, dataset_name: str) -> bool:
        return False

    def create_dataset(self, name: str, **_: object) -> Any:
        return type("Dataset", (), {"id": "d1"})()

    def list_examples(self, *, dataset_id: str) -> list[Any]:
        return [type("Example", (), {"id": n, "metadata": {"caseId": n}})() for n in self.examples]

    def create_example(self, *, metadata: dict[str, str], **_: object) -> None:
        self.examples.append(metadata["caseId"])

    def create_project(self, name: str, **_: object) -> None:
        pass

    def create_run(self, *, name: str, **_: object) -> None:
        self.runs.append(name)

    def create_feedback(self, run_id: object, key: str, *, score: float) -> None:
        self.feedback.append((key, score))

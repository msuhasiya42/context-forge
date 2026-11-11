"""The judge's model client: the worker's call path (routing to fallbacks, bounded retries, a
spend cap, a priced ModelCall per attempt and a redacted transcript), outside any request.

Every judge call of one session is recorded under the session id, so its spend and tokens are
the sum of its ModelCalls, as a request's are.
"""

import logging
import math
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from contextforge.core.entities.model_call import Stage
from contextforge.core.models.base import ModelClient, ModelRequest, ModelResponse
from contextforge.core.models.config import ModelRoute, ModelsConfig
from contextforge.core.models.recording import RecordingModelClient, cost_usd
from contextforge.core.models.transcript import TranscriptWriter
from contextforge.core.repositories.memory import InMemoryModelCalls
from contextforge.core.values import Usage
from contextforge.infra.gemini import is_gemini_model
from contextforge.worker.calls import BudgetedModel
from contextforge.worker.routing import ModelCooldown, RoutedModel

logger = logging.getLogger(__name__)

# Judge calls are offline and many; only the spend cap should stop them.
_CALL_BUDGET = 100_000
# The largest judge input expected (a long report, or 8 claims with page context), in tokens;
# each attempt reserves this much input at its model's price against the spend cap.
_INPUT_CAP_TOKENS = 32_000
# Assumed per judged run (about 24 claims in 3 batches plus one report call), for the
# estimate printed before any live call; the recorded ModelCalls give the real figure.
ESTIMATE_INPUT_TOKENS_PER_RUN = 30_000
ESTIMATE_OUTPUT_TOKENS_PER_RUN = 3_000


class DirectoryBlobs:
    """A BlobStore over a local folder, for judge transcripts beside a suite run."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def put(self, key: str, data: bytes) -> str:
        path = self._root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path)

    def get(self, ref: str) -> bytes:
        return Path(ref).read_bytes()


@dataclass
class JudgeSession:
    """A judge client and the ModelCalls it has recorded."""

    client: ModelClient
    route: ModelRoute
    calls: InMemoryModelCalls
    session_id: str

    def spent_usd(self) -> float:
        return sum(c.estimated_cost_usd or 0.0 for c in self.calls.list_by_request(self.session_id))

    def usage(self) -> Usage | None:
        return self.calls.sum_usage(self.session_id)


class _JudgeStage:
    """Sends each judge request to the judge route's model with the stage's token cap; the
    routed client below tries the fallbacks while that model is down."""

    def __init__(self, inner: ModelClient, primary: str, max_tokens: int) -> None:
        self._inner, self._primary, self._max_tokens = inner, primary, max_tokens

    def invoke(self, request: ModelRequest) -> ModelResponse:
        pinned = request.model_copy(update={"model": self._primary, "max_tokens": self._max_tokens})
        return self._inner.invoke(pinned)


# Inference-profile prefixes: ``global.<model id>`` is the same model as ``<model id>``.
_GEO_PREFIXES = frozenset({"global", "apac", "us", "eu", "au", "jp", "ca", "us-gov"})


def judge_route(models: ModelsConfig) -> ModelRoute:
    return models.stages[Stage.JUDGE.value].route


def usable_route(route: ModelRoute, *, gemini_key_set: bool) -> ModelRoute:
    """``route`` without its Gemini models when no Gemini key is set, so the judge grades with
    its Bedrock fallback. ``ValueError`` when no model is left."""
    if gemini_key_set:
        return route
    models = [model for model in (route.primary, *route.fallbacks) if not is_gemini_model(model)]
    if not models:
        raise ValueError("Every judge model is a Gemini model; set CONTEXTFORGE_GEMINI_API_KEY.")
    if len(models) < len(route.fallbacks) + 1:
        _warn_gemini_skipped()
    return ModelRoute(models[0], tuple(models[1:]))


@cache
def _warn_gemini_skipped() -> None:
    """Logged once per process, however many judge sessions are built."""
    logger.warning("CONTEXTFORGE_GEMINI_API_KEY is not set; the judge skips its Gemini models.")


def base_model(model_id: str) -> str:
    prefix, _, rest = model_id.partition(".")
    return rest if prefix in _GEO_PREFIXES and rest else model_id


def shared_with_research(judge: ModelRoute, routes: Mapping[Stage, ModelRoute]) -> set[str]:
    """The judge's models that also serve a research stage: a model must not grade its own
    pipeline, so the CLI refuses to judge with any of them."""
    research = {
        base_model(model)
        for stage, route in routes.items()
        if stage != Stage.JUDGE
        for model in (route.primary, *route.fallbacks)
    }
    return {model for model in (judge.primary, *judge.fallbacks) if base_model(model) in research}


def judge_session(
    provider_client: ModelClient,
    models: ModelsConfig,
    *,
    route: ModelRoute | None = None,
    max_usd: float = math.inf,
    retries: int = 1,
    reserve_usd: float = 0.10,
    transcripts: TranscriptWriter | None = None,
    provider: str | Callable[[str], str] = "bedrock",
) -> JudgeSession:
    """A session over ``provider_client`` (Bedrock, or a scripted model in tests) on the judge
    stage's route, or on ``route`` when one is given (calibration compares models)."""
    route = route or judge_route(models)
    calls = InMemoryModelCalls()
    session_id = f"judge-{uuid.uuid4()}"
    recorded = RecordingModelClient(
        provider_client,
        calls,
        request_message_id=session_id,
        memory_version="none",
        price_version=models.price_version,
        prices=models.prices,
        unknown_cost_usd=reserve_usd,
        provider=provider,
        transcripts=transcripts,
    )
    budgeted = BudgetedModel(
        recorded,
        calls,
        session_id,
        call_budget=_CALL_BUDGET,
        max_usd=max_usd,
        reserve_usd=reserve_usd,
        prices=models.prices,
        cap_input_tokens=_INPUT_CAP_TOKENS,
        retries=retries,
    )
    routed = RoutedModel(budgeted, {Stage.JUDGE: route}, ModelCooldown())
    max_tokens = models.stages[Stage.JUDGE.value].max_tokens
    client = _JudgeStage(routed, route.primary, max_tokens)
    return JudgeSession(client=client, route=route, calls=calls, session_id=session_id)


def estimate_judge_usd(
    models: ModelsConfig,
    runs: int,
    *,
    route: ModelRoute | None = None,
    input_tokens_per_run: int = ESTIMATE_INPUT_TOKENS_PER_RUN,
    output_tokens_per_run: int = ESTIMATE_OUTPUT_TOKENS_PER_RUN,
) -> float:
    """The assumed judge spend for ``runs`` judged runs at the prices of ``route``'s primary
    (default: the judge stage's)."""
    usage = Usage(
        input_tokens=input_tokens_per_run * runs,
        output_tokens=output_tokens_per_run * runs,
    )
    return cost_usd(models.prices, (route or judge_route(models)).primary, usage) or 0.0

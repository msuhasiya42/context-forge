"""Composition root for the real model, web tools and selectable application persistence."""

import logging
from functools import cache
from typing import Any

import boto3  # type: ignore[import-untyped]
import httpx
from botocore.config import Config  # type: ignore[import-untyped]
from pydantic import SecretStr

from contextforge.core.context.budgets import load_budgets
from contextforge.core.entities.model_call import Stage
from contextforge.core.models.base import ModelClient, ModelRequest, ModelResponse
from contextforge.core.models.config import ModelRoute, ModelsConfig, load_models_config
from contextforge.core.models.errors import ModelAccessDenied
from contextforge.core.models.recording import PriceTable
from contextforge.core.models.scripted import ScriptedModel
from contextforge.core.queue import InProcessQueue, Queue
from contextforge.core.repositories.memory import Repositories
from contextforge.core.settings import Settings
from contextforge.core.storage import BlobStore, MemoryBlobStore
from contextforge.infra.aws import S3Blobs, SqsQueue, dynamo_repositories
from contextforge.infra.bedrock import BedrockModel
from contextforge.infra.gemini import GeminiModel, RequestPacer, is_gemini_model
from contextforge.infra.tracing import RequestTracer, langsmith_client
from contextforge.infra.web import PublicWebClient, web_tools
from contextforge.worker.dependencies import WorkerDependencies
from contextforge.worker.tools.sql_query import SqlQueryTool

logger = logging.getLogger(__name__)


def aws_session(settings: Settings) -> Any:
    """The boto3 session for the configured profile (or the default chain) and region."""
    if settings.aws_profile:
        return boto3.Session(profile_name=settings.aws_profile, region_name=settings.aws_region)
    return boto3.Session(region_name=settings.aws_region)


def build_worker(settings: Settings) -> WorkerDependencies:
    session = aws_session(settings)
    repositories, blobs, queue = _persistence(session, settings)
    model: ModelClient = ScriptedModel({})
    if settings.model_backend == "bedrock":
        model = bedrock_model(session, settings)
    web = PublicWebClient(
        provider=settings.search_provider,
        api_key=_secret(settings.serper_api_key),
        tavily_api_key=_secret(settings.tavily_api_key),
        limit=settings.search_max_results,
        timeout=settings.web_timeout_seconds,
        max_bytes=settings.web_max_bytes,
        max_chars=settings.web_max_chars,
    )
    models = load_models_config()
    tracer = _tracer(settings, models.prices)
    return WorkerDependencies(
        repositories=repositories,
        blobs=blobs,
        queue=queue,
        model=tracer.model(model),
        budgets=load_budgets(),
        tools=tracer.tools(
            {
                "sql_query": SqlQueryTool(repositories.artifacts, blobs),
                **web_tools(web, repositories, blobs),
            }
        ),
        tracer=tracer,
        price_version=models.price_version,
        prices=models.prices,
        model_provider=settings.model_backend,
        routes=stage_routes(models, settings),
        settings=settings,
    )


def _secret(value: SecretStr | None) -> str | None:
    return value.get_secret_value() if value else None


def stage_routes(models: ModelsConfig, settings: Settings) -> dict[Stage, ModelRoute]:
    """config/models.yaml's model and fallbacks per stage, with the settings' overrides: the
    planner's apply to plan, lead and cite, the executor's to execute."""
    routes = {Stage(name): stage.route for name, stage in models.stages.items()}
    overrides = (
        (
            (Stage.PLAN, Stage.LEAD, Stage.CITE),
            settings.planner_model,
            settings.planner_fallbacks,
        ),
        ((Stage.EXECUTE,), settings.executor_model, settings.executor_fallbacks),
    )
    for stages, model, fallbacks in overrides:
        for stage in stages:
            route = routes[stage]
            routes[stage] = ModelRoute(
                model or route.primary, _model_ids(fallbacks) or route.fallbacks
            )
    return routes


def _model_ids(text: str | None) -> tuple[str, ...]:
    """Comma-separated model ids, blanks dropped."""
    return tuple(part.strip() for part in (text or "").split(",") if part.strip())


def _persistence(session: Any, settings: Settings) -> tuple[Repositories, BlobStore, Queue]:
    """In-memory records, files and queue, or DynamoDB, S3 and SQS in AWS mode. The AWS clients
    keep the SDK's default retries: one throttle or reset must not fail a request."""
    if settings.backend != "aws":
        return Repositories.in_memory(), MemoryBlobStore(), InProcessQueue()
    if not settings.sqs_queue_url:
        raise ValueError("AWS mode requires CONTEXTFORGE_SQS_QUEUE_URL.")
    config = Config(connect_timeout=5)
    return (
        dynamo_repositories(session.client("dynamodb", config=config), settings.dynamodb_table),
        S3Blobs(session.client("s3", config=config), settings.s3_bucket),
        SqsQueue(
            session.client("sqs", config=config),
            settings.sqs_queue_url,
            visibility_timeout_seconds=settings.request_deadline_seconds + 60,
        ),
    )


def bedrock_model(session: Any, settings: Settings) -> BedrockModel:
    """Bedrock with one client per read timeout. botocore's retries are off: BudgetedModel's
    bounded retry is the only one, so each attempt is admitted and recorded once."""

    def client(read_timeout: int) -> Any:
        config = Config(
            connect_timeout=5, read_timeout=read_timeout, retries={"total_max_attempts": 1}
        )
        return session.client("bedrock-runtime", config=config)

    planner = client(settings.planner_timeout_seconds)
    return BedrockModel(
        client(settings.executor_timeout_seconds),
        # The offline judge reads long reports and page context, so it waits like the planner.
        by_stage={
            Stage.PLAN: planner,
            Stage.LEAD: planner,
            Stage.CITE: planner,
            Stage.JUDGE: planner,
        },
    )


class ByProvider:
    """Sends a call for a Gemini model id to Gemini and every other call to Bedrock."""

    def __init__(self, bedrock: ModelClient, gemini: ModelClient | None) -> None:
        self._bedrock, self._gemini = bedrock, gemini

    def invoke(self, request: ModelRequest) -> ModelResponse:
        if not is_gemini_model(request.model):
            return self._bedrock.invoke(request)
        if self._gemini is None:
            # Access denied routes to the next model, as Gemini refusing the key would.
            raise ModelAccessDenied(f"{request.model} needs CONTEXTFORGE_GEMINI_API_KEY.")
        return self._gemini.invoke(request)


def model_provider(model_id: str) -> str:
    """The provider recorded on a ModelCall (``<provider>/<model id>``)."""
    return "gemini" if is_gemini_model(model_id) else "bedrock"


def judge_model_client(settings: Settings, reasoning_effort: str | None = None) -> ByProvider:
    """The offline judge's provider client: Gemini when a key is set, Bedrock for the rest.
    Gemini waits like the planner, as the judge reads long reports and page context."""
    gemini = None
    if settings.gemini_api_key is not None:
        http = httpx.Client(timeout=httpx.Timeout(settings.planner_timeout_seconds, connect=5))
        gemini = GeminiModel(
            http,
            settings.gemini_api_key,
            base_url=settings.gemini_base_url,
            pacer=gemini_pacer(settings.gemini_min_interval_seconds),
            reasoning_effort=reasoning_effort,
        )
    return ByProvider(bedrock_model(aws_session(settings), settings), gemini)


@cache
def gemini_pacer(min_interval: float) -> RequestPacer:
    """One pacer per process, so every judge client built (calibration builds one per model)
    shares the free tier's request rate."""
    return RequestPacer(min_interval)


def _tracer(settings: Settings, prices: PriceTable) -> RequestTracer:
    """The LangSmith tracer when CONTEXTFORGE_MODEL_TRACING is on and a key is set; otherwise
    one that traces nothing."""
    if not settings.model_tracing:
        return RequestTracer()
    if settings.langsmith_api_key is None:
        logger.warning("CONTEXTFORGE_MODEL_TRACING is on but LANGSMITH_API_KEY is not set.")
        return RequestTracer()
    content = settings.trace_content
    client = langsmith_client(
        settings.langsmith_api_key.get_secret_value(), include_content=content
    )
    return RequestTracer(client, settings.langsmith_project, include_content=content, prices=prices)

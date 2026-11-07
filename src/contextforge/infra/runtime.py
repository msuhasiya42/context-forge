"""Composition root for the real model, web tools and selectable application persistence."""

import logging
from typing import Any

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]

from contextforge.core.context.budgets import load_budgets
from contextforge.core.entities.model_call import Stage
from contextforge.core.models.base import ModelClient
from contextforge.core.models.config import load_models_config
from contextforge.core.models.recording import PriceTable
from contextforge.core.models.scripted import ScriptedModel
from contextforge.core.queue import InProcessQueue, Queue
from contextforge.core.repositories.memory import Repositories
from contextforge.core.settings import Settings
from contextforge.core.storage import BlobStore, MemoryBlobStore
from contextforge.infra.aws import S3Blobs, SqsQueue, dynamo_repositories
from contextforge.infra.bedrock import BedrockModel
from contextforge.infra.tracing import RequestTracer, langsmith_client
from contextforge.infra.web import FetchURLTool, PublicWebClient, WebSearchTool
from contextforge.worker.deps import WorkerDeps
from contextforge.worker.tools.sql_query import SqlQueryTool

logger = logging.getLogger(__name__)


def build_worker(settings: Settings) -> WorkerDeps:
    session = (
        boto3.Session(profile_name=settings.aws_profile, region_name=settings.aws_region)
        if settings.aws_profile
        else boto3.Session(region_name=settings.aws_region)
    )
    # Persistence keeps the SDK's default retries: one throttle or reset must not fail a request.
    config = Config(connect_timeout=5)
    repos = Repositories.in_memory()
    blobs: BlobStore = MemoryBlobStore()
    queue: Queue = InProcessQueue()
    if settings.backend == "aws":
        repos = dynamo_repositories(
            session.client("dynamodb", config=config), settings.dynamodb_table
        )
        blobs = S3Blobs(session.client("s3", config=config), settings.s3_bucket)
        if not settings.sqs_queue_url:
            raise ValueError("AWS mode requires CONTEXTFORGE_SQS_QUEUE_URL.")
        queue = SqsQueue(session.client("sqs", config=config), settings.sqs_queue_url)
    model: ModelClient = ScriptedModel({})
    if settings.model_backend == "bedrock":
        model = _bedrock_model(session, settings)
    web = PublicWebClient(
        provider=settings.search_provider,
        api_key=settings.serper_api_key.get_secret_value() if settings.serper_api_key else None,
        limit=settings.search_max_results,
        timeout=settings.web_timeout_seconds,
        max_bytes=settings.web_max_bytes,
        max_chars=settings.web_max_chars,
    )
    models = load_models_config()
    tracer = _tracer(settings, models.prices)
    return WorkerDeps(
        repos=repos,
        blobs=blobs,
        queue=queue,
        model=tracer.model(model),
        budgets=load_budgets(),
        tools=tracer.tools(
            {
                "sql_query": SqlQueryTool(repos.artifacts, blobs),
                "web_search": WebSearchTool(web, repos.artifacts, blobs),
                "fetch_url": FetchURLTool(web, repos.artifacts, blobs),
            }
        ),
        tracer=tracer,
        price_version=models.price_version,
        prices=models.prices,
        model_provider=settings.model_backend,
        # config/models.yaml names each stage's model; a setting may override it.
        planner_model=settings.planner_model or models.stages["plan"].model,
        executor_model=settings.executor_model or models.stages["execute"].model,
        settings=settings,
    )


def _bedrock_model(session: Any, settings: Settings) -> BedrockModel:
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
        by_stage={Stage.PLAN: planner, Stage.CITE: planner},
    )


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

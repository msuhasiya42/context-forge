"""Composition root for the real model, web tools and selectable application persistence."""

from typing import Any

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]

from contextforge.core.context.budgets import load_budgets
from contextforge.core.entities.model_call import Stage
from contextforge.core.models.base import ModelClient
from contextforge.core.models.config import load_models_config
from contextforge.core.models.scripted import ScriptedModel
from contextforge.core.queue import InProcessQueue, Queue
from contextforge.core.repositories.memory import Repositories
from contextforge.core.settings import Settings
from contextforge.core.storage import BlobStore, MemoryBlobStore
from contextforge.infra.aws import S3Blobs, SqsQueue, dynamo_repositories
from contextforge.infra.bedrock import BedrockModel
from contextforge.infra.web import FetchURLTool, PublicWebClient, WebSearchTool
from contextforge.worker.deps import WorkerDeps
from contextforge.worker.tools.sql_query import SqlQueryTool


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
    if settings.langsmith_tracing and settings.langsmith_api_key:
        from contextforge.infra.tracing import TracedModel

        model = TracedModel(
            model, settings.langsmith_api_key.get_secret_value(), settings.langsmith_project
        )
    web = PublicWebClient(
        provider=settings.search_provider,
        api_key=settings.serper_api_key.get_secret_value() if settings.serper_api_key else None,
        limit=settings.search_max_results,
        timeout=settings.web_timeout_seconds,
        max_bytes=settings.web_max_bytes,
        max_chars=settings.web_max_chars,
    )
    models = load_models_config()
    return WorkerDeps(
        repos=repos,
        blobs=blobs,
        queue=queue,
        model=model,
        budgets=load_budgets(),
        tools={
            "sql_query": SqlQueryTool(repos.artifacts, blobs),
            "web_search": WebSearchTool(web, repos.artifacts, blobs),
            "fetch_url": FetchURLTool(web, repos.artifacts, blobs),
        },
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

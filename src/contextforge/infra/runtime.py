"""Composition root for the real model, web tools and selectable application persistence."""

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]

from contextforge.core.context.budgets import load_budgets
from contextforge.core.models.base import ModelClient
from contextforge.core.models.config import load_models_config
from contextforge.core.models.scripted import ScriptedModel
from contextforge.core.queue import InProcessQueue, Queue
from contextforge.core.repositories.memory import Repositories
from contextforge.core.settings import Settings
from contextforge.core.storage import BlobStore, MemoryBlobStore
from contextforge.infra.aws import S3Blobs, SqsQueue, dynamo_repositories
from contextforge.infra.bedrock import BedrockModel
from contextforge.infra.web import PublicWebClient, web_tools
from contextforge.worker.deps import WorkerDeps
from contextforge.worker.tools.sql_query import SqlQueryTool


def build_worker(settings: Settings) -> WorkerDeps:
    session = (
        boto3.Session(profile_name=settings.aws_profile, region_name=settings.aws_region)
        if settings.aws_profile
        else boto3.Session(region_name=settings.aws_region)
    )
    config = Config(
        connect_timeout=5,
        read_timeout=settings.model_timeout_seconds,
        retries={"total_max_attempts": 1},
    )
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
        model = BedrockModel(session.client("bedrock-runtime", config=config))
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
            **web_tools(web, repos, blobs),
        },
        price_version=models.price_version + ":reserve-v1",
        planner_model=settings.planner_model,
        executor_model=settings.executor_model,
        settings=settings,
    )

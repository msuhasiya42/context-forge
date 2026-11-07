"""Validated local settings. Credentials use provider environment/default chains."""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CONTEXTFORGE_", env_file=".env", extra="ignore", protected_namespaces=()
    )
    backend: Literal["memory", "aws"] = "memory"
    model_backend: Literal["bedrock", "scripted"] = "bedrock"
    aws_region: str = "ap-southeast-2"
    aws_profile: str | None = None
    dynamodb_table: str = "contextforge"
    s3_bucket: str = "contextforge-441186132531-apse2"
    sqs_queue_url: str | None = None
    checkpoint_path: Path = Path(".data/checkpoints.sqlite")
    backend_log_path: Path = Path("backend.log")
    dev_user_id: str = "local-user"
    dev_token: SecretStr | None = None
    cors_origins: list[str] = ["http://localhost:5173"]
    search_provider: Literal["duckduckgo", "serper"] = "duckduckgo"
    serper_api_key: SecretStr | None = Field(default=None, validation_alias="SERPER_API_KEY")
    # Overrides of the config/models.yaml stage models: the planner's for plan and cite, the
    # executor's for execute (compress keeps its own). Fallbacks are comma-separated model ids
    # that replace the stage's fallbacks; empty or unset keeps config/models.yaml's.
    planner_model: str | None = None
    executor_model: str | None = None
    planner_fallbacks: str | None = None
    executor_fallbacks: str | None = None
    # Read timeouts per call: the planner model (plan, cite) may think longer than the executor
    # model (execute, compress); each gets its own bedrock-runtime client.
    planner_timeout_seconds: int = Field(default=120, ge=1, le=300)
    executor_timeout_seconds: int = Field(default=60, ge=1, le=300)
    model_retries: int = Field(default=1, ge=0, le=2)
    request_max_usd: float = Field(default=1.0, gt=0)
    call_reserve_usd: float = Field(default=0.10, gt=0)
    request_deadline_seconds: int = Field(default=900, ge=1, le=1800)
    search_max_results: int = Field(default=5, ge=1, le=10)
    web_timeout_seconds: int = Field(default=15, ge=1, le=60)
    web_max_bytes: int = Field(default=1_000_000, ge=1024)
    web_max_chars: int = Field(default=12_000, ge=100)
    # Our own switch, not LANGSMITH_TRACING: exporting that turns on the LangSmith SDK's global
    # auto-tracing, which would send LangGraph's full state (requests, pages) to LangSmith.
    model_tracing: bool = False
    # Developer machines only: put prompts and model and tool outputs on trace runs.
    trace_content: bool = False
    langsmith_api_key: SecretStr | None = Field(default=None, validation_alias="LANGSMITH_API_KEY")
    langsmith_project: str = Field(default="contextforge", validation_alias="LANGSMITH_PROJECT")
    # Store each ModelCall's exact prompt and answer as JSON in the blob store, for the Deep Dive
    # (GET .../model-calls/{id}/transcript). They hold user data and fetched page text: turn this
    # off for a shared deployment. Known secret values are redacted from them either way.
    record_transcripts: bool = True
    verify_web_claims: bool = True
    research_reports: bool = True
    web_tool_calls_per_task: int = Field(default=12, ge=1, le=50)
    run_worker: bool = True
    sse_poll_seconds: float = Field(default=1, gt=0)

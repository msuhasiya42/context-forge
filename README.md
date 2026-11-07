# ContextForge

AI Research Agent with Context Engineering and Persistent Memory. A private assistant for spreadsheet analysis, PDF questions, and later Gmail/Drive research, built to learn context engineering, LangGraph, AWS, NoSQL modeling, MCP, observability and evaluations at very low cost.

Three documents hold everything:

1. [System design](docs/system-design.md): what the system is, fixed choices, vocabulary, architecture, memory and context-engineering design, decisions log, phases.
2. [Implementation plan](docs/implementation-plan.md): entities and fields, Message shapes, working brief, DynamoDB physical design, context flow and compaction, worked prompt examples, stage prompts, repository layout, phase 1 tasks.
3. [Progress](docs/progress.md): status, open decisions, phase checklist, session log. Read this first when resuming.

React, FastAPI/Python, LangGraph and LangSmith are fixed. DynamoDB and S3 hold application data; Bedrock is the first model provider behind an adapter. The Python scripts at the repository root are an earlier event-delivery prototype, not the application.

## What exists

The local engine now also has a web-first API slice: Bedrock Converse, DuckDuckGo search, bounded page fetching, URL evidence, clarification/resume, SSE, standing instructions and optional AWS application adapters. See the [latest handoff](docs/handoff-web-first-2026-11-07.md) for verified scope and remaining live checks.
The chat UI lives in the sibling repository `context-forge-frontend`.

## Run the tests

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/). Run from the repository root (config and prompt paths are relative to it).

```bash
uv sync
uv run pytest -v
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

Copy `.env.example` to `.env` for local settings; its comments explain each one. The tests need no credentials: they use a scripted model, no network and no AWS. Tracing is off unless `CONTEXTFORGE_MODEL_TRACING=true` and `LANGSMITH_API_KEY` are set, and sends metadata only unless `CONTEXTFORGE_TRACE_CONTENT=true`. Never set `LANGSMITH_TRACING`: it is the LangSmith SDK's global switch and would send LangGraph's full state, page text included.

## Web-first development

No upload or vector database is required for the first research flow.

```bash
uv sync --frozen
cp .env.example .env
uv run contextforge-api
```

The API binds to `http://127.0.0.1:8000`; interactive API docs are at `/docs`.
Configure the existing AWS CLI profile in `.env`. Models run through Bedrock: each stage has a model and ordered fallbacks, used while a model is down (defaults gpt-oss-120b to plan, Qwen3-235B to execute, Nova 2 Lite to compress; stage models, fallbacks and prices in `config/models.yaml`, overrides in `.env.example`); each request is capped at `CONTEXTFORGE_REQUEST_MAX_USD`, with every call priced from its token usage. Web search defaults to DuckDuckGo. LangSmith tracing is optional. Application records default to memory and graph checkpoints to local SQLite. `CONTEXTFORGE_BACKEND=aws` selects the implemented DynamoDB/S3/SQS adapters; the cloud graph checkpointer is still pending.

Connect the sibling frontend with `VITE_API_BASE_URL=http://localhost:8000 npm run dev`. The tested SSE/reply/memory UI changes are delivered as [a patch](patches/frontend-web-first.patch), because that repository was outside this session's writable workspace. Apply instructions and remaining work are in the latest handoff.

For a real model/search acceptance run without a server socket: `uv run python scripts/smoke_web.py --live`. This is opt-in and may incur model charges. This session verified the flow offline; external networking and local server binding were restricted.

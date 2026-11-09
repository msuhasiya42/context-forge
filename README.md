# ContextForge

A public-web research agent that plans investigations, searches and reads sources, checks whether evidence supports its claims, and produces cited reports. The immediate goal is to measure and improve research quality against saved benchmark reports. Context engineering, LangGraph, AWS, observability and evaluations support that goal; uploads, PDF, private connectors and learned memory follow it.

Start with [Learning the code](docs/learning-the-code.md): a folder map, one request traced
through actual functions, and four short reading sessions with offline exercises.

The design and delivery documents are:

1. [System design](docs/system-design.md): what the system is, fixed choices, vocabulary, architecture, memory and context-engineering design, decisions log, phases.
2. [Implementation plan](docs/implementation-plan.md): entities and fields, Message shapes, request state, DynamoDB physical design, context flow and compaction, worked prompt examples, stage prompts, repository layout, phase 1 tasks.
3. [Progress](docs/progress.md): status, open decisions, phase checklist, session log. Read this first when resuming.

React, FastAPI/Python, LangGraph and LangSmith are fixed. DynamoDB and S3 hold application data; Bedrock is the first model provider behind an adapter. The Python scripts at the repository root are an earlier event-delivery prototype, not the application.

## What exists

The engine has Bedrock stage routing, Serper/DuckDuckGo search, page fetching, saved URL evidence, clarification/resume, SSE, standing instructions, AWS application persistence, tracing and Deep Dive. The research continuation adds a shared request-wide 50-search/fetch cap, source metadata, conservative claim and summary review, and an evidence-grounded report writer. These changes pass offline checks; live acceptance of this build remains pending. See the [current handoff](docs/handoff-research-v2-2026-11-07.md), [research-first plan](docs/research-first-plan.md) and [benchmark instructions](docs/evals/web-research-running.md). The public [FRAMES benchmark slice](docs/evals/frames.md) (Krishna et al. 2024, "Fact, Fetch, and Reason: A Unified Evaluation of Retrieval-Augmented Generation", `google/frames-benchmark`, Apache-2.0) measures answer accuracy and evidence-supported accuracy on the open web.
Layout: `backend/` (Python engine and API: `src/`, `tests/`, `config/`, `prompts/`, `evals/`, `scripts/`) and `frontend/` (React chat UI). Project-wide docs stay in `docs/`.

Research reference: [AI cost-engineering report](docs/research/ai-cost-engineering-report-2026-11-07.md), [comparison with the Claude report in Box](docs/evals/ai-cost-box-comparison-2026-11-07.md), and [ten complex web research cases](backend/evals/cases/web-research-suite.md).

[API testing and live logs](docs/api-testing.md): submit research from the command line, watch
progress, save evidence/reports and inspect the same chat in the UI. The API CLI now writes
rotating JSON logs to `backend/backend.log`.

## Run the tests

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/). Run from `backend/` (config, prompt and `.env` paths are relative to it).

```bash
cd backend
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
cd backend
uv sync --frozen
cp .env.example .env
uv run contextforge-api
```

The API binds to `http://127.0.0.1:8000`; interactive API docs are at `/docs`.
Configure the existing AWS CLI profile in `.env`. Models run through Bedrock: each stage has a model and ordered fallbacks, used while a model is down (defaults gpt-oss-120b to plan, Qwen3-235B to execute, Nova 2 Lite to compress; stage models, fallbacks and prices in `config/models.yaml`, overrides in `.env.example`); each request is capped at `CONTEXTFORGE_REQUEST_MAX_USD`, with every call priced from its token usage. Web search defaults to DuckDuckGo. LangSmith tracing is optional. Application records default to memory and graph checkpoints to local SQLite. `CONTEXTFORGE_BACKEND=aws` selects the implemented DynamoDB/S3/SQS adapters; the cloud graph checkpointer is still pending.

Run the UI from `frontend/` with `npm install` once, then `VITE_API_BASE_URL=http://localhost:8000 npm run dev` (or put `VITE_API_BASE_URL` in `frontend/.env.local`). The earlier [patch](docs/archive/patches/frontend-web-first.patch) is historical.

For a real model/search acceptance run without a server socket: `uv run python scripts/smoke_web.py --live` (from `backend/`). This is opt-in and may incur model charges. The evening handoff records earlier live acceptance; the newest model routes and planner repair still need a successful live check. With the already populated local environment, `uv run --no-sync contextforge-api` avoids syncing dependencies before starting the API.

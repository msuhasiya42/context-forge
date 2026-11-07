# ContextForge

AI Research Agent with Context Engineering and Persistent Memory. A private assistant for spreadsheet analysis, PDF questions, and later Gmail/Drive research, built to learn context engineering, LangGraph, AWS, NoSQL modeling, MCP, observability and evaluations at very low cost.

Three documents hold everything:

1. [System design](docs/system-design.md): what the system is, fixed choices, vocabulary, architecture, memory and context-engineering design, decisions log, phases.
2. [Implementation plan](docs/implementation-plan.md): entities and fields, Message shapes, working brief, DynamoDB physical design, context flow and compaction, worked prompt examples, stage prompts, repository layout, phase 1 tasks.
3. [Progress](docs/progress.md): status, open decisions, phase checklist, session log. Read this first when resuming.

React, FastAPI/Python, LangGraph and LangSmith are fixed. DynamoDB and S3 hold application data; Bedrock is the first model provider behind an adapter. The Python scripts at the repository root are an earlier event-delivery prototype, not the application.

## What exists

Phase 1, the local engine (planner, task loop, context builder, compaction, checkpoints with resume and restart), runs on a scripted fake model; see [progress](docs/progress.md).
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

Copy `.env.example` to `.env` for local settings. Phase 1 needs no credentials: the tests use the fake model and no AWS; `LANGSMITH_API_KEY` is only for tracing from phase 2.
